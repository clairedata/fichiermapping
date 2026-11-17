"""src/mapper.py.

Gestion de la mémoire de mapping persistante (config/mapping_memory.json),
du dictionnaire bilingue de synonymes standard (config/standard_aliases.json),
de la normalisation linguistique (accents, séparateurs) et du fuzzy matching.

Conforme à la Clean Architecture : aucune interaction CLI bloquante (input/print)
n'est réalisée dans ce module. Toute résolution interactive passe par un callback injecté.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
import re
from typing import Any, Callable, Optional
import unicodedata
import polars as pl

from src.models import IGNORE_TOKEN, MappingAction

# Chemin par défaut vers le dictionnaire de synonymes standard (FR / EN)
DEFAULT_ALIASES_PATH = Path(__file__).resolve().parent.parent / "config" / "standard_aliases.json"

# Type du callback de résolution: (col_name, 3_samples, target_columns) -> chosen_target or IGNORE_TOKEN
ResolverCallback = Callable[[str, list[str], list[str]], str]


class UnresolvedColumnError(Exception):
    """Exception levée lorsqu'une colonne inconnue ne peut pas être résolue sans intervention."""

    def __init__(self, column_name: str, samples: list[str]):
        super().__init__(
            f"La colonne '{column_name}' est inconnue et aucune règle n'est mémorisée. "
            f"Exemples de valeurs : {samples}"
        )
        self.column_name = column_name
        self.samples = samples


def normalize_column_name(text: str) -> str:
    """Normalise un nom de colonne pour une comparaison insensible à la langue et au style :

    - Décomposition Unicode pour supprimer les accents (ex: 'Prénom' -> 'prenom', 'Tél' -> 'tel')
    - Passage en minuscules
    - Remplacement des séparateurs (espaces, tirets, points, slashes) par des underscores
    - Suppression des underscores consécutifs et aux extrémités
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    ascii_text = decomposed.encode("ASCII", "ignore").decode("utf-8").lower().strip()
    cleaned = re.sub(r"[^a-z0-9]+", "_", ascii_text)
    return cleaned.strip("_")


def compact_column_name(text: str) -> str:
    """Retourne la version compacte sans underscore ni séparateur (ex: 'code_postal' -> 'codepostal')."""
    return normalize_column_name(text).replace("_", "")


def load_standard_aliases(aliases_path: Optional[Path | str] = None) -> dict[str, list[str]]:
    """Charge le dictionnaire bilingue (FR / EN) de synonymes et alias courants.

    Associe chaque nom de colonne cible à sa liste de variantes courantes en français et anglais.
    Retourne un dictionnaire vide si le fichier est introuvable.
    """
    path = Path(aliases_path) if aliases_path else DEFAULT_ALIASES_PATH
    if not path.exists():
        return {}

    try:
        with open(path, mode="r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {}
            data = json.loads(content)
            if isinstance(data, dict):
                return {
                    str(k): [str(item) for item in v] if isinstance(v, list) else []
                    for k, v in data.items()
                }
            return {}
    except (json.JSONDecodeError, OSError):
        return {}


def load_mapping_memory(memory_path: Path | str) -> dict[str, str]:
    """Charge le dictionnaire de mémoire de mapping depuis le fichier JSON.

    Retourne un dictionnaire vide si le fichier n'existe pas ou est vide.
    """
    path = Path(memory_path)
    if not path.exists():
        return {}

    try:
        with open(path, mode="r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {}
            data = json.loads(content)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
            return {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_mapping_memory(memory_path: Path | str, memory: dict[str, str]) -> None:
    """Sauvegarde le dictionnaire de mémoire de mapping au format JSON lisible."""
    path = Path(memory_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, mode="w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2, ensure_ascii=False)


def extract_sample_values(
    df: pl.DataFrame,
    column_name: str,
    limit: int = 3,
) -> list[str]:
    """Extrait les premières valeurs non-nulles et non-vides d'une colonne donnée."""
    if column_name not in df.columns:
        return []

    series = df[column_name]
    non_empty = (
        series.drop_nulls()
        .filter(series.drop_nulls().str.strip_chars() != "")
        .head(limit)
    )

    return [str(val) for val in non_empty.to_list()]


