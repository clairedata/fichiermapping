"""web_app.py.

Point d'entrée du serveur Web interactif (FastAPI + Uvicorn).
Permet de visualiser la structure cible, d'analyser les colonnes d'un nouveau fichier,
d'associer les colonnes inconnues et de lancer le nettoyage depuis un navigateur web.

Usage:
    uv run python web_app.py
    uv run python web_app.py --port 8080
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import uvicorn
from rich.console import Console
from rich.panel import Panel

# Ajout de la racine du projet dans sys.path
project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

console = Console()


def run_server() -> None:
    """Lance le serveur Uvicorn avec configuration d'écoute."""
    parser = argparse.ArgumentParser(description="Serveur Web du Moteur d'Alignement & Nettoyage")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Hôte d'écoute (défaut: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port d'écoute (défaut: 8000)")
    parser.add_argument("--reload", action="store_true", default=False, help="Active le rechargement à chaud")

    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    banner = f"[bold cyan]Moteur d'Alignement Structurel & Nettoyage — Interface Web[/]\n\n" \
             f"Accédez à l'application dans votre navigateur : [bold green underline]{url}[/]\n" \
             f"[dim]Appuyez sur Ctrl+C pour arrêter le serveur.[/]"

    console.print(Panel(banner, border_style="cyan"))

    uvicorn.run("src.web:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    run_server()
