"""src/cleaner.py.

Module de nettoyage standardisé, déduplication et routage en quarantaine.
Règles téléphone :
- Si commence par 33, +33 ou 0033 : remplacement par 0 et contrôle strict de 10 caractères.
- Si un autre indicatif international (+1, +44, etc.) : préservation de l'indicatif.
- Vérification que c'est un numéro de téléphone réel (pas de texte, pas de numéros fictifs).
- Détection des doublons par téléphone et mise en quarantaine des occurrences suivantes.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any, Optional
import polars as pl
import yaml

from src.models import CleaningResult, CleaningRuleConfig


def load_cleaning_rules(rules_path: Path | str) -> dict[str, CleaningRuleConfig]:
    """Charge les règles de nettoyage depuis un fichier YAML."""
    path = Path(rules_path)
    if not path.exists():
        return {}

    try:
        with open(path, mode="r", encoding="utf-8") as f:
            raw_data = yaml.safe_load(f)
    except Exception:
        return {}

    if not isinstance(raw_data, dict):
        return {}

    rules_dict = raw_data.get("rules", raw_data)
    if not isinstance(rules_dict, dict):
        return {}

    parsed_rules: dict[str, CleaningRuleConfig] = {}
    for col, rule_def in rules_dict.items():
        if not isinstance(rule_def, dict):
            continue
        parsed_rules[col] = CleaningRuleConfig(
            trim=bool(rule_def.get("trim", True)),
            case=rule_def.get("case"),
            phone_standardize=bool(rule_def.get("phone_standardize", False)),
            unique=bool(rule_def.get("unique", False) or rule_def.get("deduplicate", False)),
            date_format=rule_def.get("date_format"),
            regex_replace=rule_def.get("regex_replace", []),
            required=bool(rule_def.get("required", False)),
            regex_match=rule_def.get("regex_match"),
            allowed_values=rule_def.get("allowed_values"),
            error_message=rule_def.get("error_message"),
        )

    return parsed_rules


def standardize_phone(val: Optional[str]) -> tuple[Optional[str], bool, str]:
    """Nettoie et standardise un numéro de téléphone selon les exigences métier.

    Règles :
    1. Si le numéro commence par +33, 0033 ou 33 :
       Remplacer l'indicatif par '0' et obtenir exactement 10 chiffres (ex: 0612345678).
    2. Si un autre indicatif (+1, +44, 0044, etc.) :
       Conserver l'indicatif international intact.
    3. Vérifier que c'est un numéro réel (pas de texte, pas de séquence fictive comme 0000000000).

    Retourne:
        tuple[Optional[str], bool, str]: (numéro standardisé, booléen de validité, motif d'erreur éventuel)
    """
    if val is None:
        return None, True, ""
    s = val.strip()
    if not s:
        return None, True, ""

    # 1. Vérification : est-ce du texte (non un numéro de téléphone) ?
    letters = re.findall(r"[a-zA-ZÀ-ÿ]", s)
    if len(letters) > 0:
        return s, False, f"Ne correspond pas à un numéro de téléphone valide (contient des lettres) : '{s}'"

    # 2. Suppression de (0) et nettoyage des caractères de séparation (espaces, tirets, points, parenthèses)
    s_cleaned = re.sub(r"\(0\)", "", s)
    has_plus = s_cleaned.startswith("+")
    digits = re.sub(r"\D", "", s_cleaned)

    if not digits:
        return s, False, f"Numéro vide ou invalide : '{s}'"

    # Vérification numéro fictif (tous les chiffres identiques, ex: 0000000000 ou 1111111111)
    if len(digits) >= 6 and len(set(digits)) == 1:
        return s, False, f"Numéro de téléphone fictif invalide : '{s}'"

    # 3. Traitement indicatif français : +33, 0033 ou 33 (11 chiffres)
    if has_plus and s_cleaned.startswith("+33"):
        remainder = digits[2:]  # Enlève 33
        if remainder.startswith("0"):
            remainder = remainder[1:]
        res = "0" + remainder
        if len(res) != 10:
            return res, False, f"Numéro français invalide : doit comporter exactement 10 chiffres (reçu {len(res)}) : '{res}'"
        if not re.match(r"^0[1-9]\d{8}$", res):
            return res, False, f"Format français invalide (attendu 0[1-9]... sur 10 chiffres) : '{res}'"
        return res, True, ""

    if digits.startswith("0033"):
        remainder = digits[4:]
        if remainder.startswith("0"):
            remainder = remainder[1:]
        res = "0" + remainder
        if len(res) != 10:
            return res, False, f"Numéro français invalide : doit comporter exactement 10 chiffres (reçu {len(res)}) : '{res}'"
        if not re.match(r"^0[1-9]\d{8}$", res):
            return res, False, f"Format français invalide (attendu 0[1-9]... sur 10 chiffres) : '{res}'"
        return res, True, ""

    # Si commence par 33 sans '+' et fait 11 chiffres (ex: 33612345678)
    if digits.startswith("33") and len(digits) == 11 and not has_plus:
        remainder = digits[2:]
        if remainder.startswith("0"):
            remainder = remainder[1:]
        res = "0" + remainder
        if not re.match(r"^0[1-9]\d{8}$", res):
            return res, False, f"Format français invalide : '{res}'"
        return res, True, ""

    # Si commence par 0 (numéro français national usuel) sans '+'
    if digits.startswith("0") and not has_plus:
        if len(digits) != 10:
            return digits, False, f"Numéro français invalide : doit comporter exactement 10 chiffres (reçu {len(digits)}) : '{digits}'"
        if not re.match(r"^0[1-9]\d{8}$", digits):
            return digits, False, f"Format français invalide (attendu 0[1-9]... sur 10 chiffres) : '{digits}'"
        return digits, True, ""

    # 4. Autre indicatif international (+1, +44, +49, etc.)
    # "si un autre indicatif tu laisses"
    if has_plus:
        res = "+" + digits
        if len(digits) < 7 or len(digits) > 15:
            return res, False, f"Numéro international hors norme E.164 (7 à 15 chiffres) : '{res}'"
        return res, True, ""

    if digits.startswith("00") and len(digits) >= 9:
        res = "+" + digits[2:]
        if len(digits[2:]) < 7 or len(digits[2:]) > 15:
            return res, False, f"Numéro international hors norme E.164 : '{res}'"
        return res, True, ""

    # Numéro étranger sans indicatif '+' explicite (ex: mock américain 10 chiffres)
    if 8 <= len(digits) <= 15:
        return digits, True, ""

    return digits, False, f"Longueur de numéro de téléphone anormale ({len(digits)} chiffres) : '{s}'"


def standardize_date(
    val: Optional[str],
    input_formats: list[str],
    output_format: str = "%Y-%m-%d",
) -> tuple[Optional[str], bool]:
    """Tente de parser et reformater une chaîne de date.

    Retourne:
        tuple[Optional[str], bool]: (valeur formatée, succès du parsing)
    """
    if val is None:
        return None, True
    s = val.strip()
    if not s:
        return None, True

    for fmt in input_formats:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime(output_format), True
        except ValueError:
            continue

    return s, False


def clean_and_quarantine(
    df: pl.DataFrame,
    rules: dict[str, CleaningRuleConfig],
) -> CleaningResult:
    """Applique les transformations de nettoyage, la déduplication par téléphone

    et isole les lignes non conformes dans un DataFrame de quarantaine.
    """
    if df.height == 0:
        empty_clean = df.clone()
        empty_quarantine = df.with_columns(pl.lit(None, dtype=pl.String).alias("motif_rejet"))
        return CleaningResult(
            clean_df=empty_clean,
            quarantine_df=empty_quarantine,
            total_rows=0,
            clean_rows=0,
            quarantine_rows=0,
        )

    # Sécurisation du typage : s'assurer que toutes les colonnes sont de type pl.String
    df = df.with_columns([pl.col(c).cast(pl.String) for c in df.columns])

    # 1. Étape de transformation / standardisation de base (colonne par colonne)
    transformed_cols: list[pl.Expr] = []

    for col_name in df.columns:
        expr = pl.col(col_name)
        rule = rules.get(col_name)

        if rule is not None:
            if rule.trim:
                expr = expr.str.strip_chars()

            for rep in rule.regex_replace:
                pattern = rep.get("pattern", "")
                replacement = rep.get("replacement", "")
                if pattern:
                    expr = expr.str.replace_all(pattern, replacement)

            if rule.case:
                case_type = rule.case.lower()
                if case_type == "lower":
                    expr = expr.str.to_lowercase()
                elif case_type == "upper":
                    expr = expr.str.to_uppercase()
                elif case_type in ("title", "capitalize"):
                    expr = expr.str.to_titlecase()

        transformed_cols.append(expr.alias(col_name))

    transformed_df = df.select(transformed_cols)

    # Transformations complexes (téléphone, date)
    cols_to_update: dict[str, list[Optional[str]]] = {}
    phone_validation_per_col: dict[str, tuple[list[bool], list[str]]] = {}
    date_failures_per_col: dict[str, list[bool]] = {}

    for col_name, rule in rules.items():
        if col_name not in transformed_df.columns:
            continue

        series_list = transformed_df[col_name].to_list()

        # Phone standardisation & validation
        if rule.phone_standardize:
            cleaned_phones: list[Optional[str]] = []
            phone_fails: list[bool] = []
            phone_errors: list[str] = []
            for v in series_list:
                cleaned_num, is_valid, err_msg = standardize_phone(v)
                cleaned_phones.append(cleaned_num)
                phone_fails.append(not is_valid)
                phone_errors.append(err_msg)
            cols_to_update[col_name] = cleaned_phones
            phone_validation_per_col[col_name] = (phone_fails, phone_errors)

        # Date formatting
        if rule.date_format:
            in_fmts = rule.date_format.get(
                "input_formats", ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"]
            )
            out_fmt = rule.date_format.get("output_format", "%Y-%m-%d")
            new_dates: list[Optional[str]] = []
            failures: list[bool] = []
            for v in series_list:
                formatted_val, success = standardize_date(v, in_fmts, out_fmt)
                new_dates.append(formatted_val)
                failures.append(not success)
            cols_to_update[col_name] = new_dates
            date_failures_per_col[col_name] = failures

    if cols_to_update:
        update_exprs = [
            pl.Series(k, v, dtype=pl.String) for k, v in cols_to_update.items()
        ]
        transformed_df = transformed_df.with_columns(update_exprs)

    # 2. Étape de validation et détection des motifs de rejet
    total_rows = transformed_df.height
    row_motifs: list[list[str]] = [[] for _ in range(total_rows)]
    motifs_summary: dict[str, int] = {}

    # A. Détection des doublons (par téléphone ou colonnes avec unique=True)
    for col_name, rule in rules.items():
        if col_name not in transformed_df.columns:
            continue
        # Déduplication activée si unique=True ou si colonne telephone
        is_dedup_col = rule.unique or (col_name == "telephone" and rule.phone_standardize)
        if is_dedup_col:
            values = transformed_df[col_name].to_list()
            seen_entries: dict[str, int] = {}
            for i, val in enumerate(values):
                if val is not None and str(val).strip() != "":
                    norm_val = str(val).strip().lower()
                    if norm_val in seen_entries:
                        first_line = seen_entries[norm_val] + 1
                        motif = (
                            f"[{col_name}] Doublon détecté sur le numéro de téléphone : '{val}' "
                            f"(première occurrence ligne {first_line} conservée)"
                        )
                        row_motifs[i].append(motif)
                        motifs_summary["Doublon téléphone"] = (
                            motifs_summary.get("Doublon téléphone", 0) + 1
                        )
                    else:
                        seen_entries[norm_val] = i

    # B. Validations de champs (required, phone format, date, regex, allowed values)
    for col_name, rule in rules.items():
        if col_name not in transformed_df.columns:
            continue

        values = transformed_df[col_name].to_list()
        regex_compiled = re.compile(rule.regex_match) if rule.regex_match else None
        allowed_set = set(rule.allowed_values) if rule.allowed_values is not None else None
        date_fails = date_failures_per_col.get(col_name)
        phone_data = phone_validation_per_col.get(col_name)

        for i, val in enumerate(values):
            is_empty = val is None or str(val).strip() == ""

            # Règle Required
            if rule.required and is_empty:
                msg = rule.error_message or "Valeur obligatoire manquante"
                motif = f"[{col_name}] {msg}"
                row_motifs[i].append(motif)
                motifs_summary[motif] = motifs_summary.get(motif, 0) + 1
                continue

            if not is_empty:
                # Échec validation téléphone (règles strictes : lettres, longueur, format)
                if phone_data:
                    phone_fails, phone_errors = phone_data
                    if phone_fails[i]:
                        detail = phone_errors[i] or rule.error_message or "Numéro de téléphone invalide"
                        motif = f"[{col_name}] {detail}"
                        row_motifs[i].append(motif)
                        motifs_summary[motif] = motifs_summary.get(motif, 0) + 1
                        continue

                # Échec du parsing de date
                if date_fails and date_fails[i]:
                    msg = rule.error_message or "Format de date invalide"
                    motif = f"[{col_name}] {msg} : '{val}'"
                    row_motifs[i].append(motif)
                    motifs_summary[motif] = motifs_summary.get(motif, 0) + 1
                    continue

                # Règle Regex match
                if regex_compiled and not regex_compiled.match(str(val)):
                    msg = rule.error_message or "Format invalide"
                    motif = f"[{col_name}] {msg} : '{val}'"
                    row_motifs[i].append(motif)
                    motifs_summary[motif] = motifs_summary.get(motif, 0) + 1
                    continue

                # Règle Allowed values
                if allowed_set is not None and str(val) not in allowed_set:
                    msg = rule.error_message or "Valeur non autorisée"
                    motif = f"[{col_name}] {msg} : '{val}'"
                    row_motifs[i].append(motif)
                    motifs_summary[motif] = motifs_summary.get(motif, 0) + 1
                    continue

    # 3. Séparation Clean / Quarantaine
    motif_column_values: list[Optional[str]] = [
        "; ".join(m) if m else None for m in row_motifs
    ]
    is_quarantined = [m is not None for m in motif_column_values]

    df_with_motif = transformed_df.with_columns(
        pl.Series("motif_rejet", motif_column_values, dtype=pl.String)
    )

    clean_mask = [not q for q in is_quarantined]
    clean_df = transformed_df.filter(clean_mask)
    quarantine_df = df_with_motif.filter(is_quarantined)

    return CleaningResult(
        clean_df=clean_df,
        quarantine_df=quarantine_df,
        total_rows=total_rows,
        clean_rows=clean_df.height,
        quarantine_rows=quarantine_df.height,
        rejection_motifs_summary=motifs_summary,
    )
