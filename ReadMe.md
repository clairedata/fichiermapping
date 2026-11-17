# Moteur d'Alignement Structurel et de Nettoyage de Données (MVP)

Application locale haute performance (Windows / Linux / macOS) modulaire et pilotée par les métadonnées, conçue pour automatiser l'ingestion, le mapping dynamique de schéma et le nettoyage de fichiers clients (`CSV`, `TSV`, `XLSX`). Elle produit systématiquement des fichiers de sortie normalisés et strictement conformes à un modèle cible fixe (`reference/target_schema.csv`).

---

## 🚀 Fonctionnalités Clés

1. **Ingestion & Sniffer Automatique :**
   - Détection automatique de l'encodage (UTF-8, UTF-8 avec BOM, Windows-1252, Latin-1).
   - Détection automatique du délimiteur (`;`, `,`, `\t`, `|`).
   - Typage strict en `pl.String` (Polars) dès l'ingestion pour préserver l'intégrité intégrale des données (zéros initiaux, codes postaux, téléphones).
   - Prise en charge native des fichiers Excel `.xlsx` et `.xls`.

2. **Comparaison Structurelle & Typage (Schema Diff) :**
   - Comparaison automatique des colonnes entrantes avec le modèle de référence `reference/target_schema.csv`.
   - Modèle cible enrichi avec le **type** (`string`, `email`, `phone`, `date`, `integer`) et la description de chaque colonne.
   - Identification immédiate des colonnes reconnues, inédites ou cibles manquantes.

3. **Dictionnaire Bilingue de Synonymes (FR / EN) & Matching Intelligent :**
   - Dictionnaire exhaustif de synonymes usuels stocké dans `config/standard_aliases.json`.
   - **Reconnaissance automatique out-of-the-box** des champs récurrents en français et en anglais :
     - **Nom :** `last_name`, `lastname`, `surname`, `family_name`, `nom_famille`, `nom_de_famille`, `patronyme`, `name`, etc.
     - **Prénom :** `first_name`, `firstname`, `given_name`, `forename`, `premier_prenom`, `fname`, `prénom`, etc.
     - **Téléphone :** `phone`, `phone_number`, `mobile`, `cell`, `portable`, `tel_mobile`, `gsm`, `num_tel`, `tel`, etc.
     - **Email :** `email`, `e-mail`, `courriel`, `mail`, `email_address`, `adresse_email`, `mel`, etc.
     - **Ville :** `city`, `town`, `commune`, `localite`, `municipality`, `nom_ville`, etc.
     - **Code Postal :** `zip`, `zipcode`, `postal_code`, `postcode`, `cp`, `codepostal`, etc.
     - **Adresse :** `address`, `street`, `street_address`, `addr`, `rue`, `voie`, `adresse_postale`, etc.
     - **Pays :** `country`, `country_name`, `nation`, `state_country`, `pays_residence`, etc.
     - **ID Client :** `id`, `client_id`, `customer_id`, `account_id`, `cust_id`, `code_client`, etc.
     - **Civilité :** `title`, `salutation`, `gender`, `prefix`, `sexe`, `civilité`, `genre`, etc.
     - **Date de création :** `created_at`, `creation_date`, `signup_date`, `registration_date`, `date_inscription`, etc.
     - **Commandes & E-commerce :** `id_commande`, `order_id`, `reference_produit`, `sku`, `quantite`, `qty`, `montant_total`, `total_amount`, etc.
   - **Normalisation linguistique poussée :** suppression automatique des accents (`Prénom` -> `prenom`, `Téléphone` -> `telephone`), mise en minuscules, suppression des tirets/espaces.
   - **Fuzzy Matching (tolérance aux fautes de frappe) :** détection des approximations (ex: `telephon`, `adrese`) via `difflib.SequenceMatcher`.
   - **Hiérarchie stricte des correspondances :** Exact (1.0) ➔ Normalisé (0.98) ➔ Mémoire (0.95) ➔ Synonyme FR/EN (0.92) ➔ Approximatif (0.82-0.88).

4. **Mémoire de Mapping Persistante & Résolution Human-in-the-Loop :**
   - Cache persistant dans `config/mapping_memory.json`.
   - Résolution instantanée des colonnes déjà connues ou mappées manuellement.
   - Pour chaque colonne totalement inédite et non résolue par les synonymes, pause interactive (CLI stylisée via `rich`) affichant les 3 premières valeurs non-nulles pour guider l'opérateur.
   - Enregistrement immédiat du choix (colonne cible ou `__IGNORE__`) dans la mémoire pour les futurs traitements.

