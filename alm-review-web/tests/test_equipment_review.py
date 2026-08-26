from datetime import date

from app.models import EquipmentRegistry
from app.services.equipment_review import (
    OpenQuestion,
    _candidate_equipment,
    _serial_aliases,
    analyze_equipment_steps,
    apply_equipment_disambiguation,
    apply_extracted_equipment,
    merge_pending_names,
    registry_equipment_names,
)


def equipment(
    equipment_id: str,
    serial_number: str,
    *,
    description: str = "ECG simulator",
    model_number: str = "ProSim2",
    calibration_date: date | None = date(2025, 10, 29),
    calibration_due_date: date | None = date(2026, 10, 28),
    calibration_interval: str = "12",
    equipment_status: str = "使用中 In Use",
    equipment_pk: int = 1,
) -> EquipmentRegistry:
    return EquipmentRegistry(
        id=equipment_pk,
        equipment_id=equipment_id,
        description=description,
        manufacturer="FLUKE",
        model_number=model_number,
        accuracy_class="",
        measurement_range="",
        serial_number=serial_number,
        calibration_date=calibration_date,
        calibration_due_date=calibration_due_date,
        received_date=date(2025, 10, 31),
        calibration_interval=calibration_interval,
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


def test_equipment_marked_no_calibration_required_passes_without_dates() -> None:
    registry = [
        equipment(
            "PCCSY-RD-CT-0-0001",
            "80508-2927",
            calibration_date=None,
            calibration_due_date=None,
            calibration_interval="No calibration required",
        )
    ]
    content = review_content("Equipment ID: PCCSY-RD-CT-0-0001")

    checks, ambiguous = analyze_equipment_steps(content, registry)

    assert ambiguous == []
    assert checks[0]["status"] == "pass"
    assert checks[0]["code"] == "equipment_valid"


def test_bilingual_registry_name_aliases_are_equipment_candidates() -> None:
    registry = [
        equipment(
            "PCCSY-RD-CT-0-0048",
            "BIOPSY-48",
            description="Biopsy Training Phantom",
            equipment_pk=1,
        ),
        equipment(
            "PCCSY-RD-CT-1-0076",
            "STOP-76",
            description="秒表(Stop watch)",
            equipment_pk=2,
        ),
        equipment(
            "PHSZ-RD-VV-0-0119",
            "11A-09",
            description="Anthropomorphic phantom (Full Body)- Sandy",
            equipment_pk=3,
        ),
    ]

    biopsy = _candidate_equipment("Position biopsy phantom", registry)
    stopwatch = _candidate_equipment("Check the stopwatch display time", registry)
    head = _candidate_equipment("Perform a scan of the head phantom", registry)

    assert [item.equipment_id for item in biopsy] == ["PCCSY-RD-CT-0-0048"]
    assert [item.equipment_id for item in stopwatch] == ["PCCSY-RD-CT-1-0076"]
    assert "PHSZ-RD-VV-0-0119" not in {item.equipment_id for item in head}


def test_selected_registry_name_disambiguates_a_previous_equipment_id() -> None:
    registry = [
        equipment(
            "PCCSY-RD-CT-1-0076",
            "STOP-76",
            description="秒表",
            equipment_pk=1,
        ),
        equipment(
            "PCCSY-RD-CT-1-0200",
            "STOP-200",
            description="秒表",
            calibration_due_date=date(2027, 4, 2),
            equipment_pk=2,
        ),
    ]
    content = review_content(
        "Time was 53.66 seconds.",
        description="Check the stopwatch display time.",
        expected="Time does not exceed 8.5 minutes.",
    )
    checks, _ = analyze_equipment_steps(content, registry)
    checks[0]["previously_matched_equipment_ids"] = ["PCCSY-RD-CT-1-0076"]
    checks[0]["pending_device_names"] = ["Stop watch"]

    apply_equipment_disambiguation(
        content,
        checks,
        {
            1: {
                "role": "controlled_equipment",
                "required": False,
                "selected_equipment_ids": ["PCCSY-RD-CT-1-0076"],
                "selected_equipment_names": ["秒表"],
                "reason": "The Step continues to use the recorded stopwatch.",
            }
        },
        registry,
    )

    assert checks[0]["status"] == "pass"
    assert checks[0]["matches"][0]["equipment_id"] == "PCCSY-RD-CT-1-0076"


def test_required_equipment_without_actual_identifier_waits_for_ai_confirmation() -> None:
    checks, ambiguous = analyze_equipment_steps(review_content("Not recorded"), [])

    assert [question.review_step for question in ambiguous] == [1]
    assert checks[0]["status"] == "manual"
    assert checks[0]["code"] == "equipment_role_ambiguous"


def test_unknown_asset_id_waits_for_ai_confirmation() -> None:
    content = review_content(
        "ECG simulator SN: PCCSY-RD-CT-9-9999; "
        "Calibration Date From: 2025/10/29 to 2026/10/28"
    )

    checks, ambiguous = analyze_equipment_steps(content, [])

    assert [question.review_step for question in ambiguous] == [1]
    assert checks[0]["status"] == "manual"
    assert checks[0]["code"] == "equipment_role_ambiguous"
    assert checks[0]["unknown_identifiers"] == ["PCCSY-RD-CT-9-9999"]


def test_ai_confirmation_turns_an_unknown_required_id_into_a_failure() -> None:
    content = review_content("ECG simulator SN: PCCSY-RD-CT-9-9999")
    checks, _ = analyze_equipment_steps(content, [])

    apply_equipment_disambiguation(
        content,
        checks,
        {
            1: {
                "role": "controlled_equipment",
                "required": True,
                "selected_equipment_ids": [],
                "selected_equipment_names": [],
                "reason": "The Step records controlled test equipment.",
            }
        },
        [],
    )

    assert checks[0]["status"] == "fail"
    assert checks[0]["code"] == "equipment_not_found"


def test_ai_confirmation_turns_a_missing_required_id_into_a_failure() -> None:
    content = review_content("Not recorded")
    checks, _ = analyze_equipment_steps(content, [])

    apply_equipment_disambiguation(
        content,
        checks,
        {
            1: {
                "role": "controlled_equipment",
                "required": True,
                "selected_equipment_ids": [],
                "selected_equipment_names": [],
                "reason": "The Step requires controlled test equipment.",
            }
        },
        [],
    )

    assert checks[0]["status"] == "fail"
    assert checks[0]["code"] == "equipment_missing"


def test_second_pass_candidate_from_requirement_does_not_prove_actual_equipment() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content("Not recorded")
    checks, _ = analyze_equipment_steps(content, registry)

    apply_equipment_disambiguation(
        content,
        checks,
        {
            1: {
                "role": "controlled_equipment",
                "required": True,
                "selected_equipment_ids": ["PCCSY-RD-CT-1-0175"],
                "selected_equipment_names": [],
                "reason": "Expected requires the registry simulator.",
            }
        },
        registry,
    )

    assert checks[0]["actual_candidate_equipment_ids"] == []
    assert checks[0]["status"] == "fail"
    assert checks[0]["code"] == "equipment_missing"


def test_second_pass_candidate_named_in_actual_can_be_verified() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "The ECG simulator was used.",
        expected="The waveform is stable.",
    )
    checks, _ = analyze_equipment_steps(content, registry)

    apply_equipment_disambiguation(
        content,
        checks,
        {
            1: {
                "role": "controlled_equipment",
                "required": True,
                "selected_equipment_ids": ["PCCSY-RD-CT-1-0175"],
                "selected_equipment_names": [],
                "reason": "Actual names the registry simulator.",
            }
        },
        registry,
    )

    assert checks[0]["actual_candidate_equipment_ids"] == [
        "PCCSY-RD-CT-1-0175"
    ]
    assert checks[0]["status"] == "pass"
    assert checks[0]["code"] == "equipment_valid"


