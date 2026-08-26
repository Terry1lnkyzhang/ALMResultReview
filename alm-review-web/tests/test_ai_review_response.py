import json
from types import SimpleNamespace

import pytest

from app.models import AiConfig, EvidenceConfig
from app.services.html_evidence import HtmlEvidenceResult
from app.services.image_evidence import ImageEvidenceResult, ResolvedImage
from app.services.reviews import (
    PreparedImageEvidence,
    ReviewContext,
    _ai_headers,
    _apply_capability_guards,
    _apply_equipment_guards,
    _apply_reference_routing,
    _attach_reference_candidates,
    _completion_url,
    _image_review_batches,
    _image_review_stage,
    _recalculate_result,
    _run_text_semantic_skills,
)


class IntentStubResponse:
    def __init__(self, content: str) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self.content}}]}


# Mirrors the aggregated structure that _run_text_semantic_skills hands to the guards.
def text_review_result(
    *,
    steps: list[int] | None = None,
    not_applicable_steps: list[int] | None = None,
    issues: list[dict] | None = None,
    warnings: list[dict] | None = None,
) -> dict:
    review_steps = steps or [1]
    not_applicable = set(not_applicable_steps or [])
    step_results = {
        step: {
            "review_step": step,
            "applicability": (
                "not_applicable" if step in not_applicable else "applicable"
            ),
            "status": "pass",
            "summary": "",
            "issues": [],
            "warnings": [],
        }
        for step in review_steps
    }
    for issue in issues or []:
        step_results[issue["step"]]["issues"].append(
            {key: value for key, value in issue.items() if key != "step"}
        )
    for warning in warnings or []:
        step_results[warning["step"]]["warnings"].append(
            {key: value for key, value in warning.items() if key != "step"}
        )
    return _recalculate_result(
        {
            "model_summary": "Short summary",
            "step_results": list(step_results.values()),
            "warnings": [],
        }
    )


def test_saved_ai_api_key_takes_precedence_over_environment(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.ai_transport.get_settings",
        lambda: SimpleNamespace(ai_api_key="environment-key"),
    )

    assert _ai_headers(AiConfig(api_key="saved-key")) == {
        "Authorization": "Bearer saved-key"
    }
    assert _ai_headers(AiConfig(api_key="")) == {
        "Authorization": "Bearer environment-key"
    }


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


def test_reference_candidates_are_stable_typed_and_deduplicated() -> None:
    content = {
        "steps": [
            {
                "review_step": 1,
                "description": "Measure with Meter M1.",
                "expected": "The measurement is recorded.",
                "actual": "Used EQ-100. See evidence files.",
                "attachment_declared": True,
                "evidence_profile": {
                    "actual_paths": [
                        {"raw": r"\\server\case\screen.png", "kind": "image"},
                        {"raw": r"\\server\case\report.html", "kind": "html"},
                        {"raw": r"\\server\case\results", "kind": "folder_or_unknown"},
                    ]
                },
            }
        ]
    }
    equipment_checks = [
        {
            "review_step": 1,
            "matches": [
                {
                    "equipment_id": "EQ-100",
                    "description": "Meter",
                    "model_number": "M1",
                    "serial_number": "SN-1",
                }
            ],
            "reported_identifiers": ["EQ-100", "UNKNOWN-9"],
            "unknown_identifiers": ["UNKNOWN-9"],
        }
    ]

    _attach_reference_candidates(content, equipment_checks, [])
    first = content["steps"][0]["reference_candidates"]
    _attach_reference_candidates(content, equipment_checks, [])

    assert content["steps"][0]["reference_candidates"] == first
    assert [candidate["candidate_id"] for candidate in first] == [
        f"step-1-ref-{index}" for index in range(1, 7)
    ]
    assert [candidate["type"] for candidate in first] == [
        "image",
        "html_report",
        "folder_or_unknown",
        "file",
        "equipment",
        "equipment",
    ]
    assert first[4] == {
        "candidate_id": "step-1-ref-5",
        "type": "equipment",
        "value": "EQ-100",
        "source_field": "actual",
        "detection_source": "equipment_registry_match",
    }
    assert first[5]["value"] == "UNKNOWN-9"
    assert first[5]["detection_source"] == "equipment_identifier"