5. **Alignement & Injection dans le Moule (Aligner) :**
   - Fusion intelligente (`coalesce`) en cas de collision (si plusieurs colonnes sont mappées vers la même cible ou si la cible existait déjà, les données non nulles ne sont jamais écrasées).
   - Création des colonnes cibles manquantes remplies avec `NULL` (`None`).
   - Suppression stricte des colonnes ignorées et ordonnancement rigoureusement conforme au schéma cible.

5. **Nettoyage Déclaratif & Quarantaine (Cleaner) :**
   - Moteur de règles déclaratives configurables dans `config/cleaning_rules.yaml`.
   - **Nettoyage téléphonique avancé :**
     - Remplacement systématique de `+33`, `33` ou `0033` par `0` avec contrôle strict de **10 caractères** (format français `0612345678`).
     - Préservation intégrale des autres indicatifs internationaux (`+1`, `+44`, etc.).
     - Rejet des chaînes non téléphoniques (lettres, texte arbitraire, numéros fictifs comme `0000000000`).
   - **Déduplication automatique par téléphone :**
     - Détection des doublons sur le numéro de téléphone normalisé.
     - Conservation de la première occurrence dans le fichier propre.
     - Mise en quarantaine automatique des occurrences suivantes avec le motif :  
       `[telephone] Doublon détecté sur le numéro de téléphone : '...' (première occurrence ligne X conservée)`.
   - Transformations standard : `trim`, casse (`upper`, `lower`, `title`), reformatage de dates multiformats vers `YYYY-MM-DD`.
   - Validation de champs obligatoires (`required`), expressions régulières (`regex_match`), listes de valeurs permises (`allowed_values`).

