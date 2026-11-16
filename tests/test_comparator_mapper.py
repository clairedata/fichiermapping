"""tests/test_comparator_mapper.py.

Tests unitaires pour les modules comparator.py et mapper.py.
"""

from __future__ import annotations

import json
from pathlib import Path
import polars as pl
import pytest

from src.comparator import compare_schemas, load_target_schema
from src.mapper import (
    UnresolvedColumnError,
    extract_sample_values,
    load_mapping_memory,
    resolve_mappings,
    save_mapping_memory,
)
from src.models import IGNORE_TOKEN


def test_load_target_schema(tmp_path: Path):
    """Vérifie le chargement exact et ordonné du schéma cible."""
    schema_file = tmp_path / "target_schema.csv"
    schema_file.write_text("id,nom,prenom,email\n", encoding="utf-8")

    cols = load_target_schema(schema_file)
    assert cols == ["id", "nom", "prenom", "email"]


def test_load_target_schema_with_types(tmp_path: Path):
    """Vérifie le chargement du schéma cible au format tabulaire avec types et descriptions."""
    from src.comparator import load_target_schema_definitions
    schema_file = tmp_path / "target_schema_typed.csv"
    schema_file.write_text(
        "colonne,type,description\n"
        "id_client,string,ID unique\n"
        "email,email,Contact email\n"
        "date_creation,date,Date d'inscription\n",
        encoding="utf-8",
    )

    cols = load_target_schema(schema_file)
    assert cols == ["id_client", "email", "date_creation"]

    defs = load_target_schema_definitions(schema_file)
    assert len(defs) == 3
    assert defs[0].name == "id_client" and defs[0].data_type == "string"
    assert defs[1].name == "email" and defs[1].data_type == "email"
    assert defs[2].name == "date_creation" and defs[2].data_type == "date"


def test_load_target_schema_empty(tmp_path: Path):
    """Vérifie le comportement en cas de schéma vide."""
    schema_file = tmp_path / "empty_schema.csv"
    schema_file.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="vide ou invalide"):
        load_target_schema(schema_file)


def test_compare_schemas_exact_match():
    """Vérifie la détection d'une concordance parfaite."""
    target = ["id", "nom", "prenom", "email"]
    incoming = ["id", "nom", "prenom", "email"]

    diff = compare_schemas(incoming, target)

    assert diff.is_exact_match is True
    assert diff.has_unknown_columns is False
    assert diff.matched_columns == incoming
    assert diff.unknown_columns == []
    assert diff.missing_target_columns == []


def test_compare_schemas_diff():
    """Vérifie la détection des colonnes inconnues et cibles manquantes."""
    target = ["id", "nom", "prenom", "email", "telephone"]
    incoming = ["id", "nom", "prenom", "mail_perso", "tel_fixe"]

    diff = compare_schemas(incoming, target)

    assert diff.is_exact_match is False
    assert diff.has_unknown_columns is True
    assert diff.matched_columns == ["id", "nom", "prenom"]
    assert diff.unknown_columns == ["mail_perso", "tel_fixe"]
    assert diff.missing_target_columns == ["email", "telephone"]


def test_extract_sample_values():
    """Vérifie l'extraction des premières valeurs non-nulles et non-vides."""
    df = pl.DataFrame(
        {
            "col": [None, "", "  ", "Paris", "Lyon", "Marseille", "Bordeaux"],
        }
    )

    samples = extract_sample_values(df, "col", limit=3)
    assert samples == ["Paris", "Lyon", "Marseille"]


def test_mapping_memory_persistence(tmp_path: Path):
    """Vérifie la persistance du cache mémoire en JSON."""
    mem_file = tmp_path / "mapping_memory.json"
    initial = {"mail": "email", "tel": "telephone"}
    save_mapping_memory(mem_file, initial)

    loaded = load_mapping_memory(mem_file)
    assert loaded == initial


def test_resolve_mappings_from_memory(tmp_path: Path):
    """Vérifie que les colonnes déjà mémorisées sont appliquées automatiquement sans callback."""
    mem_file = tmp_path / "mapping_memory.json"
    save_mapping_memory(mem_file, {"mail_perso": "email", "notes": IGNORE_TOKEN})

    df = pl.DataFrame({"mail_perso": ["a@b.com"], "notes": ["blabla"]})
    target_columns = ["email", "nom"]

    actions, memory = resolve_mappings(
        df=df,
        unknown_columns=["mail_perso", "notes"],
        target_columns=target_columns,
        memory_path=mem_file,
        resolver_callback=None,  # Aucun callback nécessaire
    )

    assert actions["mail_perso"].target_col == "email"
    assert actions["mail_perso"].from_memory is True
    assert actions["notes"].is_ignored is True
    assert actions["notes"].from_memory is True


def test_resolve_mappings_with_interactive_callback(tmp_path: Path):
    """Vérifie la résolution interactive d'une colonne inédite et sa mémorisation immédiate."""
    mem_file = tmp_path / "mapping_memory.json"
    mem_file.write_text("{}", encoding="utf-8")

    df = pl.DataFrame({"tel_mobile": ["0611223344", "0799887766"]})
    target_columns = ["telephone", "email"]

    called_with: dict[str, list[str]] = {}

    def mock_callback(col: str, samples: list[str], targets: list[str]) -> str:
        called_with["col"] = [col]
        called_with["samples"] = samples
        return "telephone"

    actions, memory = resolve_mappings(
        df=df,
        unknown_columns=["tel_mobile"],
        target_columns=target_columns,
        memory_path=mem_file,
        resolver_callback=mock_callback,
    )

    assert called_with["col"] == ["tel_mobile"]
    assert len(called_with["samples"]) == 2
    assert actions["tel_mobile"].target_col == "telephone"
    assert actions["tel_mobile"].from_memory is False

    # Vérification que le choix a bien été enregistré dans le fichier JSON
    reloaded_memory = load_mapping_memory(mem_file)
    assert reloaded_memory.get("tel_mobile") == "telephone"


def test_resolve_mappings_unresolved_raises_error(tmp_path: Path):
    """Vérifie qu'une colonne inconnue sans mémoire et sans callback lève une erreur explicite."""
    mem_file = tmp_path / "empty_memory.json"
    mem_file.write_text("{}", encoding="utf-8")

    df = pl.DataFrame({"inconnue": ["valeur1"]})
    target_columns = ["email", "nom"]

    with pytest.raises(UnresolvedColumnError, match="inconnue"):
        resolve_mappings(
            df=df,
            unknown_columns=["inconnue"],
            target_columns=target_columns,
            memory_path=mem_file,
            resolver_callback=None,
        )