def resolve_column_from_memory(
    source_col: str,
    memory: dict[str, str],
) -> Optional[str]:
    """Tente de résoudre une colonne source depuis la mémoire persistante.

    Supporte la correspondance exacte puis insensible à la casse.
    """
    if source_col in memory:
        return memory[source_col]

    normalized_source = source_col.strip().lower()
    for mem_key, mem_target in memory.items():
        if mem_key.strip().lower() == normalized_source:
            return mem_target

    return None


def _resolve_target_aliases(
    target_col: str,
    aliases: Optional[dict[str, list[str]]],
) -> list[str]:
    """Résout la liste des synonymes applicables à une colonne cible donnée.

    Supporte aussi bien les colonnes canoniques ('telephone', 'code_postal', 'nom')
    que les colonnes personnalisées (ex: 'TEL', 'CP', 'VILLE', 'RS', 'NOM_DIRIGEANT'
    issues d'un fichier Askia .qex) par recherche bidirectionnelle du groupe d'équivalence.
    """
    if not aliases or not target_col:
        return []

    if target_col in aliases:
        return aliases[target_col]

    tgt_lower = target_col.strip().lower()
    tgt_norm = normalize_column_name(target_col)
    tgt_compact = compact_column_name(target_col)

    # 1. Correspondance directe sur une clé canonique (insensible à la casse / normalisation)
    for key, vals in aliases.items():
        if key.lower() == tgt_lower or normalize_column_name(key) == tgt_norm:
            return vals

    # 2. Recherche bidirectionnelle : la colonne cible appartient-elle à un groupe d'alias ?
    for key, vals in aliases.items():
        for alias in vals:
            a_lower = alias.strip().lower()
            a_norm = normalize_column_name(alias)
            a_compact = compact_column_name(alias)
            if tgt_lower == a_lower or (tgt_norm and tgt_norm == a_norm) or (tgt_compact and tgt_compact == a_compact):
                return [key, *vals]

    return []


def score_column_match(
    source_col: str,
    target_col: str,
    memory: Optional[dict[str, str]] = None,
    aliases: Optional[dict[str, list[str]]] = None,
    fuzzy_threshold: float = 0.82,
) -> tuple[float, str]:
    """Calcule le score de correspondance (0.0 à 1.0) et le type d'association entre deux colonnes.

    Hiérarchie des correspondances :
    1. Correspondance exacte insensible à la casse (score: 1.0, type: 'exact')
    2. Correspondance normalisée sans accents/séparateurs (score: 0.98, type: 'normalized')
    3. Mémoire utilisateur persistante (score: 0.95, type: 'memory')
    4. Synonymes et alias bilingues FR / EN (score: 0.92 ou 0.89 pour génériques, type: 'alias')
    5. Approximative / Fuzzy Matching (score: 0.82 - 0.88, type: 'fuzzy')

    Retourne:
        tuple[float, str]: (score, match_type)
    """
    if not source_col or not target_col:
        return 0.0, "unmapped"

    src_lower = source_col.strip().lower()
    tgt_lower = target_col.strip().lower()

    # 1. Correspondance exacte insensible à la casse
    if src_lower == tgt_lower:
        return 1.0, "exact"

    src_norm = normalize_column_name(source_col)
    tgt_norm = normalize_column_name(target_col)
    src_compact = compact_column_name(source_col)
    tgt_compact = compact_column_name(target_col)

    # 2. Correspondance normalisée (sans accents, séparateurs unifiés ou forme compacte)
    if src_norm == tgt_norm or (src_compact and src_compact == tgt_compact):
        return 0.98, "normalized"

    # 3. Correspondance par mémoire utilisateur persistante
    if memory:
        mem_target = memory.get(source_col) or memory.get(src_lower)
        if mem_target and mem_target.strip().lower() == tgt_lower:
            return 0.95, "memory"
        for mem_k, mem_v in memory.items():
            if normalize_column_name(mem_k) == src_norm and mem_v.strip().lower() == tgt_lower:
                return 0.95, "memory"

    # 4. Correspondance par dictionnaire d'alias bilingue (FR / EN), y compris bidirectionnel (ex: TEL <-> phone)
    target_aliases = _resolve_target_aliases(target_col, aliases)
    if target_aliases:
        for alias in target_aliases:
            a_lower = alias.strip().lower()
            a_norm = normalize_column_name(alias)
            a_compact = compact_column_name(alias)

            if src_lower == a_lower or (src_norm and src_norm == a_norm) or (src_compact and src_compact == a_compact):
                # Termes plus génériques ('name', 'id', 'te') avec priorité légèrement moindre
                if src_norm in {"name", "id", "te", "statut", "status"}:
                    return 0.89, "alias"
                return 0.92, "alias"

    # 5. Correspondance approximative (Fuzzy Matching)
    # Appliquée uniquement sur les chaînes de longueur suffisante (>= 4) pour éviter les faux positifs
    if len(src_norm) >= 4 and len(tgt_norm) >= 4:
        best_ratio = difflib.SequenceMatcher(None, src_norm, tgt_norm).ratio()

        if target_aliases:
            for alias in target_aliases:
                a_norm = normalize_column_name(alias)
                if len(a_norm) >= 4:
                    r = difflib.SequenceMatcher(None, src_norm, a_norm).ratio()
                    if r > best_ratio:
                        best_ratio = r

        if best_ratio >= fuzzy_threshold:
            confidence = round(0.70 + (best_ratio * 0.18), 3)
            return confidence, "fuzzy"

    return 0.0, "unmapped"


