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
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
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
    extract_sample_values,
    load_mapping_memory,
    resolve_column_from_memory,
    save_mapping_memory,
)
from src.models import IGNORE_TOKEN, MappingAction
from src.sniffer import read_file

app = FastAPI(
    title="Moteur d'Alignement & Nettoyage de Données",
    description="Interface Web Interactive pour le mapping de schéma orienté structure cible.",
    version="1.1.0",
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
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
QUARANTINE_DIR = BASE_DIR / "quarantine"
ARCHIVE_DIR = BASE_DIR / "archive"

for d in (REFERENCE_DIR, INPUT_DIR, OUTPUT_DIR, QUARANTINE_DIR, ARCHIVE_DIR):
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
    """Retourne la liste des fichiers de schéma enregistrés dans reference/."""
    return sorted(REFERENCE_DIR.glob("*.csv"))


def evaluate_schema_match(
    incoming_columns: list[str],
    schema_path: Path,
    memory: dict[str, str],
) -> dict[str, Any]:
    """Évalue le taux de concordance d'un schéma cible avec les colonnes d'un fichier entrant."""
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

    incoming_lower = {c.lower(): c for c in incoming_columns}
    matched_count = 0

    for col in target_defs:
        # A. Même nom
        if col.name.lower() in incoming_lower:
            matched_count += 1
        else:
            # B. Recherche mémoire
            for mem_src, mem_tgt in memory.items():
                if mem_tgt.lower() == col.name.lower() and mem_src in incoming_columns:
                    matched_count += 1
                    break

    total = len(target_defs)
    score = int(round((matched_count / total * 100))) if total > 0 else 0

    return {
        "file_name": schema_path.name,
        "columns_count": total,
        "matched_count": matched_count,
        "score_percent": score,
        "columns": [col.name for col in target_defs],
    }


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

    # 1. Échantillons des colonnes du nouveau fichier
    incoming_cols_map: dict[str, list[str]] = {}
    incoming_cols_info: list[dict[str, Any]] = []
    for col in raw_df.columns:
        samples = extract_sample_values(raw_df, col, limit=3)
        incoming_cols_map[col] = samples
        incoming_cols_info.append({"name": col, "samples": samples})

    incoming_lower_map = {col.lower(): col for col in raw_df.columns}

    # 2. Évaluation de tous les schémas disponibles dans reference/
    schema_files = get_available_schemas()
    schemas_eval: list[dict[str, Any]] = []
    best_schema_name: str = DEFAULT_SCHEMA_NAME
    highest_score: int = -1

    for s_path in schema_files:
        ev = evaluate_schema_match(raw_df.columns, s_path, memory)
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

    # 3. Construction de l'alignement pour le schéma actif
    target_defs = load_target_schema_definitions(active_schema_path)
    target_alignment: list[dict[str, Any]] = []
    used_incoming: set[str] = set()

    for col in target_defs:
        rule = rules.get(col.name)
        tgt_name = col.name
        matched_source: Optional[str] = None
        match_type: str = "unmapped"

        # Correspondance A : Même nom
        if tgt_name.lower() in incoming_lower_map:
            matched_source = incoming_lower_map[tgt_name.lower()]
            match_type = "same_name"
        else:
            # Correspondance B : Mémoire de mapping
            for mem_src, mem_tgt in memory.items():
                if mem_tgt.lower() == tgt_name.lower() and mem_src in raw_df.columns:
                    matched_source = mem_src
                    match_type = "memory"
                    break

        is_found = matched_source is not None
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
    target_columns = load_target_schema(schema_path)

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

    rules = load_cleaning_rules(RULES_PATH)
    cleaning_res = clean_and_quarantine(aligned_df, rules)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = file_path.stem

    clean_filename = f"{stem}_clean_{timestamp}.csv"
    clean_path = OUTPUT_DIR / clean_filename
    cleaning_res.clean_df.write_csv(clean_path)

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
        "quarantine_file": quar_filename,
        "archive_file": archive_filename,
        "clean_preview": clean_preview,
        "quarantine_preview": quar_preview,
        "rejection_motifs_summary": cleaning_res.rejection_motifs_summary,
    }


