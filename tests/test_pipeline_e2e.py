"""tests/test_pipeline_e2e.py.

Test d'intégration End-to-End du pipeline complet (process_file).
Simule le dépôt d'un fichier client brut, la résolution automatique,
l'alignement, le nettoyage, la quarantaine et l'archivage.
"""

from __future__ import annotations

import json
from pathlib import Path
import polars as pl
import pytest

from main import process_file
from src.comparator import load_target_schema


def test_full_pipeline_e2e(tmp_path: Path):
    """Test complet de bout en bout du pipeline d'ingestion et de nettoyage."""
    # 1. Mise en place de l'environnement de test
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    quarantine_dir = tmp_path / "quarantine"
    archive_dir = tmp_path / "archive"
    config_dir = tmp_path / "config"
    ref_dir = tmp_path / "reference"

    for d in (input_dir, output_dir, quarantine_dir, archive_dir, config_dir, ref_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Schéma cible
    target_schema_file = ref_dir / "target_schema.csv"
    target_schema_file.write_text(
        "id_client,nom,prenom,email,telephone,code_postal,date_creation\n",
        encoding="utf-8",
    )

    # Mémoire initiale : 'courriel' est déjà connu -> email
    memory_file = config_dir / "mapping_memory.json"
    memory_file.write_text(
        json.dumps({"courriel": "email"}),
        encoding="utf-8",
    )

    # Règles de nettoyage
    rules_file = config_dir / "cleaning_rules.yaml"
    rules_file.write_text(
        """
rules:
  id_client:
    required: true
    error_message: "ID manquant"
  nom:
    trim: true
    case: "upper"
    required: true
    error_message: "Nom manquant"
  prenom:
    trim: true
    case: "title"
  email:
    trim: true
    case: "lower"
    regex_match: "^[\\\\w\\\\.-]+@[\\\\w\\\\.-]+\\\\.\\\\w+$"
    error_message: "Email invalide"
  telephone:
    phone_standardize: true
  code_postal:
    trim: true
    regex_match: "^[0-9]{5}$"
    error_message: "Code postal invalide"
  date_creation:
    date_format:
      input_formats: ["%d/%m/%Y", "%Y-%m-%d"]
      output_format: "%Y-%m-%d"
""",
        encoding="utf-8",
    )

    # Fichier client brut avec :
    # - colonnes connues : nom, prenom, code_postal
    # - colonne mémorisée : courriel (-> email)
    # - colonne inédite : tel_port (résolue via callback -> telephone)
    # - colonne inutile : commentaire (résolue via callback -> __IGNORE__)
    # - ligne 1 : saine
    # - ligne 2 : ID manquant (-> quarantaine)
    # - ligne 3 : Email invalide (-> quarantaine)
    raw_file = input_dir / "export_clients_raw.csv"
    raw_file.write_text(
        "id_client;nom;prenom;courriel;tel_port;code_postal;date_creation;commentaire\n"
        "C001; dupont ;jean-marc;DUPONT@TEST.COM;06 12 34 56 78;06000;14/11/2026;client vip\n"
        ";martin;sophie;sophie@test.fr;+33 (0)7 00 11 22 33;75001;2026-11-14;sans id\n"
        "C003;durand;paul;bad_email@;01.40.00.00.00;999;10/10/2025;email et cp invalides\n",
        encoding="utf-8",
    )

    # Callback simulant l'opérateur CLI
    def mock_operator_resolver(col: str, samples: list[str], targets: list[str]) -> str:
        if col == "tel_port":
            return "telephone"
        if col == "commentaire":
            return "__IGNORE__"
        return "__IGNORE__"

    # 2. Exécution du pipeline
    report = process_file(
        file_path=raw_file,
        target_schema_path=target_schema_file,
        memory_path=memory_file,
        rules_path=rules_file,
        output_dir=output_dir,
        quarantine_dir=quarantine_dir,
        archive_dir=archive_dir,
        interactive=False,
        resolver_callback=mock_operator_resolver,
    )

    # 3. Vérifications
    # Vérification que le fichier brut a été déplacé dans archive/
    assert not raw_file.exists(), "Le fichier original doit être retiré de input/"
    assert report.archive_file is not None
    assert Path(report.archive_file).exists()

    # Vérification des statistiques
    assert report.status == "SUCCESS"
    assert report.total_rows == 3
    assert report.clean_rows == 1
    assert report.quarantined_rows == 2

    # Vérification du fichier de sortie propre
    assert report.output_file is not None
    clean_path = Path(report.output_file)
    assert clean_path.exists()
    clean_df = pl.read_csv(clean_path, infer_schema_length=0)

    # Ordre strict des colonnes cibles
    expected_cols = load_target_schema(target_schema_file)
    assert clean_df.columns == expected_cols
    assert clean_df.height == 1

    clean_row = clean_df.to_dicts()[0]
    assert clean_row["id_client"] == "C001"
    assert clean_row["nom"] == "DUPONT"
    assert clean_row["prenom"] == "Jean-Marc"
    assert clean_row["email"] == "dupont@test.com"
    assert clean_row["telephone"] == "0612345678"
    assert clean_row["code_postal"] == "06000"
    assert clean_row["date_creation"] == "2026-11-14"

    # Vérification du fichier de quarantaine
    assert report.quarantine_file is not None
    quar_path = Path(report.quarantine_file)
    assert quar_path.exists()
    quar_df = pl.read_csv(quar_path, infer_schema_length=0)
    assert quar_df.height == 2
    assert "motif_rejet" in quar_df.columns

    quar_rows = quar_df.to_dicts()
    q_martin = next(r for r in quar_rows if r["nom"] == "MARTIN")
    assert "[id_client] ID manquant" in q_martin["motif_rejet"]

    q_durand = next(r for r in quar_rows if r["nom"] == "DURAND")
    assert "[email] Email invalide" in q_durand["motif_rejet"]
    assert "[code_postal] Code postal invalide" in q_durand["motif_rejet"]