def find_best_target_match(
    source_col: str,
    target_columns: list[str],
    memory: Optional[dict[str, str]] = None,
    aliases: Optional[dict[str, list[str]]] = None,
    fuzzy_threshold: float = 0.82,
) -> tuple[Optional[str], float, str]:
    """Identifie la meilleure colonne cible pour une colonne source donnée.

    Retourne:
        tuple[Optional[str], float, str]: (cible_retenue, score, type_association)
    """
    best_target: Optional[str] = None
    best_score: float = 0.0
    best_type: str = "unmapped"

    for target in target_columns:
        score, match_type = score_column_match(
            source_col=source_col,
            target_col=target,
            memory=memory,
            aliases=aliases,
            fuzzy_threshold=fuzzy_threshold,
        )
        if score > best_score:
            best_score = score
            best_target = target
            best_type = match_type

    if best_score > 0.0:
        return best_target, best_score, best_type
    return None, 0.0, "unmapped"


def match_targets_to_incoming(
    target_columns: list[str],
    incoming_columns: list[str],
    memory: Optional[dict[str, str]] = None,
    aliases: Optional[dict[str, list[str]]] = None,
    fuzzy_threshold: float = 0.82,
) -> dict[str, dict[str, Any]]:
    """Résout de manière optimale et non ambiguë les correspondances entre la structure cible

    et les colonnes du nouveau fichier (approche gloutonne par score décroissant).

    Garantit qu'une même colonne source ne soit pas attribuée arbitrairement à deux cibles,
    et maximise la pertinence globale (Exact > Normalisé > Mémoire > Synonyme FR/EN > Approximatif).

    Retourne:
        dict[str, dict[str, Any]]:
            {
                target_col: {
                    "source": incoming_col or None,
                    "score": float,
                    "type": "exact" | "normalized" | "memory" | "alias" | "fuzzy" | "unmapped",
                    "found": bool
                }
            }
    """
    if aliases is None:
        aliases = load_standard_aliases()

    candidates: list[tuple[float, str, str, str]] = []
    for tgt in target_columns:
        for src in incoming_columns:
            score, mtype = score_column_match(
                source_col=src,
                target_col=tgt,
                memory=memory,
                aliases=aliases,
                fuzzy_threshold=fuzzy_threshold,
            )
            if score > 0.0:
                candidates.append((score, tgt, src, mtype))

    # Tri par score décroissant pour favoriser les correspondances les plus fiables
    candidates.sort(key=lambda x: x[0], reverse=True)

    assigned_targets: set[str] = set()
    assigned_sources: set[str] = set()
    result: dict[str, dict[str, Any]] = {}

    for score, tgt, src, mtype in candidates:
        if tgt not in assigned_targets and src not in assigned_sources:
            assigned_targets.add(tgt)
            assigned_sources.add(src)
            result[tgt] = {
                "source": src,
                "score": score,
                "type": mtype,
                "found": True,
            }

    # Complétion des colonnes cibles restantes sans correspondance
    for tgt in target_columns:
        if tgt not in result:
            result[tgt] = {
                "source": None,
                "score": 0.0,
                "type": "unmapped",
                "found": False,
            }

    return result