def test_execution_outside_calibration_window_fails() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "ECG simulator SN: PCCSY-RD-CT-1-0175",
        execution_date="2026-11-01",
    )

    checks, _ = analyze_equipment_steps(content, registry)

    assert checks[0]["status"] == "fail"
    assert "outside the calibration period" in checks[0]["summary"]


def test_reported_calibration_range_must_match_registry() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "ECG simulator SN: PCCSY-RD-CT-1-0175; "
        "Calibration Date From: 2025/10/28 to 2026/10/27"
    )

    checks, _ = analyze_equipment_steps(content, registry)

    assert checks[0]["status"] == "fail"
    assert "does not match the registry" in checks[0]["summary"]


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
    decisions = {
        1: {
            "role": "dut_or_other",
            "required": False,
            "selected_equipment_ids": [],
            "reason": "PIM is the product under test.",
        }
    }

    apply_equipment_disambiguation(content, checks, decisions, [])

    assert checks[0]["status"] == "not_applicable"


def test_dut_disambiguation_with_previous_equipment_stays_manual() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "ECG Leads Disconnected displayed.",
        description="Disconnect and reconnect the ECG simulator.",
        expected="ECG Leads Disconnected displays.",
    )
    checks, _ = analyze_equipment_steps(content, registry)
    checks[0]["previously_matched_equipment_ids"] = ["PCCSY-RD-CT-1-0175"]

    apply_equipment_disambiguation(
        content,
        checks,
        {
            1: {
                "role": "dut_or_other",
                "required": False,
                "selected_equipment_ids": [],
                "selected_equipment_names": [],
                "reason": "Actual does not repeat the simulator ID.",
            }
        },
        registry,
    )

    assert checks[0]["status"] == "manual"
    assert checks[0]["code"] == "equipment_role_ambiguous"
    assert "previously verified equipment" in checks[0]["summary"]

def test_calibration_tool_does_not_match_to_inside_tool_as_date_requirement() -> None:
    content = review_content(
        "The inner laser calibration tool was installed successfully.",
        description="Install the inner laser calibration tool.",
        expected="The calibration tool should be installed successfully.",
    )

    checks, ambiguous = analyze_equipment_steps(content, [])

    assert checks[0]["status"] == "manual"
    assert ambiguous[0].review_step == 1
    assert ambiguous[0].kind == "role"


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


def test_serial_field_stops_at_comma_before_trailing_attributes() -> None:
    content = review_content(
        "SYSTEM PHANTOM P/N:459801550744, SN: F53331-0046, REV: A."
    )

    checks, _ = analyze_equipment_steps(content, [])

    assert checks[0]["reported_identifiers"] == ["F53331-0046"]
    assert checks[0]["reported_part_numbers"] == ["459801550744"]