def test_reference_decisions_route_specialists_but_attachment_is_forced() -> None:
    content = {
        "steps": [
            {
                "review_step": 1,
                "attachment_declared": True,
                "reference_candidates": [
                    {
                        "candidate_id": "step-1-ref-1",
                        "type": "image",
                        "value": r"\\server\case\manual.png",
                        "source_field": "actual",
                        "detection_source": "path_extension",
                    },
                    {
                        "candidate_id": "step-1-ref-2",
                        "type": "html_report",
                        "value": r"\\server\case\result.html",
                        "source_field": "actual",
                        "detection_source": "path_extension",
                    },
                ],
                "reference_decisions": [
                    {
                        "candidate_id": "step-1-ref-1",
                        "role": "reference_document",
                        "requires_check": False,
                        "reason": "The image is an instruction reference.",
                    },
                    {
                        "candidate_id": "step-1-ref-2",
                        "role": "result_evidence",
                        "requires_check": True,
                        "reason": "The report contains this execution result.",
                    },
                ],
                "evidence_profile": {
                    "routing": {"triggers": ["alm_attachment_declared"]}
                },
            }
        ]
    }

    _apply_reference_routing(content)

    profile = content["steps"][0]["evidence_profile"]
    assert profile["routing"]["intent"] == "html_report"
    assert profile["routing"]["actions"] == [
        "parse_html_report",
        "review_attachment",
        "validate_path",
    ]
    assert profile["screenshot_review_required"] is True


def test_first_pass_rejects_missing_reference_decision(monkeypatch) -> None:
    content = {
        "review_plan": {"text_steps": [1]},
        "steps": [
            {
                "review_step": 1,
                "description": "Review the result image.",
                "expected": "The result is visible.",
                "actual": r"See \\server\case\result.png.",
                "actual_format": {"layout_text": "", "signals": []},
                "numbered_comparison": [],
                "reference_candidates": [
                    {
                        "candidate_id": "step-1-ref-1",
                        "type": "image",
                        "value": r"\\server\case\result.png",
                        "source_field": "actual",
                        "detection_source": "path_extension",
                    }
                ],
            }
        ],
    }
    monkeypatch.setattr(
        "app.services.reviews.httpx.post",
        lambda *args, **kwargs: IntentStubResponse(
            json.dumps(
                {
                    "assessments": [
                        {
                            "review_step": 1,
                            "applicability": "applicable",
                            "findings": [],
                            "reference_decisions": [],
                            "summary": "Actual supports Expected.",
                        }
                    ]
                }
            )
        ),
    )

    with pytest.raises(ValueError, match="exactly cover supplied references"):
        _run_text_semantic_skills(
            AiConfig(base_url="https://ai.example/v1", model_name="test"),
            content,
        )


def test_first_pass_restores_alm_step_order(monkeypatch) -> None:
    content = {
        "review_plan": {"text_steps": [1, 2]},
        "steps": [
            {
                "review_step": review_step,
                "description": f"Step {review_step}",
                "expected": "Result is recorded.",
                "actual": "Result was recorded.",
                "actual_format": {"layout_text": "", "signals": []},
                "numbered_comparison": [],
                "reference_candidates": [],
            }
            for review_step in (1, 2)
        ],
    }
    monkeypatch.setattr(
        "app.services.reviews.httpx.post",
        lambda *args, **kwargs: IntentStubResponse(
            json.dumps(
                {
                    "assessments": [
                        {
                            "review_step": review_step,
                            "applicability": "applicable",
                            "findings": [],
                            "reference_decisions": [],
                            "summary": f"Step {review_step} passed.",
                        }
                        for review_step in (2, 1)
                    ]
                }
            )
        ),
    )

    parsed, _ = _run_text_semantic_skills(
        AiConfig(base_url="https://ai.example/v1", model_name="test"),
        content,
    )

    assert [step["review_step"] for step in parsed["step_results"]] == [1, 2]


