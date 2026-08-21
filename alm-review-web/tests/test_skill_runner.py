import json

import httpx
import pytest

from app.services.skill_runner import (
    SkillRunner,
    discover_skills,
    load_skill,
    skill_manifest_metadata,
    skill_policy_identity,
)


class StubResponse:
    def __init__(self, content: str) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self.content}}]}


class FailingResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = "quota exceeded"

    def raise_for_status(self) -> None:
        raise httpx.HTTPStatusError(
            f"Error '{self.status_code}' for url 'https://ai.example'",
            request=httpx.Request("POST", "https://ai.example"),
            response=self,
        )


def skill_input() -> dict:
    return {
        "steps": [
            {
                "review_step": 1,
                "description": "Archive review evidence.",
                "expected": "Evidence is available.",
                "actual": r"Screenshots saved under \\server\share\case-1",
                "actual_format": {},
                "numbered_comparison": [],
                "reference_candidates": [
                    {
                        "candidate_id": "step-1-ref-1",
                        "type": "folder_or_unknown",
                        "value": r"\\server\share\case-1",
                        "source_field": "actual",
                        "detection_source": "path_without_extension",
                    }
                ],
            }
        ]
    }


def test_alm_text_skill_package_has_versioned_policy_identity() -> None:
    definition = load_skill("alm-text-review")
    identity = skill_policy_identity("alm-text-review")

    assert definition.version == "1.2.1"
    assert len(definition.skill_hash) == 64
    assert definition.input_schema["additionalProperties"] is False
    assert definition.output_schema["additionalProperties"] is False
    assert "alm-text-review" in discover_skills()
    assert identity == {
        "skill_id": "alm-text-review",
        "status": "available",
        "version": "1.2.1",
        "skill_hash": definition.skill_hash,
    }


def test_all_review_skill_packages_are_discoverable_and_versioned() -> None:
    active = {
        "alm-text-review": "1.2.1",
        "equipment-role": "1.0.2",
        "image-evidence-review": "1.0.3",
    }

    assert set(discover_skills()) == {*active, "html-evidence-review"}
    for skill_id, version in active.items():
        definition = load_skill(skill_id)
        assert definition.version == version
        assert definition.contract_version == "1"
        assert len(definition.skill_hash) == 64
        assert definition.required_capabilities
        assert not (
            set(definition.required_capabilities)
            & set(definition.forbidden_capabilities)
        )

    planned = skill_manifest_metadata("html-evidence-review")
    assert planned["status"] == "planned"
    assert planned["version"] == "0.1.0"
    with pytest.raises(ValueError, match="Unsupported review Skill"):
        load_skill("html-evidence-review")


def test_skill_runner_validates_input_output_and_separates_untrusted_data(
    monkeypatch,
) -> None:
    requests = []

    def post(*args, **kwargs):
        requests.append(kwargs["json"])
        return StubResponse(
            json.dumps(
                {
                    "assessments": [
                        {
                            "review_step": 1,
                            "applicability": "applicable",
                            "findings": [],
                            "reference_decisions": [
                                {
                                    "candidate_id": "step-1-ref-1",
                                    "role": "result_evidence_location",
                                    "requires_check": True,
                                    "reason": "Actual identifies an evidence location.",
                                }
                            ],
                            "summary": "Actual identifies the expected evidence.",
                        }
                    ]
                }
            )
        )

    monkeypatch.setattr("app.services.skill_runner.httpx.post", post)

    trace = SkillRunner().run(
        "alm-text-review",
        skill_input(),
        endpoint="https://ai.example/v1/chat/completions",
        model_name="test-model",
        headers={},
        timeout_seconds=30,
        granted_capabilities={
            "review.step_text",
            "review.text_format",
            "review.numbered_comparison",
            "evidence.path_metadata",
            "equipment.registry.candidates",
        },
    )

    assert trace["status"] == "completed"
    assert trace["skill_version"] == "1.2.1"
    assert len(trace["skill_hash"]) == 64
    assert len(trace["input_hash"]) == 64
    assert len(trace["output_hash"]) == 64
    assert trace["ai_calls"] == 1
    assert trace["output"]["assessments"][0]["reference_decisions"][0][
        "role"
    ] == "result_evidence_location"
    assert requests[0]["messages"][0]["role"] == "system"
    assert requests[0]["messages"][1]["role"] == "user"
    assert "Screenshots saved" not in requests[0]["messages"][0]["content"]
    assert "Screenshots saved" in requests[0]["messages"][1]["content"]


