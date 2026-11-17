"""tests/test_qex_parser.py.

Tests unitaires et d'intégration pour le module d'extraction Askia Design (.qex)
et la génération de schémas de référence Excel (.xlsx).
"""

from __future__ import annotations

from pathlib import Path
from fastapi.testclient import TestClient
import openpyxl
import pytest

from src.comparator import load_target_schema, load_target_schema_definitions
from src.mapper import load_standard_aliases, match_targets_to_incoming
from src.qex_parser import (
    export_qex_schema_to_excel,
    extract_importation_variables,
    list_qex_chapters,
)
from src.web import app

QEX_DIR = Path(__file__).resolve().parent.parent / "qex"
QEX_ELECTRICIEN = QEX_DIR / "T0233_OCC_Electricien (Copie de Travail).qex"
QEX_UCANSS = QEX_DIR / "J0925_Mailling_UCANSS (Copie de Travail).qex"

client = TestClient(app)


@pytest.mark.skipif(not QEX_ELECTRICIEN.exists(), reason="Fichier QEX d'exemple absent")
def test_extract_importation_variables_from_qex():
    """Vérifie l'extraction exacte des 34 variables sous le libellé Importation."""
    res = extract_importation_variables(QEX_ELECTRICIEN, chapter_label="Importation")

    assert res.selected_chapter == "Importation"
    assert len(res.variables) == 34

    names = [v.shortcut for v in res.variables]
    assert names[0] == "SIRET"
    assert names[-1] == "CH5"
    assert "TEL" in names
    assert "VILLE" in names
    assert "CP" in names
    assert "RS" in names
    assert "NOM_DIRIGEANT" in names
    # Vérifie que les questions hors chapitre Importation ne sont pas incluses
    assert "INTRO" not in names
    assert "QCP" not in names

    # Vérifie l'inférence de types (TEL -> phone, mais TYPE_TEL -> string)
    types_by_name = {v.shortcut: v.inferred_type for v in res.variables}
    assert types_by_name["TEL"] == "phone"
    assert types_by_name["TYPE_TEL"] == "string"
    assert types_by_name["SIRET"] == "string"


@pytest.mark.skipif(not QEX_UCANSS.exists(), reason="Fichier QEX UCANSS absent")
def test_extract_missing_importation_raises_with_chapters():
    """Vérifie qu'un QEX sans libellé 'Importation' lève une erreur listant les chapitres disponibles."""
    with pytest.raises(ValueError, match="Chapitres disponibles"):
        extract_importation_variables(QEX_UCANSS, chapter_label="Importation")

    # Extraction réussie en précisant un autre chapitre existant (INFOS_IDS)
    res = extract_importation_variables(QEX_UCANSS, chapter_label="INFOS_IDS")
    assert res.selected_chapter == "INFOS_IDS"
    assert [v.shortcut for v in res.variables] == ["BRKID", "id_guid", "id_seed"]


@pytest.mark.skipif(not QEX_ELECTRICIEN.exists(), reason="Fichier QEX d'exemple absent")
def test_export_qex_schema_to_excel_and_reload(tmp_path: Path):
    """Vérifie la génération de l'Excel au nom du QEX et sa relecture par comparator.py."""
    excel_path, res = export_qex_schema_to_excel(QEX_ELECTRICIEN, tmp_path)

    assert excel_path.exists()
    assert excel_path.name == f"{QEX_ELECTRICIEN.stem}.xlsx"

    # Vérification des 2 onglets Excel
    wb = openpyxl.load_workbook(excel_path, read_only=True)
    assert "Schema" in wb.sheetnames
    assert "Modele_Colonnes" in wb.sheetnames
    wb.close()

    # Relecture directe par comparator.py
    defs = load_target_schema_definitions(excel_path)
    cols = load_target_schema(excel_path)
    assert len(defs) == 34
    assert cols[0] == "SIRET"
    assert cols[-1] == "CH5"

    tel_def = next(d for d in defs if d.name == "TEL")
    assert tel_def.data_type == "phone"