def test_unique_part_number_matches_registry_model_number() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "NA", model_number="459801705001")]
    content = review_content("System phantom P/N: 459801705001")

    checks, ambiguous = analyze_equipment_steps(content, registry)

    assert ambiguous == []
    assert checks[0]["status"] == "pass"
    assert checks[0]["matches"][0]["matched_by"] == ["model_number"]


def test_part_number_shared_by_several_devices_is_not_a_match() -> None:
    registry = [
        equipment("PCCSY-RD-CT-1-0175", "NA", model_number="SHARED-1", equipment_pk=1),
        equipment("PCCSY-RD-CT-1-0176", "NA", model_number="SHARED-1", equipment_pk=2),
    ]
    content = review_content("System phantom P/N: SHARED-1")

    checks, ambiguous = analyze_equipment_steps(content, registry)

    assert checks[0]["matches"] == []
    assert checks[0]["status"] == "manual"
    assert checks[0]["code"] == "equipment_role_ambiguous"
    assert [question.review_step for question in ambiguous] == [1]


def test_slashed_serial_label_is_extracted() -> None:
    content = review_content("CT Injector S/N: C1221B993G")

    checks, _ = analyze_equipment_steps(content, [])

    assert checks[0]["reported_identifiers"] == ["C1221B993G"]


def test_recorded_due_date_must_match_the_registry() -> None:
    registry = [
        equipment(
            "PCCSY-RD-CT-1-0076",
            "PCCSY-RD-CT-1-0076",
            calibration_date=date(2026, 7, 14),
            calibration_due_date=date(2027, 7, 13),
        )
    ]
    content = review_content(
        "Record stop watch: S/N: PCCSY-RD-CT-1-0076; Calibration Due date:2027-07-05"
    )

    checks, _ = analyze_equipment_steps(content, registry)

    assert checks[0]["reported_due_date"] == "2027-07-05"
    assert checks[0]["status"] == "fail"
    assert checks[0]["code"] == "equipment_invalid"
    assert "2027-07-13" in checks[0]["summary"]


def test_recorded_due_date_matching_the_registry_passes() -> None:
    registry = [
        equipment(
            "PCCSY-RD-CT-1-0076",
            "PCCSY-RD-CT-1-0076",
            calibration_date=date(2026, 7, 14),
            calibration_due_date=date(2027, 7, 13),
        )
    ]
    content = review_content(
        "Record stop watch: S/N: PCCSY-RD-CT-1-0076; Calibration Due date:2027-07-13"
    )

    checks, _ = analyze_equipment_steps(content, registry)

    assert checks[0]["status"] == "pass"


def test_us_calibration_range_is_not_stitched_into_a_false_due_date() -> None:
    registry = [
        equipment(
            "PCCSY-RD-CT-1-0127",
            "13303294",
            description="Caliper",
            calibration_date=date(2025, 12, 2),
            calibration_due_date=date(2026, 12, 1),
        )
    ]
    content = review_content(
        "Caliper: PCCSY-RD-CT-1-0127; "
        "Calibration due date: 12/2/2025-12/1/2026"
    )

    checks, _ = analyze_equipment_steps(content, registry)

    assert checks[0]["reported_calibration_range"] == [
        "2025-12-02",
        "2026-12-01",
    ]
    assert checks[0]["reported_due_date"] == "2026-12-01"
    assert checks[0]["status"] == "pass"


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

    assert ambiguous[0].previously_matched_equipment_ids == (
        "PCCSY-RD-CT-1-0175",
    )
    assert ambiguous[0].candidates[0].equipment_id == "PCCSY-RD-CT-1-0175"


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
    assert "does not match the registry" in checks[0]["summary"]
    stop_watch = next(
        item
        for item in checks[0]["matches"]
        if item["equipment_id"] == "PCCSY-RD-CT-1-0076"
    )
    assert stop_watch["reported_calibration_range"] == ["2026-07-15", "2027-07-13"]


def _extracted(**overrides) -> dict:
    entry = {
        "device_name": "",
        "equipment_id": "",
        "serial_number": "",
        "reported_calibration_due_date": "",
        "source_text": "",
    }
    entry.update(overrides)
    return entry


def test_registry_equipment_names_are_distinct_and_sorted() -> None:
    registry = [
        equipment("PCCSY-RD-CT-1-0175", "0001", description="ECG simulator"),
        equipment("PCCSY-RD-CT-1-0176", "0002", description="ECG simulator"),
        equipment("PCCSY-RD-CT-1-0177", "0003", description="Stop watch"),
    ]

    assert registry_equipment_names(registry) == ["ECG simulator", "Stop watch"]


def test_extracted_name_matches_registry_when_regexes_found_nothing() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "The ECG simulator was used for the measurement.",
        description="Use the ECG simulator",
        expected="The waveform is stable.",
    )
    checks, _ = analyze_equipment_steps(content, registry)
    assert checks[0]["matches"] == []

    resolved, pending = apply_extracted_equipment(
        content,
        checks,
        {
            1: [
                _extracted(
                    device_name="ECG simulator",
                    source_text="The ECG simulator was used for the measurement.",
                )
            ]
        },
        registry,
    )

    assert resolved == {1}
    assert pending == {}
    assert checks[0]["status"] == "pass"
    assert checks[0]["matches"][0]["equipment_id"] == "PCCSY-RD-CT-1-0175"
    assert checks[0]["matches"][0]["matched_by"] == ["extracted_device_name"]


