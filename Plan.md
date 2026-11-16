# PLAN.md — Moteur d'Alignement Structurel et de Nettoyage de Données (MVP)

## 1. Vision & Objectif du Projet
Construire une application Python locale (Windows), modulaire et pilotée par les métadonnées, capable d'automatiser l'ingestion, le mapping dynamique de schéma et le nettoyage de fichiers clients (`CSV`, `XLSX`) afin de produire systématiquement un fichier de sortie conforme à un modèle cible fixe (`reference/target_schema.csv`).

### Cinématique du Pipeline (Workflow)
1. **Dépôt & Détection :** Un fichier client brut est déposé dans `input/`. Le moteur détecte automatiquement son encodage et son délimiteur, puis lit toutes les colonnes en texte brut (`String`) pour préserver l'intégrité des données (ex: zéros initiaux).
2. **Comparaison Structurelle (Schema Diff) :** Comparaison des en-têtes du fichier entrant avec ceux de `reference/target_schema.csv`.
3. **Résolution & Mémoire de Mapping (Human-in-the-Loop) :**
   - Si toutes les colonnes existent dans le modèle cible $\rightarrow$ passage direct au remplissage.
   - Si des colonnes inconnues sont détectées $\rightarrow$ vérification dans `config/mapping_memory.json`.
   - Si une colonne inconnue est déjà mémorisée $\rightarrow$ application automatique sans interruption.
   - Si une colonne est totalement inédite $\rightarrow$ pause interactive (CLI) affichant un aperçu des 3 premières valeurs non nulles et demandant à l'opérateur dans quelle colonne cible ranger les données (ou de l'ignorer). Le choix est immédiatement enregistré dans `config/mapping_memory.json`.
4. **Remplissage & Alignement (Injection dans le Moule) :**
   - Fusion (`coalesce`) en cas de collision (si une colonne cible existante reçoit également les données d'une colonne mappée, ne jamais écraser les données non nulles).
   - Création des colonnes cibles manquantes remplies avec `NULL` (`None`).
   - Suppression des colonnes ignorées et projection stricte selon l'ordre exact de `target_schema.csv`.
5. **Nettoyage Standardisé & Quarantaine :** Application des règles définies dans `config/cleaning_rules.yaml` sur la structure unifiée. Les lignes rejetées sont isolées dans `quarantine/` avec un motif explicite.
6. **Export & Archivage :** Génération du fichier propre dans `output/` et déplacement horodaté du fichier brut vers `archive/`.

---

## 2. Stack Technique & Contraintes d'Architecture
- **Environnement :** Windows, Python 3.11+, gestionnaire de paquets `uv`.
- **Dépendances principales :**
  - `polars` : moteur de transformation haute performance (strictement typé).
  - `charset-normalizer` : détection automatique d'encodage (UTF-8, Windows-1252, Latin-1).
  - `openpyxl` / `fastexcel` : support des fichiers Excel `.xlsx`.
  - `pyyaml` : lecture des règles de nettoyage déclaratives.
  - `pytest` : suite de tests unitaires.
- **Règles d'Ingénierie (Clean Architecture) :**
  - **Séparation stricte Core / UI :** Aucun `input()` ou `print()` bloquant dans les modules métier (`sniffer.py`, `comparator.py`, `mapper.py`, `aligner.py`, `cleaner.py`). Toute interaction utilisateur est isolée dans `cli.py` afin de pouvoir brancher ultérieurement une API **FastAPI** + interface **Next.js** sans réécrire le moteur.
  - **Immutabilité des données brutes :** Ne jamais modifier un fichier dans `input/` ; toujours archiver l'original dans `archive/`.
  - **Typage fort & Docstrings :** Utiliser les annotations de type Python (`typing` / dataclasses) sur toutes les fonctions et structures de données.

---

## 3. Arborescence du Dépôt

```text
schema-align-cleaner/
├── PLAN.md                        # Ce document de référence
├── pyproject.toml                 # Configuration projet et dépendances (uv)
├── reference/
│   └── target_schema.csv          # Fichier témoin définissant l'ordre et le nom exact des colonnes finales
├── input/                         # Dépôt des fichiers clients bruts à traiter
├── archive/                       # Sauvegarde horodatée des fichiers bruts traités
├── output/                        # Fichiers finaux alignés et nettoyés
├── quarantine/                    # Lignes rejetées lors du nettoyage avec colonne 'motif_rejet'
├── config/
│   ├── mapping_memory.json        # Cache persistant : {"col_source": "col_cible" | "__IGNORE__"}
│   └── cleaning_rules.yaml        # Règles de nettoyage par colonne cible
├── src/
│   ├── __init__.py
│   ├── models.py                  # Dataclasses (SchemaDiff, PipelineReport, MappingAction)
│   ├── sniffer.py                 # Détection auto encodage, séparateur CSV, lecture Excel en pl.Utf8
│   ├── comparator.py              # Comparaison des colonnes entrantes vs target_schema
│   ├── mapper.py                  # Gestion de mapping_memory.json et résolution des colonnes
│   ├── aligner.py                 # Construction du DataFrame cible (coalesce, missing cols, reorder)
│   ├── cleaner.py                 # Nettoyage standardisé et routage quarantaine
│   └── cli.py                     # Interface terminal interactive (aperçu 3 valeurs + choix cible)
├── tests/
│   ├── test_sniffer.py
│   ├── test_comparator_mapper.py
│   ├── test_aligner.py
│   └── test_cleaner.py
└── main.py                        # Orchestrateur principal (CLI entry point)