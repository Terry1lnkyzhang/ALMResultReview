from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Literal

from app.models import EquipmentRegistry

_ASSET_ID_RE = re.compile(
    r"(?<![A-Z0-9])[A-Z0-9]{2,}-RD-[A-Z0-9]+-\d+-[A-Z0-9]+(?![A-Z0-9])",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"(?<![\d/.])(?P<year>20\d{2})(?P<separator>[-/.])"
    r"(?P<month>0?[1-9]|1[0-2])(?P=separator)"
    r"(?P<day>0?[1-9]|[12]\d|3[01])(?![\d/.])"
)
_US_DATE_RE = re.compile(
    r"(?<![\d/.])(?P<month>0?[1-9]|1[0-2])/"
    r"(?P<day>0?[1-9]|[12]\d|3[01])/(?P<year>20\d{2})(?![\d/.])"
)
# A comma ends the value: `SN: F53331-0046, REV: A` records one serial, not `0046, REV: A`.
_SERIAL_FIELD_RE = re.compile(
    r"(?i)(?:\bS/?N\b|serial\s*(?:number|no\.?)?|序列号)\s*[:=]\s*"
    r"_*(?P<value>[^;,\r\n]+?)_*"
    r"(?=$|[;,\r\n]|\s+(?:and\s+)?(?:S/?N|serial\s*(?:number|no\.?))\s*[:=])"
)
_PART_FIELD_RE = re.compile(
    r"(?i)(?:\bP/?N\b|part\s*(?:number|no\.?)|型号|部件号)\s*[:=]\s*"
    r"_*(?P<value>[^;,\r\n]+?)_*(?=$|[;,\r\n])"
)
_DUE_DATE_LABEL_RE = re.compile(
    r"(?i)(?:calibration\s*)?due\s*date|(?:校准|校验)?(?:到期日?期?|有效期至?)"
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
_NAME_NOISE_RE = re.compile(r"[\s\-_/.,;:()\[\]{}'\"，。、（）]+")
_INVALID_SERIALS = {"", "na", "n/a", "none", "unknown", "待填"}
_NO_CALIBRATION_INTERVALS = {"no calibration required", "no need calibration"}


@dataclass(frozen=True)
class Candidate:
    equipment_id: str
    description: str
    model_number: str
    serial_number: str

    @classmethod
    def of(cls, item: EquipmentRegistry) -> Candidate:
        return cls(
            equipment_id=item.equipment_id,
            description=item.description,
            model_number=item.model_number,
            serial_number=item.serial_number,
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "equipment_id": self.equipment_id,
            "description": self.description,
            "model_number": self.model_number,
            "serial_number": self.serial_number,
        }


@dataclass(frozen=True)
class OpenQuestion:
    """A Step the deterministic layer could not settle, addressed to the second pass.

    `kind` decides whether the request needs the registry name vocabulary.
    """

    review_step: int
    kind: Literal["role", "name_mapping"]
    description: str
    expected: str
    actual: str
    reported_identifiers: tuple[str, ...] = ()
    device_names: tuple[str, ...] = ()
    previously_matched_equipment_ids: tuple[str, ...] = ()
    candidates: tuple[Candidate, ...] = ()

    def asking_for_names(self, names: Sequence[str]) -> OpenQuestion:
        return replace(self, kind="name_mapping", device_names=tuple(names))


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


def _dates_in(value: str) -> list[date]:
    matches = [
        (match.start(), match)
        for pattern in (_DATE_RE, _US_DATE_RE)
        for match in pattern.finditer(value)
    ]
    return [
        date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
        for _, match in sorted(matches, key=lambda item: item[0])
    ]


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


def _matches_equipment_identity(value: str, equipment: EquipmentRegistry) -> bool:
    identity = _normalized(value).casefold()
    return identity == equipment.equipment_id.casefold() or any(
        identity == alias.casefold() for alias in _serial_aliases(equipment.serial_number)
    )


def _unrecognized_equipment_summary(
    identifiers: Iterable[str], part_numbers: Iterable[str]
) -> str:
    details = "identifier(s): " + ", ".join(dict.fromkeys(identifiers))
    unique_part_numbers = list(dict.fromkeys(part_numbers))
    if unique_part_numbers:
        details += "; reported part number(s): " + ", ".join(unique_part_numbers)
    return (
        "Verified the matched equipment, but additional equipment data could not "
        f"be confirmed against the registry ({details})."
    )


def _labelled_values(pattern: re.Pattern[str], actual: str) -> list[str]:
    values: list[str] = []
    for match in pattern.finditer(actual):
        value = match.group("value").strip(" _:\t.")
        if value and not _PLACEHOLDER_RE.fullmatch(value):
            values.append(value)
    return list(dict.fromkeys(values))


def _reported_identifiers(actual: str) -> list[str]:
    return _labelled_values(_SERIAL_FIELD_RE, actual)


def _reported_part_numbers(actual: str) -> list[str]:
    return _labelled_values(_PART_FIELD_RE, actual)


def _reported_due_date(actual: str) -> date | None:
    # Many Steps only record the due date, so the full period comparison never fires.
    for label in _DUE_DATE_LABEL_RE.finditer(actual):
        segment = re.split(r"[;\r\n]", actual[label.end() : label.end() + 60], maxsplit=1)[0]
        dates = _dates_in(segment)
        if dates:
            return dates[-1]
    return None


def _reported_calibration_range(actual: str) -> tuple[date, date] | None:
    if not _STRONG_REQUIREMENT_RE.search(actual):
        return None
    dates = _dates_in(actual)
    return (dates[0], dates[1]) if len(dates) >= 2 else None


def _reported_asset_ranges(actual: str) -> dict[str, tuple[date, date]]:
    matches = list(_ASSET_ID_RE.finditer(actual))
    ranges: dict[str, tuple[date, date]] = {}
    for index, match in enumerate(matches):
        segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(actual)
        segment = actual[match.end() : segment_end]
        dates = _dates_in(segment)
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


def registry_equipment_names(equipment: Iterable[EquipmentRegistry]) -> list[str]:
    """Closed vocabulary of device names offered to the second pass."""
    names = {_normalized(item.description) for item in equipment}
    return sorted(name for name in names if name)


def _name_key(value: str) -> str:
    # `Stop watch`, `Stop-Watch` and `stopwatch` name the same registry device.
    return _NAME_NOISE_RE.sub("", value.casefold())


def _equipment_name_aliases(description: str) -> tuple[str, ...]:
    normalized = _normalized(description)
    if not normalized:
        return ()
    parenthetical = re.findall(r"[（(]([^）)]+)[）)]", normalized)
    outside = re.sub(r"[（(][^）)]+[）)]", " ", normalized).strip(" -_/；;")
    return tuple(dict.fromkeys([normalized, *parenthetical, outside]))


def _latin_name_tokens(value: str) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9]+", value)
        if len(token) >= 3
    }