def test_shared_name_with_identical_calibration_data_still_decides() -> None:
    registry = [
        equipment("PCCSY-RD-CT-1-0175", "0001", equipment_pk=1),
        equipment("PCCSY-RD-CT-1-0176", "0002", equipment_pk=2),
    ]
    content = review_content(
        "ECG simulator was used.", expected="The waveform is stable."
    )
    checks, _ = analyze_equipment_steps(content, registry)

    apply_extracted_equipment(
        content,
        checks,
        {
            1: [
                _extracted(
                    device_name="ECG simulator",
                    source_text="ECG simulator was used.",
                )
            ]
        },
        registry,
    )

    assert checks[0]["status"] == "pass"
    assert checks[0]["matches"][0]["equipment_id"] == "PCCSY-RD-CT-1-0175"
    assert checks[0]["warnings"][0]["type"] == "equipment_name_shared"


def test_shared_name_with_different_due_dates_fails_without_an_identifier() -> None:
    registry = [
        equipment("PCCSY-RD-CT-1-0175", "0001", equipment_pk=1),
        equipment(
            "PCCSY-RD-CT-1-0176",
            "0002",
            calibration_due_date=date(2027, 4, 2),
            equipment_pk=2,
        ),
    ]
    content = review_content(
        "ECG simulator was used.", expected="The waveform is stable."
    )
    checks, _ = analyze_equipment_steps(content, registry)

    apply_extracted_equipment(
        content,
        checks,
        {
            1: [
                _extracted(
                    device_name="ECG simulator",
                    source_text="ECG simulator was used.",
                )
            ]
        },
        registry,
    )

    assert checks[0]["status"] == "fail"
    assert checks[0]["code"] == "equipment_ambiguous_name"


def test_extracted_identifier_absent_from_step_text_is_treated_as_hallucination() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "ECG simulator was used.", expected="The waveform is stable."
    )
    checks, _ = analyze_equipment_steps(content, registry)

    apply_extracted_equipment(
        content,
        checks,
        {
            1: [
                _extracted(
                    equipment_id="PCCSY-RD-CT-1-0175",
                    source_text="ECG simulator was used.",
                )
            ]
        },
        registry,
    )

    assert checks[0]["status"] == "manual"
    assert checks[0]["code"] == "equipment_extraction_unverified"


def test_extracted_source_text_absent_from_step_text_is_rejected() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "ECG simulator was used.", expected="The waveform is stable."
    )
    checks, _ = analyze_equipment_steps(content, registry)

    apply_extracted_equipment(
        content,
        checks,
        {
            1: [
                _extracted(
                    device_name="Torque wrench",
                    source_text="Calibration certificate 12345 was attached.",
                )
            ]
        },
        registry,
    )

    assert checks[0]["status"] == "manual"
    assert checks[0]["code"] == "equipment_extraction_unverified"


def test_extracted_part_number_missing_from_the_registry_is_only_manual() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "Record Body Phantom P/N: PCCSY-RD-CT-0002",
        expected="Record Body Phantom P/N: ____",
    )
    checks, _ = analyze_equipment_steps(content, registry)

    apply_extracted_equipment(
        content,
        checks,
        {
            1: [
                _extracted(
                    device_name="Body Phantom",
                    serial_number="PCCSY-RD-CT-0002",
                    source_text="Record Body Phantom P/N: PCCSY-RD-CT-0002",
                )
            ]
        },
        registry,
    )

    assert checks[0]["status"] == "manual"
    assert checks[0]["code"] == "equipment_identifier_unrecognized"


def test_unrecognized_second_device_downgrades_a_matched_step_to_manual() -> None:
    registry = [
        equipment(
            "PCCSY-RD-CT-0-0048",
            "36588.1-2",
            description="Biopsy Training Phantom",
            model_number="",
            calibration_date=None,
            calibration_due_date=None,
            calibration_interval="No calibration required",
        )
    ]
    actual = (
        "CCT pedal PN: 454110416181; SN: 20-02-30\n"
        "Biopsy phantom equipment ID: PCCSY-RD-CT-0-0048\n"
        "The calibration due date: Non-Calibration equipment"
    )
    content = review_content(
        actual,
        description="Record the CCT Pedal and biopsy phantom details.",
        expected=(
            "CCT pedal PN:__; SN:__\n"
            "Biopsy phantom equipment ID:__\n"
            "The calibration due date:__"
        ),
    )
    checks, _ = analyze_equipment_steps(content, registry)
    assert checks[0]["status"] == "manual"
    assert checks[0]["code"] == "equipment_identifier_unrecognized"
    assert checks[0]["unrecognized_reported_identifiers"] == ["20-02-30"]
    assert "20-02-30" in checks[0]["summary"]
    assert "454110416181" in checks[0]["summary"]

    apply_extracted_equipment(
        content,
        checks,
        {
            1: [
                _extracted(
                    device_name="CCT pedal",
                    serial_number="20-02-30",
                    source_text="CCT pedal PN: 454110416181; SN: 20-02-30",
                ),
                _extracted(
                    device_name="Biopsy phantom",
                    equipment_id="PCCSY-RD-CT-0-0048",
                    source_text=(
                        "Biopsy phantom equipment ID: PCCSY-RD-CT-0-0048"
                    ),
                ),
            ]
        },
        registry,
    )

    assert checks[0]["status"] == "manual"
    assert checks[0]["code"] == "equipment_identifier_unrecognized"
    assert checks[0]["matches"][0]["equipment_id"] == "PCCSY-RD-CT-0-0048"
    assert "20-02-30" in checks[0]["summary"]
    assert "454110416181" in checks[0]["summary"]