def test_not_applicable_step_drops_text_findings(monkeypatch) -> None:
    content = {
        "review_plan": {"text_steps": [1]},
        "steps": [
            {
                "review_step": 1,
                "description": "Update the CDM firmware. (only for Taichi)",
                "expected": "All firmware is updated successfully.",
                "actual": "This was Earth.",
                "actual_format": {"layout_text": "", "signals": []},
                "numbered_comparison": [],
                "reference_candidates": [],
            }
        ],
    }
    monkeypatch.setattr(
        "app.services.reviews.httpx.post",
        lambda *args, **kwargs: IntentStubResponse(
            json.dumps(
                {
                    "assessments": [
                        {
                            "review_step": 1,
                            "applicability": "not_applicable",
                            "findings": [
                                {
                                    "code": "expected_actual_mismatch",
                                    "severity": "fail",
                                    "reason": "Actual does not confirm the update.",
                                }
                            ],
                            "reference_decisions": [],
                            "summary": "The Step only applies to Taichi.",
                        }
                    ]
                }
            )
        ),
    )

    parsed, _ = _run_text_semantic_skills(
        AiConfig(base_url="https://ai.example/v1", model_name="test"),
        content,
    )

    assert parsed["verdict"] == "qualified"
    assert parsed["step_results"][0]["issues"] == []
    assert parsed["step_results"][0]["suppressed_findings"] == [
        {
            "code": "expected_actual_mismatch",
            "severity": "fail",
            "summary": "Actual does not confirm the update.",
            "cause": "step_not_applicable",
        }
    ]


def test_routed_result_evidence_drops_missing_content_findings(monkeypatch) -> None:
    content = {
        "review_plan": {"text_steps": [1]},
        "steps": [
            {
                "review_step": 1,
                "description": "Check the computer environment used.",
                "expected": "Record computer parameters: ______.",
                "actual": r"Recorded computer parameters: \\server\case\step1.jpg",
                "actual_format": {"layout_text": "", "signals": []},
                "numbered_comparison": [],
                "reference_candidates": [
                    {
                        "candidate_id": "step-1-ref-1",
                        "type": "image",
                        "value": r"\\server\case\step1.jpg",
                        "source_field": "actual",
                        "detection_source": "path_extension",
                    }
                ],
            }
        ],
    }
    monkeypatch.setattr(
        "app.services.reviews.httpx.post",
        lambda *args, **kwargs: IntentStubResponse(
            json.dumps(
                {
                    "assessments": [
                        {
                            "review_step": 1,
                            "applicability": "applicable",
                            "findings": [
                                {
                                    "code": "actual_insufficient",
                                    "severity": "fail",
                                    "reason": "Actual supplies a path instead of values.",
                                },
                                {
                                    "code": "language_quality",
                                    "severity": "warning",
                                    "reason": "Actual mixes tenses.",
                                },
                            ],
                            "reference_decisions": [
                                {
                                    "candidate_id": "step-1-ref-1",
                                    "role": "result_evidence",
                                    "requires_check": True,
                                    "reason": "Actual cites the captured evidence.",
                                }
                            ],
                            "summary": "Actual answers Expected through evidence.",
                        }
                    ]
                }
            )
        ),
    )

    parsed, _ = _run_text_semantic_skills(
        AiConfig(base_url="https://ai.example/v1", model_name="test"),
        content,
    )

    step_result = parsed["step_results"][0]
    assert step_result["issues"] == []
    assert step_result["warnings"] == [
        {"type": "minor_language", "summary": "Actual mixes tenses."}
    ]
    assert [item["cause"] for item in step_result["suppressed_findings"]] == [
        "result_evidence_routed"
    ]


