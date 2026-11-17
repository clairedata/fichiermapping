"""src/web.py.

Application Web FastAPI offrant une interface graphique interactive orientée structure cible :
- Indicateur visuel vert (Trouvée) si une correspondance existe, rouge (Non trouvée) sinon.
- Détection et liste de tous les schémas enregistrés dans reference/, avec focus stylisé
  (couleur violet/indigo distincte) sur le schéma le plus convenable détecté.
- Possibilité pour chaque colonne de la structure de choisir parmi la liste des colonnes du nouveau fichier.
- Aperçu en direct des 3 premières valeurs injectées pour chaque colonne cible.
- Mémorisation persistante dans config/mapping_memory.json et exécution du pipeline.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
from typing import Any, Optional
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
import polars as pl

from src.aligner import align_dataframe
from src.cleaner import clean_and_quarantine, load_cleaning_rules
from src.comparator import (
    load_target_schema,
    load_target_schema_definitions,
)
from src.mapper import (
    DEFAULT_ALIASES_PATH,
    extract_sample_values,
    load_mapping_memory,
    load_standard_aliases,
    match_targets_to_incoming,
    resolve_column_from_memory,
    save_mapping_memory,
)
from src.models import CleaningRuleConfig, IGNORE_TOKEN, MappingAction
from src.qex_parser import export_qex_schema_to_excel, list_qex_chapters
from src.sniffer import read_file

app = FastAPI(
    title="Moteur d'Alignement & Nettoyage de Données",
    description="Interface Web Interactive pour le mapping de schéma orienté structure cible.",
    version="1.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Chemins de référence du projet
BASE_DIR = Path(__file__).resolve().parent.parent
REFERENCE_DIR = BASE_DIR / "reference"
DEFAULT_SCHEMA_NAME = "target_schema.csv"
MEMORY_PATH = BASE_DIR / "config" / "mapping_memory.json"
RULES_PATH = BASE_DIR / "config" / "cleaning_rules.yaml"
ALIASES_PATH = BASE_DIR / "config" / "standard_aliases.json"
QEX_DIR = BASE_DIR / "qex"
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
QUARANTINE_DIR = BASE_DIR / "quarantine"
ARCHIVE_DIR = BASE_DIR / "archive"

for d in (REFERENCE_DIR, QEX_DIR, INPUT_DIR, OUTPUT_DIR, QUARANTINE_DIR, ARCHIVE_DIR):
    d.mkdir(parents=True, exist_ok=True)


class SaveMappingRequest(BaseModel):
    mappings: dict[str, str]


class ProcessFileRequest(BaseModel):
    file_name: str
    schema_name: Optional[str] = DEFAULT_SCHEMA_NAME
    target_to_source: Optional[dict[str, Optional[str]]] = None
    mappings: Optional[dict[str, str]] = None
    save_to_memory: bool = True


def get_available_schemas() -> list[Path]:
    """Retourne la liste des fichiers de schéma enregistrés dans reference/ (.csv et .xlsx).

    Conserve target_schema.csv en première position par défaut.
    """
    all_files = [
        f for f in REFERENCE_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in (".csv", ".xlsx") and not f.name.startswith("~$")
    ]
    all_files.sort(key=lambda p: (0 if p.name == DEFAULT_SCHEMA_NAME else 1, p.name.lower()))
    return all_files


def evaluate_schema_match(
    incoming_columns: list[str],
    schema_path: Path,
    memory: dict[str, str],
    aliases: Optional[dict[str, list[str]]] = None,
) -> dict[str, Any]:
    """Évalue le taux de concordance d'un schéma cible avec les colonnes d'un fichier entrant

    en exploitant la mémoire de mapping, le dictionnaire de synonymes FR / EN et le fuzzy matching.
    """
    try:
        target_defs = load_target_schema_definitions(schema_path)
    except Exception:
        return {
            "file_name": schema_path.name,
            "columns_count": 0,
            "matched_count": 0,
            "score_percent": 0,
            "columns": [],
        }

    target_names = [col.name for col in target_defs]
    match_res = match_targets_to_incoming(
        target_columns=target_names,
        incoming_columns=incoming_columns,
        memory=memory,
        aliases=aliases,
    )

    matched_count = sum(1 for info in match_res.values() if info.get("found"))
    total = len(target_defs)
    score = int(round((matched_count / total * 100))) if total > 0 else 0

    return {
        "file_name": schema_path.name,
        "columns_count": total,
        "matched_count": matched_count,
        "score_percent": score,
        "columns": target_names,
    }


@app.get("/api/aliases")
def get_standard_aliases() -> dict[str, list[str]]:
    """Retourne le dictionnaire de synonymes et variantes bilingues FR / EN."""
    return load_standard_aliases(ALIASES_PATH)


@app.get("/api/schemas")
def list_schemas() -> list[dict[str, Any]]:
    """Retourne la liste de tous les schémas enregistrés dans reference/."""
    schemas_list: list[dict[str, Any]] = []
    for schema_file in get_available_schemas():
        try:
            target_defs = load_target_schema_definitions(schema_file)
            schemas_list.append(
                {
                    "file_name": schema_file.name,
                    "columns_count": len(target_defs),
                    "columns": [
                        {"name": col.name, "data_type": col.data_type, "description": col.description}
                        for col in target_defs
                    ],
                }
            )
        except Exception:
            continue
    return schemas_list


@app.get("/api/schema")
def get_target_schema(name: Optional[str] = DEFAULT_SCHEMA_NAME) -> dict[str, Any]:
    """Retourne la définition complète des colonnes d'un schéma cible donné."""
    schema_path = REFERENCE_DIR / (name or DEFAULT_SCHEMA_NAME)
    if not schema_path.exists():
        raise HTTPException(status_code=404, detail=f"Schéma '{name}' introuvable.")

    target_defs = load_target_schema_definitions(schema_path)
    rules = load_cleaning_rules(RULES_PATH)

    columns_data: list[dict[str, Any]] = []
    for col in target_defs:
        rule = rules.get(col.name)
        columns_data.append(
            {
                "name": col.name,
                "data_type": col.data_type,
                "description": col.description,
                "required": rule.required if rule else False,
                "phone_standardize": rule.phone_standardize if rule else False,
                "has_date_format": bool(rule.date_format) if rule else False,
                "case": rule.case if rule else None,
            }
        )

    return {"file_name": schema_path.name, "columns": columns_data}


@app.get("/api/memory")
def get_memory() -> dict[str, str]:
    return load_mapping_memory(MEMORY_PATH)


