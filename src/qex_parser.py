"""src/qex_parser.py.

Module dédié à l'analyse des fichiers Askia Design (.qex) et à la génération
automatique de schémas de référence Excel (.xlsx) dans reference/.

Les fichiers .qex d'Askia Design utilisent la sérialisation binaire MFC (CArchive)
contenant la liste ordonnée des objets CQuestion (chapitres/libellés, variables, boucles).
Ce module parse directement le flux binaire sans dépendre d'Askia Design.

Conforme à la Clean Architecture : aucune interaction UI/CLI directe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import struct
from typing import Optional
import unicodedata
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

# Codes types Askia Design (champ CQuestion)
ASKIA_TYPE_LABELS: dict[int, str] = {
    65535: "Chapitre",
    1: "Fermée",
    2: "Fermée (Multiple)",
    3: "Date / Heure",
    4: "Ouverte (Texte)",
    5: "Numérique",
}

PHONE_EXACT_TOKENS = {
    "tel", "telephone", "phone", "portable", "mobile", "gsm",
    "tel1", "tel2", "phone1", "phone2", "num_tel", "numero_tel",
    "tel_fixe", "tel_mobile", "tel_portable", "tel_contact", "tel_pro",
}
PHONE_EXCLUDED_SUBSTRINGS = ("type_tel", "typetel", "statut_tel", "flag_tel", "code_tel")

EMAIL_EXACT_TOKENS = {
    "email", "mail", "courriel", "e_mail", "mel",
    "email_contact", "adresse_email", "adresse_mail", "email_client",
}


@dataclass
class QexVariable:
    """Représente un élément (chapitre, variable ou boucle) extrait d'un fichier Askia .qex."""

    qid: int
    shortcut: str
    caption: str
    kind: int  # 0 = Chapitre/Libellé, 1 = Variable/Question, 2 = Boucle
    level: int  # Profondeur hiérarchique dans l'arbre du questionnaire
    askia_type_code: int
    askia_type_label: str
    inferred_type: str  # string | phone | email | date | integer


@dataclass
class QexChapterInfo:
    """Résumé d'un chapitre/libellé présent dans le fichier .qex."""

    shortcut: str
    caption: str
    level: int
    variable_count: int


@dataclass
class QexParseResult:
    """Résultat complet de l'extraction des variables d'un fichier .qex."""

    file_name: str
    version: int
    total_questions: int
    chapters: list[QexChapterInfo] = field(default_factory=list)
    selected_chapter: Optional[str] = None
    variables: list[QexVariable] = field(default_factory=list)