def _verdict_signature(equipment: EquipmentRegistry) -> tuple[Any, ...]:
    # Rows that agree on these fields produce the same verdict, so a name that
    # matches several of them is still decisive.
    return (
        equipment.calibration_date,
        equipment.calibration_due_date,
        _calibration_not_required(equipment),
        _normalized(equipment.equipment_status).casefold(),
    )


def _candidate_equipment(
    combined_text: str,
    equipment: Iterable[EquipmentRegistry],
) -> list[EquipmentRegistry]:
    candidates: list[EquipmentRegistry] = []
    folded = combined_text.casefold()
    name_key = _name_key(combined_text)
    text_tokens = _latin_name_tokens(combined_text)
    for item in equipment:
        aliases = _equipment_name_aliases(item.description)
        name_matches = any(
            len(alias_key := _name_key(alias)) >= 4 and alias_key in name_key
            for alias in aliases
        ) or any(
            len(alias_tokens := _latin_name_tokens(alias)) >= 2
            and len(alias_tokens & text_tokens) >= 2
            for alias in aliases
        )
        model_number = _normalized(item.model_number)
        model_matches = (
            len(model_number) >= 4 and model_number.casefold() in folded
        )
        if name_matches or model_matches:
            candidates.append(item)
    return candidates[:10]


