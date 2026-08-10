import json
from datetime import date

import pytest

from app.models import EquipmentRegistry
from app.services.equipment_review import (
    _serial_aliases,
    analyze_equipment_steps,
    apply_equipment_disambiguation,
    parse_equipment_disambiguation,
)


def equipment(
    equipment_id: str,
    serial_number: str,
    *,
    description: str = "ECG simulator",
    calibration_date: date = date(2025, 10, 29),
    calibration_due_date: date = date(2026, 10, 28),
    equipment_status: str = "使用中 In Use",
    equipment_pk: int = 1,
) -> EquipmentRegistry:
    return EquipmentRegistry(
        id=equipment_pk,
        equipment_id=equipment_id,
        description=description,
        manufacturer="FLUKE",
        model_number="ProSim2",
        accuracy_class="",
        measurement_range="",
        serial_number=serial_number,
        calibration_date=calibration_date,
        calibration_due_date=calibration_due_date,
        received_date=date(2025, 10, 31),
        calibration_interval="12",
        subordinate_area="CT RD area",
        equipment_user="Tester",
        equipment_status=equipment_status,
        calibration_location="External",
        instruction_number="",
        source_filename="master.xlsx",
        source_sheet="Master Calibration List",
        source_row=10,
    )


def review_content(
    actual: str,
    *,
    description: str = "Record the SN and Calibration Date of the ECG simulator",
    expected: str = "ECG simulator SN:__; Calibration Date:__",
    execution_date: str = "2026-08-01",
) -> dict:
    return {
        "execution_date": execution_date,
        "steps": [
            {
                "review_step": 1,
                "execution_date": execution_date,
                "description": description,
                "expected": expected,
                "actual": actual,
            }
        ],
    }


def test_exact_equipment_id_and_calibration_range_pass() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "ECG simulator SN: PCCSY-RD-CT-1-0175; "
        "Calibration Date From: 2025/10/29 to 2026/10/28"
    )

    checks, ambiguous = analyze_equipment_steps(content, registry)

    assert ambiguous == []
    assert checks[0]["status"] == "pass"
    assert checks[0]["matches"][0]["equipment_id"] == "PCCSY-RD-CT-1-0175"
    assert checks[0]["execution_date"] == "2026-08-01"


def test_required_equipment_without_actual_identifier_fails() -> None:
    checks, ambiguous = analyze_equipment_steps(review_content("Not recorded"), [])

    assert ambiguous == []
    assert checks[0]["status"] == "fail"
    assert checks[0]["code"] == "equipment_missing"


def test_unknown_asset_id_fails_when_equipment_is_required() -> None:
    content = review_content(
        "ECG simulator SN: PCCSY-RD-CT-9-9999; "
        "Calibration Date From: 2025/10/29 to 2026/10/28"
    )

    checks, ambiguous = analyze_equipment_steps(content, [])

    assert ambiguous == []
    assert checks[0]["status"] == "fail"
    assert checks[0]["code"] == "equipment_not_found"
    assert checks[0]["unknown_identifiers"] == ["PCCSY-RD-CT-9-9999"]


def test_execution_outside_calibration_window_fails() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "ECG simulator SN: PCCSY-RD-CT-1-0175",
        execution_date="2026-11-01",
    )

    checks, _ = analyze_equipment_steps(content, registry)

    assert checks[0]["status"] == "fail"
    assert "不在校准有效期" in checks[0]["summary"]


def test_reported_calibration_range_must_match_registry() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "ECG simulator SN: PCCSY-RD-CT-1-0175; "
        "Calibration Date From: 2025/10/28 to 2026/10/27"
    )

    checks, _ = analyze_equipment_steps(content, registry)

    assert checks[0]["status"] == "fail"
    assert "与台账" in checks[0]["summary"]


def test_current_non_use_status_is_warning_not_historical_failure() -> None:
    registry = [
        equipment(
            "PCCSY-RD-CT-1-0076",
            "PCCSY-RD-CT-1-0076",
            description="Stop watch",
            equipment_status="校验中In Calibration",
        )
    ]
    content = review_content(
        "Stop Watch SN: PCCSY-RD-CT-1-0076",
        description="Record the SN and Calibration Date of the Stop Watch",
        expected="Stop Watch SN:__; Calibration Date:__",
    )

    checks, _ = analyze_equipment_steps(content, registry)

    assert checks[0]["status"] == "pass"
    assert checks[0]["warnings"][0]["type"] == "equipment_status"


def test_equipment_id_and_other_equipment_serial_is_conflict() -> None:
    registry = [
        equipment("PCCSY-RD-CT-1-0175", "00850540007089", equipment_pk=1),
        equipment(
            "PCCSY-RD-CT-1-0076",
            "STOPWATCH-7788",
            description="Stop watch",
            equipment_pk=2,
        ),
    ]
    content = review_content(
        "ECG simulator SN: PCCSY-RD-CT-1-0175; Serial Number: STOPWATCH-7788"
    )

    checks, _ = analyze_equipment_steps(content, registry)

    assert checks[0]["status"] == "fail"
    assert checks[0]["code"] == "equipment_identifier_conflict"


