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


def test_normalize_and_compact_column_name():
    """Vérifie le nettoyage et la normalisation des accents et séparateurs."""
    from src.mapper import compact_column_name, normalize_column_name

    assert normalize_column_name("Prénom") == "prenom"
    assert normalize_column_name("Téléphone portable") == "telephone_portable"
    assert normalize_column_name("  E-Mail Address  ") == "e_mail_address"
    assert normalize_column_name("Code Postal") == "code_postal"
    assert normalize_column_name("ID / Client #") == "id_client"

    assert compact_column_name("Code Postal") == "codepostal"
    assert compact_column_name("first_name") == "firstname"
    assert compact_column_name("téléphone") == "telephone"


def test_load_standard_aliases():
    """Vérifie le chargement du dictionnaire d'alias bilingue FR / EN."""
    from src.mapper import load_standard_aliases

    aliases = load_standard_aliases()
    assert isinstance(aliases, dict)
    assert len(aliases) >= 10

    # Vérification des colonnes principales
    for col in ("nom", "prenom", "telephone", "email", "ville", "code_postal", "adresse", "pays", "id_client"):
        assert col in aliases
        assert len(aliases[col]) > 0

    # Vérification de synonymes français et anglais usuels
    assert "last_name" in aliases["nom"]
    assert "nom_famille" in aliases["nom"]
    assert "first_name" in aliases["prenom"]
    assert "prénom" in aliases["prenom"]
    assert "phone" in aliases["telephone"]
    assert "tel" in aliases["telephone"]
    assert "city" in aliases["ville"]
    assert "commune" in aliases["ville"]
    assert "zip" in aliases["code_postal"]
    assert "cp" in aliases["code_postal"]
    assert "country" in aliases["pays"]


def test_score_column_match_hierarchy():
    """Vérifie les niveaux de priorité de correspondance : Exact > Normalisé > Mémoire > Alias > Fuzzy."""
    from src.mapper import score_column_match

    memory = {"mail_perso": "email"}
    aliases = {
        "telephone": ["phone", "tel", "portable"],
        "nom": ["last_name", "surname", "name"],
    }

    # 1. Exact (1.0)
    score, mtype = score_column_match("email", "email")
    assert score == 1.0 and mtype == "exact"

    # 2. Normalisé (0.98)
    score, mtype = score_column_match("Prénom", "prenom")
    assert score == 0.98 and mtype == "normalized"

    # 3. Mémoire (0.95)
    score, mtype = score_column_match("mail_perso", "email", memory=memory)
    assert score == 0.95 and mtype == "memory"

    # 4. Alias FR / EN (0.92)
    score, mtype = score_column_match("phone", "telephone", aliases=aliases)
    assert score == 0.92 and mtype == "alias"

    # 5. Fuzzy matching (0.82 - 0.88)
    score, mtype = score_column_match("telephon", "telephone", aliases=aliases)
    assert mtype == "fuzzy"
    assert score >= 0.82


def test_find_best_target_match_fr_and_en():
    """Vérifie l'identification de la cible optimale pour des colonnes en FR et EN."""
    from src.mapper import find_best_target_match, load_standard_aliases

    targets = ["id_client", "nom", "prenom", "email", "telephone", "ville", "code_postal", "pays"]
    aliases = load_standard_aliases()

    test_cases = [
        ("first_name", "prenom"),
        ("last_name", "nom"),
        ("phone_number", "telephone"),
        ("tel_portable", "telephone"),
        ("city", "ville"),
        ("commune", "ville"),
        ("zip_code", "code_postal"),
        ("cp", "code_postal"),
        ("country", "pays"),
        ("customer_id", "id_client"),
        ("courriel", "email"),
    ]

    for incoming_col, expected_target in test_cases:
        best_tgt, score, mtype = find_best_target_match(incoming_col, targets, aliases=aliases)
        assert best_tgt == expected_target, f"Échec pour '{incoming_col}': obtenu '{best_tgt}', attendu '{expected_target}'"
        assert score >= 0.85


def test_match_targets_to_incoming_optimal_bipartite():
    """Vérifie l'attribution gloutonne sans collision ni ambiguïté."""
    from src.mapper import match_targets_to_incoming

    targets = ["id_client", "civilite", "nom", "prenom", "email", "telephone", "adresse", "code_postal", "ville", "pays"]
    incoming = ["name", "email", "phone", "city", "id", "siret"]

    result = match_targets_to_incoming(targets, incoming)

    assert result["email"]["source"] == "email"
    assert result["email"]["found"] is True

    assert result["telephone"]["source"] == "phone"
    assert result["telephone"]["found"] is True

    assert result["ville"]["source"] == "city"
    assert result["ville"]["found"] is True

    assert result["nom"]["source"] == "name"
    assert result["nom"]["found"] is True

    assert result["id_client"]["source"] == "id"
    assert result["id_client"]["found"] is True

    assert result["adresse"]["source"] is None
    assert result["adresse"]["found"] is False


def test_resolve_mappings_with_auto_aliases(tmp_path: Path):
    """Vérifie que resolve_mappings résout automatiquement les alias lorsque auto_use_aliases est activé."""
    from src.mapper import resolve_mappings

    mem_file = tmp_path / "memory.json"
    mem_file.write_text("{}", encoding="utf-8")

    df = pl.DataFrame({
        "first_name": ["Alice", "Bob"],
        "phone": ["0611223344", "0799887766"],
    })
    target_columns = ["prenom", "telephone", "nom"]

    actions, memory = resolve_mappings(
        df=df,
        unknown_columns=["first_name", "phone"],
        target_columns=target_columns,
        memory_path=mem_file,
        auto_use_aliases=True,
    )

    assert actions["first_name"].target_col == "prenom"
    assert actions["first_name"].from_memory is True
    assert actions["phone"].target_col == "telephone"
    assert actions["phone"].from_memory is True

    # Persistance vérifiée
    assert memory["first_name"] == "prenom"
    assert memory["phone"] == "telephone"