@pytest.mark.skipif(not QEX_ELECTRICIEN.exists(), reason="Fichier QEX d'exemple absent")
def test_bidirectional_alias_matching_with_qex_schema():
    """Vérifie que les colonnes entrantes FR/EN matchent automatiquement les variables Askia (TEL, CP, VILLE, RS)."""
    res = extract_importation_variables(QEX_ELECTRICIEN)
    qex_targets = [v.shortcut for v in res.variables]

    incoming = ["siret", "company", "phone", "city", "zip_code", "first_name", "last_name"]
    matches = match_targets_to_incoming(qex_targets, incoming, aliases=load_standard_aliases())

    assert matches["SIRET"]["source"] == "siret"
    assert matches["RS"]["source"] == "company"
    assert matches["TEL"]["source"] == "phone"
    assert matches["VILLE"]["source"] == "city"
    assert matches["CP"]["source"] == "zip_code"
    assert matches["PRENOM_DIRIGEANT"]["source"] == "first_name"
    assert matches["NOM_DIRIGEANT"]["source"] == "last_name"


@pytest.mark.skipif(not QEX_ELECTRICIEN.exists(), reason="Fichier QEX d'exemple absent")
def test_web_api_qex_endpoints():
    """Vérifie les routes Web /api/qex-files, /api/qex/chapters et /api/qex/import."""
    # 1. Liste des QEX dans qex/
    res_list = client.get("/api/qex-files")
    assert res_list.status_code == 200
    qex_names = [f["name"] for f in res_list.json()]
    assert QEX_ELECTRICIEN.name in qex_names

    # 2. Liste des chapitres du QEX
    res_chap = client.get(f"/api/qex/chapters/{QEX_ELECTRICIEN.name}")
    assert res_chap.status_code == 200
    chaps = [c["shortcut"] for c in res_chap.json()["chapters"]]
    assert "Importation" in chaps

    # 3. Import QEX -> création du schéma Excel dans reference/
    res_imp = client.post(
        "/api/qex/import",
        data={"qex_filename": QEX_ELECTRICIEN.name, "chapter_label": "Importation"},
    )
    assert res_imp.status_code == 200
    payload = res_imp.json()
    assert payload["status"] == "SUCCESS"
    assert payload["schema_file"] == f"{QEX_ELECTRICIEN.stem}.xlsx"
    assert payload["variables_count"] == 34

    # 4. Vérifie que /api/schemas contient à la fois target_schema.csv (défaut) et le nouveau .xlsx
    res_schemas = client.get("/api/schemas")
    assert res_schemas.status_code == 200
    schema_names = [s["file_name"] for s in res_schemas.json()]
    assert schema_names[0] == "target_schema.csv"
    assert f"{QEX_ELECTRICIEN.stem}.xlsx" in schema_names


@pytest.mark.skipif(not QEX_ELECTRICIEN.exists(), reason="Fichier QEX d'exemple absent")
def test_web_api_process_with_qex_excel_schema():
    """Vérifie l'exécution complète POST /api/process avec un schéma Excel issu d'un QEX."""
    import io
    from src.web import ARCHIVE_DIR, OUTPUT_DIR, QUARANTINE_DIR

    schema_name = f"{QEX_ELECTRICIEN.stem}.xlsx"
    csv_content = (
        "siret;company;phone;city;zip_code\n"
        "12345678901234;Elec Pro;+33 6 12 34 56 78;Paris;75001\n"
        "98765432109876;Elec Bis;+33 6 12 34 56 78;Lyon;69001\n"
        "11111111111111;Elec Bad;pas_un_tel;Lille;59000\n"
    )

    upload_res = client.post(
        f"/api/upload?schema_name={schema_name}",
        files={"file": ("_qex_e2e_test.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")},
    )
    assert upload_res.status_code == 200
    info = upload_res.json()
    target_to_source = {t["target_name"]: t["matched_source"] for t in info["target_alignment"]}

    proc_res = client.post(
        "/api/process",
        json={
            "file_name": "_qex_e2e_test.csv",
            "schema_name": schema_name,
            "target_to_source": target_to_source,
            "save_to_memory": False,
        },
    )
    assert proc_res.status_code == 200, proc_res.text
    result = proc_res.json()
    assert result["status"] == "SUCCESS"
    assert result["total_rows"] == 3
    assert result["clean_rows"] == 1
    assert result["quarantine_rows"] == 2  # 1 doublon TEL + 1 TEL invalide
    assert result["clean_preview"][0]["TEL"] == "0612345678"
    assert result["clean_excel_file"].endswith(".xlsx")

    # Nettoyage des fichiers temporaires générés par le test
    for folder, fname in [
        (OUTPUT_DIR, result.get("clean_file")),
        (OUTPUT_DIR, result.get("clean_excel_file")),
        (QUARANTINE_DIR, result.get("quarantine_file")),
        (ARCHIVE_DIR, result.get("archive_file")),
    ]:
        if fname:
            (folder / fname).unlink(missing_ok=True)