def test_not_applicable_step_does_not_route_external_reference() -> None:
    content = {
        "steps": [
            {
                "review_step": 1,
                "text_applicability": "not_applicable",
                "attachment_declared": True,
                "reference_candidates": [
                    {
                        "candidate_id": "step-1-ref-1",
                        "type": "file",
                        "value": r"\\server\case\result.csv",
                        "source_field": "actual",
                        "detection_source": "path_extension",
                    }
                ],
                "reference_decisions": [
                    {
                        "candidate_id": "step-1-ref-1",
                        "role": "result_evidence",
                        "requires_check": True,
                        "reason": "The file contains result evidence.",
                    }
                ],
                "evidence_profile": {
                    "actual_paths": [
                        {
                            "raw": r"\\server\case\result.csv",
                            "kind": "file",
                        }
                    ],
                    "routing": {"triggers": ["alm_attachment_declared"]},
                },
            }
        ]
    }

    _apply_reference_routing(content)

    profile = content["steps"][0]["evidence_profile"]
    assert profile["routing"]["actions"] == []
    assert profile["actual_paths"][0]["route_requested"] is False


def test_full_completion_endpoint_is_not_duplicated() -> None:
    endpoint = "http://161.92.92.153:6000/v1/chat/completions"
    assert _completion_url(endpoint) == endpoint
    assert _completion_url("http://host/v1") == "http://host/v1/chat/completions"


def test_failed_step_makes_run_unqualified() -> None:
    parsed = text_review_result(
        steps=[1, 2],
        issues=[
            {
                "step": 2,
                "status": "fail",
                "type": "expected_actual",
                "summary": "Actual does not record the required parameter.",
            }
        ],
    )
    assert parsed["verdict"] == "unqualified"
    assert parsed["step_results"][0]["status"] == "pass"
    assert parsed["step_results"][1]["status"] == "fail"
    assert parsed["criteria"]["expected_vs_actual"]["status"] == "fail"


def test_manual_step_requires_manual_review_when_nothing_fails() -> None:
    parsed = text_review_result(
        issues=[
            {
                "step": 1,
                "status": "manual",
                "type": "language",
                "summary": "Grammar may affect the meaning.",
            }
        ]
    )
    assert parsed["verdict"] == "needs_manual_review"


def test_screenshot_issue_requires_manual_review_when_uncertain() -> None:
    parsed = text_review_result(
        issues=[
            {
                "step": 1,
                "status": "manual",
                "type": "screenshot",
                "summary": "The screenshot content is illegible.",
            }
        ]
    )

    assert parsed["verdict"] == "needs_manual_review"
    assert parsed["criteria"]["screenshot_evidence"]["status"] == "manual"


def test_minor_language_warning_does_not_change_qualified_verdict() -> None:
    parsed = text_review_result(
        warnings=[
            {
                "step": 1,
                "type": "minor_language",
                "summary": "Actual has a minor spelling mistake.",
            }
        ]
    )
    assert parsed["verdict"] == "qualified"
    assert parsed["step_results"][0]["status"] == "pass"
    assert parsed["warnings"][0]["type"] == "minor_language"


def test_distinct_warnings_of_same_type_are_preserved() -> None:
    warnings = [
        {
            "step": 1,
            "type": "minor_language",
            "summary": "Actual has a minor spelling mistake.",
        },
        {
            "step": 1,
            "type": "minor_language",
            "summary": "Actual has another grammar mistake.",
        },
    ]

    parsed = text_review_result(warnings=warnings)

    assert parsed["verdict"] == "qualified"
    assert parsed["warnings"] == warnings