6. **Export & Archivage Immuable :**
   - Export horodaté des données nettoyées dans `output/`.
   - Export horodaté des lignes en quarantaine dans `quarantine/`.
   - Déplacement sécurisé du fichier brut original vers `archive/` (garantie d'immutabilité).

---

## 📁 Arborescence du Projet

```text
fichiermapping/
├── PLAN.md                        # Spécification détaillée du projet
├── README.md                      # Guide d'utilisation et documentation
├── pyproject.toml                 # Dépendances et configuration uv / hatchling
├── reference/
│   └── target_schema.csv          # Modèle cible définissant les colonnes finales
├── input/                         # Dépôt des fichiers clients bruts à traiter
├── archive/                       # Sauvegarde horodatée des fichiers bruts traités
├── output/                        # Fichiers finaux normalisés et nettoyés
├── quarantine/                    # Lignes rejetées avec colonne explicite 'motif_rejet'
├── config/
│   ├── standard_aliases.json      # Dictionnaire bilingue (FR / EN) de synonymes et variantes
│   ├── mapping_memory.json        # Cache persistant des mappings {"source": "cible" | "__IGNORE__"}
│   └── cleaning_rules.yaml        # Règles de nettoyage et de validation déclaratives
├── src/
│   ├── __init__.py
│   ├── models.py                  # Dataclasses (SchemaDiff, PipelineReport, MappingAction, etc.)
│   ├── sniffer.py                 # Détection encodage/délimiteur et lecture en pl.String
│   ├── comparator.py              # Comparaison structurelle vs target_schema
│   ├── mapper.py                  # Gestion du cache persistant et résolution
│   ├── aligner.py                 # Alignement, injection, coalesce et projection
│   ├── cleaner.py                 # Nettoyage déclaratif et routage en quarantaine
│   └── cli.py                     # Interface interactive terminal (rich)
├── tests/
│   ├── test_sniffer.py            # Tests de détection et préservation des types
│   ├── test_comparator_mapper.py  # Tests de comparaison et mémoire de mapping
│   ├── test_aligner.py            # Tests d'alignement et de coalesce
│   ├── test_cleaner.py            # Tests de nettoyage et de quarantaine
│   └── test_pipeline_e2e.py       # Test d'intégration de bout en bout
└── main.py                        # Orchestrateur principal & point d'entrée CLI
```

---

## 🛠️ Installation & Prérequis

- **Python :** 3.11 ou supérieur (testé sur Python 3.13)
- **Gestionnaire :** [`uv`](https://docs.astral.sh/uv/)

Installer les dépendances et synchroniser l'environnement virtuel :

```bash
uv sync
```

---

## 🌐 Interface Web Interactive

Une interface web moderne (FastAPI + SPA interactive) est incluse pour tester visuellement les mappings et le nettoyage :

```bash
uv run python web_app.py
# ou directement via le script CLI
uv run fichiermapping-web
```
L'interface s'ouvre sur **`http://127.0.0.1:8000`** et intègre :

1. **📁 Catalogue des Schémas Enregistrés & Détection Automatique :**
   - Scanne automatiquement tous les schémas définis dans `reference/` (ex: `target_schema.csv`, `schema_commandes.csv`).
   - Calcule le score de correspondance avec le fichier client chargé.
   - **Mise en focus visuelle (violet/indigo vibrant)** du **schéma convenable détecté** avec son badge : `⭐ Schéma convenable (ex: 82% concordance)`.
   - Permet de basculer d'un clic vers n'importe quel autre schéma enregistré.

2. **🟢 Indicateurs Dynamiques Vert / Rouge & Badges Précis :**
   - Devant chaque colonne de la structure cible active :
     - 🟢 **Vert (`Trouvée`)** avec précision du mode d'association :
       - `Trouvée (Exact)` : nom identique.
       - `Trouvée (Normalisé)` : sans accents ni caractères spéciaux (`Prénom` -> `prenom`).
       - `Trouvée (Mémoire)` : issu de vos choix précédents persistés.
       - `Trouvée (Synonyme)` : détecté automatiquement via le dictionnaire bilingue FR / EN.
       - `Trouvée (Fuzzy XX%)` : détection approximative avec score de confiance.
       - `Choix manuel` : sélectionné manuellement dans l'interface.
     - 🔴 **Rouge (`Non trouvée`)** : Aucune colonne source n'est reliée (laisse la valeur à `NULL`).
   - L'indicateur réagit **instantanément** dès que vous changez la sélection dans le menu déroulant !

3. **📖 Modal de Consultation du Dictionnaire de Synonymes (FR / EN) :**
   - Bouton d'accès rapide dans la barre de navigation.
   - Permet de rechercher et visualiser instantanément toutes les variantes supportées pour chaque champ.
   - Entièrement extensible en modifiant le fichier `config/standard_aliases.json`.

4. **Changement libre et affectation des champs inédits :**
   - En face de chaque champ cible, la colonne optimale est pré-sélectionnée automatiquement (`✨ Même nom` ou `💡 Synonyme`).
   - Si un champ n'a pas de correspondance ou si vous souhaitez modifier l'affectation, choisissez directement dans la liste déroulante la colonne de votre choix.
   - Les **3 premières valeurs réelles du fichier** s'affichent immédiatement en face pour contrôler les données.

5. **Actions & Téléchargements :**
   - Mémorisation instantanée dans `config/mapping_memory.json`.
   - Lancement de l'alignement et du nettoyage avec prévisualisation des données conformes et des lignes rejetées en quarantaine (avec motif).
   - Liens de téléchargement direct des fichiers générés.

---

## 💻 Utilisation CLI (Terminal)

### 1. Traitement par lot (Dossier `input/`)

Déposez simplement vos fichiers bruts (`.csv`, `.tsv`, `.xlsx`) dans le dossier `input/`, puis lancez :

```bash
uv run python main.py
# ou directement
uv run fichiermapping
```

### 2. Traitement d'un fichier spécifique

```bash
uv run python main.py --file chemin/vers/mon_fichier.csv
```

### 3. Mode non-interactif (Automatisé / Headless)

Pour les exécutions automatisées (scripts cron, CI/CD) où aucune invite terminal ne doit bloquer l'exécution :

```bash
uv run python main.py --non-interactive
```

### 4. Options CLI disponibles

```text
options:
  -h, --help            Affiche l'aide
  --file FILE           Fichier spécifique à traiter
  --input-dir DIR       Répertoire des fichiers bruts (défaut: input/)
  --output-dir DIR      Répertoire des sorties propres (défaut: output/)
  --quarantine-dir DIR  Répertoire des rejets (défaut: quarantine/)
  --archive-dir DIR     Répertoire d'archivage (défaut: archive/)
  --target-schema CSV   Schéma cible (défaut: reference/target_schema.csv)
  --memory JSON         Mémoire de mapping (défaut: config/mapping_memory.json)
  --rules YAML          Règles de nettoyage (défaut: config/cleaning_rules.yaml)
  --non-interactive     Désactive les invites interactives
```

---

## 🧪 Lancer la Suite de Tests

Tous les modules métier et d'intégration sont couverts par `pytest` :

```bash
uv run pytest
```

Pour afficher le détail de chaque test :

```bash
uv run pytest -v
```

---

## 🧩 Clean Architecture & Extensibilité

Les modules du dossier `src/` (`sniffer.py`, `comparator.py`, `mapper.py`, `aligner.py`, `cleaner.py`) sont **strictement découplés de l'interface utilisateur** (aucun `print()` ni `input()` bloquant).

Pour brancher une API **FastAPI** ou une interface **Next.js** :
- Le moteur accepte un `resolver_callback` asynchrone ou via websocket/requête HTTP pour les colonnes inconnues.
- Les données et rapports sont renvoyés sous forme de dataclasses typées sérialisables (`PipelineReport`, `SchemaDiff`, `CleaningResult`).