def test_extracted_serial_equal_to_matched_equipment_id_is_not_unrecognized() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    actual = (
        "PIM SN: CN82105508; "
        "ECG simulator SN: PCCSY-RD-CT-1-0175; "
        "ECG simulator Calibration Date: 2025.10.29-2026.10.28"
    )
    content = review_content(actual)
    checks, _ = analyze_equipment_steps(content, registry)
    assert checks[0]["unrecognized_reported_identifiers"] == ["CN82105508"]

    apply_extracted_equipment(
        content,
        checks,
        {
            1: [
                _extracted(
                    device_name="ECG simulator",
                    serial_number="PCCSY-RD-CT-1-0175",
                    source_text="ECG simulator SN: PCCSY-RD-CT-1-0175",
                )
            ]
        },
        registry,
    )

    assert checks[0]["status"] == "manual"
    assert checks[0]["code"] == "equipment_identifier_unrecognized"
    assert "CN82105508" in checks[0]["summary"]
    assert "PCCSY-RD-CT-1-0175" not in checks[0]["summary"]


def test_identifier_leaked_from_another_step_keeps_the_verbatim_device_name() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "ECG simulator was used.", expected="The waveform is stable."
    )
    checks, _ = analyze_equipment_steps(content, registry)

    resolved, pending = apply_extracted_equipment(
        content,
        checks,
        {
            1: [
                _extracted(
                    device_name="ECG simulator",
                    equipment_id="PCCSY-RD-CT-9-9999",
                    source_text="ECG simulator was used.",
                )
            ]
        },
        registry,
    )

    assert checks[0]["code"] != "equipment_extraction_unverified"
    assert resolved or pending


def test_extracted_name_outside_the_registry_is_deferred_to_the_second_pass() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "A torque wrench was used.", expected="The waveform is stable."
    )
    checks, _ = analyze_equipment_steps(content, registry)

    resolved, pending = apply_extracted_equipment(
        content,
        checks,
        {
            1: [
                _extracted(
                    device_name="Torque wrench",
                    source_text="A torque wrench was used.",
                )
            ]
        },
        registry,
    )

    assert resolved == set()
    assert pending == {1: ["Torque wrench"]}
    assert checks[0]["code"] != "equipment_identifier_unrecognized"


def test_part_number_shaped_like_an_asset_id_is_not_an_unknown_identifier() -> None:
    content = review_content(
        "Record: Phantom: location phantom, P/N:PCCSY-RD-CT-0-20151202",
        description="Record the location phantom used for the scan",
        expected="The phantom is recorded.",
    )

    checks, _ = analyze_equipment_steps(content, [])

    assert checks[0]["reported_part_numbers"] == ["PCCSY-RD-CT-0-20151202"]
    assert checks[0]["unknown_identifiers"] == []


def test_part_number_shaped_like_an_asset_id_cannot_fail_a_required_step() -> None:
    content = review_content(
        "Record: Phantom: location phantom, P/N:PCCSY-RD-CT-0-20151202"
    )

    checks, _ = analyze_equipment_steps(content, [])

    assert checks[0]["code"] != "equipment_not_found"


def test_pending_names_become_second_pass_requests() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "A torque wrench was used.", expected="The waveform is stable."
    )

    questions = merge_pending_names(content, {1: ["Torque wrench"]}, registry, [])

    assert [question.review_step for question in questions] == [1]
    assert questions[0].kind == "name_mapping"
    assert questions[0].device_names == ("Torque wrench",)


def test_pending_names_attach_to_an_existing_second_pass_request() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "A torque wrench was used.", expected="The waveform is stable."
    )
    existing = [
        OpenQuestion(
            review_step=1,
            kind="role",
            description="",
            expected="",
            actual="",
            reported_identifiers=("SN-1",),
        )
    ]

    questions = merge_pending_names(
        content, {1: ["Torque wrench"]}, registry, existing
    )

    assert len(questions) == 1
    assert questions[0].kind == "name_mapping"
    assert questions[0].device_names == ("Torque wrench",)
    assert questions[0].reported_identifiers == ("SN-1",)
    assert existing[0].kind == "role"


def test_second_pass_registry_name_resolves_the_check() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "A torque wrench was used.", expected="The waveform is stable."
    )
    checks, _ = analyze_equipment_steps(content, registry)
    decisions = {
        1: {
            "role": "controlled_equipment",
            "required": True,
            "selected_equipment_ids": [],
            "selected_equipment_names": ["ECG simulator"],
            "reason": "The recorded device is the registry simulator.",
        }
    }

    apply_equipment_disambiguation(content, checks, decisions, registry)

    assert checks[0]["status"] == "pass"
    assert checks[0]["matches"][0]["matched_by"] == ["ai_registry_name"]


