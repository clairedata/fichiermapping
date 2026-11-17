"""main.py.

Orchestrateur principal du pipeline d'alignement structurel et de nettoyage de données.
Point d'entrée de l'application (CLI).
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import shutil
import sys
from typing import Optional

# Insertion de la racine du projet dans sys.path pour les imports sécurisés
project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.aligner import align_dataframe
from src.cleaner import clean_and_quarantine, load_cleaning_rules
from src.cli import (
    console,
    display_pipeline_report,
    display_schema_diff,
    interactive_column_resolver,
    print_banner,
)
from src.comparator import compare_schemas, load_target_schema
from src.mapper import resolve_mappings
from src.models import PipelineReport
from src.sniffer import read_file

SUPPORTED_EXTENSIONS = {".csv", ".tsv", ".txt", ".xlsx", ".xls"}


def process_file(
    file_path: Path,
    target_schema_path: Path,
    memory_path: Path,
    rules_path: Path,
    output_dir: Path,
    quarantine_dir: Path,
    archive_dir: Path,
    interactive: bool = True,
    resolver_callback: Optional[interactive_column_resolver] = None,
) -> PipelineReport:
    """Traite un fichier client brut selon la cinématique complète du pipeline.

    1. Ingestion brute (pl.String) et sniffer (encodage/délimiteur).
    2. Comparaison structurelle (SchemaDiff).
    3. Résolution des colonnes inconnues (Mémoire / CLI).
    4. Remplissage, coalesce des collisions et alignement strict.
    5. Nettoyage déclaratif et routage en quarantaine.
    6. Export du fichier propre, export quarantaine et archivage de l'original.
    """
    report = PipelineReport(
        input_file=str(file_path),
        target_schema_file=str(target_schema_path),
        total_rows=0,
        clean_rows=0,
        quarantined_rows=0,
    )

    try:
        # Étape 1 : Ingestion et détection
        raw_df, metadata = read_file(file_path)
        report.detected_encoding = metadata.get("encoding")
        report.detected_delimiter = metadata.get("delimiter")
        report.total_rows = raw_df.height

        # Étape 2 : Chargement cible et comparaison structurelle
        target_columns = load_target_schema(target_schema_path)
        schema_diff = compare_schemas(raw_df.columns, target_columns)
        report.schema_diff = schema_diff

        if interactive:
            display_schema_diff(schema_diff, file_path.name)

        # Étape 3 : Résolution des mappings
        if resolver_callback is not None:
            resolver_cb = resolver_callback
        else:
            resolver_cb = interactive_column_resolver if interactive else None
        actions, updated_memory = resolve_mappings(
            df=raw_df,
            unknown_columns=schema_diff.unknown_columns,
            target_columns=target_columns,
            memory_path=memory_path,
            resolver_callback=resolver_cb,
            auto_use_aliases=True,
        )

        for src_c, act in actions.items():
            report.mappings[src_c] = act.target_col if act.target_col else "__IGNORE__"

        # Étape 4 : Alignement et fusion
        aligned_df = align_dataframe(
            df=raw_df,
            target_columns=target_columns,
            matched_columns=schema_diff.matched_columns,
            mapping_actions=actions,
        )

        # Étape 5 : Nettoyage déclaratif et quarantaine
        rules = load_cleaning_rules(rules_path)
        cleaning_res = clean_and_quarantine(aligned_df, rules)
        report.clean_rows = cleaning_res.clean_rows
        report.quarantined_rows = cleaning_res.quarantine_rows

        # Étape 6 : Export et archivage horodaté
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = file_path.stem

        output_dir.mkdir(parents=True, exist_ok=True)
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        archive_dir.mkdir(parents=True, exist_ok=True)

        # Export propre
        clean_file = output_dir / f"{stem}_clean_{timestamp}.csv"
        cleaning_res.clean_df.write_csv(clean_file)
        report.output_file = str(clean_file.resolve())

        # Export quarantaine si lignes rejetées
        if cleaning_res.quarantine_rows > 0:
            quar_file = quarantine_dir / f"{stem}_quarantine_{timestamp}.csv"
            cleaning_res.quarantine_df.write_csv(quar_file)
            report.quarantine_file = str(quar_file.resolve())

        # Déplacement sécurisé vers archive/
        archive_file = archive_dir / f"{stem}_{timestamp}{file_path.suffix}"
        shutil.move(str(file_path), str(archive_file))
        report.archive_file = str(archive_file.resolve())

        report.status = "SUCCESS"

    except Exception as e:
        report.status = "ERROR"
        report.error_message = str(e)

    return report


def main(argv: Optional[list[str]] = None) -> int:
    """Point d'entrée principal de l'application CLI."""
    parser = argparse.ArgumentParser(
        description="Moteur d'alignement structurel et de nettoyage de données clients."
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Chemin vers un fichier spécifique à traiter (optionnel).",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("input"),
        help="Répertoire d'ingestion des fichiers bruts (défaut: input/).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="Répertoire des fichiers nettoyés conformes (défaut: output/).",
    )
    parser.add_argument(
        "--quarantine-dir",
        type=Path,
        default=Path("quarantine"),
        help="Répertoire des rejets avec motif (défaut: quarantine/).",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=Path("archive"),
        help="Répertoire d'archivage horodaté des fichiers bruts (défaut: archive/).",
    )
    parser.add_argument(
        "--target-schema",
        type=Path,
        default=Path("reference/target_schema.csv"),
        help="Fichier de schéma cible de référence (défaut: reference/target_schema.csv).",
    )
    parser.add_argument(
        "--memory",
        type=Path,
        default=Path("config/mapping_memory.json"),
        help="Fichier de cache persistant de mapping (défaut: config/mapping_memory.json).",
    )
    parser.add_argument(
        "--rules",
        type=Path,
        default=Path("config/cleaning_rules.yaml"),
        help="Fichier de règles de nettoyage déclaratives (défaut: config/cleaning_rules.yaml).",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Désactive les invites interactives en ligne de commande.",
    )

    args = parser.parse_args(argv)

    print_banner()

    # Vérification du schéma cible
    if not args.target_schema.exists():
        console.print(
            f"[bold red]Erreur critique : Schéma cible introuvable : '{args.target_schema}'.[/]"
        )
        return 1

    # Liste des fichiers à traiter
    files_to_process: list[Path] = []
    if args.file:
        if not args.file.exists():
            console.print(f"[bold red]Le fichier spécifié n'existe pas : '{args.file}'.[/]")
            return 1
        files_to_process.append(args.file)
    else:
        if args.input_dir.exists():
            for f in sorted(args.input_dir.iterdir()):
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
                    files_to_process.append(f)

    if not files_to_process:
        console.print(
            f"[yellow]Aucun fichier à traiter trouvé dans '{args.input_dir}'.[/]\n"
            "[dim]Déposez vos fichiers clients (.csv, .xlsx, .tsv) dans le dossier input/ "
            "puis relancez la commande.[/]"
        )
        return 0

    console.print(
        f"[bold cyan]{len(files_to_process)} fichier(s) détecté(s) pour traitement.[/]\n"
    )

    success_count = 0
    error_count = 0

    for file_path in files_to_process:
        console.print(f"\n[bold underline]Traitement en cours : {file_path.name}[/]")
        report = process_file(
            file_path=file_path,
            target_schema_path=args.target_schema,
            memory_path=args.memory,
            rules_path=args.rules,
            output_dir=args.output_dir,
            quarantine_dir=args.quarantine_dir,
            archive_dir=args.archive_dir,
            interactive=not args.non_interactive,
        )

        display_pipeline_report(report)

        if report.status == "SUCCESS":
            success_count += 1
        else:
            error_count += 1
            if report.error_message:
                console.print(f"[bold red]Détail de l'erreur : {report.error_message}[/]")

    console.print(
        f"\n[bold green]Fin du traitement. Succès : {success_count} | Erreurs : {error_count}[/]"
    )
    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
