from __future__ import annotations

import hashlib
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import EquipmentImportHistory, EquipmentRegistry

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_MAX_WORKBOOK_BYTES = 10 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
_MAX_ROWS = 10_000
_BUILTIN_DATE_FORMATS = {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47}


@dataclass(frozen=True)
class EquipmentSpreadsheetRow:
    source_sheet: str
    source_row: int
    sequence_number: str
    equipment_id: str
    description: str
    manufacturer: str
    model_number: str
    accuracy_class: str
    measurement_range: str
    serial_number: str
    calibration_date: date | None
    calibration_due_date: date | None
    received_date: date | None
    calibration_interval: str
    subordinate_area: str
    equipment_user: str
    equipment_status: str
    calibration_location: str
    instruction_number: str


@dataclass(frozen=True)
class ParsedEquipmentWorkbook:
    sheet_name: str
    rows: list[EquipmentSpreadsheetRow]
    available_fields: frozenset[str]


@dataclass(frozen=True)
class EquipmentImportResult:
    total: int
    inserted: int
    updated: int
    unchanged: int


def _xml(root_bytes: bytes) -> ElementTree.Element:
    return ElementTree.fromstring(root_bytes)


def _text(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _column_number(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    if letters is None:
        raise ValueError(f"Invalid cell reference {reference!r}.")
    number = 0
    for letter in letters.group(0):
        number = number * 26 + ord(letter) - ord("A") + 1
    return number


def _safe_archive(content: bytes) -> zipfile.ZipFile:
    if len(content) > _MAX_WORKBOOK_BYTES:
        raise ValueError("Excel file exceeds the 10 MB upload limit.")
    if not content.startswith(b"PK"):
        raise ValueError("The uploaded file is not a valid .xlsx workbook.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError("The uploaded file is not a valid .xlsx workbook.") from exc
    total_size = sum(item.file_size for item in archive.infolist())
    if total_size > _MAX_UNCOMPRESSED_BYTES:
        archive.close()
        raise ValueError("The Excel workbook is too large after decompression.")
    return archive


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = _xml(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [_text(item) for item in root.findall(f"{{{_MAIN_NS}}}si")]


def _date_styles(archive: zipfile.ZipFile) -> set[int]:
    try:
        root = _xml(archive.read("xl/styles.xml"))
    except KeyError:
        return set()
    custom_formats = {
        int(item.attrib["numFmtId"]): item.attrib.get("formatCode", "")
        for item in root.findall(f".//{{{_MAIN_NS}}}numFmt")
    }
    cell_formats = root.find(f"{{{_MAIN_NS}}}cellXfs")
    if cell_formats is None:
        return set()
    date_styles: set[int] = set()
    for index, cell_format in enumerate(cell_formats):
        format_id = int(cell_format.attrib.get("numFmtId", "0"))
        format_code = custom_formats.get(format_id, "")
        cleaned_code = re.sub(r'"[^"]*"|\\.|\[[^]]*]', "", format_code.casefold())
        if format_id in _BUILTIN_DATE_FORMATS or re.search(r"[ymdhis]", cleaned_code):
            date_styles.add(index)
    return date_styles


def _excel_date(value: str, date_1904: bool) -> date | datetime:
    base = datetime(1904, 1, 1) if date_1904 else datetime(1899, 12, 30)
    converted = base + timedelta(days=float(value))
    return converted.date() if converted.time() == datetime.min.time() else converted


def _cell_value(
    cell: ElementTree.Element,
    shared_strings: list[str],
    date_styles: set[int],
    date_1904: bool,
) -> Any:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return _text(cell.find(f"{{{_MAIN_NS}}}is"))
    raw_value = _text(cell.find(f"{{{_MAIN_NS}}}v"))
    if not raw_value:
        return ""
    if cell_type == "s":
        index = int(raw_value)
        return shared_strings[index] if index < len(shared_strings) else ""
    if cell_type == "b":
        return raw_value == "1"
    if cell_type in {"str", "e"}:
        return raw_value
    if cell_type == "d":
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    style_index = int(cell.attrib.get("s", "0"))
    if style_index in date_styles:
        try:
            return _excel_date(raw_value, date_1904)
        except ValueError:
            return raw_value
    try:
        number = float(raw_value)
    except ValueError:
        return raw_value
    return int(number) if number.is_integer() else number


def _sheet_path(archive: zipfile.ZipFile) -> tuple[str, str, bool]:
    workbook = _xml(archive.read("xl/workbook.xml"))
    workbook_properties = workbook.find(f"{{{_MAIN_NS}}}workbookPr")
    date_1904 = bool(
        workbook_properties is not None
        and workbook_properties.attrib.get("date1904", "0") in {"1", "true"}
    )
    relationship_id = ""
    sheet_name = ""
    for sheet in workbook.findall(f".//{{{_MAIN_NS}}}sheet"):
        if sheet.attrib.get("name", "").strip().casefold() == "master calibration list":
            sheet_name = sheet.attrib["name"]
            relationship_id = sheet.attrib[f"{{{_REL_NS}}}id"]
            break
    if not relationship_id:
        raise ValueError("Workbook does not contain a 'Master Calibration List' sheet.")

    relationships = _xml(archive.read("xl/_rels/workbook.xml.rels"))
    for relationship in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship"):
        if relationship.attrib.get("Id") != relationship_id:
            continue
        target = relationship.attrib.get("Target", "")
        if relationship.attrib.get("TargetMode", "").casefold() == "external":
            raise ValueError("Master Calibration List worksheet must be stored in the workbook.")
        normalized = posixpath.normpath(
            target.lstrip("/") if target.startswith("/xl/") else posixpath.join("xl", target)
        )
        if PurePosixPath(normalized).is_absolute() or normalized.startswith("../"):
            raise ValueError("Workbook contains an invalid worksheet path.")
        return normalized, sheet_name, date_1904
    raise ValueError("Master Calibration List worksheet relationship is missing.")


def _worksheet_rows(content: bytes) -> tuple[str, list[tuple[int, dict[int, Any]]]]:
    with _safe_archive(content) as archive:
        worksheet_path, sheet_name, date_1904 = _sheet_path(archive)
        shared_strings = _shared_strings(archive)
        date_styles = _date_styles(archive)
        root = _xml(archive.read(worksheet_path))
        rows: list[tuple[int, dict[int, Any]]] = []
        for row in root.findall(f".//{{{_MAIN_NS}}}row"):
            row_number = int(row.attrib.get("r", str(len(rows) + 1)))
            if row_number > _MAX_ROWS:
                raise ValueError("Master Calibration List exceeds 10,000 rows.")
            values = {
                _column_number(cell.attrib["r"]): _cell_value(
                    cell, shared_strings, date_styles, date_1904
                )
                for cell in row.findall(f"{{{_MAIN_NS}}}c")
                if cell.attrib.get("r")
            }
            rows.append((row_number, values))
        return sheet_name, rows


def _canonical_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _normalize_text(value).casefold())


_HEADER_MARKERS = {
    "sequence_number": ("no",),
    "equipment_id": ("equipmentidentificationnumber",),
    "description": ("equipmentdescription", "equipmentname"),
    "manufacturer": ("equipmentmanufacturer",),
    "model_number": ("equipmentmodelnumber", "model"),
    "accuracy_class": ("accuracyclass",),
    "measurement_range": ("measurerange",),
    "serial_number": ("equipmentserialnumber",),
    "calibration_date": ("calibrationdate",),
    "calibration_due_date": ("calibrationduedate",),
    "received_date": ("receivedateaftercalibration",),
    "calibration_interval": ("calibrationinterval", "calibrationfrequency"),
    "subordinate_area": ("subordinatearea",),
    "equipment_user": ("equipmentuser",),
    "equipment_status": ("equipmentstatus",),
    "calibration_location": ("calibrationlocation",),
    "instruction_number": ("calibrationinstructionnumber",),
}


def _header_mapping(values: dict[int, Any]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for column, value in values.items():
        canonical = _canonical_header(value)
        for field, markers in _HEADER_MARKERS.items():
            if field in mapping:
                continue
            if any(canonical.startswith(marker) for marker in markers):
                mapping[field] = column
                break
    return mapping


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, int | float):
        converted = _excel_date(str(value), date_1904=False)
        return converted.date() if isinstance(converted, datetime) else converted
    text = _normalize_text(value)
    for pattern in ("%m/%d/%Y", "%Y/%m/%d", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"Unsupported calibration date {text!r}.")


def parse_equipment_workbook(content: bytes) -> ParsedEquipmentWorkbook:
    sheet_name, worksheet_rows = _worksheet_rows(content)
    header_index = -1
    mapping: dict[str, int] = {}
    for index, (_, values) in enumerate(worksheet_rows):
        candidate = _header_mapping(values)
        if "equipment_id" in candidate and "description" in candidate:
            header_index = index
            mapping = candidate
            break
    if header_index < 0:
        raise ValueError("Master Calibration List headers were not recognized.")

    def value(values: dict[int, Any], field: str) -> Any:
        column = mapping.get(field)
        return values.get(column, "") if column is not None else ""

    parsed_rows: list[EquipmentSpreadsheetRow] = []
    seen_ids: set[str] = set()
    for row_number, values in worksheet_rows[header_index + 1 :]:
        equipment_id = _normalize_text(value(values, "equipment_id"))
        if not equipment_id:
            continue
        normalized_id = equipment_id.casefold()
        if normalized_id in seen_ids:
            raise ValueError(f"Duplicate equipment ID {equipment_id!r} in row {row_number}.")
        seen_ids.add(normalized_id)
        parsed_rows.append(
            EquipmentSpreadsheetRow(
                source_sheet=sheet_name,
                source_row=row_number,
                sequence_number=_normalize_text(value(values, "sequence_number")),
                equipment_id=equipment_id,
                description=_normalize_text(value(values, "description")),
                manufacturer=_normalize_text(value(values, "manufacturer")),
                model_number=_normalize_text(value(values, "model_number")),
                accuracy_class=_normalize_text(value(values, "accuracy_class")),
                measurement_range=_normalize_text(value(values, "measurement_range")),
                serial_number=_normalize_text(value(values, "serial_number")),
                calibration_date=_as_date(value(values, "calibration_date")),
                calibration_due_date=_as_date(value(values, "calibration_due_date")),
                received_date=_as_date(value(values, "received_date")),
                calibration_interval=_normalize_text(value(values, "calibration_interval")),
                subordinate_area=_normalize_text(value(values, "subordinate_area")),
                equipment_user=_normalize_text(value(values, "equipment_user")),
                equipment_status=_normalize_text(value(values, "equipment_status")),
                calibration_location=_normalize_text(value(values, "calibration_location")),
                instruction_number=_normalize_text(value(values, "instruction_number")),
            )
        )
    if not parsed_rows:
        raise ValueError("Master Calibration List contains no equipment rows.")
    return ParsedEquipmentWorkbook(
        sheet_name=sheet_name,
        rows=parsed_rows,
        available_fields=frozenset(mapping),
    )


_IMPORT_FIELDS = (
    "description",
    "manufacturer",
    "model_number",
    "accuracy_class",
    "measurement_range",
    "serial_number",
    "calibration_date",
    "calibration_due_date",
    "received_date",
    "calibration_interval",
    "subordinate_area",
    "equipment_user",
    "equipment_status",
    "calibration_location",
    "instruction_number",
)


def import_equipment_workbook(
    db: Session,
    filename: str,
    content: bytes,
) -> EquipmentImportResult:
    if not filename.casefold().endswith(".xlsx"):
        raise ValueError("Only .xlsx Master Calibration List files are supported.")
    parsed = parse_equipment_workbook(content)
    history = EquipmentImportHistory(
        filename=filename,
        sheet_name=parsed.sheet_name,
        file_sha256=hashlib.sha256(content).hexdigest(),
        total_rows=len(parsed.rows),
    )
    db.add(history)
    db.flush()

    inserted = 0
    updated = 0
    unchanged = 0
    for spreadsheet_row in parsed.rows:
        equipment_id = spreadsheet_row.equipment_id.upper()
        equipment = db.scalar(
            select(EquipmentRegistry).where(
                func.upper(EquipmentRegistry.equipment_id) == equipment_id
            )
        )
        if equipment is None:
            equipment = EquipmentRegistry(equipment_id=equipment_id)
            db.add(equipment)
            inserted += 1
            changed = True
        else:
            changed = any(
                getattr(equipment, field) != getattr(spreadsheet_row, field)
                for field in _IMPORT_FIELDS
                if field in parsed.available_fields
            )
            if changed:
                updated += 1
            else:
                unchanged += 1

        if changed:
            for field in _IMPORT_FIELDS:
                if field in parsed.available_fields:
                    setattr(equipment, field, getattr(spreadsheet_row, field))
            equipment.source_filename = filename
            equipment.source_sheet = spreadsheet_row.source_sheet
            equipment.source_row = spreadsheet_row.source_row
            equipment.source_import_id = history.id

    history.inserted_rows = inserted
    history.updated_rows = updated
    history.unchanged_rows = unchanged
    db.commit()
    return EquipmentImportResult(
        total=len(parsed.rows),
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
    )