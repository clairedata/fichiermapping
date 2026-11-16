"""src/cli.py.

Module d'interface en ligne de commande (CLI) interactive.
Isole toutes les interactions utilisateur (input(), print(), affichage stylisé rich)
afin de respecter la Clean Architecture et permettre un découplage total du moteur métier.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.models import IGNORE_TOKEN, PipelineReport, SchemaDiff

console = Console()


def print_banner() -> None:
    """Affiche la bannière d'accueil du pipeline."""
    banner_text = Text()
    banner_text.append("Moteur d'Alignement Structurel & Nettoyage\n", style="bold cyan")
    banner_text.append("Ingestion dynamique • Mémoire de mapping • Quarantaine", style="dim")
    console.print(Panel(banner_text, border_style="cyan"))


def display_schema_diff(diff: SchemaDiff, file_name: str) -> None:
    """Affiche un tableau récapitulatif des différences de schéma pour un fichier."""
    table = Table(title=f"Analyse structurelle : {file_name}", border_style="blue")
    table.add_column("Statut", style="bold", width=22)
    table.add_column("Nombre", justify="right", width=8)
    table.add_column("Colonnes", style="dim")

    table.add_row(
        "[green]Colonnes reconnues[/]",
        str(len(diff.matched_columns)),
        ", ".join(diff.matched_columns) or "Aucune",
    )
    table.add_row(
        "[yellow]Colonnes inconnues[/]",
        str(len(diff.unknown_columns)),
        ", ".join(diff.unknown_columns) or "Aucune",
    )
    table.add_row(
        "[magenta]Colonnes cibles manquantes[/]",
        str(len(diff.missing_target_columns)),
        ", ".join(diff.missing_target_columns) or "Aucune",
    )

    console.print(table)


def interactive_column_resolver(
    column_name: str,
    samples: list[str],
    target_columns: list[str],
) -> str:
    """Pause interactive pour résoudre une colonne inconnue inédite.

    Affiche les 3 premières valeurs non nulles et propose à l'opérateur
    de choisir la colonne cible correspondante ou d'ignorer la colonne.

    Retourne:
        str: Nom de la colonne cible choisie ou '__IGNORE__'.
    """
    console.print()
    panel_title = f"[bold yellow]Résolution requise : Colonne inconnue '{column_name}'[/]"

    samples_text = Text()
    samples_text.append("Aperçu des 3 premières valeurs non-nulles :\n", style="bold")
    if samples:
        for idx, s in enumerate(samples, 1):
            samples_text.append(f"  {idx}. ", style="dim")
            samples_text.append(f"{s}\n", style="green")
    else:
        samples_text.append("  (Colonne entièrement vide ou valeurs nulles)\n", style="italic")

    console.print(Panel(samples_text, title=panel_title, border_style="yellow"))

    table = Table(title="Options de destination", border_style="dim", show_lines=False)
    table.add_column("Index", style="bold cyan", width=6)
    table.add_column("Action / Colonne cible", style="white")

    table.add_row("[0]", "[bold red]Ignorer cette colonne (__IGNORE__)[/]")
    for idx, target in enumerate(target_columns, 1):
        table.add_row(f"[{idx}]", target)

    console.print(table)

    # Boucle de saisie opérateur sécurisée
    while True:
        prompt_text = f"Entrez le numéro (0 à {len(target_columns)}) ou le nom exact de la cible : "
        user_input = console.input(f"[bold green]{prompt_text}[/]").strip()

        if not user_input:
            continue

        # Saisie par index numérique
        if user_input.isdigit():
            idx = int(user_input)
            if idx == 0:
                console.print(f"-> Colonne '[bold yellow]{column_name}[/]' [red]ignorée[/].")
                return IGNORE_TOKEN
            elif 1 <= idx <= len(target_columns):
                chosen = target_columns[idx - 1]
                console.print(
                    f"-> Colonne '[bold yellow]{column_name}[/]' mappée vers '[bold green]{chosen}[/]'."
                )
                return chosen

        # Saisie textuelle directe
        if user_input == IGNORE_TOKEN or user_input.lower() == "ignorer":
            console.print(f"-> Colonne '[bold yellow]{column_name}[/]' [red]ignorée[/].")
            return IGNORE_TOKEN

        for target in target_columns:
            if user_input.lower() == target.lower():
                console.print(
                    f"-> Colonne '[bold yellow]{column_name}[/]' mappée vers '[bold green]{target}[/]'."
                )
                return target

        console.print(
            f"[red]Choix invalide '{user_input}'. Veuillez entrer un numéro entre 0 et {len(target_columns)}.[/]"
        )


def display_pipeline_report(report: PipelineReport) -> None:
    """Affiche le rapport de synthèse d'exécution du pipeline pour un fichier."""
    table = Table(title=f"Rapport d'exécution : {Path(report.input_file).name}", border_style="green")
    table.add_column("Indicateur", style="bold")
    table.add_column("Valeur", justify="right")

    status_style = "green" if report.status == "SUCCESS" else "red"
    table.add_row("Statut global", f"[{status_style}]{report.status}[/]")
    table.add_row("Encodage détecté", report.detected_encoding or "N/A")
    table.add_row("Délimiteur détecté", repr(report.detected_delimiter) if report.detected_delimiter else "N/A")
    table.add_row("Lignes brutes ingérées", str(report.total_rows))
    table.add_row("[green]Lignes saines validées[/]", f"[green]{report.clean_rows}[/]")
    table.add_row("[yellow]Lignes en quarantaine[/]", f"[yellow]{report.quarantined_rows}[/]")

    if report.output_file:
        table.add_row("Fichier de sortie", report.output_file)
    if report.quarantine_file:
        table.add_row("Fichier de quarantaine", report.quarantine_file)
    if report.archive_file:
        table.add_row("Archive brute", report.archive_file)

    console.print(table)

    if report.mappings:
        map_table = Table(title="Mappings appliqués", border_style="dim")
        map_table.add_column("Colonne Source", style="yellow")
        map_table.add_column("Cible / Statut", style="green")
        for src_c, tgt_c in report.mappings.items():
            val = "[red]__IGNORE__[/]" if tgt_c == IGNORE_TOKEN else f"[green]{tgt_c}[/]"
            map_table.add_row(src_c, val)
        console.print(map_table)