def test_dut_disambiguation_makes_step_not_applicable() -> None:
    content = review_content(
        "PIM SN: CN52105243",
        description="Record the PIM SN",
        expected="PIM SN:__",
    )
    checks, ambiguous = analyze_equipment_steps(content, [])
    decisions = parse_equipment_disambiguation(
        json.dumps(
            {
                "steps": [
                    {
                        "step": 1,
                        "role": "dut_or_other",
                        "required": False,
                        "selected_equipment_ids": [],
                        "reason": "PIM is the product under test.",
                    }
                ]
            }
        ),
        ambiguous,
    )

    apply_equipment_disambiguation(content, checks, decisions, [])

    assert checks[0]["status"] == "not_applicable"


def test_disambiguation_cannot_invent_equipment_id() -> None:
    ambiguous = [
        {
            "step": 1,
            "candidate_equipment": [
                {
                    "equipment_id": "PCCSY-RD-CT-1-0175",
                    "description": "ECG simulator",
                    "model_number": "ProSim2",
                    "serial_number": "00850540007089",
                }
            ],
        }
    ]
    response = json.dumps(
        {
            "steps": [
                {
                    "step": 1,
                    "role": "controlled_equipment",
                    "required": True,
                    "selected_equipment_ids": ["INVENTED-DEVICE"],
                    "reason": "",
                }
            ]
        }
    )

    with pytest.raises(ValueError, match="unavailable equipment ID"):
        parse_equipment_disambiguation(response, ambiguous)


def test_calibration_tool_does_not_match_to_inside_tool_as_date_requirement() -> None:
    content = review_content(
        "The inner laser calibration tool was installed successfully.",
        description="Install the inner laser calibration tool.",
        expected="The calibration tool should be installed successfully.",
    )

    checks, ambiguous = analyze_equipment_steps(content, [])

    assert checks[0]["status"] == "manual"
    assert ambiguous[0]["step"] == 1


def test_laser_calibration_process_does_not_require_equipment_record() -> None:
    content = review_content(
        "The calibration was finished without errors.",
        description="According to the prompt, complete the Laser Calibration.",
        expected="The calibration should be finished without errors.",
    )

    checks, ambiguous = analyze_equipment_steps(content, [])

    assert ambiguous == []
    assert checks[0]["status"] == "not_applicable"


def test_serial_field_stops_at_line_break_before_next_equipment() -> None:
    content = review_content(
        "CCT pedal SN:_20-02-30_\n"
        "Biopsy phantom equipment ID:_PCCSY-RD-CT-0-0048_\n"
        "Calibration due date:_Non-Calibration equipment_"
    )

    checks, _ = analyze_equipment_steps(content, [])

    assert checks[0]["reported_identifiers"] == ["20-02-30"]
    assert checks[0]["unknown_identifiers"] == ["PCCSY-RD-CT-0-0048"]


def test_component_serial_aliases_exclude_plain_words() -> None:
    aliases = _serial_aliases(
        "X2 Base Unit: 265187; X2 CT Sensor: 248704; X2 R/F Sensor: 266532"
    )

    assert "Base" not in aliases
    assert "Unit" not in aliases
    assert "Sensor" not in aliases
    assert {"265187", "248704", "266532"} <= aliases


def test_previous_step_match_is_available_to_later_disambiguation() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = {
        "execution_date": "2026-08-01",
        "steps": [
            {
                "review_step": 1,
                "execution_date": "2026-08-01",
                "description": "Record the SN and Calibration Date of the ECG simulator",
                "expected": "ECG simulator SN:__; Calibration Date:__",
                "actual": "ECG simulator SN: PCCSY-RD-CT-1-0175",
            },
            {
                "review_step": 2,
                "execution_date": "2026-08-01",
                "description": "Set the ECG simulator HR to 60 BPM",
                "expected": "HR:__",
                "actual": "HR: 60 BPM",
            },
        ],
    }

    _, ambiguous = analyze_equipment_steps(content, registry)

    assert ambiguous[0]["previously_matched_equipment_ids"] == [
        "PCCSY-RD-CT-1-0175"
    ]
    assert ambiguous[0]["candidate_equipment"][0]["equipment_id"] == (
        "PCCSY-RD-CT-1-0175"
    )


def test_each_equipment_calibration_range_is_checked_in_multi_device_step() -> None:
    registry = [
        equipment("PCCSY-RD-CT-1-0175", "00850540007089", equipment_pk=1),
        equipment(
            "PCCSY-RD-CT-1-0076",
            "PCCSY-RD-CT-1-0076",
            description="Stop watch",
            calibration_date=date(2026, 7, 14),
            calibration_due_date=date(2027, 7, 13),
            equipment_pk=2,
        ),
    ]
    content = review_content(
        "ECG simulator SN:___PCCSY-RD-CT-1-0175___;\n"
        "ECG simulator Calibration Date\n"
        "From:__2025/10/29___to__2026/10/28___\n"
        "Stop Watch SN:___PCCSY-RD-CT-1-0076___;\n"
        "Stop watch Calibration Date:___2026-07-15~2027-07-13___"
    )

    checks, _ = analyze_equipment_steps(content, registry)

    assert checks[0]["status"] == "fail"
    assert "PCCSY-RD-CT-1-0076" in checks[0]["summary"]
    assert "与台账" in checks[0]["summary"]
    stop_watch = next(
        item
        for item in checks[0]["matches"]
        if item["equipment_id"] == "PCCSY-RD-CT-1-0076"
    )
    assert stop_watch["reported_calibration_range"] == ["2026-07-15", "2027-07-13"]
