"""src/comparator.py.

Module de comparaison structurelle entre les en-têtes d'un fichier source
et le schéma cible fixe défini dans reference/target_schema.csv.
Supporte le schéma cible typé (colonne, type, description) ainsi que le format d'en-tête simple.
"""

from __future__ import annotations

import csv
from pathlib import Path
from src.models import SchemaDiff, TargetColumn

TYPE_HEADER_KEYWORDS = {"type", "data_type", "type_donnee", "datatype"}
NAME_HEADER_KEYWORDS = {"colonne", "column", "name", "nom", "champ", "field", "nom_colonne", "column_name"}


def load_target_schema_definitions(target_schema_path: Path | str) -> list[TargetColumn]:
    """Charge le schéma cible complet avec types et descriptions associées.

    Accepte :
    1. Format tabulaire :
       colonne,type,description
       id_client,string,Identifiant unique
       date_creation,date,Date de création
    2. Format en-tête simple :
       id_client,nom,prenom (types par défaut à 'string')
    3. Format deux lignes (Ligne 1 = noms, Ligne 2 = types)

    Retourne:
        list[TargetColumn]: Liste ordonnée d'objets TargetColumn.
    """
    path = Path(target_schema_path)
    if not path.exists():
        raise FileNotFoundError(f"Le schéma cible n'existe pas : {path}")

    rows: list[list[str]] = []
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb.active
            for r in ws.iter_rows(values_only=True):
                cleaned_row = [str(cell).strip() if cell is not None else "" for cell in r]
                if cleaned_row and any(cleaned_row):
                    rows.append(cleaned_row)
        finally:
            wb.close()
    else:
        with open(path, mode="r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            for r in reader:
                cleaned_row = [cell.strip() for cell in r]
                if cleaned_row and any(cleaned_row):
                    rows.append(cleaned_row)

    if not rows:
        raise ValueError(f"Le fichier de schéma cible '{path}' est vide ou invalide.")

    first_row = [c.lower() for c in rows[0]]

    # Cas 1 : Format tabulaire avec colonnes 'colonne' et 'type'
    has_name_col = any(c in NAME_HEADER_KEYWORDS for c in first_row)
    has_type_col = any(c in TYPE_HEADER_KEYWORDS for c in first_row)

    if has_name_col and has_type_col and len(rows) > 1:
        name_idx = next(i for i, c in enumerate(first_row) if c in NAME_HEADER_KEYWORDS)
        type_idx = next(i for i, c in enumerate(first_row) if c in TYPE_HEADER_KEYWORDS)
        desc_idx = next(
            (i for i, c in enumerate(first_row) if "desc" in c or "commentaire" in c),
            None,
        )

        definitions: list[TargetColumn] = []
        for row in rows[1:]:
            if len(row) > name_idx and row[name_idx]:
                col_name = row[name_idx]
                col_type = row[type_idx] if len(row) > type_idx and row[type_idx] else "string"
                col_desc = row[desc_idx] if desc_idx is not None and len(row) > desc_idx else ""
                definitions.append(
                    TargetColumn(
                        name=col_name,
                        data_type=col_type.lower(),
                        description=col_desc,
                    )
                )
        if definitions:
            return definitions

    # Cas 2 : Format deux lignes (L1 = noms, L2 = types)
    if len(rows) == 2 and len(rows[0]) == len(rows[1]):
        # Vérification si la 2ème ligne contient des types reconnus
        common_types = {"string", "int", "integer", "float", "date", "datetime", "email", "phone", "bool", "boolean", "text"}
        if all(t.lower() in common_types for t in rows[1]):
            return [
                TargetColumn(name=name, data_type=dtype.lower(), description="")
                for name, dtype in zip(rows[0], rows[1])
            ]

    # Cas 3 : Format simple (une seule ligne d'en-tête ou liste brute)
    return [
        TargetColumn(name=col_name, data_type="string", description="")
        for col_name in rows[0]
        if col_name
    ]


def load_target_schema(target_schema_path: Path | str) -> list[str]:
    """Charge le schéma cible de référence et retourne la liste ordonnée des noms de colonnes."""
    definitions = load_target_schema_definitions(target_schema_path)
    return [col.name for col in definitions]


def compare_schemas(
    incoming_columns: list[str],
    target_columns: list[str],
) -> SchemaDiff:
    """Compare les colonnes d'un DataFrame entrant avec le schéma cible."""
    target_set = set(target_columns)
    incoming_set = set(incoming_columns)

    # Ordre préservé des colonnes entrantes
    matched: list[str] = [c for c in incoming_columns if c in target_set]
    unknown: list[str] = [c for c in incoming_columns if c not in target_set]

    # Ordre strict préservé du schéma cible pour les colonnes manquantes
    missing: list[str] = [c for c in target_columns if c not in incoming_set]

    return SchemaDiff(
        matched_columns=matched,
        unknown_columns=unknown,
        missing_target_columns=missing,
    )
