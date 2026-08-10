from __future__ import annotations

import json
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
            "summary": "未检测到需要台账核验的受控设备。",
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
                    f"设备编号 {', '.join(explicit_ids)} 与填写的 Serial Number "
                    f"指向不同台账设备 {', '.join(serial_ids)}。"
                )
            elif unknown_asset_ids:
                check["status"] = "fail"
                check["code"] = "equipment_not_found"
                check["summary"] = (
                    "以下设备编号不在台账中：" + ", ".join(unknown_asset_ids)
                )
        elif unknown_asset_ids and strong_requirement:
            check.update(
                status="fail",
                code="equipment_not_found",
                summary="以下设备编号不在台账中：" + ", ".join(unknown_asset_ids),
            )
        elif strong_requirement:
            if reported_identifiers:
                check.update(
                    status="fail",
                    code="equipment_not_found",
                    summary="已填写设备标识，但无法在台账中匹配："
                    + ", ".join(reported_identifiers),
                )
            else:
                check.update(
                    status="fail",
                    code="equipment_missing",
                    summary="步骤要求记录设备及校准信息，但 Actual 未填写可识别的设备标识。",
                )
        elif device_hint or reported_identifiers or unknown_asset_ids:
            check.update(
                status="manual",
                code="equipment_role_ambiguous",
                summary="无法确定该步骤记录的是受控设备还是 DUT/其他标识。",
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
            manuals.append(f"{item.equipment_id} 缺少可解析的步骤执行日期")
        elif _calibration_not_required(item):
            pass
        elif item.calibration_date is None or item.calibration_due_date is None:
            manuals.append(f"{item.equipment_id} 台账缺少校准起止日期")
        elif not item.calibration_date <= execution_date <= item.calibration_due_date:
            failures.append(
                f"{item.equipment_id} 执行日期 {execution_date.isoformat()} 不在校准有效期 "
                f"{item.calibration_date.isoformat()} 至 {item.calibration_due_date.isoformat()} 内"
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
                        f"{item.equipment_id} 当前状态为 {item.equipment_status}；"
                        "该状态不代表执行当天状态。"
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
                    f"{item.equipment_id} 在 Actual 记录的校准期 "
                    f"{item_range[0].isoformat()} 至 {item_range[1].isoformat()} 与台账 "
                    f"{item.calibration_date.isoformat()} 至 "
                    f"{item.calibration_due_date.isoformat()} 不一致"
                )
        if execution_date and not item_range[0] <= execution_date <= item_range[1]:
            failures.append(
                f"{item.equipment_id} 的步骤执行日期不在 Actual 记录的校准有效期内"
            )

    check["matches"] = snapshots
    if failures:
        check.update(status="fail", code="equipment_invalid", summary="；".join(failures))
    elif manuals:
        check.update(status="manual", code="equipment_date_unknown", summary="；".join(manuals))
    else:
        check.update(
            status="pass",
            code="equipment_valid",
            summary=f"已核验 {len(matched)} 台设备，设备标识及执行日期均符合台账。",
        )
    return check


def equipment_disambiguation_prompt(ambiguous: list[dict[str, Any]]) -> str:
    return (
        "You classify equipment references for an ALM test review. Return JSON only with "
        'shape {"steps":[{"step":1,"role":"controlled_equipment|dut_or_other|uncertain",'
        '"required":true,"selected_equipment_ids":[],"reason":"..."}]}. '
        "Use only candidate equipment IDs supplied for that step; never invent an ID. "
        "A DUT/product serial is not controlled equipment. A simulator, meter, stopwatch, "
        "analyzer, phantom or calibrated test tool is controlled equipment. `required` means "
        "the Description/Expected requires recording controlled-equipment identity. Equipment "
        "selected in a previous Step may remain in use in a later Step; use the supplied "
        "previously_matched_equipment_ids only when the later Step refers to the same device.\n\n"
        + json.dumps(ambiguous, ensure_ascii=False, indent=2)
    )


def parse_equipment_disambiguation(
    response_content: str,
    ambiguous: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    value = response_content.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        value = "\n".join(lines[1:-1]).strip()
    parsed = json.loads(value)
    raw_steps = parsed.get("steps") if isinstance(parsed, dict) else None
    if not isinstance(raw_steps, list):
        raise ValueError("Equipment disambiguation response must contain a steps array.")
    expected = {item["step"]: item for item in ambiguous}
    if {item.get("step") for item in raw_steps if isinstance(item, dict)} != set(expected):
        raise ValueError("Equipment disambiguation steps do not match the requested steps.")
    decisions: dict[int, dict[str, Any]] = {}
    for item in raw_steps:
        step = item["step"]
        role = str(item.get("role", "")).strip()
        required = item.get("required")
        selected = item.get("selected_equipment_ids", [])
        reason = _normalized(item.get("reason"))[:300]
        if role not in {"controlled_equipment", "dut_or_other", "uncertain"}:
            raise ValueError(f"Invalid equipment role {role!r}.")
        if not isinstance(required, bool) or not isinstance(selected, list):
            raise ValueError("Equipment disambiguation required/selected fields are invalid.")
        allowed = {
            candidate["equipment_id"]
            for candidate in expected[step]["candidate_equipment"]
        }
        invalid_selection = any(
            not isinstance(identifier, str) or identifier not in allowed
            for identifier in selected
        )
        if invalid_selection:
            raise ValueError("Equipment disambiguation selected an unavailable equipment ID.")
        decisions[step] = {
            "role": role,
            "required": required,
            "selected_equipment_ids": selected,
            "reason": reason,
        }
    return decisions


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
                summary="AI 消歧判定该标识属于 DUT 或非受控设备。",
            )
            continue
        if decision["role"] == "uncertain":
            check.update(
                status="manual",
                code="equipment_role_ambiguous",
                summary="AI 无法确认设备角色，需要人工审核。" + (
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
                summary="AI 确认这是受控设备，但标识无法在台账中匹配："
                + ", ".join(dict.fromkeys(identifiers)),
            )
        elif decision["required"]:
            check.update(
                status="fail",
                code="equipment_missing",
                summary="AI 确认步骤要求记录受控设备，但 Actual 未填写设备标识。",
            )
        else:
            check.update(
                status="manual",
                code="equipment_unidentified",
                summary="检测到受控设备语义，但没有足够标识用于台账核验。",
            )
    return checks
