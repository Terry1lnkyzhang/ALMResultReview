import json
from dataclasses import replace

import httpx
import pytest

from app.services.skill_runner import (
    SkillRunner,
    _grammar_schema,
    _request_output_schema,
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
        "alm_run_status": "Passed",
        "steps": [
            {
                "review_step": 1,
                "alm_step_status": "Passed",
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

    assert definition.version == "1.7.4"
    assert len(definition.skill_hash) == 64
    assert definition.input_schema["additionalProperties"] is False
    assert definition.output_schema["additionalProperties"] is False
    assert "alm-text-review" in discover_skills()
    assert identity == {
        "skill_id": "alm-text-review",
        "status": "available",
        "version": "1.7.4",
        "skill_hash": definition.skill_hash,
    }


def test_all_review_skill_packages_are_discoverable_and_versioned() -> None:
    active = {
        "alm-text-review": "1.7.4",
        "equipment-role": "1.4.2",
        "html-evidence-review": "1.8.1",
        "image-evidence-review": "1.3.2",
        "location-consistency": "1.0.1",
    }

    assert set(discover_skills()) == set(active)
    for skill_id, version in active.items():
        definition = load_skill(skill_id)
        assert definition.version == version
        assert definition.enable_thinking is False
        assert definition.contract_version == "1"
        assert len(definition.skill_hash) == 64
        assert definition.required_capabilities
        assert not (
            set(definition.required_capabilities)
            & set(definition.forbidden_capabilities)
        )
        assert "Simplified Chinese" in definition.instructions

    html = skill_manifest_metadata("html-evidence-review")
    assert html["status"] == "available"
    assert html["version"] == "1.8.1"
    assert load_skill("html-evidence-review").max_tokens == 32768


def test_review_skill_examples_cover_failed_run_record_consistency() -> None:
    text_examples = load_skill("alm-text-review").examples
    failed_text = next(
        example
        for example in text_examples
        if example["input"].get("alm_step_status") == "Failed"
        and "14.2 seconds" in example["input"]["actual"]
    )
    no_run_text = next(
        example
        for example in text_examples
        if example["input"].get("alm_step_status") == "No Run"
    )
    contradictory_text = next(
        example
        for example in text_examples
        if example["input"].get("alm_step_status") == "Failed"
        and "8.1 seconds" in example["input"]["actual"]
    )
    image_failed = next(
        example
        for example in load_skill("image-evidence-review").examples
        if example["input"].get("alm_step_status") == "Failed"
    )
    html_failed = next(
        example
        for example in load_skill("html-evidence-review").examples
        if example["input"].get("alm_step_status") == "Failed"
    )

    assert failed_text["output"]["findings"] == []
    assert no_run_text["input"]["actual"] == ""
    assert no_run_text["output"]["findings"] == []
    assert contradictory_text["output"]["findings"][0]["code"] == (
        "expected_actual_mismatch"
    )
    assert image_failed["output"]["status"] == "pass"
    assert html_failed["output"]["status"] == "pass"


def test_alm_text_examples_distinguish_explained_and_unexplained_na() -> None:
    examples = load_skill("alm-text-review").examples
    explained = next(
        example for example in examples
        if "service laptop" in example["input"]["actual"]
    )
    unexplained = next(
        example for example in examples
        if "0.34s/r rotation" in example["input"]["description"]
    )

    assert explained["output"]["applicability"] == "not_applicable"
    assert explained["output"]["findings"] == []
    assert unexplained["output"]["findings"][0]["code"] == "record_documentation_gap"


def test_alm_text_examples_keep_active_dms_failure_separate_from_missing_na() -> None:
    examples = load_skill("alm-text-review").examples
    dms_examples = [
        example for example in examples
        if example["input"].get("execution_location_config", {}).get("dms_coverage")
        == "4cm"
    ]

    assert len(dms_examples) == 2
    assert [example["output"]["applicability"] for example in dms_examples] == [
        "applicable", "applicable"
    ]
    assert [example["output"]["findings"][0]["severity"] for example in dms_examples] == [
        "manual", "fail"
    ]


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
    assert trace["skill_version"] == "1.7.4"
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


_TEXT_GRANTS = {
    "review.step_text",
    "review.text_format",
    "review.numbered_comparison",
    "evidence.path_metadata",
    "equipment.registry.candidates",
}


@pytest.fixture
def thinking_enabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.skill_runner.load_skill",
        lambda skill_id: replace(load_skill(skill_id), enable_thinking=True),
    )


def test_skill_runner_disables_thinking_and_strips_inline_thinking(monkeypatch) -> None:
    requests = []
    answer = json.dumps(
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

    def post(*args, **kwargs):
        requests.append(kwargs["json"])
        return StubResponse(f"<think>\nCheck the folder path {{}}.\n</think>\n\n{answer}")

    monkeypatch.setattr("app.services.skill_runner.httpx.post", post)

    trace = SkillRunner().run(
        "alm-text-review",
        skill_input(),
        endpoint="https://ai.example/v1/chat/completions",
        model_name="test-model",
        headers={},
        timeout_seconds=30,
        granted_capabilities=_TEXT_GRANTS,
    )

    assert trace["status"] == "completed"
    assert trace["enable_thinking"] is False
    assert trace["ai_calls"] == 1
    assert requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert requests[0]["temperature"] == 0.7
    assert requests[0]["seed"] == trace["seed"] == int(trace["input_hash"][:8], 16) & 0x7FFFFFFF
    assert requests[0]["response_format"]["type"] == "json_schema"
    assessments = requests[0]["response_format"]["json_schema"]["schema"][
        "properties"
    ]["assessments"]
    assert assessments["items"] is False
    assert assessments["minItems"] == assessments["maxItems"] == 1
    step = assessments["prefixItems"][0]["properties"]
    assert step["review_step"] == {"enum": [1]}
    assert [
        decision["properties"]["candidate_id"]
        for decision in step["reference_decisions"]["prefixItems"]
    ] == [{"enum": ["step-1-ref-1"]}]


def test_request_schema_limits_ids_to_supplied_values() -> None:
    html = _request_output_schema(
        load_skill("html-evidence-review"),
        {
            "steps": [
                {
                    "review_step": 8,
                    "batch_observations": [],
                    "reports": [
                        {"report_id": "r1", "blocks": [{"block_id": "b1"}, {"block_id": "b2"}]},
                        {"report_id": "r2", "blocks": [{"block_id": "b9"}]},
                    ],
                }
            ]
        },
    )
    step = html["properties"]["assessments"]["prefixItems"][0]["properties"]
    assert [item["enum"] for item in step["reviewed_report_ids"]["prefixItems"]] == [
        ["r1"],
        ["r2"],
    ]
    assert [
        (option["properties"]["report_id"]["enum"], option["properties"]["block_id"]["enum"])
        for option in step["evidence"]["items"]["anyOf"]
    ] == [(["r1"], ["b1", "b2"]), (["r2"], ["b9"])]
    assert "uniqueItems" not in json.dumps(html)

    final = _request_output_schema(
        load_skill("html-evidence-review"),
        {
            "steps": [
                {
                    "review_step": 8,
                    "batch_observations": [
                        {"evidence": [{"report_id": "r2", "block_id": "b9"}]}
                    ],
                    "reports": [
                        {"report_id": "r1", "blocks": []},
                        {"report_id": "r2", "blocks": []},
                    ],
                }
            ]
        },
    )
    final_step = final["properties"]["assessments"]["prefixItems"][0]["properties"]
    assert [
        option["properties"]["block_id"]["enum"]
        for option in final_step["evidence"]["items"]["anyOf"]
    ] == [["b9"]]

    equipment = _request_output_schema(
        load_skill("equipment-role"),
        {
            "registry_equipment_names": ["Stopwatch"],
            "steps": [
                {"review_step": 4, "candidate_equipment": [{"equipment_id": "EQ-1"}]},
                {"review_step": 5, "candidate_equipment": []},
            ],
        },
    )
    first, second = equipment["properties"]["decisions"]["prefixItems"]
    assert first["properties"]["selected_equipment_ids"]["items"]["enum"] == ["EQ-1"]
    assert first["properties"]["selected_equipment_names"]["items"]["enum"] == [
        "Stopwatch"
    ]
    assert second["properties"]["selected_equipment_ids"]["maxItems"] == 0

    image = _request_output_schema(
        load_skill("image-evidence-review"),
        {"steps": [{"review_step": 3, "images": [{"media_id": "m1"}, {"media_id": "m2"}]}]},
    )
    observed = image["properties"]["assessments"]["prefixItems"][0]["properties"][
        "observed_media_ids"
    ]
    assert [item["enum"] for item in observed["prefixItems"]] == [["m1"], ["m2"]]
    assert observed["items"] is False


def test_skill_runner_seed_is_stable_for_identical_input(monkeypatch) -> None:
    seeds = []

    def post(*args, **kwargs):
        seeds.append(kwargs["json"]["seed"])
        return StubResponse("{}")

    monkeypatch.setattr("app.services.skill_runner.httpx.post", post)
    for _ in range(2):
        SkillRunner().run(
            "alm-text-review",
            skill_input(),
            endpoint="https://ai.example/v1/chat/completions",
            model_name="test-model",
            headers={},
            timeout_seconds=30,
            granted_capabilities=_TEXT_GRANTS,
        )

    assert len(set(seeds)) == 1


def test_grammar_schema_drops_keywords_the_server_rejects() -> None:
    schema = load_skill("html-evidence-review").output_schema

    assert "uniqueItems" in json.dumps(schema)
    assert "uniqueItems" not in json.dumps(_grammar_schema(schema))


def test_skill_runner_reports_thinking_that_exhausts_the_token_budget(
    monkeypatch, thinking_enabled,
) -> None:
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs["json"])
        return StubResponse("<think>\nStill reasoning about the evidence")

    monkeypatch.setattr("app.services.skill_runner.httpx.post", post)

    trace = SkillRunner().run(
        "alm-text-review",
        skill_input(),
        endpoint="https://ai.example/v1/chat/completions",
        model_name="test-model",
        headers={},
        timeout_seconds=30,
        granted_capabilities=_TEXT_GRANTS,
    )

    assert trace["status"] == "failed"
    assert "while thinking" in trace["error"]
    assert trace["retryable"] is False
    assert trace["ai_calls"] == 2
    assert [call["chat_template_kwargs"]["enable_thinking"] for call in calls] == [
        True, False
    ]


def test_skill_runner_retries_empty_thinking_once_without_thinking(
    monkeypatch, thinking_enabled,
) -> None:
    calls = []
    answer = json.dumps(
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
                            "reason": "Actual cites the screenshot folder.",
                        }
                    ],
                    "summary": "Screenshot folder identified.",
                }
            ]
        }
    )

    def post(*args, **kwargs):
        calls.append(kwargs["json"])
        return StubResponse("<think>Still reasoning") if len(calls) == 1 else StubResponse(answer)

    monkeypatch.setattr("app.services.skill_runner.httpx.post", post)

    trace = SkillRunner().run(
        "alm-text-review",
        skill_input(),
        endpoint="https://ai.example/v1/chat/completions",
        model_name="test-model",
        headers={},
        timeout_seconds=30,
        granted_capabilities=_TEXT_GRANTS,
    )

    assert trace["status"] == "completed"
    assert trace["ai_calls"] == 2
    assert trace["thinking_fallback"] == "disabled"
    assert trace["repairs"] == []
    assert [call["chat_template_kwargs"]["enable_thinking"] for call in calls] == [
        True, False
    ]
    assert calls[0]["messages"] == calls[1]["messages"]
    assert calls[0]["response_format"] == calls[1]["response_format"]
    assert calls[0]["max_tokens"] == calls[1]["max_tokens"]
    assert trace["output"]["assessments"][0]["review_step"] == 1


