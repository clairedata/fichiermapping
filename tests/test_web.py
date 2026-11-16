"""tests/test_web.py.

Tests d'intégration pour l'API et l'application Web FastAPI (src/web.py).
Valide la cinématique orientée structure cible avec indicateurs vert/rouge
et le catalogue de schémas enregistrés.
"""

from __future__ import annotations

import io
from pathlib import Path
from fastapi.testclient import TestClient
import polars as pl
import pytest

from src.web import app

client = TestClient(app)


def test_get_index():
    """Vérifie que la page d'accueil HTML est correctement servie."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Tableau d'Alignement" in response.text
    assert "Schémas Enregistrés" in response.text


def test_list_schemas():
    """Vérifie la route /api/schemas listant tous les schémas de reference/."""
    response = client.get("/api/schemas")
    assert response.status_code == 200
    schemas = response.json()
    assert isinstance(schemas, list)
    assert len(schemas) >= 2  # target_schema.csv et schema_commandes.csv

    names = [s["file_name"] for s in schemas]
    assert "target_schema.csv" in names
    assert "schema_commandes.csv" in names


def test_get_schema_detail():
    """Vérifie la route /api/schema renvoyant les colonnes avec leurs types et contraintes."""
    response = client.get("/api/schema?name=target_schema.csv")
    assert response.status_code == 200
    data = response.json()
    assert "columns" in data
    cols = data["columns"]
    assert len(cols) > 0

    col_names = [c["name"] for c in cols]
    assert "id_client" in col_names
    assert "nom" in col_names
    assert "email" in col_names

    email_col = next(c for c in cols if c["name"] == "email")
    assert email_col["data_type"] in ("email", "string")


def test_get_memory():
    """Vérifie la route /api/memory."""
    response = client.get("/api/memory")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)


def test_save_mapping():
    """Vérifie l'enregistrement de mappings via l'API."""
    payload = {"mappings": {"web_test_src": "nom", "web_ignore_test": "__IGNORE__"}}
    response = client.post("/api/save-mapping", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "SUCCESS"

    mem_res = client.get("/api/memory")
    mem_data = mem_res.json()
    assert mem_data.get("web_test_src") == "nom"


def test_upload_indicators_and_schema_matching():
    """Vérifie l'évaluation du meilleur schéma et les indicateurs vert/rouge."""
    # Fichier client typique
    csv_content = (
        "id;last_name;prenom;email;telephone\n"
        "101;Durand;Pierre;pierre@test.fr;0611223344\n"
    )

    upload_res = client.post(
        "/api/upload",
        files={"file": ("schema_eval_test.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")},
    )
    assert upload_res.status_code == 200
    data = upload_res.json()

    # 1. Vérification du schéma convenable détecté
    assert data["best_schema"] == "target_schema.csv"
    assert "schemas_evaluation" in data
    best_eval = next(s for s in data["schemas_evaluation"] if s["is_best_match"])
    assert best_eval["file_name"] == "target_schema.csv"
    assert best_eval["score_percent"] > 0

    # 2. Vérification des indicateurs vert (is_found=True) / rouge (is_found=False)
    tgt_align = data["target_alignment"]
    email_align = next(t for t in tgt_align if t["target_name"] == "email")
    assert email_align["is_found"] is True
    assert email_align["matched_source"] == "email"

    # 'adresse' n'existe pas dans le fichier -> Rouge (is_found=False)
    adresse_align = next(t for t in tgt_align if t["target_name"] == "adresse")
    assert adresse_align["is_found"] is False
    assert adresse_align["matched_source"] is None
