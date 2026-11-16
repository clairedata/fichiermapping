"""src/models.py.

Dataclasses et modèles de données pour le pipeline d'alignement et de nettoyage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
import polars as pl

IGNORE_TOKEN = "__IGNORE__"


@dataclass(frozen=True)
class TargetColumn:
    """Définition d'une colonne cible avec son nom, son type de données et sa description."""

    name: str
    data_type: str = "string"  # "string", "integer", "float", "date", "email", "phone", "boolean"
    description: str = ""


@dataclass(frozen=True)
class SchemaDiff:
    """Représente le résultat de la comparaison entre les colonnes entrantes et cibles."""

    matched_columns: list[str]
    unknown_columns: list[str]
    missing_target_columns: list[str]

    @property
    def is_exact_match(self) -> bool:
        """Indique si le schéma entrant correspond exactement au schéma cible."""
        return len(self.unknown_columns) == 0 and len(self.missing_target_columns) == 0

    @property
    def has_unknown_columns(self) -> bool:
        """Indique si le fichier entrant contient des colonnes inconnues."""
        return len(self.unknown_columns) > 0


@dataclass(frozen=True)
class MappingAction:
    """Action de mapping pour une colonne source donnée."""

    source_col: str
    target_col: Optional[str]
    is_ignored: bool = False
    from_memory: bool = False


@dataclass
class CleaningRuleConfig:
    """Configuration de nettoyage et validation pour une colonne cible."""

    trim: bool = True
    case: Optional[str] = None  # "lower", "upper", "title", "capitalize"
    phone_standardize: bool = False
    unique: bool = False  # Détection des doublons : mise en quarantaine des occurrences suivantes
    date_format: Optional[dict[str, Any]] = None  # {"input_formats": [...], "output_format": "..."}
    regex_replace: list[dict[str, str]] = field(default_factory=list)
    required: bool = False
    regex_match: Optional[str] = None
    allowed_values: Optional[list[str]] = None
    error_message: Optional[str] = None


@dataclass
class CleaningResult:
    """Résultat de l'étape de nettoyage et de quarantaine."""

    clean_df: pl.DataFrame
    quarantine_df: pl.DataFrame
    total_rows: int
    clean_rows: int
    quarantine_rows: int
    rejection_motifs_summary: dict[str, int] = field(default_factory=dict)


@dataclass
class PipelineReport:
    """Rapport synthétique d'exécution du pipeline pour un fichier."""

    input_file: str
    target_schema_file: str
    total_rows: int
    clean_rows: int
    quarantined_rows: int
    detected_encoding: Optional[str] = None
    detected_delimiter: Optional[str] = None
    schema_diff: Optional[SchemaDiff] = None
    mappings: dict[str, str] = field(default_factory=dict)
    output_file: Optional[str] = None
    quarantine_file: Optional[str] = None
    archive_file: Optional[str] = None
    status: str = "SUCCESS"
    error_message: Optional[str] = None