def test_skill_runner_contains_invalid_ai_output_as_failed_trace(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.skill_runner.httpx.post",
        lambda *args, **kwargs: StubResponse(
            json.dumps(
                {
                    "assessments": [
                        {
                            "review_step": 1,
                            "applicability": "applicable",
                            "findings": [],
                            "reference_decisions": [
                                {
                                    "candidate_id": "step-1-ref-1",
                                    "role": "invented_role",
                                    "requires_check": True,
                                    "reason": "Invalid output",
                                }
                            ],
                            "summary": "Invalid output",
                        }
                    ]
                }
            )
        ),
    )

    trace = SkillRunner().run(
        "alm-text-review",
        skill_input(),
        endpoint="https://ai.example/v1/chat/completions",
        model_name="test-model",
        headers={},
        timeout_seconds=30,
        granted_capabilities={
            "review.step_text",
            "review.text_format",
            "review.numbered_comparison",
            "evidence.path_metadata",
            "equipment.registry.candidates",
        },
    )

    assert trace["status"] == "failed"
    assert trace["ai_calls"] == 2
    assert trace["retryable"] is False
    assert len(trace["repairs"]) == 2
    assert "validation error" in trace["error"]
    assert "output" not in trace


def _text_assessment(role: str) -> str:
    return json.dumps(
        {
            "assessments": [
                {
                    "review_step": 1,
                    "applicability": "applicable",
                    "findings": [],
                    "reference_decisions": [
                        {
                            "candidate_id": "step-1-ref-1",
                            "role": role,
                            "requires_check": True,
                            "reason": "Evidence location.",
                        }
                    ],
                    "summary": "Reviewed.",
                }
            ]
        }
    )


def _run_text_skill() -> dict:
    return SkillRunner().run(
        "alm-text-review",
        skill_input(),
        endpoint="https://ai.example/v1/chat/completions",
        model_name="test-model",
        headers={},
        timeout_seconds=30,
        granted_capabilities={
            "review.step_text",
            "review.text_format",
            "review.numbered_comparison",
            "evidence.path_metadata",
            "equipment.registry.candidates",
        },
    )


def test_skill_runner_repairs_invalid_output_on_the_second_attempt(monkeypatch) -> None:
    requests: list[dict] = []
    replies = iter([_text_assessment("invented_role"), _text_assessment("uncertain")])

    def stub_post(*args, **kwargs) -> StubResponse:
        requests.append(kwargs["json"])
        return StubResponse(next(replies))

    monkeypatch.setattr("app.services.skill_runner.httpx.post", stub_post)

    trace = _run_text_skill()

    assert trace["status"] == "completed"
    assert trace["ai_calls"] == 2
    assert len(trace["repairs"]) == 1
    repair_message = requests[1]["messages"][-1]["content"]
    assert "rejected by the output validator" in repair_message
    assert "invented_role" in repair_message


def test_skill_runner_marks_client_errors_terminal_and_server_errors_retryable(
    monkeypatch,
) -> None:
    for status_code, retryable in ((401, False), (400, False), (503, True)):
        monkeypatch.setattr(
            "app.services.skill_runner.httpx.post",
            lambda *args, _code=status_code, **kwargs: FailingResponse(_code),
        )

        trace = _run_text_skill()

        assert trace["status"] == "failed"
        assert trace["ai_calls"] == 1
        assert trace["retryable"] is retryable
        assert "quota exceeded" in trace["error"]


def test_image_skill_cannot_receive_media_without_image_capability() -> None:
    trace = SkillRunner().run(
        "image-evidence-review",
        {
            "steps": [
                {
                    "review_step": 1,
                    "description": "Review evidence.",
                    "expected": "Evidence supports the result.",
                    "actual": "Evidence was captured.",
                    "images": [
                        {
                            "media_id": "step-1-image-1",
                            "source_path": r"\\server\approved\case",
                            "relative_name": "Step1.png",
                            "mime_type": "image/png",
                            "sha256": "a" * 64,
                            "width": 100,
                            "height": 100,
                        }
                    ],
                }
            ]
        },
        endpoint="https://ai.example/v1/chat/completions",
        model_name="test-model",
        headers={},
        timeout_seconds=30,
        granted_capabilities={"review.step_text", "evidence.image.metadata"},
        media_parts=[
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
        ],
    )

    assert trace["status"] == "failed"
    assert trace["ai_calls"] == 0
    assert "required capabilities were denied" in trace["error"]


def test_equipment_skill_adapter_rejects_unavailable_equipment_id(monkeypatch) -> None:
    from app.models import AiConfig
    from app.services.reviews import _request_equipment_disambiguation

    monkeypatch.setattr(
        "app.services.reviews.httpx.post",
        lambda *args, **kwargs: StubResponse(
            json.dumps(
                {
                    "decisions": [
                        {
                            "review_step": 1,
                            "role": "controlled_equipment",
                            "required": True,
                            "selected_equipment_ids": ["INVENTED-DEVICE"],
                            "reason": "Invented selection.",
                        }
                    ]
                }
            )
        ),
    )

    with pytest.raises(ValueError, match="unavailable equipment ID"):
        _request_equipment_disambiguation(
            AiConfig(base_url="https://ai.example/v1", model_name="test"),
            [
                {
                    "step": 1,
                    "description": "Record equipment.",
                    "expected": "Equipment ID is recorded.",
                    "actual": "Used equipment.",
                    "reported_identifiers": [],
                    "previously_matched_equipment_ids": [],
                    "candidate_equipment": [
                        {
                            "equipment_id": "EQ-100",
                            "description": "Meter",
                            "model_number": "M1",
                            "serial_number": "S1",
                        }
                    ],
                }
            ],
        )