def resolve_mappings(
    df: pl.DataFrame,
    unknown_columns: list[str],
    target_columns: list[str],
    memory_path: Path | str,
    resolver_callback: Optional[ResolverCallback] = None,
    aliases_path: Optional[Path | str] = None,
    auto_use_aliases: bool = False,
) -> tuple[dict[str, MappingAction], dict[str, str]]:
    """Résout toutes les colonnes inconnues en consultant la mémoire, les synonymes ou le callback injecté.

    Chaque nouveau choix résolu est immédiatement persisté dans la mémoire de mapping.

    Paramètres:
        df: Le DataFrame source (pour extraire les 3 échantillons de valeurs).
        unknown_columns: Liste des colonnes non reconnues directement.
        target_columns: Liste des colonnes cibles disponibles.
        memory_path: Chemin vers config/mapping_memory.json.
        resolver_callback: Fonction interactive appelée pour les colonnes inédites.
        aliases_path: Chemin optionnel vers config/standard_aliases.json.
        auto_use_aliases: Si True ou si aliases_path est spécifié, tente automatiquement
                          de résoudre les colonnes par les synonymes FR/EN et fuzzy matching.

    Retourne:
        tuple[dict[str, MappingAction], dict[str, str]]:
            - Dictionnaire {col_source: MappingAction}
            - Dictionnaire mémoire actualisé
    """
    memory = load_mapping_memory(memory_path)
    actions: dict[str, MappingAction] = {}
    memory_modified = False

    aliases = None
    if auto_use_aliases or aliases_path is not None:
        aliases = load_standard_aliases(aliases_path)

    for col in unknown_columns:
        cached_target = resolve_column_from_memory(col, memory)

        if cached_target is not None:
            # Colonne déjà mémorisée
            is_ignored = cached_target == IGNORE_TOKEN
            actions[col] = MappingAction(
                source_col=col,
                target_col=None if is_ignored else cached_target,
                is_ignored=is_ignored,
                from_memory=True,
            )
            continue

        # Résolution automatique via le dictionnaire de synonymes FR / EN si activé
        auto_target: Optional[str] = None
        if aliases:
            matched_tgt, score, _ = find_best_target_match(
                source_col=col,
                target_columns=target_columns,
                memory=memory,
                aliases=aliases,
                fuzzy_threshold=0.85,
            )
            if matched_tgt and score >= 0.85:
                auto_target = matched_tgt

        if auto_target is not None:
            memory[col] = auto_target
            memory_modified = True
            actions[col] = MappingAction(
                source_col=col,
                target_col=auto_target,
                is_ignored=False,
                from_memory=True,
            )
            continue

        # Colonne inédite non résolue automatiquement
        samples = extract_sample_values(df, col, limit=3)

        if resolver_callback is None:
            raise UnresolvedColumnError(col, samples)

        # Appel du callback UI (CLI ou API)
        chosen_target = resolver_callback(col, samples, target_columns)

        if chosen_target not in target_columns and chosen_target != IGNORE_TOKEN:
            raise ValueError(
                f"Cible invalide '{chosen_target}' pour la colonne source '{col}'."
            )

        # Mémorisation immédiate
        memory[col] = chosen_target
        memory_modified = True
        save_mapping_memory(memory_path, memory)

        is_ignored = chosen_target == IGNORE_TOKEN
        actions[col] = MappingAction(
            source_col=col,
            target_col=None if is_ignored else chosen_target,
            is_ignored=is_ignored,
            from_memory=False,
        )

    if memory_modified:
        save_mapping_memory(memory_path, memory)

    return actions, memory