def test_name_only_match_with_conflicting_part_number_requires_manual_review() -> None:
    registry = [
        equipment(
            "PCCSY-RD-CT-0-0002",
            "N/A",
            description="Body Phantom",
            model_number="PH-2B",
        )
    ]
    content = review_content(
        "Record Body Phantom P/N: PCCSY-RD-CT-0002",
        description="Record the Phantom information.",
        expected="Record Body Phantom P/N: ____.",
    )
    checks, _ = analyze_equipment_steps(content, registry)

    apply_equipment_disambiguation(
        content,
        checks,
        {
            1: {
                "role": "controlled_equipment",
                "required": True,
                "selected_equipment_ids": [],
                "selected_equipment_names": ["Body Phantom"],
                "reason": "The name maps to the registry phantom.",
            }
        },
        registry,
    )

    assert checks[0]["status"] == "manual"
    assert checks[0]["code"] == "equipment_part_number_unverified"


def test_uncertain_role_with_selected_name_still_reports_part_number_conflict() -> None:
    registry = [
        equipment(
            "PCCSY-RD-CT-0-0002",
            "N/A",
            description="Body Phantom",
            model_number="PH-2B",
        )
    ]
    content = review_content(
        "Record Body Phantom P/N: PCCSY-RD-CT-0002",
        description="Record the Phantom information.",
        expected="Record Body Phantom P/N: ____. Calibration Date: ____. ",
    )
    checks, _ = analyze_equipment_steps(content, registry)

    apply_equipment_disambiguation(
        content,
        checks,
        {
            1: {
                "role": "uncertain",
                "required": True,
                "selected_equipment_ids": [],
                "selected_equipment_names": ["Body Phantom"],
                "reason": "The name maps, but the reported P/N does not.",
            }
        },
        registry,
    )

    assert checks[0]["status"] == "manual"
    assert checks[0]["code"] == "equipment_part_number_unverified"


def test_exact_equipment_id_is_not_overridden_by_another_devices_part_number() -> None:
    registry = [equipment("PCCSY-RD-CT-0-0048", "BIOPSY-48")]
    content = review_content(
        "CCT pedal P/N: 454110621681; "
        "Biopsy phantom equipment ID: PCCSY-RD-CT-0-0048"
    )

    checks, _ = analyze_equipment_steps(content, registry)

    assert checks[0]["status"] == "pass"
    assert checks[0]["code"] == "equipment_valid"


def test_extracted_due_date_is_compared_against_the_registry() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "ECG simulator, calibration valid until 2026/10/29.",
        expected="The waveform is stable.",
    )
    checks, _ = analyze_equipment_steps(content, registry)

    apply_extracted_equipment(
        content,
        checks,
        {
            1: [
                _extracted(
                    device_name="ECG simulator",
                    reported_calibration_due_date="2026-10-29",
                    source_text="ECG simulator, calibration valid until 2026/10/29.",
                )
            ]
        },
        registry,
    )

    assert checks[0]["status"] == "fail"
    assert "2026-10-29" in checks[0]["summary"]
    assert "2026-10-28" in checks[0]["summary"]


def test_program_matches_are_never_overwritten_by_extraction() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "ECG simulator SN: PCCSY-RD-CT-1-0175; "
        "Calibration Date From: 2025/10/29 to 2026/10/28"
    )
    checks, _ = analyze_equipment_steps(content, registry)

    resolved, pending = apply_extracted_equipment(
        content,
        checks,
        {
            1: [
                _extracted(
                    device_name="Stop watch",
                    source_text="ECG simulator SN: PCCSY-RD-CT-1-0175",
                )
            ]
        },
        registry,
    )

    assert resolved == set()
    assert pending == {}
    assert checks[0]["status"] == "pass"
    assert checks[0]["matches"][0]["matched_by"] == ["equipment_id"]


def test_device_name_matches_ignoring_spacing_and_punctuation() -> None:
    registry = [
        equipment("PCCSY-RD-CT-1-0175", "00850540007089", description="Stop watch")
    ]
    content = review_content(
        "A stopwatch was used.", expected="The waveform is stable."
    )
    checks, _ = analyze_equipment_steps(content, registry)

    resolved, pending = apply_extracted_equipment(
        content,
        checks,
        {
            1: [
                _extracted(
                    device_name="stopwatch",
                    source_text="A stopwatch was used.",
                )
            ]
        },
        registry,
    )

    assert resolved == {1}
    assert pending == {}
    assert checks[0]["matches"][0]["equipment_id"] == "PCCSY-RD-CT-1-0175"


def test_first_pass_decisions_keep_unmapped_device_names_for_the_second_pass() -> None:
    from app.services.equipment_pipeline import apply_first_pass_equipment_decisions

    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "A torque wrench was used.", expected="The waveform is stable."
    )
    content["steps"][0]["reference_candidates"] = [
        {
            "candidate_id": "step-1-ref-1",
            "type": "equipment",
            "value": "UNKNOWN-42",
            "source_field": "actual",
            "detection_source": "equipment_identifier",
        }
    ]
    content["steps"][0]["reference_decisions"] = [
        {
            "candidate_id": "step-1-ref-1",
            "role": "test_equipment",
            "reason": "The Step used a controlled tool.",
        }
    ]
    checks, _ = analyze_equipment_steps(content, registry)
    questions = [
        OpenQuestion(
            review_step=1,
            kind="name_mapping",
            description="",
            expected="",
            actual="",
            device_names=("Torque wrench",),
        )
    ]

    remaining = apply_first_pass_equipment_decisions(
        content, checks, questions, registry
    )

    assert [question.review_step for question in remaining] == [1]


