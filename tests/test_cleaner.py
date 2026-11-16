"""tests/test_cleaner.py.

Tests unitaires pour le module de nettoyage standardisé et de mise en quarantaine (cleaner.py).
"""

from __future__ import annotations

from pathlib import Path
import polars as pl
import pytest

from src.cleaner import (
    clean_and_quarantine,
    load_cleaning_rules,
    standardize_date,
    standardize_phone,
)
from src.models import CleaningRuleConfig


def test_standardize_phone():
    """Vérifie la normalisation des numéros de téléphone :

    - 33, +33 ou 0033 remplacé par 0 et 10 caractères exacts.
    - Autre indicatif international conservé (+1, +44, etc.).
    - Rejet du texte et des numéros fictifs.
    """
    # Format français : remplacement par 0 et 10 caractères
    num1, ok1, _ = standardize_phone("+33 6 12 34 56 78")
    assert ok1 is True
    assert num1 == "0612345678"

    num2, ok2, _ = standardize_phone("+33 (0)6 12 34 56 78")
    assert ok2 is True
    assert num2 == "0612345678"

    num3, ok3, _ = standardize_phone("33612345678")
    assert ok3 is True
    assert num3 == "0612345678"

    num4, ok4, _ = standardize_phone("0033612345678")
    assert ok4 is True
    assert num4 == "0612345678"

    num5, ok5, _ = standardize_phone("06.12.34.56.78")
    assert ok5 is True
    assert num5 == "0612345678"

    # Autre indicatif conservé (+1, +44, etc.)
    num_us, ok_us, _ = standardize_phone("+1 350-420-6062")
    assert ok_us is True
    assert num_us == "+13504206062"

    num_uk, ok_uk, _ = standardize_phone("+44 7911 123456")
    assert ok_uk is True
    assert num_uk == "+447911123456"

    # Invalide : texte
    _, ok_txt, err_txt = standardize_phone("pas_un_telephone")
    assert ok_txt is False
    assert "contient des lettres" in err_txt

    # Invalide : numéro fictif
    _, ok_dummy, _ = standardize_phone("0000000000")
    assert ok_dummy is False

    # Invalide : numéro français avec mauvaise longueur
    num_short, ok_short, _ = standardize_phone("+33 6 12 34")
    assert ok_short is False

    # Null / vide
    res_none, ok_none, _ = standardize_phone(None)
    assert res_none is None and ok_none is True

    res_empty, ok_empty, _ = standardize_phone("   ")
    assert res_empty is None and ok_empty is True


def test_deduplicate_by_phone():
    """Vérifie la mise en quarantaine des doublons par numéro de téléphone."""
    rules = {
        "nom": CleaningRuleConfig(required=True),
        "telephone": CleaningRuleConfig(phone_standardize=True, unique=True),
    }

    df = pl.DataFrame(
        {
            "id": ["1", "2", "3", "4", "5"],
            "nom": ["Dupont", "Martin", "Durand", "Lefebvre", "Petit"],
            "telephone": [
                "+33 6 12 34 56 78",    # Ligne 1 : premier 0612345678 -> Conservé propre
                "06.12.34.56.78",       # Ligne 2 : DOUBLON de la ligne 1 -> Quarantaine !
                "+1 350-420-6062",       # Ligne 3 : numéro US unique -> Conservé propre
                "06 12 34 56 78",       # Ligne 4 : DOUBLON de la ligne 1 -> Quarantaine !
                "07 88 99 00 11",       # Ligne 5 : numéro français unique -> Conservé propre
            ],
        }
    )

    result = clean_and_quarantine(df, rules)

    assert result.total_rows == 5
    assert result.clean_rows == 3  # Lignes 1, 3, 5
    assert result.quarantine_rows == 2  # Lignes 2 et 4

    clean_ids = result.clean_df["id"].to_list()
    assert clean_ids == ["1", "3", "5"]

    quar_dicts = result.quarantine_df.to_dicts()
    assert len(quar_dicts) == 2
    assert "Doublon détecté" in quar_dicts[0]["motif_rejet"]
    assert "0612345678" in quar_dicts[0]["motif_rejet"]
    assert "Doublon détecté" in quar_dicts[1]["motif_rejet"]


