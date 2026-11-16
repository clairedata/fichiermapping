"""src/aligner.py.

Module d'alignement structurel et de projection dans le schéma cible.
Gère :
- La création des colonnes cibles manquantes remplies de NULL (None).
- La suppression des colonnes marquées comme ignorées (__IGNORE__).
- La fusion par coalesce en cas de collision (plusieurs colonnes sources vers une cible),
  en garantissant de ne jamais écraser les données non nulles existantes.
- La projection stricte selon l'ordre exact défini dans reference/target_schema.csv.
"""

from __future__ import annotations

from typing import Mapping
import polars as pl
from src.models import MappingAction


def align_dataframe(
    df: pl.DataFrame,
    target_columns: list[str],
    matched_columns: list[str] = (),
    mapping_actions: Mapping[str, MappingAction] = (),
    target_to_source: Optional[Mapping[str, Optional[str]]] = None,
) -> pl.DataFrame:
    """Aligne un DataFrame source dans le schéma cible fixe.

    Paramètres:
        df: Le DataFrame source brut (toutes colonnes en pl.String).
        target_columns: Ordre exact des colonnes cibles finales.
        matched_columns: Colonnes sources déjà présentes dans le schéma cible.
        mapping_actions: Actions de mapping pour les colonnes inconnues (mode CLI).
        target_to_source: Mappage direct orienté cible {colonne_cible: colonne_source} (mode Web).

    Retourne:
        pl.DataFrame: DataFrame projeté rigoureusement selon target_columns.
    """
    target_to_sources: dict[str, list[str]] = {t: [] for t in target_columns}

    # 1. Mode d'alignement direct cible -> source (prioritaire si fourni)
    if target_to_source is not None:
        for tgt, src in target_to_source.items():
            if tgt in target_to_sources and src and src in df.columns:
                if src not in target_to_sources[tgt]:
                    target_to_sources[tgt].append(src)
    else:
        # 2. Mode d'alignement classique (matched_columns + mapping_actions)
        matched_set = set(matched_columns)
        for col in df.columns:
            if col in matched_set and col in target_to_sources:
                target_to_sources[col].append(col)

        for source_col, action in mapping_actions.items():
            if action.is_ignored or action.target_col is None:
                continue
            tgt = action.target_col
            if tgt in target_to_sources:
                if source_col not in target_to_sources[tgt]:
                    target_to_sources[tgt].append(source_col)

    # Expression de projection pour chaque colonne cible
    select_exprs: list[pl.Expr] = []

    for tgt in target_columns:
        sources = target_to_sources.get(tgt, [])

        if not sources:
            # Colonne cible absente dans l'entrant : créée remplie de nulls
            expr = pl.lit(None, dtype=pl.String).alias(tgt)
        elif len(sources) == 1:
            # Une seule source : renommage / alias direct en pl.String
            expr = pl.col(sources[0]).cast(pl.String).alias(tgt)
        else:
            # Collision : plusieurs sources alimentent la même colonne cible.
            # Règle : Fusion (coalesce) sans écraser les données non nulles.
            # On traite également les chaînes vides comme nulls pour le coalesce.
            coalesce_inputs = [
                pl.when(
                    pl.col(s).is_not_null() & (pl.col(s).str.strip_chars() != "")
                )
                .then(pl.col(s))
                .otherwise(None)
                for s in sources
            ]
            expr = pl.coalesce(coalesce_inputs).cast(pl.String).alias(tgt)

        select_exprs.append(expr)

    # Projection stricte dans l'ordre exact du schéma cible
    aligned_df = df.select(select_exprs)

    return aligned_df