def _normalize_label(text: str) -> str:
    """Normalise un libellé pour la comparaison insensible à la casse et aux accents."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    ascii_text = decomposed.encode("ASCII", "ignore").decode("utf-8").lower().strip()
    return re.sub(r"[^a-z0-9]+", "_", ascii_text).strip("_")


def _read_mfc_cstring(data: bytes, pos: int) -> tuple[str, int]:
    """Lit une CString MFC sérialisée (Unicode UTF-16LE ou ANSI CP1252)."""
    if pos >= len(data):
        raise ValueError("Fin de flux inattendue lors de la lecture d'une CString MFC.")

    if pos + 4 <= len(data) and data[pos : pos + 3] == b"\xff\xfe\xff":
        pos += 3
        b0 = data[pos]
        pos += 1
        if b0 == 0xFF:
            length = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            if length == 0xFFFF:
                length = struct.unpack_from("<I", data, pos)[0]
                pos += 4
        else:
            length = b0
        end = pos + length * 2
        if end > len(data):
            raise ValueError("Longueur de chaîne Unicode MFC invalide.")
        return data[pos:end].decode("utf-16-le", errors="replace"), end

    # Chaîne ANSI
    b0 = data[pos]
    pos += 1
    if b0 == 0xFF:
        length = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        if length == 0xFFFF:
            length = struct.unpack_from("<I", data, pos)[0]
            pos += 4
    else:
        length = b0
    end = pos + length
    if end > len(data):
        raise ValueError("Longueur de chaîne ANSI MFC invalide.")
    return data[pos:end].decode("cp1252", errors="replace"), end


def _extract_caption(data: bytes, start_pos: int, limit_pos: int, fallback: str) -> str:
    """Extrait le premier libellé localisé (précédé d'un LCID Windows) d'un bloc CQuestion."""
    search_end = min(limit_pos, start_pos + 350)
    chunk = data[start_pos:search_end]

    # Recherche prioritaire du libellé localisé (LCID Windows ex: 0x040C FR, 0x0409 EN)
    idx = 0
    while idx < len(chunk):
        sp = chunk.find(b"\xff\xfe\xff", idx)
        if sp == -1:
            break
        if sp >= 4:
            lcid = struct.unpack_from("<I", chunk, sp - 4)[0]
            # Un LCID Windows valide est compris entre 0x0001 et 0x7FFF avec langue primaire non nulle
            if 0x0001 <= lcid <= 0x7FFF and (lcid & 0xFF) != 0:
                try:
                    txt, end_rel = _read_mfc_cstring(chunk, sp)
                    cleaned = " ".join(txt.replace("\r", " ").replace("\n", " ").split())
                    if cleaned:
                        return cleaned
                    idx = end_rel
                    continue
                except ValueError:
                    pass
        idx = sp + 3

    return fallback


def _infer_semantic_type(shortcut: str, askia_type_code: int) -> str:
    """Infère le type de colonne cible (phone, email, date, integer, string) depuis le nom et le type Askia."""
    norm = _normalize_label(shortcut)

    # Exclusion explicite des indicateurs/catégories sur téléphone (ex: TYPE_TEL)
    if not any(excl in norm for excl in PHONE_EXCLUDED_SUBSTRINGS):
        if norm in PHONE_EXACT_TOKENS or norm.startswith("tel_") or norm.endswith("_tel") or norm.startswith("phone_"):
            return "phone"

    if norm in EMAIL_EXACT_TOKENS or "email" in norm or "courriel" in norm:
        return "email"

    if askia_type_code == 3:
        return "date"

    if askia_type_code == 5:
        return "integer"

    return "string"


def parse_qex_questions(qex_path: Path | str) -> tuple[int, list[QexVariable]]:
    """Parse un fichier binaire Askia Design (.qex) et retourne (version, liste des QexVariable).

    Lève ValueError si le fichier n'est pas un fichier Askia .qex valide.
    """
    path = Path(qex_path)
    if not path.exists():
        raise FileNotFoundError(f"Fichier QEX introuvable : {path}")

    data = path.read_bytes()
    if len(data) < 24:
        raise ValueError(f"Fichier QEX invalide ou tronqué : {path.name}")

    # Signature de la première classe MFC CQuestion
    first_tag = b"\xff\xff\x01\x00\t\x00CQuestion"
    p0 = data.find(first_tag)
    if p0 == -1:
        raise ValueError(
            f"Le fichier '{path.name}' ne semble pas être un fichier Askia Design (.qex) valide "
            "(signature CQuestion introuvable)."
        )

    version = struct.unpack_from("<I", data, 0)[0]

    # Détermine la borne de fin de la table des questions (début de CFiltre, CEcran ou CLanguage)
    section_end = len(data)
    for stop_marker in (
        b"\xff\xff\x01\x00\x07\x00CFiltre",
        b"\xff\xff\x01\x00\x06\x00CEcran",
        b"\xff\xff\x01\x00\t\x00CLanguage",
    ):
        pos_stop = data.find(stop_marker, p0)
        if pos_stop != -1 and pos_stop < section_end:
            section_end = pos_stop

    # Collecte des positions de chaque CQuestion
    # 1ère question immédiatement après 'CQuestion', suivantes précédées par la balise de classe \x01\x80
    positions: list[int] = [p0 + len(first_tag)]
    idx = p0 + len(first_tag)
    while idx < section_end:
        p = data.find(b"\x01\x80", idx, section_end)
        if p == -1:
            break
        # Validation structurelle : QID (4 octets) + préfixe Unicode CString (\xff\xfe\xff)
        if p + 10 < section_end and data[p + 6 : p + 9] == b"\xff\xfe\xff":
            positions.append(p + 2)
        idx = p + 2

    items: list[QexVariable] = []
    for i, pos in enumerate(positions):
        next_limit = positions[i + 1] - 2 if i + 1 < len(positions) else section_end
        if pos + 16 > next_limit:
            continue

        qid = struct.unpack_from("<I", data, pos)[0]
        try:
            shortcut, meta_pos = _read_mfc_cstring(data, pos + 4)
        except ValueError:
            continue

        shortcut = shortcut.strip()
        if not shortcut or meta_pos + 6 > next_limit:
            continue

        kind, level, askia_type_code = struct.unpack_from("<HHH", data, meta_pos)
        if kind not in (0, 1, 2) or level > 50:
            continue

        caption = _extract_caption(data, meta_pos + 6, next_limit, fallback=shortcut)
        type_label = ASKIA_TYPE_LABELS.get(askia_type_code, f"Type {askia_type_code}")
        inferred = "chapter" if kind == 0 else _infer_semantic_type(shortcut, askia_type_code)

        items.append(
            QexVariable(
                qid=qid,
                shortcut=shortcut,
                caption=caption,
                kind=kind,
                level=level,
                askia_type_code=askia_type_code,
                askia_type_label=type_label,
                inferred_type=inferred,
            )
        )

    return version, items


def _collect_chapter_variables(items: list[QexVariable], chapter_idx: int) -> list[QexVariable]:
    """Collecte toutes les variables (kind == 1) situées hiérarchiquement sous le chapitre donné."""
    chapter = items[chapter_idx]
    extracted: list[QexVariable] = []

    for child in items[chapter_idx + 1 :]:
        # Dès que le niveau hiérarchique revient au niveau du chapitre (ou au-dessus), le chapitre est terminé
        if child.level <= chapter.level:
            break
        # On conserve uniquement les variables réelles (exclut les sous-chapitres kind == 0 et boucles kind == 2)
        if child.kind == 1:
            extracted.append(child)

    return extracted


def _build_chapters_summary(items: list[QexVariable]) -> list[QexChapterInfo]:
    """Construit le résumé de tous les chapitres/libellés du questionnaire."""
    chapters: list[QexChapterInfo] = []
    for idx, item in enumerate(items):
        if item.kind == 0:
            vars_under = _collect_chapter_variables(items, idx)
            chapters.append(
                QexChapterInfo(
                    shortcut=item.shortcut,
                    caption=item.caption,
                    level=item.level,
                    variable_count=len(vars_under),
                )
            )
    return chapters


def list_qex_chapters(qex_path: Path | str) -> list[QexChapterInfo]:
    """Liste tous les chapitres/libellés disponibles dans un fichier .qex."""
    _, items = parse_qex_questions(qex_path)
    return _build_chapters_summary(items)


def extract_importation_variables(
    qex_path: Path | str,
    chapter_label: str = "importation",
) -> QexParseResult:
    """Extrait toutes les variables situées sous le libellé/chapitre 'Importation' d'un fichier .qex.

    Paramètres:
        qex_path: Chemin vers le fichier Askia .qex.
        chapter_label: Nom ou libellé du chapitre à extraire (défaut: 'importation', insensible à la casse).

    Retourne:
        QexParseResult contenant les variables extraites et la liste des chapitres du QEX.

    Lève:
        ValueError si le chapitre recherché n'existe pas dans le fichier .qex.
    """
    path = Path(qex_path)
    version, items = parse_qex_questions(path)
    chapters_summary = _build_chapters_summary(items)

    target_norm = _normalize_label(chapter_label)
    if not target_norm:
        raise ValueError("Le libellé du chapitre à extraire ne peut pas être vide.")

    # 1. Recherche exacte normalisée sur shortcut ou caption parmi les chapitres (kind == 0)
    matched_idx: Optional[int] = None
    for idx, item in enumerate(items):
        if item.kind == 0:
            if _normalize_label(item.shortcut) == target_norm or _normalize_label(item.caption) == target_norm:
                matched_idx = idx
                break

    # 2. Recherche partielle (contient le mot-clé) parmi les chapitres si aucune correspondance exacte
    if matched_idx is None:
        for idx, item in enumerate(items):
            if item.kind == 0:
                if target_norm in _normalize_label(item.shortcut) or target_norm in _normalize_label(item.caption):
                    matched_idx = idx
                    break

    if matched_idx is None:
        available = [f"{c.shortcut} ({c.variable_count} var.)" for c in chapters_summary]
        raise ValueError(
            f"Aucun libellé/chapitre '{chapter_label}' trouvé dans '{path.name}'. "
            f"Chapitres disponibles : {', '.join(available) if available else 'Aucun'}"
        )

    chapter_item = items[matched_idx]
    variables = _collect_chapter_variables(items, matched_idx)

    if not variables:
        raise ValueError(
            f"Le chapitre '{chapter_item.shortcut}' a été trouvé dans '{path.name}', "
            "mais aucune variable n'est présente dessous."
        )

    return QexParseResult(
        file_name=path.name,
        version=version,
        total_questions=len(items),
        chapters=chapters_summary,
        selected_chapter=chapter_item.shortcut,
        variables=variables,
    )


def export_qex_schema_to_excel(
    qex_path: Path | str,
    reference_dir: Path | str,
    chapter_label: str = "importation",
) -> tuple[Path, QexParseResult]:
    """Extrait les variables sous le libellé spécifié d'un .qex et génère un fichier schéma Excel (.xlsx)

    dans le dossier reference/ avec le même nom que le fichier .qex (en laissant target_schema.csv intact).

    Le classeur Excel généré contient :
    - Feuille 1 ('Schema') : tableau structuré (colonne, type, description, type_askia) lu par le moteur.
    - Feuille 2 ('Modele_Colonnes') : en-têtes horizontaux prêts à visualiser la structure finale.

    Retourne:
        tuple[Path, QexParseResult]: (chemin du fichier .xlsx créé, résultat d'extraction QEX)
    """
    qex_file = Path(qex_path)
    ref_dir = Path(reference_dir)
    ref_dir.mkdir(parents=True, exist_ok=True)

    parse_result = extract_importation_variables(qex_file, chapter_label=chapter_label)

    # Nom exact du fichier QEX avec extension .xlsx
    excel_path = ref_dir / f"{qex_file.stem}.xlsx"

    wb = openpyxl.Workbook()

    # Styles d'en-tête
    header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    body_font = Font(name="Calibri", size=10, color="1E293B")
    center_align = Alignment(horizontal="center", vertical="center")

    # --- Feuille 1 : Schema (métadonnées lues par comparator.py) ---
    ws_schema = wb.active
    ws_schema.title = "Schema"

    headers = ["colonne", "type", "description", "type_askia"]
    ws_schema.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws_schema.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center_align

    for var in parse_result.variables:
        desc = var.caption if var.caption else var.shortcut
        ws_schema.append([var.shortcut, var.inferred_type, desc, var.askia_type_label])

    for row in ws_schema.iter_rows(min_row=2, max_row=ws_schema.max_row, max_col=4):
        for idx_c, cell in enumerate(row):
            cell.font = body_font
            if idx_c in (1, 3):
                cell.alignment = center_align

    # Ajustement automatique des largeurs de colonnes
    col_widths = {1: 28, 2: 14, 3: 40, 4: 20}
    for col_idx, width in col_widths.items():
        col_letter = openpyxl.utils.get_column_letter(col_idx)
        ws_schema.column_dimensions[col_letter].width = width
    ws_schema.freeze_panes = "A2"

    # --- Feuille 2 : Modele_Colonnes (structure horizontale prête à l'extraction) ---
    ws_template = wb.create_sheet(title="Modele_Colonnes")
    var_names = [v.shortcut for v in parse_result.variables]
    ws_template.append(var_names)
    for col_idx, vname in enumerate(var_names, start=1):
        cell = ws_template.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center_align
        col_letter = openpyxl.utils.get_column_letter(col_idx)
        ws_template.column_dimensions[col_letter].width = max(len(vname) + 4, 14)
    ws_template.freeze_panes = "A2"

    wb.save(excel_path)
    return excel_path, parse_result
