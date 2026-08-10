import json

import pytest

from app.models import EvidenceConfig
from app.services.image_evidence import ImageEvidenceResult, ResolvedImage
from app.services.reviews import (
    PreparedImageEvidence,
    _apply_capability_guards,
    _apply_equipment_guards,
    _completion_url,
    _parse_response,
    _review_message,
)


def compact_response(
    *,
    steps: list[int] | None = None,
    not_applicable_steps: list[int] | None = None,
    issues: list[dict] | None = None,
    warnings: list[dict] | None = None,
) -> str:
    return json.dumps(
        {
            "reviewed_steps": steps or [1],
            "not_applicable_steps": not_applicable_steps or [],
            "issues": issues or [],
            "warnings": warnings or [],
            "summary": "简短总结",
        }
    )


def evidence_content(
    *,
    paths: list[dict] | None = None,
    screenshot_required: bool = False,
    attachment_declared: bool = False,
    dates: list[dict] | None = None,
) -> dict:
    return {
        "steps": [
            {
                "order": "1",
                "name": "Step 1",
                "evidence_profile": {
                    "actual_paths": paths or [],
                    "actual_dates": dates or [],
                    "attachment_declared": attachment_declared,
                    "screenshot_review_required": screenshot_required,
                    "reference_lookup_required": False,
                },
            }
        ]
    }


def test_full_completion_endpoint_is_not_duplicated() -> None:
    endpoint = "http://161.92.92.153:6000/v1/chat/completions"
    assert _completion_url(endpoint) == endpoint
    assert _completion_url("http://host/v1") == "http://host/v1/chat/completions"


def test_failed_step_makes_run_unqualified() -> None:
    parsed = _parse_response(
        compact_response(
            steps=[1, 2],
            issues=[
                {
                    "step": 2,
                    "status": "fail",
                    "type": "expected_actual",
                    "summary": "Actual未记录要求的参数。",
                }
            ],
        ),
        [1, 2],
    )
    assert parsed["verdict"] == "unqualified"
    assert parsed["step_results"][0]["status"] == "pass"
    assert parsed["step_results"][1]["status"] == "fail"
    assert parsed["criteria"]["expected_vs_actual"]["status"] == "fail"


def test_manual_step_requires_manual_review_when_nothing_fails() -> None:
    parsed = _parse_response(
        compact_response(
            issues=[
                {
                    "step": 1,
                    "status": "manual",
                    "type": "language",
                    "summary": "语义可能受语法影响。",
                }
            ]
        ),
        [1],
    )
    assert parsed["verdict"] == "needs_manual_review"


def test_screenshot_issue_requires_manual_review_when_uncertain() -> None:
    parsed = _parse_response(
        compact_response(
            issues=[
                {
                    "step": 1,
                    "status": "manual",
                    "type": "screenshot",
                    "summary": "截图内容无法辨认。",
                }
            ]
        ),
        [1],
    )

    assert parsed["verdict"] == "needs_manual_review"
    assert parsed["criteria"]["screenshot_evidence"]["status"] == "manual"


def test_minor_language_warning_does_not_change_qualified_verdict() -> None:
    parsed = _parse_response(
        compact_response(
            warnings=[
                {
                    "step": 1,
                    "type": "minor_language",
                    "summary": "Actual存在轻微拼写错误。",
                }
            ]
        ),
        [1],
    )
    assert parsed["verdict"] == "qualified"
    assert parsed["step_results"][0]["status"] == "pass"
    assert parsed["warnings"][0]["type"] == "minor_language"


def test_identical_duplicate_warning_is_recorded_once() -> None:
    warning = {
        "step": 1,
        "type": "minor_language",
        "summary": "Actual存在轻微拼写错误。",
    }

    parsed = _parse_response(compact_response(warnings=[warning, warning]), [1])

    assert parsed["verdict"] == "qualified"
    assert parsed["warnings"] == [warning]