def test_repeated_language_findings_produce_one_warning_each(monkeypatch) -> None:
    repeated = {
        "code": "language_quality",
        "severity": "warning",
        "reason": "Actual has a minor spelling mistake.",
    }
    distinct = {
        "code": "language_quality",
        "severity": "warning",
        "reason": "Actual has another grammar mistake.",
    }
    content = {
        "review_plan": {"text_steps": [1]},
        "steps": [
            {
                "review_step": 1,
                "description": "Record the result.",
                "expected": "The result is recorded.",
                "actual": "The result was recordd.",
            }
        ],
    }

    def post(*_args, **_kwargs):
        return IntentStubResponse(
            json.dumps(
                {
                    "assessments": [
                        {
                            "review_step": 1,
                            "applicability": "applicable",
                            "findings": [repeated, repeated, distinct],
                            "reference_decisions": [],
                            "summary": "Actual supports Expected.",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )

    monkeypatch.setattr("app.services.reviews.httpx.post", post)

    parsed, _traces = _run_text_semantic_skills(
        AiConfig(base_url="https://ai.example/v1", model_name="test"),
        content,
    )

    assert parsed["warnings"] == [
        {"step": 1, "type": "minor_language", "summary": repeated["reason"]},
        {"step": 1, "type": "minor_language", "summary": distinct["reason"]},
    ]
    assert parsed["verdict"] == "qualified"


def test_image_review_batches_respect_limit_and_keep_steps_isolated() -> None:
    def image(name: str) -> ResolvedImage:
        return ResolvedImage(
            relative_name=name,
            media_type="image/jpeg",
            size_bytes=10,
            sha256=name,
            data_url="data:image/jpeg;base64,dGVzdA==",
        )

    source = r"\\server\evidence"
    prepared = PreparedImageEvidence(
        external_review_enabled=True,
        results={
            1: {
                source: ImageEvidenceResult(
                    status="ready",
                    images=tuple(image(f"Step1{suffix}.jpg") for suffix in "ab"),
                )
            },
            2: {
                source: ImageEvidenceResult(
                    status="ready",
                    images=tuple(image(f"Step2{suffix}.jpg") for suffix in "abcd"),
                )
            },
            3: {
                source: ImageEvidenceResult(
                    status="ready",
                    images=tuple(image(f"Step{step}.jpg") for step in range(3, 6)),
                )
            },
        },
    )

    batches = _image_review_batches(prepared)

    assert [len(batch) for batch in batches] == [2, 4, 3]
    assert all(len({review_step for review_step, _, _ in batch}) == 1 for batch in batches)
    assert sum(len(batch) for batch in batches) == 9


def test_text_and_image_reviews_use_separate_bounded_requests(monkeypatch) -> None:
    source = r"\\server\evidence"
    images = tuple(
        ResolvedImage(
            relative_name=f"Step1{index}.jpg",
            media_type="image/jpeg",
            size_bytes=10,
            sha256=f"{index:064x}",
            data_url="data:image/jpeg;base64,dGVzdA==",
        )
        for index in range(9)
    )
    prepared = PreparedImageEvidence(
        external_review_enabled=True,
        results={1: {source: ImageEvidenceResult(status="ready", images=images)}},
    )
    content = {
        "review_plan": {"text_steps": [1]},
        "steps": [
            {
                "review_step": 1,
                "order": "1",
                "description": "Check the report image.",
                "expected": "The report is correct.",
                "actual": "The report was correct.",
            }
        ],
    }
    requests = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            request_number = len(requests)
            request = requests[-1]
            system_content = request["messages"][0]["content"]
            user_content = request["messages"][1]["content"]
            if "ALM Text Review and Evidence Planning" in system_content:
                skill_input = json.loads(user_content)
                return {
                    "choices": [{"message": {"content": json.dumps({
                        "assessments": [
                            {
                                "review_step": step["review_step"],
                                "applicability": "applicable",
                                "findings": [],
                                "reference_decisions": [],
                                "summary": "Actual supports Expected.",
                            }
                            for step in skill_input["steps"]
                        ]
                    })}}]
                }
            if "Image Evidence Review" in system_content:
                skill_input = json.loads(user_content[0]["text"])
                assessments = []
                for step in skill_input["steps"]:
                    assessments.append(
                        {
                            "review_step": step["review_step"],
                            "status": "manual" if request_number == 2 else "pass",
                            "reason": (
                                "The screenshot content cannot be confirmed."
                                if request_number == 2
                                else "The supplied images support Expected."
                            ),
                            "observed_media_ids": [
                                image["media_id"] for image in step["images"]
                            ],
                        }
                    )
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"assessments": assessments},
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
            raise AssertionError(f"Unexpected Skill request: {system_content[:80]}")

    def post(*_args, **kwargs):
        requests.append(kwargs["json"])
        return Response()

    monkeypatch.setattr("app.services.reviews.httpx.post", post)

    ai_config = AiConfig(base_url="https://ai.example/v1", model_name="test")
    text_result, text_skill_traces = _run_text_semantic_skills(ai_config, content)
    content["text_skill_traces"] = text_skill_traces
    ctx = ReviewContext(
        ai_config=ai_config,
        content=content,
        evidence_config=None,
        equipment_enabled=False,
        text_result=text_result,
        evidence=prepared,
    )
    image_calls = _image_review_stage(ctx)["ai_calls"]
    parsed = ctx.text_result

    assert image_calls == 3
    assert len(requests) == 4
    assert len(content["text_skill_traces"]) == 1
    for request in requests[1:]:
        assert request["messages"][0]["role"] == "system"
        assert "Image Evidence Review" in request["messages"][0]["content"]
        message = request["messages"][1]["content"]
        assert sum(part["type"] == "image_url" for part in message) <= 4
    assert len(prepared.image_skill_traces) == 3
    assert all(
        trace["capabilities"]["granted"]
        == [
            "evidence.image.content",
            "evidence.image.metadata",
            "review.step_text",
        ]
        for trace in prepared.image_skill_traces
    )
    assert parsed["step_results"][0]["issues"] == [
        {
            "status": "manual",
            "type": "screenshot",
            "summary": "The screenshot content cannot be confirmed.",
        }
    ]


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
    parsed = text_review_result()
    prepared = PreparedImageEvidence(
        external_review_enabled=True,
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
    parsed = text_review_result()
    prepared = PreparedImageEvidence(
        external_review_enabled=True,
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
    assert "does not exist" in guarded["step_results"][0]["summary"]


def test_reparse_point_evidence_requires_manual_review() -> None:
    source_path = r"\\server\approved\linked"
    parsed = text_review_result()
    prepared = PreparedImageEvidence(
        external_review_enabled=True,
        results={1: {source_path: ImageEvidenceResult(status="outside_root")}},
    )

    guarded = _apply_capability_guards(
        parsed,
        evidence_content(paths=[{"raw": source_path}], screenshot_required=True),
        EvidenceConfig(allowed_network_root=r"\\server\approved"),
        prepared,
    )

    assert guarded["step_results"][0]["status"] == "manual"
    assert guarded["verdict"] == "needs_manual_review"
    assert "reparse point" in guarded["step_results"][0]["summary"]


def test_not_applicable_step_skips_network_image_guards() -> None:
    source_path = r"\\server\approved\missing"
    parsed = text_review_result(not_applicable_steps=[1])
    prepared = PreparedImageEvidence(
        external_review_enabled=True,
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
    parsed = text_review_result()
    content = evidence_content(paths=[{"raw": r"C:\Evidence\step.png"}])

    guarded = _apply_capability_guards(
        parsed,
        content,
        EvidenceConfig(allowed_network_root=r"\\server\approved"),
    )

    assert guarded["criteria"]["path_validation"]["status"] == "fail"
    assert guarded["step_results"][0]["status"] == "fail"
    assert guarded["verdict"] == "unqualified"


def test_narrative_local_directory_skips_evidence_path_guard() -> None:
    parsed = text_review_result()
    content = evidence_content(paths=[{"raw": r"D:\PerformanceData\Report"}])
    content["steps"][0]["evidence_profile"]["path_validation_required"] = False

    guarded = _apply_capability_guards(
        parsed,
        content,
        EvidenceConfig(allowed_network_root=r"\\server\approved"),
    )

    assert guarded["criteria"]["path_validation"]["status"] == "not_applicable"
    assert guarded["step_results"][0]["status"] == "pass"
    assert guarded["verdict"] == "qualified"


def test_required_screenshot_without_reference_is_unqualified() -> None:
    parsed = text_review_result()

    guarded = _apply_capability_guards(
        parsed,
        evidence_content(screenshot_required=True),
    )

    assert guarded["criteria"]["screenshot_evidence"]["status"] == "fail"
    assert guarded["verdict"] == "unqualified"


def test_continuous_html_reports_with_all_passed_results_are_qualified() -> None:
    parent = r"\\server\approved\automation"
    first_report = parent + r"\report_34834.html"
    second_report = parent + r"\report_34834_2.html"
    parsed = text_review_result()
    content = evidence_content(
        paths=[
            {"raw": first_report, "kind": "html"},
            {"raw": second_report, "kind": "html"},
        ],
        screenshot_required=True,
    )
    prepared = PreparedImageEvidence(
        external_review_enabled=True,
        results={},
        html_results={
            1: {
                first_report: HtmlEvidenceResult(status="pass", result_count=2),
                second_report: HtmlEvidenceResult(status="pass", result_count=3),
            }
        },
    )

    guarded = _apply_capability_guards(
        parsed,
        content,
        EvidenceConfig(allowed_network_root=r"\\server\approved"),
        prepared,
    )

    assert guarded["verdict"] == "qualified"
    assert guarded["criteria"]["html_report_sequence"]["status"] == "pass"
    assert guarded["criteria"]["automation_results"]["status"] == "pass"
    assert guarded["step_results"][0]["html_evidence"][1]["result_count"] == 3


def test_missing_html_report_suffix_is_unqualified() -> None:
    parent = r"\\server\approved\automation"
    parsed = text_review_result()
    content = evidence_content(
        paths=[
            {"raw": parent + r"\report_34834.html", "kind": "html"},
            {"raw": parent + r"\report_34834_3.html", "kind": "html"},
        ]
    )

    guarded = _apply_capability_guards(parsed, content)

    assert guarded["verdict"] == "unqualified"
    assert guarded["criteria"]["html_report_sequence"]["status"] == "fail"
    assert "_2.html" in guarded["step_results"][0]["summary"]


def test_any_non_passed_html_result_is_unqualified() -> None:
    report = r"\\server\approved\automation\report_34834.html"
    parsed = text_review_result()
    content = evidence_content(paths=[{"raw": report, "kind": "html"}])
    prepared = PreparedImageEvidence(
        external_review_enabled=True,
        results={},
        html_results={
            1: {
                report: HtmlEvidenceResult(
                    status="fail",
                    result_count=2,
                    non_passed_values=("Failed",),
                )
            }
        },
    )

    guarded = _apply_capability_guards(
        parsed,
        content,
        EvidenceConfig(allowed_network_root=r"\\server\approved"),
        prepared,
    )

    assert guarded["verdict"] == "unqualified"
    assert guarded["criteria"]["automation_results"]["status"] == "fail"
    assert "Failed" in guarded["step_results"][0]["summary"]


def test_date_mismatch_does_not_affect_verdict_until_date_rules_are_enabled() -> None:
    parsed = text_review_result()
    content = evidence_content(
        dates=[{"raw": "2026-07-29", "same_day_as_execution": False}]
    )

    guarded = _apply_capability_guards(parsed, content)

    assert guarded["criteria"]["automation_timing"]["status"] == "not_applicable"
    assert guarded["criteria"]["path_validation"]["status"] == "not_applicable"
    assert guarded["verdict"] == "qualified"


def test_equipment_failure_makes_run_unqualified() -> None:
    parsed = text_review_result()
    check = {
        "review_step": 1,
        "status": "fail",
        "code": "equipment_invalid",
        "summary": "The execution date is outside the calibration period.",
        "matches": [],
        "warnings": [],
    }

    guarded = _apply_equipment_guards(parsed, [check])

    assert guarded["verdict"] == "unqualified"
    assert guarded["step_results"][0]["status"] == "fail"
    assert guarded["criteria"]["equipment_traceability"]["status"] == "fail"


def test_equipment_pass_and_current_status_warning_remain_qualified() -> None:
    parsed = text_review_result()
    warning = {
        "type": "equipment_status",
        "summary": (
            "The device is currently In Calibration; this status does not "
            "describe the execution day."
        ),
    }
    check = {
        "review_step": 1,
        "status": "pass",
        "code": "equipment_valid",
        "summary": "Identifiers and execution dates match the registry.",
        "matches": [{"equipment_id": "PCCSY-RD-CT-1-0076"}],
        "warnings": [warning],
    }

    guarded = _apply_equipment_guards(parsed, [check])

    assert guarded["verdict"] == "qualified"
    assert guarded["warnings"] == [{"step": 1, **warning}]
    assert guarded["criteria"]["equipment_traceability"]["status"] == "pass"