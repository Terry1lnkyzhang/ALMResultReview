from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from app.models import EquipmentRegistry

_ASSET_ID_RE = re.compile(
    r"(?<![A-Z0-9])[A-Z0-9]{2,}-RD-[A-Z0-9]+-\d+-[A-Z0-9]+(?![A-Z0-9])",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"(?<!\d)(20\d{2})[-/.](0?[1-9]|1[0-2])[-/.](0?[1-9]|[12]\d|3[01])(?!\d)"
)
_SERIAL_FIELD_RE = re.compile(
    r"(?i)(?:\bSN\b|serial(?:\s+number)?|序列号)\s*[:=]\s*_*(?P<value>[^;\r\n]+?)_*(?=$|[;\r\n])"
)
_STRONG_REQUIREMENT_RE = re.compile(
    r"(?is)\bcalibrat(?:ion|ed)\b.{0,30}"
    r"\b(?:date|due|valid(?:ity)?|from)\b"
    r"|\b(?:date|due|valid(?:ity)?)\b.{0,30}\bcalibrat(?:ion|ed)\b"
    r"|(?:校准|校验).{0,20}(?:日期|有效期|到期)"
    r"|(?:日期|有效期|到期).{0,20}(?:校准|校验)"
)
_DEVICE_HINT_RE = re.compile(
    r"(?i)\b(?:equipment|device|instrument|simulator|stop\s*watch|multimeter|"
    r"oscilloscope|analyzer|meter|phantom|calibration\s+tool)\b|"
    r"设备|仪器|模拟器|秒表|万用表|示波器|校准工具|校验工具"
)
_PLACEHOLDER_RE = re.compile(r"^(?:_*|\.*|-*|n/?a|none|unknown|待填)$", re.IGNORECASE)
_INVALID_SERIALS = {"", "na", "n/a", "none", "unknown", "待填"}
_NO_CALIBRATION_INTERVALS = {"no calibration required", "no need calibration"}


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _calibration_not_required(equipment: EquipmentRegistry) -> bool:
    return equipment.calibration_interval.strip().casefold() in _NO_CALIBRATION_INTERVALS


def _contains_identifier(text: str, identifier: str) -> bool:
    if not identifier:
        return False
    return re.search(
        rf"(?<![A-Za-z0-9]){re.escape(identifier)}(?![A-Za-z0-9])",
        text,
        re.IGNORECASE,
    ) is not None


def _parse_date(value: str) -> date | None:
    text = value.strip()
    for date_format in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    return None


def _execution_date(step: dict[str, Any], content: dict[str, Any]) -> date | None:
    return _parse_date(
        _normalized(step.get("execution_date"))
        or _normalized(content.get("execution_date"))
    )


def _serial_aliases(serial_number: str) -> set[str]:
    serial = _normalized(serial_number)
    if serial.casefold() in _INVALID_SERIALS:
        return set()
    aliases = {serial}
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._/-]{3,}", serial):
        if token.casefold() in _INVALID_SERIALS:
            continue
        if not any(character.isdigit() for character in token):
            continue
        if token.isdigit() and len(token) < 6:
            continue
        aliases.add(token)
    return aliases


def _reported_identifiers(actual: str) -> list[str]:
    identifiers: list[str] = []
    for match in _SERIAL_FIELD_RE.finditer(actual):
        value = match.group("value").strip(" _:\t.")
        if value and not _PLACEHOLDER_RE.fullmatch(value):
            identifiers.append(value)
    return list(dict.fromkeys(identifiers))


def _reported_calibration_range(actual: str) -> tuple[date, date] | None:
    if not _STRONG_REQUIREMENT_RE.search(actual):
        return None
    dates = [date(int(year), int(month), int(day)) for year, month, day in _DATE_RE.findall(actual)]
    return (dates[0], dates[1]) if len(dates) >= 2 else None


def _reported_asset_ranges(actual: str) -> dict[str, tuple[date, date]]:
    matches = list(_ASSET_ID_RE.finditer(actual))
    ranges: dict[str, tuple[date, date]] = {}
    for index, match in enumerate(matches):
        segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(actual)
        segment = actual[match.end() : segment_end]
        dates = [
            date(int(year), int(month), int(day))
            for year, month, day in _DATE_RE.findall(segment)
        ]
        if len(dates) >= 2:
            ranges[match.group(0).upper()] = (dates[0], dates[1])
    return ranges