def test_first_pass_candidate_from_requirement_does_not_prove_actual_equipment() -> None:
    from app.services.equipment_pipeline import apply_first_pass_equipment_decisions

    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content("Not recorded")
    content["steps"][0]["reference_candidates"] = [
        {
            "candidate_id": "step-1-ref-1",
            "type": "equipment",
            "value": "PCCSY-RD-CT-1-0175",
            "source_field": "expected",
            "detection_source": "equipment_registry_match",
        }
    ]
    content["steps"][0]["reference_decisions"] = [
        {
            "candidate_id": "step-1-ref-1",
            "role": "test_equipment",
            "reason": "Expected requires controlled equipment.",
        }
    ]
    checks, questions = analyze_equipment_steps(content, registry)

    remaining = apply_first_pass_equipment_decisions(
        content, checks, questions, registry
    )

    assert remaining == []
    assert checks[0]["status"] == "fail"
    assert checks[0]["code"] == "equipment_missing"


def test_first_pass_candidate_can_reuse_equipment_verified_in_a_prior_step() -> None:
    from app.services.equipment_pipeline import apply_first_pass_equipment_decisions

    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content("Set the simulator to 60 BPM.")
    content["steps"][0]["reference_candidates"] = [
        {
            "candidate_id": "step-1-ref-1",
            "type": "equipment",
            "value": "PCCSY-RD-CT-1-0175",
            "source_field": "description",
            "detection_source": "equipment_registry_match",
        }
    ]
    content["steps"][0]["reference_decisions"] = [
        {
            "candidate_id": "step-1-ref-1",
            "role": "test_equipment",
            "reason": "The previously recorded simulator is still in use.",
        }
    ]
    checks, _ = analyze_equipment_steps(content, registry)
    checks[0]["previously_matched_equipment_ids"] = ["PCCSY-RD-CT-1-0175"]
    questions = [
        OpenQuestion(
            review_step=1,
            kind="role",
            description="",
            expected="",
            actual="",
            previously_matched_equipment_ids=("PCCSY-RD-CT-1-0175",),
        )
    ]

    remaining = apply_first_pass_equipment_decisions(
        content, checks, questions, registry
    )

    assert remaining == []
    assert checks[0]["status"] == "pass"
    assert checks[0]["matches"][0]["equipment_id"] == "PCCSY-RD-CT-1-0175"


def test_new_device_name_cannot_be_satisfied_only_by_a_previous_match() -> None:
    from app.services.equipment_pipeline import apply_first_pass_equipment_decisions

    registry = [
        equipment(
            "PCCSY-RD-CT-1-0175",
            "00850540007089",
            description="Full Body Phantom",
        )
    ]
    content = review_content(
        "The result was recorded.",
        description="Perform the scan with the head phantom.",
        expected="The scan succeeds.",
    )
    content["steps"][0]["reference_candidates"] = [
        {
            "candidate_id": "step-1-ref-1",
            "type": "equipment",
            "value": "PCCSY-RD-CT-1-0175",
            "source_field": "description",
            "detection_source": "equipment_registry_match",
        }
    ]
    content["steps"][0]["reference_decisions"] = [
        {
            "candidate_id": "step-1-ref-1",
            "role": "test_equipment",
            "reason": "A controlled phantom is required.",
        }
    ]
    checks, _ = analyze_equipment_steps(content, registry)
    checks[0]["pending_device_names"] = ["head phantom"]
    checks[0]["previously_matched_equipment_ids"] = ["PCCSY-RD-CT-1-0175"]
    question = OpenQuestion(
        review_step=1,
        kind="name_mapping",
        description="Perform the scan with the head phantom.",
        expected="The scan succeeds.",
        actual="The result was recorded.",
        device_names=("head phantom",),
        previously_matched_equipment_ids=("PCCSY-RD-CT-1-0175",),
    )

    remaining = apply_first_pass_equipment_decisions(
        content, checks, [question], registry
    )

    assert remaining == [question]
    assert checks[0]["status"] == "manual"


def test_second_pass_must_map_a_new_name_before_reusing_previous_equipment() -> None:
    registry = [
        equipment(
            "PCCSY-RD-CT-1-0175",
            "00850540007089",
            description="Full Body Phantom",
        )
    ]
    content = review_content(
        "The result was recorded.",
        description="Perform the scan with the head phantom.",
        expected="The scan succeeds.",
    )
    checks, _ = analyze_equipment_steps(content, registry)
    checks[0]["pending_device_names"] = ["head phantom"]
    checks[0]["previously_matched_equipment_ids"] = ["PCCSY-RD-CT-1-0175"]

    apply_equipment_disambiguation(
        content,
        checks,
        {
            1: {
                "role": "controlled_equipment",
                "required": True,
                "selected_equipment_ids": ["PCCSY-RD-CT-1-0175"],
                "selected_equipment_names": [],
                "reason": "The previous phantom might still be in use.",
            }
        },
        registry,
    )

    assert checks[0]["status"] == "fail"
    assert checks[0]["code"] == "equipment_missing"


