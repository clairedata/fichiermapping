"""src/mapper.py.

Gestion de la mémoire de mapping persistante (config/mapping_memory.json)
et résolution des colonnes inconnues.
Conforme à la Clean Architecture : aucune interaction CLI bloquante (input/print)
n'est réalisée dans ce module. Toute résolution interactive passe par un callback injecté.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional
import polars as pl

from src.models import IGNORE_TOKEN, MappingAction


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

    # Filtrage des nulls et chaînes vides
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
    # 1. Correspondance exacte
    if source_col in memory:
        return memory[source_col]

    # 2. Correspondance insensible à la casse / espaces
    normalized_source = source_col.strip().lower()
    for mem_key, mem_target in memory.items():
        if mem_key.strip().lower() == normalized_source:
            return mem_target

    return None


def resolve_mappings(
    df: pl.DataFrame,
    unknown_columns: list[str],
    target_columns: list[str],
    memory_path: Path | str,
    resolver_callback: Optional[ResolverCallback] = None,
) -> tuple[dict[str, MappingAction], dict[str, str]]:
    """Résout toutes les colonnes inconnues en consultant la mémoire ou le callback injecté.

    Chaque nouveau choix résolu est immédiatement persisté dans la mémoire de mapping.

    Paramètres:
        df: Le DataFrame source (pour extraire les 3 échantillons de valeurs).
        unknown_columns: Liste des colonnes non reconnues directement.
        target_columns: Liste des colonnes cibles disponibles.
        memory_path: Chemin vers config/mapping_memory.json.
        resolver_callback: Fonction interactive appelée pour les colonnes inédites.

    Retourne:
        tuple[dict[str, MappingAction], dict[str, str]]:
            - Dictionnaire {col_source: MappingAction}
            - Dictionnaire mémoire actualisé
    """
    memory = load_mapping_memory(memory_path)
    actions: dict[str, MappingAction] = {}
    memory_modified = False

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
        else:
            # Colonne totalement inédite
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