def test_distinct_warnings_of_same_type_are_preserved() -> None:
    warnings = [
        {
            "step": 1,
            "type": "minor_language",
            "summary": "Actual存在轻微拼写错误。",
        },
        {
            "step": 1,
            "type": "minor_language",
            "summary": "Actual存在另一处语法错误。",
        },
    ]

    parsed = _parse_response(compact_response(warnings=warnings), [1])

    assert parsed["verdict"] == "qualified"
    assert parsed["warnings"] == warnings


def test_missing_reviewed_step_is_rejected() -> None:
    with pytest.raises(ValueError, match="do not match expected steps"):
        _parse_response(compact_response(steps=[1]), [1, 2])


def test_issue_for_unknown_step_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown step"):
        _parse_response(
            compact_response(
                issues=[
                    {
                        "step": 2,
                        "status": "fail",
                        "type": "language",
                        "summary": "错误。",
                    }
                ]
            ),
            [1],
        )


def test_review_message_stays_text_only_without_images() -> None:
    assert _review_message("prompt", {"run": {"steps": []}}) == "prompt"


def test_review_message_does_not_send_images_while_capability_is_deferred() -> None:
    snapshot = {
        "run": {
            "steps": [
                {
                    "attachmentContents": [
                        {"name": "evidence.png", "data_url": "data:image/png;base64,AA=="}
                    ]
                }
            ]
        }
    }
    assert _review_message("prompt", snapshot) == "prompt"


def test_ready_network_image_is_sent_with_its_review_step() -> None:
    source_path = r"\\server\approved\case"
    image = ResolvedImage(
        relative_name="evidence.png",
        media_type="image/png",
        size_bytes=12,
        sha256="a" * 64,
        data_url="data:image/png;base64,AA==",
    )
    prepared = PreparedImageEvidence(
        network_enabled=True,
        image_review_enabled=True,
        transport_allowed=True,
        results={
            1: {
                source_path: ImageEvidenceResult(status="ready", images=(image,))
            }
        },
    )

    message = _review_message("prompt", {"run": {"steps": []}}, prepared)

    assert isinstance(message, list)
    assert any("review_step 1" in item.get("text", "") for item in message)
    assert any(item.get("image_url", {}).get("url") == image.data_url for item in message)


def test_oversized_transport_evidence_requires_manual_review() -> None:
    source_path = r"\\server\approved\large-case"
    image = ResolvedImage(
        relative_name="large.jpg",
        media_type="image/jpeg",
        size_bytes=512 * 1024,
        sha256="a" * 64,
        data_url="data:image/jpeg;base64,AA==",
        width=4000,
        height=4000,
    )
    parsed = _parse_response(compact_response(), [1])
    prepared = PreparedImageEvidence(
        network_enabled=True,
        image_review_enabled=True,
        transport_allowed=True,
        results={
            1: {
                source_path: ImageEvidenceResult(
                    status="transport_too_large",
                    images=(image,),
                )
            }
        },
    )

    guarded = _apply_capability_guards(
        parsed,
        evidence_content(paths=[{"raw": source_path}], screenshot_required=True),
        EvidenceConfig(allowed_network_root=r"\\server\approved"),
        prepared,
    )

    assert guarded["verdict"] == "needs_manual_review"
    assert guarded["step_results"][0]["image_evidence"][0]["status"] == (
        "transport_too_large"
    )


def test_missing_network_evidence_is_unqualified() -> None:
    source_path = r"\\server\approved\missing"
    parsed = _parse_response(compact_response(), [1])
    prepared = PreparedImageEvidence(
        network_enabled=True,
        image_review_enabled=True,
        transport_allowed=True,
        results={1: {source_path: ImageEvidenceResult(status="missing")}},
    )

    guarded = _apply_capability_guards(
        parsed,
        evidence_content(paths=[{"raw": source_path}], screenshot_required=True),
        EvidenceConfig(allowed_network_root=r"\\server\approved"),
        prepared,
    )

    assert guarded["step_results"][0]["status"] == "fail"
    assert guarded["verdict"] == "unqualified"
    assert "证据路径不存在" in guarded["step_results"][0]["summary"]