def test_second_pass_cannot_reuse_previous_device_for_a_different_requirement() -> None:
    registry = [
        equipment(
            "PCCSY-RD-CT-1-0175",
            "00850540007089",
            description="Full Body Phantom",
        )
    ]
    content = review_content(
        "The result was recorded.",
        description="Perform the scan with the head phantom.",
        expected="The scan succeeds.",
    )
    checks, _ = analyze_equipment_steps(content, registry)
    checks[0]["previously_matched_equipment_ids"] = ["PCCSY-RD-CT-1-0175"]

    apply_equipment_disambiguation(
        content,
        checks,
        {
            1: {
                "role": "controlled_equipment",
                "required": False,
                "selected_equipment_ids": ["PCCSY-RD-CT-1-0175"],
                "selected_equipment_names": [],
                "reason": "The previous phantom might still be in use.",
            }
        },
        registry,
    )

    assert checks[0]["requirement_candidate_equipment_ids"] == []
    assert checks[0]["status"] == "manual"
    assert checks[0]["code"] == "equipment_unidentified"


def test_extraction_is_recorded_even_when_the_program_already_matched() -> None:
    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    content = review_content(
        "ECG simulator SN: PCCSY-RD-CT-1-0175; "
        "Calibration Date From: 2025/10/29 to 2026/10/28"
    )
    checks, _ = analyze_equipment_steps(content, registry)
    entries = [
        _extracted(
            device_name="ECG simulator",
            source_text="ECG simulator SN: PCCSY-RD-CT-1-0175",
        )
    ]

    apply_extracted_equipment(content, checks, {1: entries}, registry)

    assert checks[0]["extracted_equipment"] == entries


def test_lone_due_date_must_belong_to_one_of_several_matched_devices() -> None:
    registry = [
        equipment("PCCSY-RD-CT-1-0175", "00850540007089", equipment_pk=1),
        equipment(
            "PCCSY-RD-CT-1-0176",
            "00850540007090",
            calibration_due_date=date(2027, 4, 2),
            equipment_pk=2,
        ),
    ]
    content = review_content(
        "SN: 00850540007089 and SN: 00850540007090; "
        "Calibration Due date: 2030/01/01"
    )

    checks, _ = analyze_equipment_steps(content, registry)

    assert checks[0]["reported_identifiers"] == [
        "00850540007089",
        "00850540007090",
    ]
    assert checks[0]["status"] == "fail"
    assert "2030-01-01" in checks[0]["summary"]


def test_lone_due_date_matching_any_matched_device_still_passes() -> None:
    registry = [
        equipment("PCCSY-RD-CT-1-0175", "00850540007089", equipment_pk=1),
        equipment(
            "PCCSY-RD-CT-1-0176",
            "00850540007090",
            calibration_due_date=date(2027, 4, 2),
            equipment_pk=2,
        ),
    ]
    content = review_content(
        "SN: 00850540007089 and SN: 00850540007090; "
        "Calibration Due date: 2027/04/02"
    )

    checks, _ = analyze_equipment_steps(content, registry)

    assert checks[0]["status"] == "pass"


def _question(review_step: int, *, names: tuple[str, ...] = ()) -> OpenQuestion:
    return OpenQuestion(
        review_step=review_step,
        kind="name_mapping" if names else "role",
        description="Record equipment.",
        expected="Equipment ID is recorded.",
        actual="Used equipment.",
        device_names=names,
    )


def test_second_pass_splits_large_question_sets_into_batches(monkeypatch) -> None:
    from app.services import equipment_pipeline

    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    sent: list[list[str]] = []

    def stub_request(ai_config, questions, registry_names):
        sent.append(registry_names)
        return {}, {"skill_id": "equipment-role", "status": "completed", "ai_calls": 1}

    monkeypatch.setattr(
        equipment_pipeline, "request_equipment_disambiguation", stub_request
    )

    _, trace = equipment_pipeline.run_equipment_pipeline(
        None,
        {"steps": []},
        [],
        [_question(step) for step in range(1, 9)]
        + [_question(9, names=("Torque wrench",))],
        registry,
    )

    assert trace["batches"] == 2
    assert trace["ai_calls"] == 2
    assert sent == [[], ["ECG simulator"]]


def test_second_pass_with_previous_equipment_receives_name_vocabulary(
    monkeypatch,
) -> None:
    from app.services import equipment_pipeline

    registry = [equipment("PCCSY-RD-CT-1-0175", "00850540007089")]
    sent: list[list[str]] = []

    def stub_request(ai_config, questions, registry_names):
        sent.append(registry_names)
        return {}, {"skill_id": "equipment-role", "status": "completed", "ai_calls": 1}

    monkeypatch.setattr(
        equipment_pipeline, "request_equipment_disambiguation", stub_request
    )
    question = OpenQuestion(
        review_step=1,
        kind="role",
        description="Check the simulator output.",
        expected="The output is stable.",
        actual="The output was stable.",
        previously_matched_equipment_ids=("PCCSY-RD-CT-1-0175",),
    )

    equipment_pipeline.run_equipment_pipeline(
        None, {"steps": []}, [], [question], registry
    )

    assert sent == [["ECG simulator"]]