def _equipment_snapshot(equipment: EquipmentRegistry) -> dict[str, Any]:
    return {
        "equipment_id": equipment.equipment_id,
        "description": equipment.description,
        "manufacturer": equipment.manufacturer,
        "model_number": equipment.model_number,
        "serial_number": equipment.serial_number,
        "calibration_date": (
            equipment.calibration_date.isoformat() if equipment.calibration_date else None
        ),
        "calibration_due_date": (
            equipment.calibration_due_date.isoformat()
            if equipment.calibration_due_date
            else None
        ),
        "received_date": equipment.received_date.isoformat() if equipment.received_date else None,
        "equipment_status": equipment.equipment_status,
        "source_filename": equipment.source_filename,
        "source_sheet": equipment.source_sheet,
        "source_row": equipment.source_row,
    }


def _candidate_equipment(
    combined_text: str,
    equipment: Iterable[EquipmentRegistry],
) -> list[EquipmentRegistry]:
    candidates: list[EquipmentRegistry] = []
    folded = combined_text.casefold()
    for item in equipment:
        values = (item.description, item.model_number)
        if any(
            len(value.strip()) >= 4 and value.strip().casefold() in folded
            for value in values
        ):
            candidates.append(item)
    return candidates[:10]


def analyze_equipment_steps(
    content: dict[str, Any],
    equipment: Iterable[EquipmentRegistry],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    registry = list(equipment)
    by_id = {item.equipment_id.casefold(): item for item in registry}
    serial_map: dict[str, list[EquipmentRegistry]] = {}
    for item in registry:
        for alias in _serial_aliases(item.serial_number):
            serial_map.setdefault(alias.casefold(), []).append(item)

    checks: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    prior_matches: dict[int, EquipmentRegistry] = {}
    for step in content.get("steps", []):
        review_step = int(step["review_step"])
        description = _normalized(step.get("description"))
        expected = _normalized(step.get("expected"))
        raw_actual = str(step.get("actual") or "")
        actual = _normalized(raw_actual)
        requirement_text = f"{description}\n{expected}"
        combined_text = f"{requirement_text}\n{actual}"
        strong_requirement = bool(_STRONG_REQUIREMENT_RE.search(requirement_text))
        device_hint = bool(_DEVICE_HINT_RE.search(combined_text))
        reported_identifiers = _reported_identifiers(raw_actual)
        mentioned_asset_ids = list(
            dict.fromkeys(match.upper() for match in _ASSET_ID_RE.findall(actual))
        )
        matched: dict[int, EquipmentRegistry] = {}
        matched_by: dict[int, set[str]] = {}
        for item in registry:
            if _contains_identifier(actual, item.equipment_id):
                matched[item.id] = item
                matched_by.setdefault(item.id, set()).add("equipment_id")
        for alias, rows in serial_map.items():
            if not _contains_identifier(actual, alias):
                continue
            for item in rows:
                matched[item.id] = item
                matched_by.setdefault(item.id, set()).add("serial_number")

        unknown_asset_ids = [
            identifier
            for identifier in mentioned_asset_ids
            if identifier.casefold() not in by_id
        ]
        execution_date = _execution_date(step, content)
        reported_range = _reported_calibration_range(raw_actual)
        reported_asset_ranges = _reported_asset_ranges(raw_actual)
        candidates = _candidate_equipment(combined_text, registry)
        candidate_ids = {item.id for item in candidates}
        candidates.extend(
            item for item_id, item in prior_matches.items() if item_id not in candidate_ids
        )
        explicit_id_matches = {
            equipment_id
            for equipment_id, methods in matched_by.items()
            if "equipment_id" in methods
        }
        serial_only_matches = {
            equipment_id
            for equipment_id, methods in matched_by.items()
            if "serial_number" in methods and "equipment_id" not in methods
        }
        identifier_conflict = bool(explicit_id_matches and serial_only_matches)
        check = {
            "review_step": review_step,
            "status": "not_applicable",
            "code": "not_applicable",
            "summary": "No controlled equipment requires registry checks.",
            "required": strong_requirement,
            "execution_date": execution_date.isoformat() if execution_date else None,
            "reported_identifiers": reported_identifiers,
            "unknown_identifiers": unknown_asset_ids,
            "reported_calibration_range": (
                [reported_range[0].isoformat(), reported_range[1].isoformat()]
                if reported_range
                else None
            ),
            "reported_calibration_ranges": [
                {
                    "equipment_id": equipment_id,
                    "start": calibration_range[0].isoformat(),
                    "end": calibration_range[1].isoformat(),
                }
                for equipment_id, calibration_range in reported_asset_ranges.items()
            ],
            "matches": [],
            "warnings": [],
        }
        if matched:
            check = _evaluate_matches(
                check,
                list(matched.values()),
                matched_by,
                execution_date,
                reported_range,
                reported_asset_ranges,
            )
            if identifier_conflict:
                explicit_ids = [matched[item_id].equipment_id for item_id in explicit_id_matches]
                serial_ids = [matched[item_id].equipment_id for item_id in serial_only_matches]
                check["status"] = "fail"
                check["code"] = "equipment_identifier_conflict"
                check["summary"] = (
                    f"Equipment ID {', '.join(explicit_ids)} and the recorded serial "
                    f"number point to different registry devices "
                    f"{', '.join(serial_ids)}."
                )
            elif unknown_asset_ids:
                check["status"] = "fail"
                check["code"] = "equipment_not_found"
                check["summary"] = (
                    "These equipment IDs are not in the registry: "
                    + ", ".join(unknown_asset_ids)
                )
        elif unknown_asset_ids and strong_requirement:
            check.update(
                status="fail",
                code="equipment_not_found",
                summary="These equipment IDs are not in the registry: "
                + ", ".join(unknown_asset_ids),
            )
        elif strong_requirement:
            if reported_identifiers:
                check.update(
                    status="fail",
                    code="equipment_not_found",
                    summary="Equipment identifiers were recorded but cannot be "
                    "matched in the registry: "
                    + ", ".join(reported_identifiers),
                )
            else:
                check.update(
                    status="fail",
                    code="equipment_missing",
                    summary="The Step requires equipment and calibration records, "
                    "but Actual records no identifiable equipment.",
                )
        elif device_hint or reported_identifiers or unknown_asset_ids:
            check.update(
                status="manual",
                code="equipment_role_ambiguous",
                summary="It is unclear whether the Step records controlled equipment "
                "or a DUT/other identifier.",
            )
            ambiguous.append(
                {
                    "step": review_step,
                    "description": description[:600],
                    "expected": expected[:600],
                    "actual": actual[:800],
                    "reported_identifiers": reported_identifiers + unknown_asset_ids,
                    "previously_matched_equipment_ids": [
                        item.equipment_id for item in prior_matches.values()
                    ],
                    "candidate_equipment": [
                        {
                            "equipment_id": item.equipment_id,
                            "description": item.description,
                            "model_number": item.model_number,
                            "serial_number": item.serial_number,
                        }
                        for item in candidates
                    ],
                }
            )
        prior_matches.update(matched)
        checks.append(check)
    return checks, ambiguous


def _evaluate_matches(
    check: dict[str, Any],
    matched: list[EquipmentRegistry],
    matched_by: dict[int, set[str]],
    execution_date: date | None,
    reported_range: tuple[date, date] | None,
    reported_asset_ranges: dict[str, tuple[date, date]] | None = None,
) -> dict[str, Any]:
    snapshots = []
    failures: list[str] = []
    manuals: list[str] = []
    available_ranges = reported_asset_ranges or {}
    for item in matched:
        snapshot = _equipment_snapshot(item)
        snapshot["matched_by"] = sorted(matched_by[item.id])
        item_range = available_ranges.get(item.equipment_id)
        if item_range is None and len(matched) == 1:
            item_range = reported_range
        snapshot["reported_calibration_range"] = (
            [item_range[0].isoformat(), item_range[1].isoformat()]
            if item_range
            else None
        )
        snapshots.append(snapshot)
        if execution_date is None:
            manuals.append(
                f"{item.equipment_id} has no parsable Step execution date"
            )
        elif _calibration_not_required(item):
            pass
        elif item.calibration_date is None or item.calibration_due_date is None:
            manuals.append(
                f"{item.equipment_id} has no calibration period in the registry"
            )
        elif not item.calibration_date <= execution_date <= item.calibration_due_date:
            failures.append(
                f"{item.equipment_id} execution date {execution_date.isoformat()} is "
                f"outside the calibration period "
                f"{item.calibration_date.isoformat()} to "
                f"{item.calibration_due_date.isoformat()}"
            )
        status_is_not_in_use = (
            item.equipment_status
            and "in use" not in item.equipment_status.casefold()
            and "使用中" not in item.equipment_status
        )
        if status_is_not_in_use:
            check["warnings"].append(
                {
                    "type": "equipment_status",
                    "summary": (
                        f"{item.equipment_id} is currently {item.equipment_status}; "
                        "this status does not describe the execution day."
                    ),
                }
            )

    for item in matched:
        item_range = available_ranges.get(item.equipment_id)
        if item_range is None and len(matched) == 1:
            item_range = reported_range
        if item_range is None:
            continue
        if item.calibration_date and item.calibration_due_date:
            expected_range = (item.calibration_date, item.calibration_due_date)
            if item_range != expected_range:
                failures.append(
                    f"{item.equipment_id} calibration period recorded in Actual "
                    f"{item_range[0].isoformat()} to {item_range[1].isoformat()} "
                    f"does not match the registry "
                    f"{item.calibration_date.isoformat()} to "
                    f"{item.calibration_due_date.isoformat()}"
                )
        if execution_date and not item_range[0] <= execution_date <= item_range[1]:
            failures.append(
                f"{item.equipment_id} Step execution date is outside the calibration "
                "period recorded in Actual"
            )

    check["matches"] = snapshots
    if failures:
        check.update(status="fail", code="equipment_invalid", summary="; ".join(failures))
    elif manuals:
        check.update(
            status="manual", code="equipment_date_unknown", summary="; ".join(manuals)
        )
    else:
        check.update(
            status="pass",
            code="equipment_valid",
            summary=(
                f"Verified {len(matched)} device(s); identifiers and execution dates "
                "match the registry."
            ),
        )
    return check


def apply_equipment_disambiguation(
    content: dict[str, Any],
    checks: list[dict[str, Any]],
    decisions: dict[int, dict[str, Any]],
    equipment: Iterable[EquipmentRegistry],
) -> list[dict[str, Any]]:
    by_id = {item.equipment_id: item for item in equipment}
    step_by_number = {int(step["review_step"]): step for step in content.get("steps", [])}
    for check in checks:
        decision = decisions.get(check["review_step"])
        if decision is None:
            continue
        check["disambiguation"] = decision
        if decision["role"] == "dut_or_other":
            check.update(
                status="not_applicable",
                code="not_applicable",
                summary="AI disambiguation decided the identifier belongs to a DUT "
                "or a non-controlled object.",
            )
            continue
        if decision["role"] == "uncertain":
            check.update(
                status="manual",
                code="equipment_role_ambiguous",
                summary="AI could not confirm the equipment role." + (
                    f" {decision['reason']}" if decision["reason"] else ""
                ),
            )
            continue
        selected = [by_id[identifier] for identifier in decision["selected_equipment_ids"]]
        step = step_by_number[check["review_step"]]
        raw_actual = str(step.get("actual") or "")
        if selected:
            matched_by = {item.id: {"ai_disambiguation"} for item in selected}
            _evaluate_matches(
                check,
                selected,
                matched_by,
                _execution_date(step, content),
                _reported_calibration_range(raw_actual),
                _reported_asset_ranges(raw_actual),
            )
        elif check["reported_identifiers"] or check["unknown_identifiers"]:
            identifiers = check["reported_identifiers"] + check["unknown_identifiers"]
            check.update(
                status="fail",
                code="equipment_not_found",
                summary="AI confirmed controlled equipment, but the identifiers "
                "cannot be matched in the registry: "
                + ", ".join(dict.fromkeys(identifiers)),
            )
        elif decision["required"]:
            check.update(
                status="fail",
                code="equipment_missing",
                summary="AI confirmed the Step requires controlled equipment, but "
                "Actual records no equipment identifier.",
            )
        else:
            check.update(
                status="manual",
                code="equipment_unidentified",
                summary="Controlled equipment semantics were detected, but there is "
                "no identifier sufficient for a registry check.",
            )
    return checks