def test_not_applicable_step_skips_network_image_guards() -> None:
    source_path = r"\\server\approved\missing"
    parsed = _parse_response(
        compact_response(not_applicable_steps=[1]),
        [1],
    )
    prepared = PreparedImageEvidence(
        network_enabled=True,
        image_review_enabled=True,
        transport_allowed=True,
        results={1: {source_path: ImageEvidenceResult(status="missing")}},
    )

    guarded = _apply_capability_guards(
        parsed,
        evidence_content(paths=[{"raw": source_path}], screenshot_required=True),
        EvidenceConfig(allowed_network_root=r"\\server\approved"),
        prepared,
    )

    assert guarded["step_results"][0]["applicability"] == "not_applicable"
    assert guarded["step_results"][0]["issues"] == []
    assert guarded["verdict"] == "qualified"


def test_non_unc_path_capability_guard_is_unqualified() -> None:
    parsed = _parse_response(compact_response(), [1])
    content = evidence_content(paths=[{"raw": r"C:\Evidence\step.png"}])

    guarded = _apply_capability_guards(
        parsed,
        content,
        EvidenceConfig(allowed_network_root=r"\\server\approved"),
    )

    assert guarded["criteria"]["path_validation"]["status"] == "fail"
    assert guarded["step_results"][0]["status"] == "fail"
    assert guarded["verdict"] == "unqualified"


def test_required_screenshot_without_reference_is_unqualified() -> None:
    parsed = _parse_response(compact_response(), [1])

    guarded = _apply_capability_guards(
        parsed,
        evidence_content(screenshot_required=True),
    )

    assert guarded["criteria"]["screenshot_evidence"]["status"] == "fail"
    assert guarded["verdict"] == "unqualified"


def test_date_mismatch_does_not_affect_verdict_until_date_rules_are_enabled() -> None:
    parsed = _parse_response(compact_response(), [1])
    content = evidence_content(
        dates=[{"raw": "2026-07-29", "same_day_as_execution": False}]
    )

    guarded = _apply_capability_guards(parsed, content)

    assert guarded["criteria"]["automation_timing"]["status"] == "not_applicable"
    assert guarded["criteria"]["path_validation"]["status"] == "not_applicable"
    assert guarded["verdict"] == "qualified"


def test_equipment_failure_makes_run_unqualified() -> None:
    parsed = _parse_response(compact_response(), [1])
    check = {
        "review_step": 1,
        "status": "fail",
        "code": "equipment_invalid",
        "summary": "设备执行日期不在校准有效期内。",
        "matches": [],
        "warnings": [],
    }

    guarded = _apply_equipment_guards(parsed, [check])

    assert guarded["verdict"] == "unqualified"
    assert guarded["step_results"][0]["status"] == "fail"
    assert guarded["criteria"]["equipment_traceability"]["status"] == "fail"


def test_equipment_pass_and_current_status_warning_remain_qualified() -> None:
    parsed = _parse_response(compact_response(), [1])
    warning = {
        "type": "equipment_status",
        "summary": "设备当前状态为校验中；该状态不代表执行当天状态。",
    }
    check = {
        "review_step": 1,
        "status": "pass",
        "code": "equipment_valid",
        "summary": "设备标识及执行日期均符合台账。",
        "matches": [{"equipment_id": "PCCSY-RD-CT-1-0076"}],
        "warnings": [warning],
    }

    guarded = _apply_equipment_guards(parsed, [check])

    assert guarded["verdict"] == "qualified"
    assert guarded["warnings"] == [{"step": 1, **warning}]
    assert guarded["criteria"]["equipment_traceability"]["status"] == "pass"