def test_standardize_date():
    """Vérifie le parsing multiformat et la conversion en ISO (YYYY-MM-DD)."""
    formats = ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"]

    res1, ok1 = standardize_date("14/11/2026", formats, "%Y-%m-%d")
    assert ok1 is True
    assert res1 == "2026-11-14"

    res2, ok2 = standardize_date("2026-11-14", formats, "%Y-%m-%d")
    assert ok2 is True
    assert res2 == "2026-11-14"

    res3, ok3 = standardize_date("invalid-date", formats, "%Y-%m-%d")
    assert ok3 is False


def test_load_cleaning_rules_from_yaml(tmp_path: Path):
    """Vérifie le chargement d'un fichier de règles YAML."""
    yaml_file = tmp_path / "rules.yaml"
    yaml_file.write_text(
        """
rules:
  email:
    trim: true
    case: "lower"
    required: true
    regex_match: "^[\\\\w\\\\.-]+@[\\\\w\\\\.-]+\\\\.\\\\w+$"
    error_message: "Format email invalide"
  nom:
    trim: true
    case: "upper"
    required: true
""",
        encoding="utf-8",
    )

    rules = load_cleaning_rules(yaml_file)
    assert "email" in rules
    assert "nom" in rules
    assert rules["email"].required is True
    assert rules["email"].case == "lower"
    assert rules["nom"].case == "upper"


def test_clean_and_quarantine_workflow():
    """Vérifie le nettoyage complet et la séparation rigoureuse en quarantaine."""
    rules = {
        "nom": CleaningRuleConfig(trim=True, case="upper", required=True),
        "prenom": CleaningRuleConfig(trim=True, case="capitalize"),
        "email": CleaningRuleConfig(
            trim=True,
            case="lower",
            required=True,
            regex_match=r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$",
            error_message="Email invalide",
        ),
        "code_postal": CleaningRuleConfig(
            trim=True,
            regex_match=r"^[0-9]{5}$",
            error_message="Code postal doit comporter 5 chiffres",
        ),
    }

    df = pl.DataFrame(
        {
            "id": ["1", "2", "3", "4"],
            "nom": ["  dupont  ", "martin", None, "durand"],
            "prenom": ["jean-paul", "marie", "pierre", "luc"],
            "email": ["DUPONT@TEST.COM", "bad_email@", "pierre@test.fr", "luc@test.fr"],
            "code_postal": ["75001", "75002", "75003", "999"],  # "999" est invalide
        }
    )

    result = clean_and_quarantine(df, rules)

    assert result.total_rows == 4
    # Lignes saines : Ligne 1 ("dupont")
    # Lignes en quarantaine :
    # - Ligne 2 : bad_email@
    # - Ligne 3 : nom manquant (None)
    # - Ligne 4 : code_postal 999
    assert result.clean_rows == 1
    assert result.quarantine_rows == 3

    # Vérification des transformations sur la ligne saine
    clean_row = result.clean_df.to_dicts()[0]
    assert clean_row["nom"] == "DUPONT"
    assert clean_row["email"] == "dupont@test.com"
    assert clean_row["prenom"] == "Jean-Paul"
    assert "motif_rejet" not in result.clean_df.columns

    # Vérification des motifs de rejet en quarantaine
    quarantine_dicts = result.quarantine_df.to_dicts()
    assert "motif_rejet" in result.quarantine_df.columns

    q_row_2 = next(r for r in quarantine_dicts if r["id"] == "2")
    assert "[email] Email invalide : 'bad_email@'" in q_row_2["motif_rejet"]

    q_row_3 = next(r for r in quarantine_dicts if r["id"] == "3")
    assert "[nom] Valeur obligatoire manquante" in q_row_3["motif_rejet"]

    q_row_4 = next(r for r in quarantine_dicts if r["id"] == "4")
    assert "[code_postal] Code postal doit comporter 5 chiffres : '999'" in q_row_4["motif_rejet"]


def test_multiple_rejection_motifs():
    """Vérifie le cumul explicite de plusieurs motifs de rejet pour une même ligne."""
    rules = {
        "nom": CleaningRuleConfig(required=True, error_message="Nom requis"),
        "email": CleaningRuleConfig(
            regex_match=r"^[\w\.-]+@[\w\.-]+\.\w+$",
            error_message="Email invalide",
        ),
    }

    df = pl.DataFrame(
        {
            "nom": [None],
            "email": ["bad_email"],
        }
    )

    result = clean_and_quarantine(df, rules)
    assert result.quarantine_rows == 1
    motif = result.quarantine_df["motif_rejet"][0]

    assert "[nom] Nom requis" in motif
    assert "[email] Email invalide" in motif
    assert "; " in motif