@app.get("/api/input-files")
def get_input_files() -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for f in sorted(INPUT_DIR.iterdir()):
        if f.is_file() and f.suffix.lower() in (".csv", ".tsv", ".txt", ".xlsx", ".xls"):
            stat = f.stat()
            files.append(
                {
                    "name": f.name,
                    "size_bytes": stat.st_size,
                    "modified_time": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
    return files


@app.get("/api/qex-files")
def get_qex_files() -> list[dict[str, Any]]:
    """Liste les fichiers Askia Design (.qex) présents dans le dossier qex/."""
    files: list[dict[str, Any]] = []
    for f in sorted(QEX_DIR.iterdir()):
        if f.is_file() and f.suffix.lower() == ".qex":
            stat = f.stat()
            expected_excel = REFERENCE_DIR / f"{f.stem}.xlsx"
            files.append(
                {
                    "name": f.name,
                    "size_bytes": stat.st_size,
                    "modified_time": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "has_schema": expected_excel.exists(),
                    "schema_name": expected_excel.name,
                }
            )
    return files


@app.get("/api/qex/chapters/{filename}")
def get_qex_chapters(filename: str) -> dict[str, Any]:
    """Liste les chapitres / libellés disponibles dans un fichier .qex du dossier qex/."""
    qex_path = QEX_DIR / filename
    if not qex_path.exists():
        raise HTTPException(status_code=404, detail=f"Fichier QEX '{filename}' introuvable dans qex/.")
    try:
        chapters = list_qex_chapters(qex_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erreur lors de la lecture du fichier QEX : {e}")

    return {
        "file_name": filename,
        "chapters": [
            {
                "shortcut": c.shortcut,
                "caption": c.caption,
                "level": c.level,
                "variable_count": c.variable_count,
            }
            for c in chapters
        ],
    }


@app.post("/api/qex/import")
async def import_qex_schema(
    file: Optional[UploadFile] = File(None),
    qex_filename: Optional[str] = Form(None),
    chapter_label: str = Form("Importation"),
) -> dict[str, Any]:
    """Importe un fichier Askia Design (.qex), extrait toutes les variables sous le libellé spécifié

    (par défaut 'Importation') et crée le fichier schéma Excel (.xlsx) correspondant dans reference/,
    tout en conservant target_schema.csv intact.
    """
    target_qex_path: Optional[Path] = None

    if file is not None and file.filename:
        if not file.filename.lower().endswith(".qex"):
            raise HTTPException(status_code=400, detail="Veuillez sélectionner un fichier au format .qex (Askia Design).")
        target_qex_path = QEX_DIR / Path(file.filename).name
        try:
            with open(target_qex_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Erreur sauvegarde QEX : {e}")
    elif qex_filename:
        target_qex_path = QEX_DIR / qex_filename
        if not target_qex_path.exists():
            raise HTTPException(status_code=404, detail=f"Fichier QEX '{qex_filename}' introuvable dans qex/.")
    else:
        raise HTTPException(status_code=400, detail="Aucun fichier .qex fourni ou sélectionné.")

    try:
        excel_path, parse_res = export_qex_schema_to_excel(
            qex_path=target_qex_path,
            reference_dir=REFERENCE_DIR,
            chapter_label=chapter_label or "Importation",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de l'extraction QEX : {e}")

    return {
        "status": "SUCCESS",
        "qex_file": target_qex_path.name,
        "schema_file": excel_path.name,
        "selected_chapter": parse_res.selected_chapter,
        "variables_count": len(parse_res.variables),
        "variables": [
            {
                "name": v.shortcut,
                "data_type": v.inferred_type,
                "description": v.caption,
                "askia_type": v.askia_type_label,
            }
            for v in parse_res.variables
        ],
        "chapters": [
            {
                "shortcut": c.shortcut,
                "caption": c.caption,
                "variable_count": c.variable_count,
            }
            for c in parse_res.chapters
        ],
    }


@app.post("/api/upload")
async def upload_and_inspect_file(
    file: UploadFile = File(...),
    schema_name: Optional[str] = Query(None),
) -> dict[str, Any]:
    """Reçoit un fichier client, l'enregistre dans input/ et l'analyse immédiatement."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Nom de fichier invalide.")

    dest_path = INPUT_DIR / file.filename
    try:
        with open(dest_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de la sauvegarde : {e}")

    return inspect_file_data(dest_path, selected_schema_name=schema_name)


@app.get("/api/inspect/{filename}")
def inspect_existing_file(
    filename: str,
    schema_name: Optional[str] = Query(None),
) -> dict[str, Any]:
    """Inspecte un fichier déjà présent dans input/ avec un schéma cible sélectionné ou automatique."""
    path = INPUT_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Fichier '{filename}' non trouvé dans input/.")
    return inspect_file_data(path, selected_schema_name=schema_name)


def inspect_file_data(
    file_path: Path,
    selected_schema_name: Optional[str] = None,
) -> dict[str, Any]:
    """Lit un fichier, évalue tous les schémas enregistrés dans reference/,

    détecte le meilleur schéma convenable, et prépare l'alignement avec indicateurs vert/rouge.
    """
    try:
        raw_df, metadata = read_file(file_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erreur d'ingestion : {e}")

    memory = load_mapping_memory(MEMORY_PATH)
    rules = load_cleaning_rules(RULES_PATH)
    aliases = load_standard_aliases(ALIASES_PATH)

    # 1. Échantillons des colonnes du nouveau fichier
    incoming_cols_map: dict[str, list[str]] = {}
    incoming_cols_info: list[dict[str, Any]] = []
    for col in raw_df.columns:
        samples = extract_sample_values(raw_df, col, limit=3)
        incoming_cols_map[col] = samples
        incoming_cols_info.append({"name": col, "samples": samples})

    # 2. Évaluation de tous les schémas disponibles dans reference/
    schema_files = get_available_schemas()
    schemas_eval: list[dict[str, Any]] = []
    best_schema_name: str = DEFAULT_SCHEMA_NAME
    highest_score: int = -1

    for s_path in schema_files:
        ev = evaluate_schema_match(raw_df.columns, s_path, memory, aliases)
        schemas_eval.append(ev)
        if ev["score_percent"] > highest_score:
            highest_score = ev["score_percent"]
            best_schema_name = s_path.name

    # Si aucun schéma spécifié explicitement, on retient le schéma convenable détecté
    active_schema_name = selected_schema_name or best_schema_name
    active_schema_path = REFERENCE_DIR / active_schema_name
    if not active_schema_path.exists():
        active_schema_path = REFERENCE_DIR / DEFAULT_SCHEMA_NAME
        active_schema_name = DEFAULT_SCHEMA_NAME

    # Marquer dans les évaluations le meilleur schéma et le schéma actif
    for ev in schemas_eval:
        ev["is_best_match"] = (ev["file_name"] == best_schema_name)
        ev["is_active"] = (ev["file_name"] == active_schema_name)

    # 3. Construction de l'alignement pour le schéma actif via le moteur de matching bilingue
    target_defs = load_target_schema_definitions(active_schema_path)
    target_names = [col.name for col in target_defs]
    match_res = match_targets_to_incoming(
        target_columns=target_names,
        incoming_columns=raw_df.columns,
        memory=memory,
        aliases=aliases,
    )

    target_alignment: list[dict[str, Any]] = []
    used_incoming: set[str] = set()

    for col in target_defs:
        rule = rules.get(col.name)
        tgt_name = col.name
        info = match_res.get(tgt_name, {})
        matched_source = info.get("source")
        match_type = info.get("type", "unmapped")
        confidence = info.get("score", 0.0)
        is_found = bool(matched_source)

        if matched_source:
            used_incoming.add(matched_source)

        target_alignment.append(
            {
                "target_name": tgt_name,
                "data_type": col.data_type,
                "description": col.description,
                "required": rule.required if rule else False,
                "matched_source": matched_source,
                "is_found": is_found,  # Indicateur Vert si True, Rouge si False
                "match_type": match_type,
                "confidence": confidence,
                "samples": incoming_cols_map.get(matched_source, []) if matched_source else [],
            }
        )

    unused_incoming = [col for col in raw_df.columns if col not in used_incoming]

    return {
        "file_name": file_path.name,
        "format": metadata.get("format"),
        "encoding": metadata.get("encoding"),
        "delimiter": metadata.get("delimiter"),
        "row_count": raw_df.height,
        "active_schema": active_schema_name,
        "best_schema": best_schema_name,
        "schemas_evaluation": schemas_eval,
        "target_alignment": target_alignment,
        "incoming_columns": incoming_cols_info,
        "unused_incoming": unused_incoming,
        "aliases_count": len(aliases),
    }


@app.post("/api/save-mapping")
def save_mappings(req: SaveMappingRequest) -> dict[str, Any]:
    memory = load_mapping_memory(MEMORY_PATH)
    memory.update(req.mappings)
    save_mapping_memory(MEMORY_PATH, memory)
    return {"status": "SUCCESS", "memory_count": len(memory)}


@app.post("/api/process")
def process_pipeline(req: ProcessFileRequest) -> dict[str, Any]:
    file_path = INPUT_DIR / req.file_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Fichier '{req.file_name}' introuvable.")

    raw_df, metadata = read_file(file_path)

    # Schéma utilisé
    schema_name = req.schema_name or DEFAULT_SCHEMA_NAME
    schema_path = REFERENCE_DIR / schema_name
    if not schema_path.exists():
        schema_path = REFERENCE_DIR / DEFAULT_SCHEMA_NAME
    target_defs = load_target_schema_definitions(schema_path)
    target_columns = [c.name for c in target_defs]

    # Alignement du DataFrame dans le schéma cible
    if req.target_to_source is not None:
        aligned_df = align_dataframe(
            df=raw_df,
            target_columns=target_columns,
            target_to_source=req.target_to_source,
        )

        # Mémorisation des correspondances {col_source: col_cible}
        if req.save_to_memory:
            memory = load_mapping_memory(MEMORY_PATH)
            # Priorité aux colonnes cibles prioritaires/obligatoires (ex: nom avant prenom)
            for tgt_col, src_col in req.target_to_source.items():
                if src_col and src_col in raw_df.columns and src_col.lower() != tgt_col.lower():
                    # Si la source est 'name', elle doit pointer vers 'nom'
                    if src_col.lower() == "name" and tgt_col != "nom" and "name" in memory and memory["name"] == "nom":
                        continue
                    memory[src_col] = tgt_col
            save_mapping_memory(MEMORY_PATH, memory)
    else:
        # Mode actions classique
        source_to_target = {k: v for k, v in (req.mappings or {}).items() if v != IGNORE_TOKEN}
        actions: dict[str, MappingAction] = {}
        for col in raw_df.columns:
            if col in source_to_target:
                tgt = source_to_target[col]
                actions[col] = MappingAction(source_col=col, target_col=tgt, is_ignored=False)
            else:
                actions[col] = MappingAction(source_col=col, target_col=None, is_ignored=True)

        matched_cols = [
            col for col in raw_df.columns
            if col in target_columns and source_to_target.get(col) == col
        ]

        aligned_df = align_dataframe(
            df=raw_df,
            target_columns=target_columns,
            matched_columns=matched_cols,
            mapping_actions=actions,
        )

    # Règles de nettoyage : règles YAML + règles dynamiques selon le type des colonnes du schéma (ex: TEL issu d'un QEX)
    try:
        rules = load_cleaning_rules(RULES_PATH)
        for col_def in target_defs:
            if col_def.name not in rules:
                if col_def.data_type == "phone":
                    rules[col_def.name] = CleaningRuleConfig(
                        trim=True,
                        phone_standardize=True,
                        unique=True,
                        error_message="Numéro de téléphone invalide",
                    )
                elif col_def.data_type == "email":
                    rules[col_def.name] = CleaningRuleConfig(
                        trim=True,
                        case="lower",
                        regex_match=r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$",
                        error_message="Format d'adresse email invalide",
                    )
                else:
                    rules[col_def.name] = CleaningRuleConfig(trim=True)

        cleaning_res = clean_and_quarantine(aligned_df, rules)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors du nettoyage : {e}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = file_path.stem

    clean_filename = f"{stem}_clean_{timestamp}.csv"
    clean_path = OUTPUT_DIR / clean_filename
    cleaning_res.clean_df.write_csv(clean_path)

    # Export également au format Excel (.xlsx) en préservant le texte (zéros initiaux SIRET/CP/TEL)
    clean_excel_filename = f"{stem}_clean_{timestamp}.xlsx"
    clean_excel_path = OUTPUT_DIR / clean_excel_filename
    _write_dataframe_to_excel_text(cleaning_res.clean_df, clean_excel_path)

    quar_filename: Optional[str] = None
    if cleaning_res.quarantine_rows > 0:
        quar_filename = f"{stem}_quarantine_{timestamp}.csv"
        quar_path = QUARANTINE_DIR / quar_filename
        cleaning_res.quarantine_df.write_csv(quar_path)

    archive_filename = f"{stem}_{timestamp}{file_path.suffix}"
    archive_path = ARCHIVE_DIR / archive_filename
    shutil.move(str(file_path), str(archive_path))

    clean_preview = cleaning_res.clean_df.head(20).to_dicts()
    quar_preview = (
        cleaning_res.quarantine_df.head(20).to_dicts()
        if cleaning_res.quarantine_rows > 0
        else []
    )

    return {
        "status": "SUCCESS",
        "total_rows": cleaning_res.total_rows,
        "clean_rows": cleaning_res.clean_rows,
        "quarantine_rows": cleaning_res.quarantine_rows,
        "clean_file": clean_filename,
        "clean_excel_file": clean_excel_filename,
        "quarantine_file": quar_filename,
        "archive_file": archive_filename,
        "clean_preview": clean_preview,
        "quarantine_preview": quar_preview,
        "rejection_motifs_summary": cleaning_res.rejection_motifs_summary,
    }


def _write_dataframe_to_excel_text(df: pl.DataFrame, dest_path: Path) -> None:
    """Écrit un DataFrame Polars en fichier Excel (.xlsx) en conservant chaque cellule en texte brut."""
    import openpyxl

    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet(title="Donnees")
    ws.append(list(df.columns))
    for row in df.iter_rows():
        ws.append(["" if val is None else str(val) for val in row])
    wb.save(dest_path)


@app.get("/api/download/output/{filename}")
def download_clean_file(filename: str) -> FileResponse:
    path = OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Fichier non trouvé.")
    media_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if path.suffix.lower() == ".xlsx"
        else "text/csv"
    )
    return FileResponse(path, filename=filename, media_type=media_type)


@app.get("/api/download/quarantine/{filename}")
def download_quarantine_file(filename: str) -> FileResponse:
    path = QUARANTINE_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Fichier non trouvé.")
    return FileResponse(path, filename=filename, media_type="text/csv")


@app.get("/", response_class=HTMLResponse)
def index_page() -> str:
    return HTML_CONTENT


HTML_CONTENT = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Alignement Structurel & Nettoyage de Données</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    .type-badge-string { background-color: #EBF5FF; color: #1E429F; border: 1px solid #C3DDFD; }
    .type-badge-email { background-color: #F3FAF7; color: #046C4E; border: 1px solid #BCF0DA; }
    .type-badge-phone { background-color: #FDF6B2; color: #723B13; border: 1px solid #FCE96A; }
    .type-badge-date { background-color: #F1F5F9; color: #475569; border: 1px solid #CBD5E1; }
    .type-badge-integer { background-color: #EDEBFE; color: #5521B5; border: 1px solid #DCD7FE; }
    .type-badge-float { background-color: #FDF2F8; color: #9D174D; border: 1px solid #FBCFE8; }
  </style>
</head>
<body class="bg-slate-50 text-slate-900 font-sans min-h-screen">

  <!-- Header -->
  <header class="bg-white border-b border-slate-200 sticky top-0 z-30 shadow-sm">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-3.5 flex items-center justify-between">
      <div class="flex items-center space-x-3">
        <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-blue-600 to-indigo-600 flex items-center justify-center text-white shadow-md">
          <i class="fa-solid fa-arrows-split-up-and-left text-lg"></i>
        </div>
        <div>
          <h1 class="text-lg font-bold text-slate-900 leading-tight">Moteur d'Alignement & Nettoyage</h1>
          <p class="text-xs text-slate-500">Alignement de structure • Reconnaissance de schémas • Quarantaine</p>
        </div>
      </div>
      <div class="flex items-center space-x-3">
        <button onclick="openAliasesModal()" class="px-3 py-1.5 rounded-xl border border-blue-200 bg-blue-50/80 hover:bg-blue-100 text-blue-800 text-xs font-semibold flex items-center gap-1.5 shadow-sm transition">
          <i class="fa-solid fa-book-bookmark text-blue-600"></i>
          <span>Synonymes FR / EN</span>
          <span id="aliases-count-pill" class="px-1.5 py-0.5 bg-blue-600 text-white rounded-full text-[10px] font-bold">--</span>
        </button>
        <span id="target-count-badge" class="inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold bg-blue-50 text-blue-700 border border-blue-200">
          <i class="fa-solid fa-layer-group mr-1.5"></i> <span id="target-count-val">Chargement...</span>
        </span>
        <button onclick="refreshAll()" class="p-2 text-slate-500 hover:text-slate-800 rounded-lg hover:bg-slate-100 transition" title="Actualiser">
          <i class="fa-solid fa-rotate"></i>
        </button>
      </div>
    </div>
  </header>

  <!-- Main Container -->
  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">

    <!-- Upload / Ingestion Bar -->
    <div class="bg-white rounded-2xl border border-slate-200 p-5 mb-6 shadow-sm">
      <div class="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h2 class="text-base font-semibold text-slate-800 flex items-center">
            <i class="fa-solid fa-cloud-arrow-up text-blue-600 mr-2"></i>
            Charger un Nouveau Fichier Client
          </h2>
          <p class="text-xs text-slate-500 mt-0.5">Glissez-déposez un fichier CSV ou Excel (.xlsx) pour identifier le schéma convenable et aligner les données.</p>
        </div>
        <div class="flex items-center gap-3">
          <label class="cursor-pointer bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-xl text-xs font-medium shadow-sm transition flex items-center gap-2">
            <i class="fa-solid fa-upload"></i>
            <span>Parcourir un fichier</span>
            <input type="file" id="file-input" accept=".csv,.xlsx,.xls,.tsv,.txt" class="hidden" onchange="handleFileSelect(event)">
          </label>
          <div class="relative">
            <select id="existing-files-select" onchange="handleSelectExisting(this.value)" class="text-xs border border-slate-300 rounded-xl px-3 py-2 bg-slate-50 text-slate-700 focus:outline-none focus:ring-2 focus:ring-blue-500">
              <option value="">-- Fichiers dans input/ --</option>
            </select>
          </div>
        </div>
      </div>

      <!-- File Metadata Strip -->
      <div id="file-meta-strip" class="hidden mt-4 pt-4 border-t border-slate-100 flex flex-wrap items-center justify-between text-xs text-slate-600 gap-3">
        <div class="flex items-center gap-2">
          <span class="font-bold text-slate-900" id="meta-filename">-</span>
          <span id="meta-format" class="px-2 py-0.5 rounded bg-slate-100 font-mono text-slate-700 uppercase">-</span>
        </div>
        <div class="flex items-center gap-4">
          <span>Encodage : <b id="meta-encoding" class="text-slate-800 font-mono">-</b></span>
          <span>Séparateur : <b id="meta-delim" class="text-slate-800 font-mono">-</b></span>
          <span>Lignes brutes : <b id="meta-rows" class="text-slate-800">-</b></span>
          <span>Colonnes fichier : <b id="meta-cols" class="text-slate-800">-</b></span>
        </div>
      </div>
    </div>

    <!-- SECTION SCHÉMAS ENREGISTRÉS (LISTE DÉROULANTE + AFFICHAGE SCHÉMA EN COURS) -->
    <div class="bg-white rounded-2xl border border-slate-200 p-4 mb-6 shadow-sm flex flex-col md:flex-row md:items-center justify-between gap-4">
      <!-- Affichage du schéma en cours -->
      <div class="flex items-center gap-3">
        <div class="w-9 h-9 rounded-xl bg-purple-100 text-purple-700 flex items-center justify-center shrink-0">
          <i class="fa-solid fa-database text-base"></i>
        </div>
        <div>
          <div class="flex items-center gap-2 flex-wrap">
            <span class="text-xs font-semibold text-slate-500">Schéma en cours :</span>
            <span id="current-schema-name" class="text-sm font-bold text-slate-900 font-mono">target_schema.csv</span>
            <span id="current-schema-cols-badge" class="px-2 py-0.5 rounded-full text-[11px] font-medium bg-slate-100 text-slate-600 border border-slate-200">-- colonnes</span>
            <span id="current-schema-match-badge" class="hidden inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-purple-600 text-white shadow-sm">
              <i class="fa-solid fa-star text-amber-300"></i> Schéma convenable
            </span>
          </div>
          <p class="text-[11px] text-slate-500 mt-0.5">Sélectionnez le modèle cible parmi les Schémas Enregistrés dans <code class="font-mono text-slate-600">reference/</code>.</p>
        </div>
      </div>

      <!-- Liste déroulante de sélection + Import QEX Askia -->
      <div class="flex items-center gap-2.5 flex-wrap shrink-0">
        <label for="schemas-select" class="text-xs font-semibold text-slate-600 whitespace-nowrap">Schémas Enregistrés :</label>
        <select id="schemas-select" onchange="switchSchema(this.value)" class="text-xs font-medium border border-slate-300 rounded-xl px-3 py-2 bg-slate-50 text-slate-800 focus:outline-none focus:ring-2 focus:ring-purple-500 shadow-xs min-w-[240px]">
          <option value="">Chargement...</option>
        </select>
        <button onclick="openQexModal()" class="px-3.5 py-2 text-xs font-semibold text-white bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-700 hover:to-indigo-700 rounded-xl shadow-sm transition flex items-center gap-1.5 whitespace-nowrap" title="Importer un fichier Askia Design (.qex) pour créer son schéma Excel dans reference/">
          <i class="fa-solid fa-file-import"></i>
          <span>Importer QEX Askia</span>
        </button>
      </div>
    </div>

    <!-- MAIN TABLE : STRUCTURE DU SCHÉMA ACTIF VS CORRESPONDANT DU NOUVEAU FICHIER -->
    <div class="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden mb-6">
      <div class="p-4 border-b border-slate-100 bg-slate-50/50 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <div class="flex items-center gap-2">
            <h2 class="text-sm font-bold text-slate-900 flex items-center gap-2">
              <i class="fa-solid fa-table-list text-blue-600"></i>
              Tableau d'Alignement : Colonne Cible ➔ Source du Nouveau Fichier
            </h2>
            <span id="active-schema-badge" class="px-2 py-0.5 rounded text-[11px] font-bold bg-purple-100 text-purple-800 border border-purple-300">
              target_schema.csv
            </span>
          </div>
          <p class="text-xs text-slate-500 mt-0.5">Vérifiez chaque colonne : indicateur vert si une source est trouvée, rouge si non trouvée.</p>
        </div>
        
        <!-- Compteurs rapides trouvées / non trouvées -->
        <div class="flex items-center gap-2">
          <span class="px-2.5 py-1 rounded-full text-xs font-bold bg-emerald-100 text-emerald-800 border border-emerald-300 flex items-center gap-1.5" id="stat-found-count">
            <i class="fa-solid fa-check"></i> 0 trouvée(s)
          </span>
          <span class="px-2.5 py-1 rounded-full text-xs font-bold bg-rose-100 text-rose-800 border border-rose-300 flex items-center gap-1.5" id="stat-not-found-count">
            <i class="fa-solid fa-xmark"></i> 0 non trouvée(s)
          </span>
        </div>
      </div>

      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs border-collapse">
          <thead>
            <tr class="bg-slate-50 text-slate-700 font-semibold border-b border-slate-200">
              <th class="py-3 px-4 w-1/5">Colonne Structure Cible</th>
              <th class="py-3 px-3 w-32 text-center">Indicateur</th>
              <th class="py-3 px-4 w-1/3">Colonne Source (Nouveau Fichier)</th>
              <th class="py-3 px-4 w-1/3">Aperçu Données Injectées (3 valeurs)</th>
            </tr>
          </thead>
          <tbody id="alignment-table-body" class="divide-y divide-slate-100">
            <tr>
              <td colspan="4" class="p-8 text-center text-slate-400">
                <i class="fa-regular fa-folder-open text-3xl mb-2 text-slate-300"></i>
                <p class="text-sm font-medium text-slate-600">Aucun fichier chargé</p>
                <p class="text-xs text-slate-400 mt-1">Uploadez un fichier client ci-dessus pour faire l'alignement avec votre structure.</p>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Footer Bar with Unused Columns & Actions -->
      <div class="p-4 bg-slate-50/70 border-t border-slate-100 flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div class="flex items-center gap-2 flex-wrap text-xs">
          <span class="text-slate-500 font-medium">Colonnes du fichier non utilisées (seront ignorées) :</span>
          <span id="unused-columns-pills" class="flex items-center gap-1.5 flex-wrap">
            <span class="text-slate-400 italic">Aucune</span>
          </span>
        </div>

        <div class="flex items-center gap-3">
          <button id="btn-save-memory" onclick="saveCurrentMappings()" disabled
                  class="px-4 py-2 text-xs font-medium text-slate-700 bg-white border border-slate-300 rounded-xl hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed transition flex items-center gap-1.5 shadow-sm">
            <i class="fa-solid fa-bookmark text-slate-500"></i>
            <span>Mémoriser ces correspondances</span>
          </button>
          <button id="btn-run-pipeline" onclick="executePipeline()" disabled
                  class="px-5 py-2 text-xs font-semibold text-white bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 rounded-xl disabled:opacity-40 disabled:cursor-not-allowed transition flex items-center gap-2 shadow-sm">
            <i class="fa-solid fa-bolt"></i>
            <span>Aligner & Nettoyer les données</span>
          </button>
        </div>
      </div>
    </div>

    <!-- RESULTS SECTION (Shown after execution) -->
    <div id="results-section" class="hidden mt-8 bg-white rounded-2xl border border-slate-200 shadow-sm p-6">
      <div class="flex flex-col md:flex-row md:items-center justify-between gap-4 pb-6 border-b border-slate-100">
        <div>
          <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-emerald-100 text-emerald-800 mb-1">
            <i class="fa-solid fa-circle-check mr-1"></i> Traitement Terminé
          </span>
          <h2 class="text-lg font-bold text-slate-900">Résultat de l'Alignement & du Nettoyage</h2>
          <p class="text-xs text-slate-500">Données projetées dans la structure cible et filtrées selon les règles de nettoyage.</p>
        </div>
        <div class="flex items-center gap-2.5 flex-wrap">
          <a id="download-clean-btn" href="#" class="px-4 py-2 text-xs font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-xl hover:bg-emerald-100 transition flex items-center gap-2 shadow-sm">
            <i class="fa-solid fa-download"></i> Télécharger CSV
          </a>
          <a id="download-clean-xlsx-btn" href="#" class="hidden px-4 py-2 text-xs font-semibold text-blue-700 bg-blue-50 border border-blue-200 rounded-xl hover:bg-blue-100 transition flex items-center gap-2 shadow-sm">
            <i class="fa-solid fa-file-excel"></i> Télécharger Excel (.xlsx)
          </a>
          <a id="download-quar-btn" href="#" class="hidden px-4 py-2 text-xs font-semibold text-amber-700 bg-amber-50 border border-amber-200 rounded-xl hover:bg-amber-100 transition flex items-center gap-2 shadow-sm">
            <i class="fa-solid fa-file-shield"></i> Télécharger Quarantaine
          </a>
        </div>
      </div>

      <!-- KPIs -->
      <div class="grid grid-cols-1 sm:grid-cols-3 gap-4 my-6">
        <div class="bg-slate-50 rounded-xl p-4 border border-slate-100">
          <span class="text-xs text-slate-500 font-medium">Lignes Ingestion Brute</span>
          <p id="kpi-total" class="text-2xl font-bold text-slate-900 mt-1">0</p>
        </div>
        <div class="bg-emerald-50/50 rounded-xl p-4 border border-emerald-100">
          <span class="text-xs text-emerald-700 font-medium">Lignes Saines Validées (Output)</span>
          <p id="kpi-clean" class="text-2xl font-bold text-emerald-700 mt-1">0</p>
        </div>
        <div class="bg-amber-50/50 rounded-xl p-4 border border-amber-100">
          <span class="text-xs text-amber-700 font-medium">Lignes en Quarantaine (Rejets)</span>
          <p id="kpi-quarantine" class="text-2xl font-bold text-amber-700 mt-1">0</p>
        </div>
      </div>

      <!-- Warning Alert if 0 clean rows -->
      <div id="zero-clean-alert" class="hidden mb-6 p-4 rounded-xl bg-amber-50 border border-amber-300 text-amber-900 text-xs">
        <div class="flex items-start gap-2.5">
          <i class="fa-solid fa-triangle-exclamation text-amber-600 text-base mt-0.5"></i>
          <div>
            <p class="font-bold text-sm">Attention : 0 ligne propre validée (100% des lignes rejetées en quarantaine)</p>
            <p class="mt-0.5">Toutes les lignes du fichier ont été rejetées car elles ne respectaient pas au moins une règle de validation. Consultez l'onglet <b>Aperçu Lignes Rejetées</b> ci-dessous pour voir le détail des motifs.</p>
          </div>
        </div>
      </div>

      <!-- Breakdown of Rejection Motifs -->
      <div id="motifs-summary-card" class="hidden mb-6 p-4 rounded-xl bg-slate-50 border border-slate-200 text-xs">
        <span class="font-bold text-slate-800 flex items-center gap-1.5 mb-2">
          <i class="fa-solid fa-list-check text-slate-500"></i> Synthèse des Motifs de Rejet :
        </span>
        <ul id="motifs-summary-list" class="space-y-1 font-mono text-[11px] text-slate-700">
        </ul>
      </div>

      <!-- Tabs: Clean Data Preview / Quarantine Data Preview -->
      <div class="border-b border-slate-200 flex space-x-6 text-xs font-medium">
        <button id="tab-clean-btn" onclick="switchResultsTab('clean')" class="py-2.5 border-b-2 border-blue-600 text-blue-600 font-bold flex items-center gap-1.5">
          <i class="fa-solid fa-check"></i> Aperçu Données Conformes
        </button>
        <button id="tab-quar-btn" onclick="switchResultsTab('quar')" class="py-2.5 border-b-2 border-transparent text-slate-500 hover:text-slate-800 flex items-center gap-1.5">
          <i class="fa-solid fa-triangle-exclamation"></i> Aperçu Lignes Rejetées
        </button>
      </div>

      <div class="mt-4">
        <!-- Clean Preview Table -->
        <div id="tab-clean-content" class="overflow-x-auto max-h-96 border border-slate-200 rounded-xl">
          <table class="w-full text-left text-xs border-collapse">
            <thead id="clean-table-head" class="bg-slate-50 text-slate-700 font-semibold sticky top-0 border-b border-slate-200">
            </thead>
            <tbody id="clean-table-body" class="divide-y divide-slate-100 font-mono text-[11px]">
            </tbody>
          </table>
        </div>

        <!-- Quarantine Preview Table -->
        <div id="tab-quar-content" class="hidden overflow-x-auto max-h-96 border border-slate-200 rounded-xl">
          <table class="w-full text-left text-xs border-collapse">
            <thead id="quar-table-head" class="bg-amber-50/50 text-amber-900 font-semibold sticky top-0 border-b border-amber-200">
            </thead>
            <tbody id="quar-table-body" class="divide-y divide-slate-100 font-mono text-[11px]">
            </tbody>
          </table>
        </div>
      </div>
    </div>

  </main>

  <!-- MODAL DICTIONNAIRE DE SYNONYMES BILINGUES FR / EN -->
  <div id="aliases-modal" class="fixed inset-0 bg-slate-900/60 backdrop-blur-sm z-50 hidden flex items-center justify-center p-4">
    <div class="bg-white rounded-2xl shadow-2xl max-w-3xl w-full max-h-[85vh] flex flex-col overflow-hidden border border-slate-200">
      <div class="p-5 border-b border-slate-100 flex items-center justify-between bg-slate-50/70">
        <div class="flex items-center gap-2.5">
          <div class="w-9 h-9 rounded-xl bg-blue-600 text-white flex items-center justify-center shadow">
            <i class="fa-solid fa-book-bookmark text-base"></i>
          </div>
          <div>
            <h3 class="font-bold text-slate-900 text-sm">Dictionnaire de Synonymes Standard (FR / EN)</h3>
            <p class="text-xs text-slate-500">Reconnaissance automatique des colonnes usuelles (noms, téléphones, villes, dates...)</p>
          </div>
        </div>
        <button onclick="closeAliasesModal()" class="text-slate-400 hover:text-slate-700 p-1.5 rounded-lg hover:bg-slate-200/50 transition">
          <i class="fa-solid fa-xmark text-lg"></i>
        </button>
      </div>
      
      <div class="p-4 border-b border-slate-100 bg-white flex items-center gap-3">
        <div class="relative flex-1">
          <i class="fa-solid fa-magnifying-glass absolute left-3 top-2.5 text-slate-400 text-xs"></i>
          <input type="text" id="aliases-search-input" onkeyup="filterAliasesModal(this.value)" placeholder="Filtrer par colonne cible ou synonyme (ex: tel, city, zip, nom)..." class="w-full text-xs pl-8 pr-3 py-2 border border-slate-200 rounded-xl bg-slate-50 focus:outline-none focus:ring-2 focus:ring-blue-500">
        </div>
        <span class="text-[11px] text-slate-400 font-mono bg-slate-100 px-2 py-1 rounded">config/standard_aliases.json</span>
      </div>

      <div id="aliases-list-container" class="p-5 overflow-y-auto space-y-4 text-xs max-h-[55vh]">
        <!-- Rempli dynamiquement -->
      </div>

      <div class="p-4 bg-slate-50/90 border-t border-slate-100 flex flex-col sm:flex-row items-center justify-between gap-3 text-xs text-slate-500">
        <span class="flex items-center gap-1.5">
          <i class="fa-regular fa-lightbulb text-amber-500"></i>
          <span>Pour ajouter vos propres variantes, enrichissez directement <b>config/standard_aliases.json</b>.</span>
        </span>
        <button onclick="closeAliasesModal()" class="px-4 py-1.5 bg-slate-800 hover:bg-slate-900 text-white rounded-xl text-xs font-semibold shadow-sm transition">
          Fermer
        </button>
      </div>
    </div>
  </div>

  <!-- MODAL IMPORTATION QEX ASKIA DESIGN -->
  <div id="qex-modal" class="fixed inset-0 bg-slate-900/60 backdrop-blur-sm z-50 hidden flex items-center justify-center p-4">
    <div class="bg-white rounded-2xl shadow-2xl max-w-2xl w-full max-h-[88vh] flex flex-col overflow-hidden border border-slate-200">
      <div class="p-5 border-b border-slate-100 flex items-center justify-between bg-gradient-to-r from-purple-50 to-indigo-50">
        <div class="flex items-center gap-3">
          <div class="w-9 h-9 rounded-xl bg-gradient-to-tr from-purple-600 to-indigo-600 text-white flex items-center justify-center shadow">
            <i class="fa-solid fa-file-import text-base"></i>
          </div>
          <div>
            <h3 class="font-bold text-slate-900 text-sm">Importer un Schéma depuis Askia Design (.qex)</h3>
            <p class="text-xs text-slate-500">Extrait toutes les variables sous le libellé <b>Importation</b> et crée le schéma Excel dans <code class="font-mono">reference/</code>.</p>
          </div>
        </div>
        <button onclick="closeQexModal()" class="text-slate-400 hover:text-slate-700 p-1.5 rounded-lg hover:bg-slate-200/50 transition">
          <i class="fa-solid fa-xmark text-lg"></i>
        </button>
      </div>

      <div class="p-5 overflow-y-auto space-y-4 text-xs">
        <!-- Choix 1 : Fichier déjà présent dans qex/ -->
        <div>
          <label class="block font-semibold text-slate-700 mb-1.5">
            <i class="fa-solid fa-folder-open text-purple-600 mr-1"></i>
            1. Sélectionner un fichier présent dans le dossier <code class="font-mono text-slate-600">qex/</code>
          </label>
          <select id="qex-existing-select" onchange="handleQexExistingChange(this.value)" class="w-full text-xs border border-slate-300 rounded-xl px-3 py-2 bg-slate-50 text-slate-800 focus:outline-none focus:ring-2 focus:ring-purple-500">
            <option value="">-- Choisir un fichier .qex dans qex/ --</option>
          </select>
        </div>

        <div class="flex items-center gap-3">
          <div class="flex-1 h-px bg-slate-200"></div>
          <span class="text-[11px] font-semibold text-slate-400 uppercase">OU</span>
          <div class="flex-1 h-px bg-slate-200"></div>
        </div>

        <!-- Choix 2 : Upload d'un nouveau fichier .qex -->
        <div>
          <label class="block font-semibold text-slate-700 mb-1.5">
            <i class="fa-solid fa-cloud-arrow-up text-purple-600 mr-1"></i>
            2. Importer un nouveau fichier <code class="font-mono text-slate-600">.qex</code> depuis votre ordinateur
          </label>
          <input type="file" id="qex-file-input" accept=".qex" onchange="handleQexFileInputChange()" class="w-full text-xs border border-slate-300 rounded-xl p-2 bg-slate-50 file:mr-3 file:py-1 file:px-3 file:rounded-lg file:border-0 file:text-xs file:font-semibold file:bg-purple-100 file:text-purple-800 hover:file:bg-purple-200 cursor-pointer">
        </div>

        <!-- Libellé / Chapitre cible -->
        <div class="p-3.5 rounded-xl bg-slate-50 border border-slate-200 space-y-2">
          <label for="qex-chapter-input" class="block font-semibold text-slate-700">
            <i class="fa-solid fa-tag text-purple-600 mr-1"></i>
            Libellé / Chapitre à extraire dans le QEX
          </label>
          <input type="text" id="qex-chapter-input" value="Importation" placeholder="Ex: Importation" class="w-full text-xs border border-slate-300 rounded-xl px-3 py-2 bg-white text-slate-800 font-medium focus:outline-none focus:ring-2 focus:ring-purple-500">
          <div id="qex-chapters-preview" class="hidden pt-1">
            <span class="text-[11px] text-slate-500 block mb-1">Chapitres détectés dans ce QEX (cliquez pour choisir) :</span>
            <div id="qex-chapters-pills" class="flex flex-wrap gap-1.5"></div>
          </div>
        </div>

        <!-- Zone d'erreur ou de succès -->
        <div id="qex-error-box" class="hidden p-3 rounded-xl bg-rose-50 border border-rose-200 text-rose-800 text-xs"></div>

        <div id="qex-result-box" class="hidden p-4 rounded-xl bg-emerald-50 border border-emerald-200 text-emerald-900 space-y-2">
          <div class="flex items-center justify-between">
            <span class="font-bold flex items-center gap-1.5">
              <i class="fa-solid fa-circle-check text-emerald-600"></i>
              <span id="qex-result-title">Schéma Excel généré !</span>
            </span>
            <span id="qex-result-badge" class="px-2 py-0.5 rounded-full text-[11px] font-bold bg-emerald-600 text-white">0 variables</span>
          </div>
          <p class="text-[11px] text-emerald-800">Fichier créé : <b id="qex-result-file" class="font-mono">-</b> (le schéma <code class="font-mono">target_schema.csv</code> reste intact par défaut).</p>
          <div id="qex-result-vars" class="flex flex-wrap gap-1 pt-1 max-h-36 overflow-y-auto"></div>
        </div>
      </div>

      <div class="p-4 bg-slate-50/90 border-t border-slate-100 flex items-center justify-between gap-3 text-xs">
        <button onclick="closeQexModal()" class="px-4 py-2 bg-white border border-slate-300 hover:bg-slate-100 text-slate-700 rounded-xl font-semibold transition">
          Fermer
        </button>
        <button id="btn-import-qex" onclick="importQexSchema()" class="px-5 py-2 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-700 hover:to-indigo-700 text-white rounded-xl font-semibold shadow-sm transition flex items-center gap-2">
          <i class="fa-solid fa-wand-magic-sparkles"></i>
          <span>Extraire & Créer le schéma Excel</span>
        </button>
      </div>
    </div>
  </div>

  <!-- Toast Notification -->
  <div id="toast" class="fixed bottom-6 right-6 hidden bg-slate-900 text-white text-xs px-4 py-3 rounded-xl shadow-lg flex items-center gap-2 z-50">
    <i id="toast-icon" class="fa-solid fa-circle-check text-emerald-400"></i>
    <span id="toast-msg">Opération réussie</span>
  </div>

  <script>
    let registeredSchemas = [];
    let currentFileInfo = null;
    let currentTargetMappings = {}; // { target_name: incoming_source_name or null }
    let incomingColumnsList = []; // [ { name, samples } ]
    let currentActiveSchema = "target_schema.csv";
    let standardAliases = {}; // { target_name: [alias1, alias2, ...] }

    document.addEventListener("DOMContentLoaded", () => {
      loadRegisteredSchemas();
      loadInputFiles();
      loadStandardAliases();
    });

    async function loadStandardAliases() {
      try {
        const res = await fetch("/api/aliases");
        standardAliases = await res.json();
        const count = Object.keys(standardAliases).length;
        const pill = document.getElementById("aliases-count-pill");
        if (pill) pill.textContent = count;
      } catch (e) {
        console.error("Erreur chargement synonymes:", e);
      }
    }

    function openAliasesModal() {
      renderAliasesList("");
      document.getElementById("aliases-modal").classList.remove("hidden");
    }

    function closeAliasesModal() {
      document.getElementById("aliases-modal").classList.add("hidden");
    }

    // --- MODULE QEX ASKIA DESIGN ---
    async function openQexModal() {
      document.getElementById("qex-error-box").classList.add("hidden");
      document.getElementById("qex-result-box").classList.add("hidden");
      document.getElementById("qex-chapters-preview").classList.add("hidden");
      document.getElementById("qex-file-input").value = "";
      document.getElementById("qex-chapter-input").value = "Importation";
      document.getElementById("qex-modal").classList.remove("hidden");
      await loadQexFiles();
    }

    function closeQexModal() {
      document.getElementById("qex-modal").classList.add("hidden");
    }

    async function loadQexFiles() {
      try {
        const res = await fetch("/api/qex-files");
        const files = await res.json();
        const select = document.getElementById("qex-existing-select");
        select.innerHTML = '<option value="">-- Choisir un fichier .qex dans qex/ --</option>';
        files.forEach(f => {
          const opt = document.createElement("option");
          opt.value = f.name;
          const statusLabel = f.has_schema ? " [Schéma Excel existant]" : "";
          opt.textContent = `${f.name} (${Math.round(f.size_bytes / 1024)} KB)${statusLabel}`;
          select.appendChild(opt);
        });
      } catch (e) {
        console.error("Erreur chargement fichiers QEX:", e);
      }
    }

    async function handleQexExistingChange(filename) {
      document.getElementById("qex-error-box").classList.add("hidden");
      document.getElementById("qex-result-box").classList.add("hidden");
      const previewBox = document.getElementById("qex-chapters-preview");
      const pillsBox = document.getElementById("qex-chapters-pills");

      if (!filename) {
        previewBox.classList.add("hidden");
        return;
      }

      // Réinitialiser l'upload si l'utilisateur sélectionne un fichier du dossier qex/
      document.getElementById("qex-file-input").value = "";

      try {
        const res = await fetch(`/api/qex/chapters/${encodeURIComponent(filename)}`);
        if (!res.ok) return;
        const data = await res.json();
        if (data.chapters && data.chapters.length > 0) {
          previewBox.classList.remove("hidden");
          pillsBox.innerHTML = "";
          let hasImportation = false;
          data.chapters.forEach(ch => {
            if (ch.variable_count <= 0) return;
            const isImp = ch.shortcut.toLowerCase().includes("importation") || ch.caption.toLowerCase().includes("importation");
            if (isImp) hasImportation = true;
            const btn = document.createElement("button");
            btn.type = "button";
            btn.onclick = () => {
              document.getElementById("qex-chapter-input").value = ch.shortcut;
            };
            btn.className = isImp
              ? "px-2 py-1 rounded-lg text-[11px] font-bold bg-purple-600 text-white hover:bg-purple-700 transition"
              : "px-2 py-1 rounded-lg text-[11px] font-medium bg-white border border-slate-300 text-slate-700 hover:bg-slate-100 transition";
            btn.textContent = `${ch.shortcut} (${ch.variable_count} var.)`;
            pillsBox.appendChild(btn);
          });
          if (hasImportation) {
            document.getElementById("qex-chapter-input").value = "Importation";
          }
        }
      } catch (e) {
        console.error("Erreur lecture chapitres QEX:", e);
      }
    }

    function handleQexFileInputChange() {
      const fileInput = document.getElementById("qex-file-input");
      if (fileInput.files && fileInput.files.length > 0) {
        document.getElementById("qex-existing-select").value = "";
        document.getElementById("qex-chapters-preview").classList.add("hidden");
      }
    }

    async function importQexSchema() {
      const errBox = document.getElementById("qex-error-box");
      const resBox = document.getElementById("qex-result-box");
      errBox.classList.add("hidden");
      resBox.classList.add("hidden");

      const fileInput = document.getElementById("qex-file-input");
      const existingSelect = document.getElementById("qex-existing-select");
      const chapterLabel = (document.getElementById("qex-chapter-input").value || "Importation").trim();

      const hasUpload = fileInput.files && fileInput.files.length > 0;
      const existingName = existingSelect.value;

      if (!hasUpload && !existingName) {
        errBox.textContent = "Veuillez sélectionner un fichier dans qex/ ou importer un fichier .qex.";
        errBox.classList.remove("hidden");
        return;
      }

      const btn = document.getElementById("btn-import-qex");
      btn.disabled = true;
      btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Extraction en cours...';

      const formData = new FormData();
      if (hasUpload) {
        formData.append("file", fileInput.files[0]);
      } else {
        formData.append("qex_filename", existingName);
      }
      formData.append("chapter_label", chapterLabel);

      try {
        const res = await fetch("/api/qex/import", {
          method: "POST",
          body: formData
        });
        const data = await res.json();

        if (!res.ok) {
          errBox.textContent = data.detail || "Erreur lors de l'importation du QEX.";
          errBox.classList.remove("hidden");
          return;
        }

        // Affichage du résumé de génération
        document.getElementById("qex-result-title").textContent = `Schéma créé depuis "${data.selected_chapter}" !`;
        document.getElementById("qex-result-badge").textContent = `${data.variables_count} variables`;
        document.getElementById("qex-result-file").textContent = `reference/${data.schema_file}`;
        document.getElementById("qex-result-vars").innerHTML = data.variables.map(v => `
          <span class="px-2 py-0.5 rounded text-[10px] font-mono bg-white border border-emerald-200 text-emerald-900">
            ${v.name} <span class="text-slate-400">(${v.data_type})</span>
          </span>
        `).join("");
        resBox.classList.remove("hidden");

        // Rechargement de la liste déroulante des schémas et activation immédiate du nouveau schéma Excel
        currentActiveSchema = data.schema_file;
        await loadRegisteredSchemas();
        await loadQexFiles();

        if (currentFileInfo && currentFileInfo.file_name) {
          await inspectExistingFile(currentFileInfo.file_name, data.schema_file);
        } else {
          document.getElementById("active-schema-badge").textContent = data.schema_file;
        }

        showToast(`✅ Schéma Excel '${data.schema_file}' créé (${data.variables_count} variables) !`);
      } catch (e) {
        errBox.textContent = "Erreur réseau : " + e.message;
        errBox.classList.remove("hidden");
      } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles"></i> <span>Extraire & Créer le schéma Excel</span>';
      }
    }

    function filterAliasesModal(query) {
      renderAliasesList(query);
    }

    function renderAliasesList(query) {
      const container = document.getElementById("aliases-list-container");
      container.innerHTML = "";
      const q = (query || "").trim().toLowerCase();

      const entries = Object.entries(standardAliases);
      let matches = 0;

      entries.forEach(([col, aliases]) => {
        const colMatch = col.toLowerCase().includes(q);
        const matchedAliases = aliases.filter(a => a.toLowerCase().includes(q));

        if (!q || colMatch || matchedAliases.length > 0) {
          matches++;
          const card = document.createElement("div");
          card.className = "p-3.5 bg-slate-50/70 rounded-xl border border-slate-200";

          const aliasesPills = aliases.map(a => {
            const isHighlighted = q && a.toLowerCase().includes(q);
            const style = isHighlighted
              ? "bg-amber-100 text-amber-900 border-amber-300 font-bold"
              : "bg-white text-slate-700 border-slate-200";
            return `<span class="inline-block px-2 py-0.5 rounded text-[11px] font-mono border ${style} mr-1 mb-1">${a}</span>`;
          }).join("");

          card.innerHTML = `
            <div class="flex items-center justify-between mb-2">
              <span class="font-bold text-xs font-mono bg-blue-100 text-blue-900 px-2 py-0.5 rounded border border-blue-200">
                ${col}
              </span>
              <span class="text-[11px] text-slate-400 font-medium">${aliases.length} variantes</span>
            </div>
            <div class="flex flex-wrap items-center">
              ${aliasesPills}
            </div>
          `;
          container.appendChild(card);
        }
      });

      if (matches === 0) {
        container.innerHTML = '<p class="text-center text-slate-400 py-6">Aucun synonyme correspondant à votre recherche.</p>';
      }
    }

    async function refreshAll() {
      await loadRegisteredSchemas();
      await loadInputFiles();
      if (currentFileInfo && currentFileInfo.file_name) {
        inspectExistingFile(currentFileInfo.file_name, currentActiveSchema);
      }
      showToast("Données actualisées");
    }

    async function loadRegisteredSchemas() {
      try {
        const res = await fetch("/api/schemas");
        registeredSchemas = await res.json();
        document.getElementById("target-count-val").textContent = `${registeredSchemas.length} schémas enregistrés`;
        renderSchemasSelector(registeredSchemas, null);
      } catch (e) {
        console.error("Erreur chargement schémas:", e);
      }
    }

    function renderSchemasSelector(schemasList, activeBestMatch) {
      const select = document.getElementById("schemas-select");
      if (!select) return;
      select.innerHTML = "";

      let activeSchemaObj = null;

      schemasList.forEach(sch => {
        const fileName = sch.file_name;
        const isActive = (fileName === currentActiveSchema);
        if (isActive) activeSchemaObj = sch;

        const isBestMatch = (activeBestMatch && activeBestMatch.best_schema === fileName);
        const score = (activeBestMatch && activeBestMatch.scores && activeBestMatch.scores[fileName] !== undefined)
          ? activeBestMatch.scores[fileName]
          : null;

        const opt = document.createElement("option");
        opt.value = fileName;
        opt.selected = isActive;

        if (isBestMatch && score !== null) {
          opt.textContent = `⭐ ${fileName} (${score}% - Recommandé)`;
        } else if (score !== null) {
          opt.textContent = `${fileName} (${score}% concordance)`;
        } else {
          opt.textContent = `${fileName} (${sch.columns_count} colonnes)`;
        }

        select.appendChild(opt);
      });

      // Mise à jour de l'affichage du schéma en cours
      const currentNameEl = document.getElementById("current-schema-name");
      const currentColsEl = document.getElementById("current-schema-cols-badge");
      const matchBadgeEl = document.getElementById("current-schema-match-badge");

      if (currentNameEl) currentNameEl.textContent = currentActiveSchema;
      if (currentColsEl) {
        const colsCount = activeSchemaObj ? activeSchemaObj.columns_count : "--";
        currentColsEl.textContent = `${colsCount} colonnes`;
      }

      if (matchBadgeEl) {
        if (activeBestMatch && activeBestMatch.best_schema) {
          const activeScore = activeBestMatch.scores ? activeBestMatch.scores[currentActiveSchema] : null;
          matchBadgeEl.classList.remove("hidden");
          if (currentActiveSchema === activeBestMatch.best_schema) {
            matchBadgeEl.className = "inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-purple-600 text-white shadow-sm";
            matchBadgeEl.innerHTML = `<i class="fa-solid fa-star text-amber-300"></i> Schéma convenable (${activeScore !== null ? activeScore + '%' : 'optimal'})`;
          } else {
            matchBadgeEl.className = "inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-amber-100 text-amber-800 border border-amber-300";
            matchBadgeEl.innerHTML = `${activeScore !== null ? activeScore + '% concordance' : ''} • Recommandé : ${activeBestMatch.best_schema}`;
          }
        } else {
          matchBadgeEl.classList.add("hidden");
        }
      }
    }

    function switchSchema(schemaName) {
      if (!schemaName) return;
      currentActiveSchema = schemaName;
      document.getElementById("active-schema-badge").textContent = schemaName;
      if (currentFileInfo && currentFileInfo.file_name) {
        inspectExistingFile(currentFileInfo.file_name, schemaName);
      } else {
        // Simple recharge du schéma cible
        fetch(`/api/schema?name=${encodeURIComponent(schemaName)}`)
          .then(r => r.json())
          .then(() => {
            renderSchemasSelector(registeredSchemas, null);
          });
      }
      showToast(`Schéma en cours : ${schemaName}`);
    }

    async function loadInputFiles() {
      try {
        const res = await fetch("/api/input-files");
        const files = await res.json();
        const select = document.getElementById("existing-files-select");
        select.innerHTML = '<option value="">-- Fichiers dans input/ --</option>';
        files.forEach(f => {
          const opt = document.createElement("option");
          opt.value = f.name;
          opt.textContent = `${f.name} (${Math.round(f.size_bytes / 1024)} KB)`;
          select.appendChild(opt);
        });
      } catch (e) {
        console.error("Erreur chargement input files:", e);
      }
    }

    async function handleFileSelect(e) {
      const file = e.target.files[0];
      if (!file) return;

      const formData = new FormData();
      formData.append("file", file);

      try {
        const res = await fetch("/api/upload", {
          method: "POST",
          body: formData
        });
        if (!res.ok) {
          const err = await res.json();
          alert("Erreur upload: " + err.detail);
          return;
        }
        const data = await res.json();
        renderAlignmentInterface(data);
        showToast("Fichier analysé avec succès !");
        loadInputFiles();
      } catch (err) {
        alert("Erreur upload: " + err.message);
      }
    }

    async function handleSelectExisting(filename) {
      if (!filename) return;
      inspectExistingFile(filename, null);
    }

    async function inspectExistingFile(filename, schemaName) {
      try {
        const query = schemaName ? `?schema_name=${encodeURIComponent(schemaName)}` : "";
        const url = `/api/inspect/${encodeURIComponent(filename)}${query}`;
        const res = await fetch(url);
        const data = await res.json();
        renderAlignmentInterface(data);
        showToast(`Fichier ${filename} chargé`);
      } catch (e) {
        alert("Erreur inspection fichier: " + e.message);
      }
    }

    function renderAlignmentInterface(info) {
      currentFileInfo = info;
      incomingColumnsList = info.incoming_columns || [];
      currentTargetMappings = {};
      currentActiveSchema = info.active_schema || "target_schema.csv";
      document.getElementById("active-schema-badge").textContent = currentActiveSchema;

      // File Meta Strip
      document.getElementById("file-meta-strip").classList.remove("hidden");
      document.getElementById("meta-filename").textContent = info.file_name;
      document.getElementById("meta-format").textContent = info.format || "CSV";
      document.getElementById("meta-encoding").textContent = info.encoding || "UTF-8";
      document.getElementById("meta-delim").textContent = reprDelim(info.delimiter);
      document.getElementById("meta-rows").textContent = info.row_count;
      document.getElementById("meta-cols").textContent = incomingColumnsList.length;

      // Mise à jour de la liste déroulante des schémas enregistrés et du schéma en cours
      const scoresMap = {};
      if (info.schemas_evaluation) {
        info.schemas_evaluation.forEach(ev => {
          scoresMap[ev.file_name] = ev.score_percent;
        });
      }
      renderSchemasSelector(registeredSchemas, {
        best_schema: info.best_schema,
        scores: scoresMap
      });

      const tbody = document.getElementById("alignment-table-body");
      tbody.innerHTML = "";

      let foundCount = 0;
      let notFoundCount = 0;

      info.target_alignment.forEach(tgt => {
        const tgtName = tgt.target_name;
        const selectedSource = tgt.matched_source || "";
        currentTargetMappings[tgtName] = selectedSource || null;

        const isFound = Boolean(selectedSource);
        if (isFound) foundCount++;
        else notFoundCount++;

        const tr = document.createElement("tr");
        tr.id = `row-target-${tgtName}`;
        tr.className = "hover:bg-slate-50/70 transition border-b border-slate-100";

        // 1. Colonne de ma structure
        const typeClass = `type-badge-${tgt.data_type}` || "type-badge-string";
        const reqBadge = tgt.required 
          ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-rose-50 text-rose-700 font-bold border border-rose-200">Obligatoire</span>'
          : '<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 font-normal">Optionnel</span>';

        // 2. INDICATEUR VERT (Trouvée) / ROUGE (Non trouvée)
        const indicatorHtml = renderStatusIndicator(isFound, tgt.match_type, tgt.confidence);

        // 3. Dropdown des colonnes du nouveau fichier
        let selectOptionsHtml = '<option value="">-- Aucun (Laisser vide / NULL) --</option>';
        const tgtAliases = standardAliases[tgtName] || [];
        const tgtAliasesNorm = tgtAliases.map(a => a.toLowerCase().replace(/[^a-z0-9]/g, '_'));

        incomingColumnsList.forEach(inc => {
          const isSelected = (inc.name === selectedSource) ? "selected" : "";
          const incNorm = inc.name.toLowerCase().replace(/[^a-z0-9]/g, '_');
          const isSameName = (inc.name.toLowerCase() === tgtName.toLowerCase() || incNorm === tgtName.toLowerCase());
          
          let labelPrefix = "";
          if (isSameName) {
            labelPrefix = "✨ (Même nom) ";
          } else if (tgtAliases.includes(inc.name.toLowerCase()) || tgtAliasesNorm.includes(incNorm)) {
            labelPrefix = "💡 (Synonyme) ";
          }

          selectOptionsHtml += `<option value="${inc.name}" ${isSelected}>${labelPrefix}${inc.name}</option>`;
        });

        // 4. Aperçu des 3 premières valeurs injectées
        const samplesHtml = renderSamplesChips(tgt.samples);

        tr.innerHTML = `
          <!-- Colonne Cible -->
          <td class="py-3 px-4">
            <div class="flex items-center gap-2">
              <span class="font-bold text-slate-900 text-xs">${tgtName}</span>
              ${reqBadge}
              <span class="text-[10px] font-mono uppercase px-2 py-0.5 rounded ${typeClass}">${tgt.data_type}</span>
            </div>
            ${tgt.description ? `<p class="text-[11px] text-slate-500 mt-0.5">${tgt.description}</p>` : ''}
          </td>

          <!-- INDICATEUR VERT / ROUGE -->
          <td id="indicator-${tgtName}" class="py-3 px-3 text-center">
            ${indicatorHtml}
          </td>

          <!-- Sélecteur Colonne Nouveau Fichier -->
          <td class="py-3 px-4">
            <select onchange="handleTargetSourceChange('${tgtName}', this.value)"
                    class="w-full text-xs px-3 py-1.5 border ${isFound ? 'border-emerald-300 bg-emerald-50/20' : 'border-rose-300 bg-rose-50/20'} rounded-lg text-slate-800 font-medium focus:outline-none focus:ring-2 focus:ring-blue-500 shadow-sm"
                    id="select-${tgtName}">
              ${selectOptionsHtml}
            </select>
          </td>

          <!-- Aperçu Données Injectées -->
          <td id="samples-container-${tgtName}" class="py-3 px-4">
            ${samplesHtml}
          </td>
        `;

        tbody.appendChild(tr);
      });

      document.getElementById("stat-found-count").innerHTML = `<i class="fa-solid fa-check"></i> ${foundCount} trouvée(s)`;
      document.getElementById("stat-not-found-count").innerHTML = `<i class="fa-solid fa-xmark"></i> ${notFoundCount} non trouvée(s)`;

      updateUnusedColumnsDisplay();

      document.getElementById("btn-save-memory").disabled = false;
      document.getElementById("btn-run-pipeline").disabled = false;
    }

    function renderStatusIndicator(isFound, matchType = "unmapped", confidence = 0.0) {
      if (isFound) {
        let badgeText = "Trouvée";
        let badgeStyle = "bg-emerald-100 text-emerald-800 border-emerald-300";
        let icon = "fa-circle-check text-emerald-600";

        if (matchType === "exact") {
          badgeText = "Trouvée (Exact)";
        } else if (matchType === "normalized") {
          badgeText = "Trouvée (Normalisé)";
        } else if (matchType === "memory") {
          badgeText = "Trouvée (Mémoire)";
          badgeStyle = "bg-blue-100 text-blue-800 border-blue-300";
          icon = "fa-bolt text-blue-600";
        } else if (matchType === "alias") {
          badgeText = "Trouvée (Synonyme)";
          badgeStyle = "bg-indigo-100 text-indigo-800 border-indigo-300";
          icon = "fa-wand-magic-sparkles text-indigo-600";
        } else if (matchType === "fuzzy") {
          const pct = Math.round(confidence * 100);
          badgeText = `Trouvée (Fuzzy ${pct}%)`;
          badgeStyle = "bg-teal-100 text-teal-800 border-teal-300";
          icon = "fa-sliders text-teal-600";
        } else if (matchType === "manual") {
          badgeText = "Choix manuel";
          badgeStyle = "bg-amber-100 text-amber-800 border-amber-300";
          icon = "fa-hand text-amber-600";
        }

        return `
          <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-bold ${badgeStyle} border shadow-xs" title="Type d'association: ${matchType}">
            <i class="fa-solid ${icon}"></i> ${badgeText}
          </span>
        `;
      } else {
        return `
          <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-bold bg-rose-100 text-rose-800 border border-rose-300">
            <i class="fa-solid fa-circle-xmark text-rose-600"></i> Non trouvée
          </span>
        `;
      }
    }

    function handleTargetSourceChange(targetName, chosenSource) {
      currentTargetMappings[targetName] = chosenSource || null;
      const isFound = Boolean(chosenSource);

      // 1. Mise à jour instantanée de l'indicateur VERT / ROUGE
      const indicatorEl = document.getElementById(`indicator-${targetName}`);
      indicatorEl.innerHTML = renderStatusIndicator(isFound, isFound ? 'manual' : 'unmapped', 1.0);

      // 2. Mise à jour de la bordure du select
      const selectEl = document.getElementById(`select-${targetName}`);
      if (isFound) {
        selectEl.className = selectEl.className.replace('border-rose-300 bg-rose-50/20', 'border-emerald-300 bg-emerald-50/20');
      } else {
        selectEl.className = selectEl.className.replace('border-emerald-300 bg-emerald-50/20', 'border-rose-300 bg-rose-50/20');
      }

      // 3. Mise à jour des échantillons
      const samplesContainer = document.getElementById(`samples-container-${targetName}`);
      if (chosenSource) {
        const found = incomingColumnsList.find(c => c.name === chosenSource);
        const samples = found ? found.samples : [];
        samplesContainer.innerHTML = renderSamplesChips(samples);
      } else {
        samplesContainer.innerHTML = '<span class="text-[11px] text-slate-400 italic">Aucune donnée (valeurs NULL)</span>';
      }

      // 4. Recalcul des compteurs
      let foundCount = 0;
      let notFoundCount = 0;
      Object.values(currentTargetMappings).forEach(src => {
        if (src) foundCount++;
        else notFoundCount++;
      });
      document.getElementById("stat-found-count").innerHTML = `<i class="fa-solid fa-check"></i> ${foundCount} trouvée(s)`;
      document.getElementById("stat-not-found-count").innerHTML = `<i class="fa-solid fa-xmark"></i> ${notFoundCount} non trouvée(s)`;

      updateUnusedColumnsDisplay();
      showToast(`Colonne '${targetName}' ➔ ${chosenSource ? 'Trouvée (' + chosenSource + ')' : 'Non trouvée'}`);
    }

    function updateUnusedColumnsDisplay() {
      const assignedSources = new Set(Object.values(currentTargetMappings).filter(Boolean));
      const unused = incomingColumnsList.filter(c => !assignedSources.has(c.name));
      const container = document.getElementById("unused-columns-pills");

      if (unused.length === 0) {
        container.innerHTML = '<span class="text-emerald-700 font-semibold bg-emerald-50 px-2 py-0.5 rounded border border-emerald-200">Toutes les colonnes entrantes sont utilisées</span>';
      } else {
        container.innerHTML = unused.map(c => `
          <span class="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-mono bg-slate-100 text-slate-700 border border-slate-200" title="Cette colonne ne sera pas conservée dans la sortie">
            ${c.name}
          </span>
        `).join(" ");
      }
    }

    function renderSamplesChips(samples) {
      if (!samples || samples.length === 0) {
        return '<span class="text-[11px] text-slate-400 italic">Aucune valeur non nulle</span>';
      }
      return samples.map(s => `
        <span class="inline-block px-2 py-0.5 text-[11px] font-mono bg-slate-100 text-slate-700 rounded border border-slate-200 truncate max-w-[150px] mr-1" title="${s}">
          ${s}
        </span>
      `).join("");
    }

    async function saveCurrentMappings() {
      const mappings = {};
      for (const [tgt, src] of Object.entries(currentTargetMappings)) {
        if (src) {
          mappings[src] = tgt;
        }
      }

      if (Object.keys(mappings).length === 0) {
        alert("Aucune correspondance à mémoriser.");
        return;
      }

      try {
        const res = await fetch("/api/save-mapping", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mappings: mappings })
        });
        const data = await res.json();
        showToast(`✅ ${Object.keys(mappings).length} correspondances mémorisées dans config/mapping_memory.json !`);
      } catch (e) {
        alert("Erreur sauvegarde mémoire: " + e.message);
      }
    }

    async function executePipeline() {
      if (!currentFileInfo || !currentFileInfo.file_name) return;

      const btn = document.getElementById("btn-run-pipeline");
      btn.disabled = true;
      btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Traitement en cours...';

      try {
        const res = await fetch("/api/process", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            file_name: currentFileInfo.file_name,
            schema_name: currentActiveSchema,
            target_to_source: currentTargetMappings,
            save_to_memory: true
          })
        });

        if (!res.ok) {
          let errMsg = `HTTP ${res.status}`;
          try {
            const err = await res.json();
            errMsg = err.detail || errMsg;
          } catch (_) {
            errMsg = await res.text();
          }
          alert("Erreur pipeline: " + errMsg);
          return;
        }

        const data = await res.json();
        renderResults(data);
        showToast("Traitement terminé avec succès !");
        loadInputFiles();
      } catch (err) {
        alert("Erreur exécution: " + err.message);
      } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-bolt"></i> Aligner & Nettoyer les données';
      }
    }

    function renderResults(data) {
      const section = document.getElementById("results-section");
      section.classList.remove("hidden");
      section.scrollIntoView({ behavior: "smooth" });

      document.getElementById("kpi-total").textContent = data.total_rows;
      document.getElementById("kpi-clean").textContent = `${data.clean_rows} (${Math.round((data.clean_rows / (data.total_rows || 1)) * 100)}%)`;
      document.getElementById("kpi-quarantine").textContent = `${data.quarantine_rows} (${Math.round((data.quarantine_rows / (data.total_rows || 1)) * 100)}%)`;

      const cleanBtn = document.getElementById("download-clean-btn");
      const cleanXlsxBtn = document.getElementById("download-clean-xlsx-btn");
      if (data.clean_rows > 0) {
        cleanBtn.href = `/api/download/output/${encodeURIComponent(data.clean_file)}`;
        cleanBtn.classList.remove("opacity-50", "pointer-events-none");
        cleanBtn.innerHTML = '<i class="fa-solid fa-download"></i> Télécharger CSV (' + data.clean_rows + ' lignes)';
        if (data.clean_excel_file) {
          cleanXlsxBtn.classList.remove("hidden");
          cleanXlsxBtn.href = `/api/download/output/${encodeURIComponent(data.clean_excel_file)}`;
          cleanXlsxBtn.innerHTML = '<i class="fa-solid fa-file-excel"></i> Télécharger Excel (.xlsx)';
        }
      } else {
        cleanBtn.href = "#";
        cleanBtn.classList.add("opacity-50", "pointer-events-none");
        cleanBtn.innerHTML = '<i class="fa-solid fa-ban"></i> Fichier Propre Vide (0 ligne valide)';
        cleanXlsxBtn.classList.add("hidden");
      }
      
      const quarBtn = document.getElementById("download-quar-btn");
      if (data.quarantine_file && data.quarantine_rows > 0) {
        quarBtn.classList.remove("hidden");
        quarBtn.href = `/api/download/quarantine/${encodeURIComponent(data.quarantine_file)}`;
        quarBtn.innerHTML = '<i class="fa-solid fa-file-shield"></i> Télécharger Quarantaine (' + data.quarantine_rows + ' lignes)';
      } else {
        quarBtn.classList.add("hidden");
      }

      const zeroAlert = document.getElementById("zero-clean-alert");
      if (data.clean_rows === 0 && data.total_rows > 0) {
        zeroAlert.classList.remove("hidden");
        switchResultsTab("quar");
      } else {
        zeroAlert.classList.add("hidden");
        switchResultsTab("clean");
      }

      const motifsCard = document.getElementById("motifs-summary-card");
      const motifsList = document.getElementById("motifs-summary-list");
      if (data.rejection_motifs_summary && Object.keys(data.rejection_motifs_summary).length > 0) {
        motifsCard.classList.remove("hidden");
        motifsList.innerHTML = Object.entries(data.rejection_motifs_summary).map(([motif, count]) => `
          <li class="flex items-center justify-between py-0.5 border-b border-slate-100 last:border-0">
            <span class="text-rose-700">❌ ${motif}</span>
            <span class="font-bold text-slate-800">${count} ligne(s)</span>
          </li>
        `).join("");
      } else {
        motifsCard.classList.add("hidden");
        motifsList.innerHTML = "";
      }

      renderTable("clean-table-head", "clean-table-body", data.clean_preview);
      renderTable("quar-table-head", "quar-table-body", data.quarantine_preview, "motif_rejet");
    }

    function renderTable(headId, bodyId, rows, highlightCol = null) {
      const head = document.getElementById(headId);
      const body = document.getElementById(bodyId);
      head.innerHTML = "";
      body.innerHTML = "";

      if (!rows || rows.length === 0) {
        body.innerHTML = '<tr><td class="p-4 text-center text-slate-400 italic">Aucune donnée à afficher.</td></tr>';
        return;
      }

      const columns = Object.keys(rows[0]);
      const trHead = document.createElement("tr");
      columns.forEach(c => {
        const th = document.createElement("th");
        th.className = "px-3 py-2.5 font-bold uppercase tracking-wider text-[11px]";
        if (c === highlightCol) {
          th.classList.add("text-rose-700", "bg-rose-50");
        }
        th.textContent = c;
        trHead.appendChild(th);
      });
      head.appendChild(trHead);

      rows.forEach(r => {
        const tr = document.createElement("tr");
        tr.className = "hover:bg-slate-50/80 transition";
        columns.forEach(c => {
          const td = document.createElement("td");
          td.className = "px-3 py-2 whitespace-nowrap text-slate-700";
          if (c === highlightCol) {
            td.classList.add("text-rose-700", "font-bold", "bg-rose-50/40");
          }
          td.textContent = r[c] !== null && r[c] !== undefined ? r[c] : "";
          tr.appendChild(td);
        });
        body.appendChild(tr);
      });
    }

    function switchResultsTab(tab) {
      const cleanBtn = document.getElementById("tab-clean-btn");
      const quarBtn = document.getElementById("tab-quar-btn");
      const cleanContent = document.getElementById("tab-clean-content");
      const quarContent = document.getElementById("tab-quar-content");

      if (tab === "clean") {
        cleanBtn.className = "py-2.5 border-b-2 border-blue-600 text-blue-600 font-bold flex items-center gap-1.5";
        quarBtn.className = "py-2.5 border-b-2 border-transparent text-slate-500 hover:text-slate-800 flex items-center gap-1.5";
        cleanContent.classList.remove("hidden");
        quarContent.classList.add("hidden");
      } else {
        quarBtn.className = "py-2.5 border-b-2 border-amber-600 text-amber-700 font-bold flex items-center gap-1.5";
        cleanBtn.className = "py-2.5 border-b-2 border-transparent text-slate-500 hover:text-slate-800 flex items-center gap-1.5";
        quarContent.classList.remove("hidden");
        cleanContent.classList.add("hidden");
      }
    }

    function reprDelim(d) {
      if (!d) return "Auto";
      if (d === ";") return "Point-virgule (;)";
      if (d === ",") return "Virgule (,)";
      if (d === "\\t") return "Tabulation (\\t)";
      if (d === "|") return "Pipe (|)";
      return d;
    }

    function showToast(msg) {
      const toast = document.getElementById("toast");
      document.getElementById("toast-msg").textContent = msg;
      toast.classList.remove("hidden");
      setTimeout(() => {
        toast.classList.add("hidden");
      }, 3500);
    }
  </script>
</body>
</html>
"""
