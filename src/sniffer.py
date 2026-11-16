"""src/sniffer.py.

Module d'ingestion et de détection automatique pour fichiers CSV et XLSX.
Lit systématiquement toutes les colonnes en type String (pl.String) pour préserver
l'intégrité absolue des données brutes (zéros initiaux, codes postaux, téléphones).
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Optional
import charset_normalizer
import polars as pl


CANDIDATE_DELIMITERS = [";", ",", "\t", "|"]


def detect_encoding(file_path: Path | str, sample_size: int = 65536) -> str:
    """Détecte l'encodage d'un fichier texte en inspectant les premiers octets.

    Gère les BOM (UTF-8, UTF-16) et s'appuie sur charset-normalizer avec fallback
    sur utf-8 et latin-1.
    """
    path = Path(file_path)
    with open(path, "rb") as f:
        raw = f.read(sample_size)

    if not raw:
        return "utf-8"

    # Vérification explicite des BOMs
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if raw.startswith(b"\xfe\xff"):
        return "utf-16-be"

    # Détection par charset-normalizer
    match = charset_normalizer.from_bytes(raw).best()
    if match is not None and match.encoding:
        detected = match.encoding.lower()
        # Normalisation des alias courants
        if detected in ("utf_8", "utf8"):
            return "utf-8"
        if detected.startswith("cp125") or detected.startswith("windows-125"):
            return "windows-1252"
        if detected in ("iso-8859-1", "iso-8859-15", "latin_1", "latin1"):
            return "latin-1"
        return detected

    # Fallback sécurisé
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "latin-1"


def detect_delimiter(
    file_path: Path | str,
    encoding: str = "utf-8",
    sample_lines: int = 40,
) -> str:
    """Détecte le délimiteur d'un fichier CSV (; , \\t |).

    Combine csv.Sniffer et un mécanisme de comptage fréquentiel robuste
    en cas d'échec du sniffer standard.
    """
    path = Path(file_path)
    lines: list[str] = []
    # Lecture d'un échantillon de lignes
    enc_to_use = "utf-8" if encoding == "utf-8-sig" else encoding
    try:
        with open(path, mode="r", encoding=enc_to_use, errors="replace") as f:
            for _ in range(sample_lines):
                line = f.readline()
                if not line:
                    break
                # Ignorer lignes vides
                stripped = line.strip()
                if stripped:
                    lines.append(stripped)
    except Exception:
        # En cas d'erreur de lecture, tentative en latin-1
        with open(path, mode="r", encoding="latin-1", errors="replace") as f:
            for _ in range(sample_lines):
                line = f.readline()
                if not line:
                    break
                stripped = line.strip()
                if stripped:
                    lines.append(stripped)

    if not lines:
        return ","

    sample_text = "\n".join(lines[:10])

    # 1. Tentative avec csv.Sniffer
    try:
        dialect = csv.Sniffer().sniff(sample_text, delimiters=CANDIDATE_DELIMITERS)
        if dialect.delimiter in CANDIDATE_DELIMITERS:
            return dialect.delimiter
    except Exception:
        pass

    # 2. Heuristique de régularité par délimiteur
    # On cherche le délimiteur ayant un compte constant et > 0 sur toutes les lignes
    best_delim = ","
    best_score = -1

    for delim in CANDIDATE_DELIMITERS:
        counts = [line.count(delim) for line in lines]
        if not counts or max(counts) == 0:
            continue
        first_count = counts[0]
        # Si le compte est parfaitement identique sur toutes les lignes
        if first_count > 0 and all(c == first_count for c in counts):
            score = 1000 + first_count
        else:
            # Score basé sur la régularité (moyenne - variance)
            avg = sum(counts) / len(counts)
            variance = sum((c - avg) ** 2 for c in counts) / len(counts)
            score = avg - variance
        if score > best_score:
            best_score = score
            best_delim = delim

    return best_delim


def read_file(file_path: Path | str) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Lit un fichier client brut (CSV ou Excel) et le charge dans un DataFrame Polars.

    Toutes les colonnes sont impérativement typées en String (pl.String)
    pour préserver l'intégrité des données (ex: zéros initiaux comme '01234').

    Retourne:
        tuple[pl.DataFrame, dict[str, Any]]: Le DataFrame brut et ses métadonnées
        (encodage, délimiteur, format de fichier, taille).
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Le fichier spécifié n'existe pas : {path}")

    suffix = path.suffix.lower()
    metadata: dict[str, Any] = {
        "file_name": path.name,
        "file_path": str(path.resolve()),
        "extension": suffix,
        "size_bytes": path.stat().st_size,
    }

    if suffix in (".xlsx", ".xls", ".xlsm"):
        # Fichier Excel
        metadata["format"] = "excel"
        df = _read_excel_as_strings(path)
    elif suffix in (".csv", ".tsv", ".txt"):
        # Fichier texte délimité
        encoding = detect_encoding(path)
        delimiter = detect_delimiter(path, encoding=encoding)
        metadata["format"] = "csv"
        metadata["encoding"] = encoding
        metadata["delimiter"] = delimiter
        df = _read_csv_as_strings(path, encoding=encoding, delimiter=delimiter)
    else:
        raise ValueError(
            f"Format de fichier non supporté '{suffix}'. "
            "Formats acceptés : .csv, .tsv, .txt, .xlsx, .xls"
        )

    # Nettoyage systématique des noms de colonnes : suppression espaces superflus et BOM résiduel
    cleaned_col_names = [c.strip().lstrip("\ufeff") for c in df.columns]
    if cleaned_col_names != df.columns:
        df = df.rename(dict(zip(df.columns, cleaned_col_names)))

    metadata["columns"] = df.columns
    metadata["row_count"] = df.height

    return df, metadata


def _read_csv_as_strings(path: Path, encoding: str, delimiter: str) -> pl.DataFrame:
    """Lit un CSV en forçant tous les types de colonnes à pl.String."""
    # Si l'encodage est UTF-8 standard ou UTF-8-sig
    try:
        if encoding in ("utf-8", "utf-8-sig"):
            return pl.read_csv(
                path,
                separator=delimiter,
                infer_schema_length=0,
                truncate_ragged_lines=False,
                ignore_errors=False,
            )
        elif encoding in ("windows-1252", "cp1252"):
            # Polars supporte nativement windows-1252
            return pl.read_csv(
                path,
                separator=delimiter,
                encoding="windows-1252",
                infer_schema_length=0,
                truncate_ragged_lines=False,
                ignore_errors=False,
            )
    except Exception:
        # En cas d'incompatibilité native Polars avec le charset,
        # on décode en mémoire et on transmet en UTF-8 via BytesIO
        pass

    with open(path, "rb") as f:
        raw_bytes = f.read()

    # Décodage sûr avec remplacement des octets invalides
    decoded_text = raw_bytes.decode(encoding, errors="replace")
    utf8_buffer = io.BytesIO(decoded_text.encode("utf-8"))

    return pl.read_csv(
        utf8_buffer,
        separator=delimiter,
        infer_schema_length=0,
        truncate_ragged_lines=False,
        ignore_errors=False,
    )


def _read_excel_as_strings(path: Path) -> pl.DataFrame:
    """Lit un fichier Excel en s'assurant que toutes les colonnes sont strictement des Strings."""
    engines = ["calamine", "openpyxl"]
    last_err: Optional[Exception] = None

    for eng in engines:
        try:
            df = pl.read_excel(path, engine=eng, infer_schema_length=0)
            # Garantir le cast pl.String pour chaque colonne
            string_cols = [pl.col(c).cast(pl.String) for c in df.columns]
            return df.select(string_cols)
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"Échec de la lecture du fichier Excel '{path}': {last_err}")