@app.get("/api/download/output/{filename}")
def download_clean_file(filename: str) -> FileResponse:
    path = OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Fichier non trouvé.")
    return FileResponse(path, filename=filename, media_type="text/csv")


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

    <!-- SECTION SCHÉMAS ENREGISTRÉS AVEC FOCUS SUR LE SCHÉMA CONVENABLE -->
    <div class="bg-white rounded-2xl border border-slate-200 p-5 mb-6 shadow-sm">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-3">
        <div>
          <h2 class="text-sm font-bold text-slate-900 flex items-center gap-2">
            <i class="fa-solid fa-database text-purple-600"></i>
            Schémas Enregistrés (Catalogue de Référence)
          </h2>
          <p class="text-xs text-slate-500">Le schéma le plus adapté est automatiquement mis en évidence en violet/indigo avec son taux de correspondance.</p>
        </div>
        <span class="text-xs text-slate-400 italic">Cliquez sur un schéma pour basculer la structure</span>
      </div>

      <!-- Container des cartes de schémas -->
      <div id="schemas-cards-container" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3.5">
        <div class="p-4 rounded-xl border border-slate-200 bg-slate-50 text-center text-xs text-slate-400">
          Chargement des schémas...
        </div>
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
        <div class="flex items-center gap-3">
          <a id="download-clean-btn" href="#" class="px-4 py-2 text-xs font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-xl hover:bg-emerald-100 transition flex items-center gap-2 shadow-sm">
            <i class="fa-solid fa-download"></i> Télécharger Fichier Propre
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

    document.addEventListener("DOMContentLoaded", () => {
      loadRegisteredSchemas();
      loadInputFiles();
    });

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
        renderSchemasCards(registeredSchemas, null);
      } catch (e) {
        console.error("Erreur chargement schémas:", e);
      }
    }

    function renderSchemasCards(schemasList, activeBestMatch) {
      const container = document.getElementById("schemas-cards-container");
      container.innerHTML = "";

      schemasList.forEach(sch => {
        const fileName = sch.file_name;
        const isActive = (fileName === currentActiveSchema);
        const isBestMatch = (activeBestMatch && activeBestMatch.best_schema === fileName);
        const score = (activeBestMatch && activeBestMatch.scores && activeBestMatch.scores[fileName] !== undefined)
          ? activeBestMatch.scores[fileName]
          : null;

        const card = document.createElement("div");
        card.onclick = () => switchSchema(fileName);
        card.className = "cursor-pointer p-4 rounded-xl transition border text-xs ";

        // Focus visuel avec couleur différente (violet / indigo vibrant) pour le schéma convenable trouvé
        if (isBestMatch) {
          card.className += "bg-gradient-to-br from-purple-50 via-indigo-50 to-purple-100/50 border-2 border-purple-500 shadow-md ring-2 ring-purple-200/70";
        } else if (isActive) {
          card.className += "bg-blue-50/50 border-2 border-blue-500 shadow-sm";
        } else {
          card.className += "bg-white border-slate-200 hover:border-slate-300 hover:shadow-sm";
        }

        let badgeHtml = "";
        if (isBestMatch) {
          badgeHtml = `
            <span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-purple-600 text-white shadow-sm">
              <i class="fa-solid fa-star text-amber-300"></i> Schéma convenable (${score !== null ? score + '%' : 'optimal'})
            </span>
          `;
        } else if (score !== null) {
          badgeHtml = `<span class="px-2 py-0.5 rounded text-[10px] font-medium bg-slate-100 text-slate-600">${score}% concordance</span>`;
        } else {
          badgeHtml = `<span class="px-2 py-0.5 rounded text-[10px] font-medium bg-slate-100 text-slate-500">${sch.columns_count} colonnes</span>`;
        }

        card.innerHTML = `
          <div class="flex items-center justify-between mb-2">
            <span class="font-bold text-sm ${isBestMatch ? 'text-purple-950 font-extrabold' : (isActive ? 'text-blue-900' : 'text-slate-800')}">
              ${fileName}
            </span>
            ${badgeHtml}
          </div>
          <div class="flex items-center justify-between text-[11px] text-slate-500 mt-2">
            <span>Structure : <b>${sch.columns_count}</b> colonnes</span>
            ${isActive ? '<span class="font-bold text-blue-600 flex items-center gap-1"><i class="fa-solid fa-circle-check"></i> Actif</span>' : '<span class="text-slate-400">Cliquer pour activer</span>'}
          </div>
        `;

        container.appendChild(card);
      });
    }

    function switchSchema(schemaName) {
      currentActiveSchema = schemaName;
      document.getElementById("active-schema-badge").textContent = schemaName;
      if (currentFileInfo && currentFileInfo.file_name) {
        inspectExistingFile(currentFileInfo.file_name, schemaName);
      } else {
        // Simple recharge du schéma cible
        fetch(`/api/schema?name=${encodeURIComponent(schemaName)}`)
          .then(r => r.json())
          .then(data => {
            renderSchemasCards(registeredSchemas, null);
          });
      }
      showToast(`Schéma actif : ${schemaName}`);
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
        const res = await fetch(`/api/upload?schema_name=${encodeURIComponent(currentActiveSchema)}`, {
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
      inspectExistingFile(filename, currentActiveSchema);
    }

    async function inspectExistingFile(filename, schemaName) {
      try {
        const url = `/api/inspect/${encodeURIComponent(filename)}?schema_name=${encodeURIComponent(schemaName || currentActiveSchema)}`;
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
      currentActiveSchema = info.active_schema || DEFAULT_SCHEMA_NAME;
      document.getElementById("active-schema-badge").textContent = currentActiveSchema;

      // File Meta Strip
      document.getElementById("file-meta-strip").classList.remove("hidden");
      document.getElementById("meta-filename").textContent = info.file_name;
      document.getElementById("meta-format").textContent = info.format || "CSV";
      document.getElementById("meta-encoding").textContent = info.encoding || "UTF-8";
      document.getElementById("meta-delim").textContent = reprDelim(info.delimiter);
      document.getElementById("meta-rows").textContent = info.row_count;
      document.getElementById("meta-cols").textContent = incomingColumnsList.length;

      // Mise à jour des cartes de schémas enregistrés avec scores et focus
      const scoresMap = {};
      if (info.schemas_evaluation) {
        info.schemas_evaluation.forEach(ev => {
          scoresMap[ev.file_name] = ev.score_percent;
        });
      }
      renderSchemasCards(registeredSchemas, {
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
        const indicatorHtml = renderStatusIndicator(isFound);

        // 3. Dropdown des colonnes du nouveau fichier
        let selectOptionsHtml = '<option value="">-- Aucun (Laisser vide / NULL) --</option>';
        incomingColumnsList.forEach(inc => {
          const isSelected = (inc.name === selectedSource) ? "selected" : "";
          const isSameName = (inc.name.toLowerCase() === tgtName.toLowerCase());
          const labelPrefix = isSameName ? "✨ (Même nom) " : "";
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

    function renderStatusIndicator(isFound) {
      if (isFound) {
        return `
          <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-bold bg-emerald-100 text-emerald-800 border border-emerald-300">
            <i class="fa-solid fa-circle-check text-emerald-600"></i> Trouvée
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
      indicatorEl.innerHTML = renderStatusIndicator(isFound);

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
          const err = await res.json();
          alert("Erreur pipeline: " + err.detail);
          btn.disabled = false;
          btn.innerHTML = '<i class="fa-solid fa-bolt"></i> Aligner & Nettoyer les données';
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
      if (data.clean_rows > 0) {
        cleanBtn.href = `/api/download/output/${encodeURIComponent(data.clean_file)}`;
        cleanBtn.classList.remove("opacity-50", "pointer-events-none");
        cleanBtn.innerHTML = '<i class="fa-solid fa-download"></i> Télécharger Fichier Propre (' + data.clean_rows + ' lignes)';
      } else {
        cleanBtn.href = "#";
        cleanBtn.classList.add("opacity-50", "pointer-events-none");
        cleanBtn.innerHTML = '<i class="fa-solid fa-ban"></i> Fichier Propre Vide (0 ligne valide)';
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
