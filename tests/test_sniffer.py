"""tests/test_sniffer.py.

Tests unitaires pour le module d'ingestion et de détection automatique (sniffer.py).
"""

from __future__ import annotations

from pathlib import Path
import openpyxl
import polars as pl
import pytest

from src.sniffer import (
    detect_delimiter,
    detect_encoding,
    read_file,
)


def test_detect_encoding_utf8(tmp_path: Path):
    """Vérifie la détection d'un encodage UTF-8 standard."""
    file = tmp_path / "test_utf8.csv"
    file.write_text("id,nom\n1,Émile\n", encoding="utf-8")
    assert detect_encoding(file) == "utf-8"


def test_detect_encoding_utf8_sig(tmp_path: Path):
    """Vérifie la détection d'un encodage UTF-8 avec BOM."""
    file = tmp_path / "test_bom.csv"
    file.write_bytes(b"\xef\xbb\xbfid,nom\n1,Sophie\n")
    assert detect_encoding(file) == "utf-8-sig"


def test_detect_encoding_windows1252(tmp_path: Path):
    """Vérifie la détection d'un encodage Windows-1252 / Latin-1."""
    file = tmp_path / "test_w1252.csv"
    file.write_bytes("id;prénom;ville\n1;René;Nîmes\n".encode("windows-1252"))
    enc = detect_encoding(file)
    assert enc in ("windows-1252", "latin-1", "iso-8859-1")


def test_detect_delimiter_semicolon(tmp_path: Path):
    """Vérifie la détection du séparateur point-virgule (format français usuel)."""
    file = tmp_path / "test_semi.csv"
    file.write_text("col1;col2;col3\na;b;c\n1;2;3\n", encoding="utf-8")
    assert detect_delimiter(file, encoding="utf-8") == ";"


def test_detect_delimiter_comma(tmp_path: Path):
    """Vérifie la détection du séparateur virgule."""
    file = tmp_path / "test_comma.csv"
    file.write_text("col1,col2,col3\na,b,c\n1,2,3\n", encoding="utf-8")
    assert detect_delimiter(file, encoding="utf-8") == ","


def test_detect_delimiter_tab(tmp_path: Path):
    """Vérifie la détection du séparateur tabulation (TSV)."""
    file = tmp_path / "test_tab.tsv"
    file.write_text("col1\tcol2\tcol3\na\tb\tc\n", encoding="utf-8")
    assert detect_delimiter(file, encoding="utf-8") == "\t"


def test_detect_delimiter_pipe(tmp_path: Path):
    """Vérifie la détection du séparateur pipe (|)."""
    file = tmp_path / "test_pipe.csv"
    file.write_text("col1|col2|col3\na|b|c\n", encoding="utf-8")
    assert detect_delimiter(file, encoding="utf-8") == "|"


def test_read_csv_preserves_leading_zeros(tmp_path: Path):
    """Vérifie que les colonnes sont lues en pl.String et préservent les zéros initiaux."""
    file = tmp_path / "clients.csv"
    file.write_text(
        "code_client;code_postal;telephone\n00123;06000;0145236789\n00004;75001;0612345678\n",
        encoding="utf-8",
    )

    df, meta = read_file(file)

    assert meta["format"] == "csv"
    assert meta["delimiter"] == ";"
    assert df.height == 2
    assert df.schema["code_client"] == pl.String
    assert df.schema["code_postal"] == pl.String
    assert df.schema["telephone"] == pl.String

    # Intégrité absolue des zéros initiaux
    assert df["code_client"].to_list() == ["00123", "00004"]
    assert df["code_postal"].to_list() == ["06000", "75001"]
    assert df["telephone"].to_list() == ["0145236789", "0612345678"]


def test_read_excel_as_strings(tmp_path: Path):
    """Vérifie la lecture d'un fichier Excel XLSX avec typage String intégral."""
    excel_file = tmp_path / "clients.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["id_client", "code_postal", "actif"])
    ws.append(["007", "06000", True])
    ws.append(["042", "75015", False])
    wb.save(excel_file)

    df, meta = read_file(excel_file)

    assert meta["format"] == "excel"
    assert df.height == 2
    assert all(dtype == pl.String for dtype in df.schema.values())
    assert df["id_client"].to_list() == ["007", "042"]
    assert df["code_postal"].to_list() == ["06000", "75015"]


def test_read_file_unsupported_format(tmp_path: Path):
    """Vérifie le rejet d'un format non supporté."""
    pdf_file = tmp_path / "document.pdf"
    pdf_file.write_text("dummy", encoding="utf-8")

    with pytest.raises(ValueError, match="Format de fichier non supporté"):
        read_file(pdf_file)