def analyze_equipment_steps(
    content: dict[str, Any],
    equipment: Iterable[EquipmentRegistry],
) -> tuple[list[dict[str, Any]], list[OpenQuestion]]:
    registry = list(equipment)
    by_id = {item.equipment_id.casefold(): item for item in registry}
    serial_map: dict[str, list[EquipmentRegistry]] = {}
    for item in registry:
        for alias in _serial_aliases(item.serial_number):
            serial_map.setdefault(alias.casefold(), []).append(item)
    model_map: dict[str, list[EquipmentRegistry]] = {}
    for item in registry:
        model_number = _normalized(item.model_number)
        if model_number and model_number.casefold() not in _INVALID_SERIALS:
            model_map.setdefault(model_number.casefold(), []).append(item)

    checks: list[dict[str, Any]] = []
    ambiguous: list[OpenQuestion] = []
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
        reported_part_numbers = _reported_part_numbers(raw_actual)
        part_number_keys = {value.casefold() for value in reported_part_numbers}
        mentioned_asset_ids = [
            identifier
            for identifier in dict.fromkeys(
                match.upper() for match in _ASSET_ID_RE.findall(actual)
            )
            # A code the Step labels P/N stays a part number even when it is
            # shaped like an asset ID.
            if identifier.casefold() not in part_number_keys
        ]
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
        if not matched:
            # A model number identifies a device only while exactly one row carries it.
            for part_number in reported_part_numbers:
                rows = model_map.get(part_number.casefold(), [])
                if len(rows) != 1:
                    continue
                matched[rows[0].id] = rows[0]
                matched_by.setdefault(rows[0].id, set()).add("model_number")

        unknown_asset_ids = [
            identifier
            for identifier in mentioned_asset_ids
            if identifier.casefold() not in by_id
        ]
        execution_date = _execution_date(step, content)
        reported_range = _reported_calibration_range(raw_actual)
        reported_due_date = _reported_due_date(raw_actual)
        reported_asset_ranges = _reported_asset_ranges(raw_actual)
        actual_candidates = _candidate_equipment(actual, registry)
        requirement_candidates = _candidate_equipment(requirement_text, registry)
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
        unrecognized_reported_identifiers = [
            identifier
            for identifier in reported_identifiers
            if not any(
                _matches_equipment_identity(identifier, item)
                for item in matched.values()
            )
        ]
        check = {
            "review_step": review_step,
            "status": "not_applicable",
            "code": "not_applicable",
            "summary": "No controlled equipment requires registry checks.",
            "required": strong_requirement,
            "execution_date": execution_date.isoformat() if execution_date else None,
            "reported_identifiers": reported_identifiers,
            "unrecognized_reported_identifiers": (
                unrecognized_reported_identifiers
            ),
            "reported_part_numbers": reported_part_numbers,
            "unknown_identifiers": unknown_asset_ids,
            "actual_candidate_equipment_ids": [
                item.equipment_id for item in actual_candidates
            ],
            "requirement_candidate_equipment_ids": [
                item.equipment_id for item in requirement_candidates
            ],
            "previously_matched_equipment_ids": [
                item.equipment_id for item in prior_matches.values()
            ],
            "reported_calibration_range": (
                [reported_range[0].isoformat(), reported_range[1].isoformat()]
                if reported_range
                else None
            ),
            "reported_due_date": (
                reported_due_date.isoformat() if reported_due_date else None
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
                reported_due_date,
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
            elif unknown_asset_ids or unrecognized_reported_identifiers:
                check["status"] = "manual"
                check["code"] = "equipment_identifier_unrecognized"
                check["summary"] = _unrecognized_equipment_summary(
                    [*unknown_asset_ids, *unrecognized_reported_identifiers],
                    reported_part_numbers,
                )
        elif (
            strong_requirement
            or device_hint
            or reported_identifiers
            or reported_part_numbers
            or unknown_asset_ids
        ):
            check.update(
                status="manual",
                code="equipment_role_ambiguous",
                summary="It is unclear whether the Step records controlled equipment "
                "or a DUT/other identifier.",
            )
            ambiguous.append(
                OpenQuestion(
                    review_step=review_step,
                    kind="role",
                    description=description[:600],
                    expected=expected[:600],
                    actual=actual[:800],
                    reported_identifiers=tuple(
                        reported_identifiers + unknown_asset_ids
                    ),
                    previously_matched_equipment_ids=tuple(
                        item.equipment_id for item in prior_matches.values()
                    ),
                    candidates=tuple(Candidate.of(item) for item in candidates),
                )
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
    reported_due_date: date | None = None,
) -> dict[str, Any]:
    snapshots = []
    failures: list[str] = []
    manuals: list[str] = []
    identity_manuals: list[str] = []
    strong_identity_methods = {
        "equipment_id",
        "serial_number",
        "model_number",
        "extracted_equipment_id",
        "extracted_serial_number",
    }
    reported_part_numbers = {
        _normalized(value).casefold()
        for value in check.get("reported_part_numbers", [])
        if _normalized(value)
    }
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
        known_identity_values = {
            _normalized(value).casefold()
            for value in (
                item.equipment_id,
                item.model_number,
                item.serial_number,
            )
            if _normalized(value)
        }
        if (
            reported_part_numbers
            and matched_by[item.id].isdisjoint(strong_identity_methods)
            and reported_part_numbers.isdisjoint(known_identity_values)
        ):
            identity_manuals.append(
                f"The recorded part number(s) "
                f"{', '.join(sorted(reported_part_numbers))} do not match the "
                f"registry identity for {item.equipment_id}"
            )
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
            # A lone due date still has to agree with the registry.
            if (
                reported_due_date is not None
                and len(matched) == 1
                and item.calibration_due_date is not None
                and reported_due_date != item.calibration_due_date
            ):
                failures.append(
                    f"{item.equipment_id} calibration due date recorded in Actual "
                    f"{reported_due_date.isoformat()} does not match the registry "
                    f"{item.calibration_due_date.isoformat()}"
                )
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

    if reported_due_date is not None and len(matched) > 1:
        # The date cannot be attributed to one device, so it only has to belong
        # to some matched device.
        known_due_dates = {
            item.calibration_due_date
            for item in matched
            if item.calibration_due_date is not None
        }
        if known_due_dates and reported_due_date not in known_due_dates:
            failures.append(
                f"The calibration due date recorded in Actual "
                f"{reported_due_date.isoformat()} matches none of the devices "
                "recorded in this Step: "
                + ", ".join(
                    sorted(value.isoformat() for value in known_due_dates)
                )
            )

    check["matches"] = snapshots
    if identity_manuals:
        check.update(
            status="manual",
            code="equipment_part_number_unverified",
            summary="; ".join(identity_manuals),
        )
    elif failures:
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


def _extraction_indexes(
    registry: list[EquipmentRegistry],
) -> tuple[
    dict[str, EquipmentRegistry],
    dict[str, list[EquipmentRegistry]],
    dict[str, list[EquipmentRegistry]],
]:
    by_id = {item.equipment_id.casefold(): item for item in registry}
    serial_index: dict[str, list[EquipmentRegistry]] = {}
    name_index: dict[str, list[EquipmentRegistry]] = {}
    for item in registry:
        for alias in _serial_aliases(item.serial_number):
            serial_index.setdefault(alias.casefold(), []).append(item)
        name = _name_key(_normalized(item.description))
        if name:
            name_index.setdefault(name, []).append(item)
    return by_id, serial_index, name_index


def _rows_for_name(
    name: str,
    name_index: dict[str, list[EquipmentRegistry]],
    check: dict[str, Any],
) -> tuple[list[EquipmentRegistry], bool]:
    """Registry rows a device name identifies, plus whether it stays ambiguous."""
    rows = name_index.get(_name_key(name), [])
    if len(rows) <= 1:
        return rows, False
    if len({_verdict_signature(item) for item in rows}) > 1:
        return [], True
    representative = min(rows, key=lambda item: item.equipment_id)
    check["warnings"].append(
        {
            "type": "equipment_name_shared",
            "summary": (
                f"The device name {name} matches several registry rows with "
                f"identical calibration data; {representative.equipment_id} "
                "represents them."
            ),
        }
    )
    return [representative], False


def apply_extracted_equipment(
    content: dict[str, Any],
    checks: list[dict[str, Any]],
    extracted: dict[int, list[dict[str, Any]]],
    equipment: Iterable[EquipmentRegistry],
) -> tuple[set[int], dict[int, list[str]]]:
    """Resolve first-pass extracted equipment fields against the registry.

    Returns the review_step numbers that are already decided, and the device names
    that only an AI mapping against the registry vocabulary can resolve.
    """
    registry = list(equipment)
    if not registry:
        return set(), {}
    by_id, serial_index, name_index = _extraction_indexes(registry)
    steps = {int(step["review_step"]): step for step in content.get("steps", [])}
    resolved: set[int] = set()
    pending: dict[int, list[str]] = {}

    for check in checks:
        review_step = int(check["review_step"])
        entries = extracted.get(review_step) or []
        step = steps.get(review_step)
        if entries:
            check["extracted_equipment"] = entries
        if not entries or step is None:
            continue
        raw_actual = str(step.get("actual") or "")
        haystack = _normalized(
            f"{step.get('description') or ''}\n"
            f"{step.get('expected') or ''}\n{raw_actual}"
        )
        matched: dict[int, EquipmentRegistry] = {}
        matched_by: dict[int, set[str]] = {}
        for snapshot in check.get("matches", []):
            item = by_id.get(_normalized(snapshot.get("equipment_id")).casefold())
            if item is None:
                continue
            matched[item.id] = item
            matched_by[item.id] = set(snapshot.get("matched_by", []))
        existing_match_ids = set(matched)
        unverified: list[str] = []
        unresolved: list[str] = []
        ambiguous_names: list[str] = []
        unmapped_names: list[str] = []
        due_dates: list[date] = []
        has_grounded_extraction = False
        haystack_key = _name_key(haystack)
        for entry in entries:
            identifier = _normalized(entry.get("equipment_id"))
            serial = _normalized(entry.get("serial_number"))
            name = _normalized(entry.get("device_name"))
            source_text = _normalized(entry.get("source_text"))
            # A batched request lets a neighbouring Step's values leak in, so each
            # field survives only while this Step's own text still carries it.
            name_verified = bool(name) and _name_key(name) in haystack_key
            trusted = bool(source_text) and source_text.casefold() in haystack.casefold()
            if not trusted:
                unverified.append(source_text[:120] or "(empty source_text)")
                identifier, serial = "", ""
            if name and not name_verified:
                unverified.append(name)
            if identifier and not _contains_identifier(haystack, identifier):
                unverified.append(identifier)
                identifier, trusted = "", False
            if serial and not _contains_identifier(haystack, serial):
                unverified.append(serial)
                serial, trusted = "", False
            if not identifier and not serial and not name_verified:
                continue
            has_grounded_extraction = True
            if trusted:
                due_date = _parse_date(
                    _normalized(entry.get("reported_calibration_due_date"))
                )
                if due_date:
                    due_dates.append(due_date)
            rows: list[EquipmentRegistry] = []
            method = ""
            if identifier:
                item = by_id.get(identifier.casefold())
                if item is None:
                    unresolved.append(identifier)
                    continue
                rows, method = [item], "extracted_equipment_id"
            elif serial:
                item = by_id.get(serial.casefold())
                if item is not None:
                    rows, method = [item], "extracted_equipment_id"
                else:
                    rows = serial_index.get(serial.casefold(), [])
                    if not rows:
                        unresolved.append(serial)
                        continue
                    method = "extracted_serial_number"
            else:
                rows, ambiguous = _rows_for_name(name, name_index, check)
                if ambiguous:
                    ambiguous_names.append(name)
                    continue
                if not rows:
                    # The registry may spell this device differently or in another
                    # language, which only the second pass can map.
                    unmapped_names.append(name)
                    continue
                method = "extracted_device_name"
            for item in rows:
                matched[item.id] = item
                matched_by.setdefault(item.id, set()).add(method)

        has_additional_result = bool(
            set(matched) - existing_match_ids
            or unresolved
            or ambiguous_names
            or unmapped_names
        )
        if check.get("matches") and (
            not has_grounded_extraction or not has_additional_result
        ):
            continue
        salvaged = matched or ambiguous_names or unmapped_names or unresolved
        if unverified and not salvaged:
            check.update(
                status="manual",
                code="equipment_extraction_unverified",
                summary="First-pass extraction reported equipment data that is not "
                "present in the Step text: " + "; ".join(dict.fromkeys(unverified)),
            )
            resolved.add(review_step)
            continue
        if matched:
            _evaluate_matches(
                check,
                list(matched.values()),
                matched_by,
                _execution_date(step, content),
                _reported_calibration_range(raw_actual),
                _reported_asset_ranges(raw_actual),
                _parse_date(str(check.get("reported_due_date") or ""))
                or (due_dates[0] if len(set(due_dates)) == 1 else None),
            )
            unrecognized = list(
                dict.fromkeys(
                    [
                        *check.get("unrecognized_reported_identifiers", []),
                        *unresolved,
                    ]
                )
            )
            if unrecognized:
                unrecognized_summary = _unrecognized_equipment_summary(
                    unrecognized,
                    check.get("reported_part_numbers", []),
                )
                if check["status"] == "pass":
                    check.update(
                        status="manual",
                        code="equipment_identifier_unrecognized",
                        summary=unrecognized_summary,
                    )
                else:
                    check["summary"] += "; " + unrecognized_summary
            resolved.add(review_step)
            continue
        if ambiguous_names:
            check.update(
                status="fail",
                code="equipment_ambiguous_name",
                summary="The device name identifies several registry devices with "
                "different calibration data, and Actual records no serial number or "
                "equipment ID: " + ", ".join(dict.fromkeys(ambiguous_names)),
            )
            resolved.add(review_step)
            continue
        if unmapped_names:
            pending_names = list(dict.fromkeys(unmapped_names))
            check["pending_device_names"] = pending_names
            pending[review_step] = pending_names
            continue
        if unresolved:
            # The first pass reads any code it finds, so a part number can arrive
            # here; only the program's own labelled-field match may fail a Step.
            check.update(
                status="manual",
                code="equipment_identifier_unrecognized",
                summary="First-pass extraction read these identifiers from the Step, "
                "and the registry lists none of them: "
                + ", ".join(dict.fromkeys(unresolved)),
            )
            resolved.add(review_step)
    return resolved, pending


def merge_pending_names(
    content: dict[str, Any],
    pending: dict[int, list[str]],
    equipment: Iterable[EquipmentRegistry],
    existing: Iterable[OpenQuestion],
) -> list[OpenQuestion]:
    """Fold the names only the registry vocabulary can map into the second-pass list."""
    registry = list(equipment)
    questions = {item.review_step: item for item in existing}
    for step in content.get("steps", []):
        review_step = int(step["review_step"])
        names = pending.get(review_step)
        if not names:
            continue
        question = questions.get(review_step)
        if question is not None:
            questions[review_step] = question.asking_for_names(names)
            continue
        description = _normalized(step.get("description"))
        expected = _normalized(step.get("expected"))
        actual = _normalized(step.get("actual"))
        candidates = _candidate_equipment(
            f"{description}\n{expected}\n{actual}", registry
        )
        questions[review_step] = OpenQuestion(
            review_step=review_step,
            kind="name_mapping",
            description=description[:600],
            expected=expected[:600],
            actual=actual[:800],
            device_names=tuple(names),
            candidates=tuple(Candidate.of(item) for item in candidates),
        )
    return [questions[key] for key in sorted(questions)]


def apply_equipment_disambiguation(
    content: dict[str, Any],
    checks: list[dict[str, Any]],
    decisions: dict[int, dict[str, Any]],
    equipment: Iterable[EquipmentRegistry],
) -> list[dict[str, Any]]:
    by_id = {item.equipment_id: item for item in equipment}
    _, _, name_index = _extraction_indexes(list(by_id.values()))
    step_by_number = {int(step["review_step"]): step for step in content.get("steps", [])}
    for check in checks:
        decision = decisions.get(check["review_step"])
        if decision is None:
            continue
        check["disambiguation"] = decision
        if decision["role"] == "dut_or_other":
            if check.get("previously_matched_equipment_ids"):
                check.update(
                    status="manual",
                    code="equipment_role_ambiguous",
                    summary=(
                        "AI classified the current reference as DUT or other, but "
                        "the Step follows previously verified equipment and may "
                        "continue to operate it. Manual confirmation is required."
                    ),
                )
            else:
                check.update(
                    status="not_applicable",
                    code="not_applicable",
                    summary="AI disambiguation decided the identifier belongs to a "
                    "DUT or a non-controlled object.",
                )
            continue
        selected_names = decision.get("selected_equipment_names", [])
        can_verify_uncertain_part_number = bool(
            decision.get("required")
            and selected_names
            and check.get("reported_part_numbers")
        )
        if decision["role"] == "uncertain" and not can_verify_uncertain_part_number:
            check.update(
                status="manual",
                code="equipment_role_ambiguous",
                summary="AI could not confirm the equipment role." + (
                    f" {decision['reason']}" if decision["reason"] else ""
                ),
            )
            continue
        selected_name_equipment_ids = {
            item.equipment_id
            for name in selected_names
            for item in name_index.get(_name_key(_normalized(name)), [])
        }
        grounded_ids = set(check.get("actual_candidate_equipment_ids", []))
        previous_ids = set(check.get("previously_matched_equipment_ids", []))
        if not check.get("pending_device_names"):
            grounded_ids.update(
                previous_ids
                & set(check.get("requirement_candidate_equipment_ids", []))
            )
        grounded_ids.update(previous_ids & selected_name_equipment_ids)
        selected = {
            item.id: item
            for item in (
                by_id[identifier] for identifier in decision["selected_equipment_ids"]
                if identifier in grounded_ids
            )
        }
        matched_by = {item_id: {"ai_disambiguation"} for item_id in selected}
        ambiguous_names: list[str] = []
        for name in selected_names:
            normalized_name = _normalized(name)
            name_rows = name_index.get(_name_key(normalized_name), [])
            selected_name_rows = [item for item in name_rows if item.id in selected]
            if selected_name_rows:
                for item in selected_name_rows:
                    matched_by[item.id].add("ai_registry_name")
                continue
            rows, ambiguous = _rows_for_name(normalized_name, name_index, check)
            if ambiguous:
                ambiguous_names.append(name)
                continue
            for item in rows:
                selected[item.id] = item
                matched_by.setdefault(item.id, set()).add("ai_registry_name")
        step = step_by_number[check["review_step"]]
        raw_actual = str(step.get("actual") or "")
        if selected:
            _evaluate_matches(
                check,
                list(selected.values()),
                matched_by,
                _execution_date(step, content),
                _reported_calibration_range(raw_actual),
                _reported_asset_ranges(raw_actual),
                _reported_due_date(raw_actual),
            )
        elif ambiguous_names:
            check.update(
                status="fail",
                code="equipment_ambiguous_name",
                summary="The device name identifies several registry devices with "
                "different calibration data, and Actual records no serial number or "
                "equipment ID: " + ", ".join(dict.fromkeys(ambiguous_names)),
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