def test_skill_runner_retries_reasoning_content_without_visible_answer(
    monkeypatch, thinking_enabled,
) -> None:
    calls = []
    answer = json.dumps(
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
                            "reason": "Actual cites the screenshot folder.",
                        }
                    ],
                    "summary": "Screenshot folder identified.",
                }
            ]
        }
    )

    class ThinkingResponse(StubResponse):
        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {"content": "", "reasoning_content": "Still thinking"},
                        "finish_reason": "length",
                    }
                ]
            }

    def post(*_args, **kwargs):
        calls.append(kwargs["json"])
        return ThinkingResponse("") if len(calls) == 1 else StubResponse(answer)

    monkeypatch.setattr("app.services.skill_runner.httpx.post", post)

    trace = SkillRunner().run(
        "alm-text-review",
        skill_input(),
        endpoint="https://ai.example/v1/chat/completions",
        model_name="test-model",
        headers={},
        timeout_seconds=30,
        granted_capabilities=_TEXT_GRANTS,
    )

    assert trace["thinking_fallback"] == "disabled"
    assert trace["status"] == "completed"
    assert trace["ai_calls"] == 2
    assert calls[1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "while thinking" not in trace.get("error", "")


def test_output_token_cap_grows_with_the_batch_but_never_shrinks(monkeypatch) -> None:
    definition = load_skill("alm-text-review")
    requests: list[dict] = []

    def post(*args, **kwargs):
        payload = kwargs["json"]
        requests.append(payload)
        steps = json.loads(payload["messages"][1]["content"])["steps"]
        return StubResponse(
            json.dumps(
                {
                    "assessments": [
                        {
                            "review_step": step["review_step"],
                            "applicability": "applicable",
                            "findings": [],
                            "reference_decisions": [],
                            "extracted_equipment": [],
                            "summary": "Actual matches Expected.",
                        }
                        for step in steps
                    ]
                }
            )
        )

    monkeypatch.setattr("app.services.skill_runner.httpx.post", post)

    def cap_for(step_count: int) -> int:
        payload = skill_input()
        template = payload["steps"][0]
        payload["steps"] = [
            {**template, "review_step": number, "reference_candidates": []}
            for number in range(1, step_count + 1)
        ]
        trace = SkillRunner().run(
            "alm-text-review",
            payload,
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
        return trace["max_tokens"]

    assert cap_for(1) == definition.max_tokens
    assert cap_for(15) == definition.max_tokens_per_item * 15
    assert requests[-1]["max_tokens"] > definition.max_tokens


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
    assert trace["failure_kind"] == "invalid_output"
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
        assert "failure_kind" not in trace
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
    from app.services.equipment_pipeline import request_equipment_disambiguation
    from app.services.equipment_review import Candidate, OpenQuestion

    monkeypatch.setattr(
        "app.services.equipment_pipeline.httpx.post",
        lambda *args, **kwargs: StubResponse(
            json.dumps(
                {
                    "decisions": [
                        {
                            "review_step": 1,
                            "role": "controlled_equipment",
                            "required": True,
                            "selected_equipment_ids": ["INVENTED-DEVICE"],
                            "selected_equipment_names": [],
                            "reason": "Invented selection.",
                        }
                    ]
                }
            )
        ),
    )

    with pytest.raises(ValueError, match="unavailable equipment ID"):
        request_equipment_disambiguation(
            AiConfig(base_url="https://ai.example/v1", model_name="test"),
            [
                OpenQuestion(
                    review_step=1,
                    kind="role",
                    description="Record equipment.",
                    expected="Equipment ID is recorded.",
                    actual="Used equipment.",
                    candidates=(
                        Candidate(
                            equipment_id="EQ-100",
                            description="Meter",
                            model_number="M1",
                            serial_number="S1",
                        ),
                    ),
                )
            ],
            ["Meter"],
        )


def test_equipment_skill_adapter_rejects_a_name_outside_the_registry(monkeypatch) -> None:
    from app.models import AiConfig
    from app.services.equipment_pipeline import request_equipment_disambiguation
    from app.services.equipment_review import OpenQuestion

    monkeypatch.setattr(
        "app.services.equipment_pipeline.httpx.post",
        lambda *args, **kwargs: StubResponse(
            json.dumps(
                {
                    "decisions": [
                        {
                            "review_step": 1,
                            "role": "controlled_equipment",
                            "required": True,
                            "selected_equipment_ids": [],
                            "selected_equipment_names": ["Invented device"],
                            "reason": "Invented selection.",
                        }
                    ]
                }
            )
        ),
    )

    with pytest.raises(ValueError, match="outside the registry vocabulary"):
        request_equipment_disambiguation(
            AiConfig(base_url="https://ai.example/v1", model_name="test"),
            [
                OpenQuestion(
                    review_step=1,
                    kind="name_mapping",
                    description="Record equipment.",
                    expected="Equipment ID is recorded.",
                    actual="Used equipment.",
                    device_names=("Meter",),
                )
            ],
            ["Meter"],
        )
