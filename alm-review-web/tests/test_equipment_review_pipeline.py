import json
from datetime import date
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AiConfig,
    AlmRun,
    EquipmentRegistry,
    EvidenceConfig,
    PromptVersion,
    ReviewJob,
    RunRevision,
    Workspace,
)
from app.services.automation_release import AutomationReleaseRecord
from app.services.equipment_review import OpenQuestion
from app.services.html_evidence import HtmlEvidenceBlock, HtmlEvidenceResult
from app.services.reviews import (
    _build_review_plan,
    _compact_html_quote,
    _equipment_source_field,
    _html_skill_batch_inputs,
    _run_html_review_batches,
    _validate_html_assessment,
    process_job,
)
from app.services.skill_runner import SkillFailure
from app.services.workspaces import default_workspace


class StubResponse:
    def __init__(self, content: str) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": self.content}}]}


def main_review_response() -> str:
    return json.dumps(
        {
            "reviewed_steps": [1],
            "not_applicable_steps": [],
            "issues": [],
            "warnings": [],
            "summary": "No language or semantic problem found.",
        }
    )


def semantic_skill_response(request: dict[str, Any]) -> StubResponse | None:
    messages = request["messages"]
    if messages[0]["role"] != "system":
        return None
    system_content = messages[0]["content"]
    user_content = messages[1]["content"]
    if not isinstance(user_content, str):
        return None
    skill_input = json.loads(user_content)
    steps = skill_input["steps"]
    if "ALM Text Review and Evidence Planning" in system_content:
        output = {
            "assessments": [
                {
                    "review_step": step["review_step"],
                    "applicability": "applicable",
                    "findings": [],
                    "reference_decisions": [
                        {
                            "candidate_id": candidate["candidate_id"],
                            "role": (
                                "test_equipment"
                                if candidate["detection_source"]
                                == "equipment_registry_match"
                                else "dut_or_other"
                                if candidate["type"] == "equipment"
                                else "result_evidence"
                            ),
                            "requires_check": (
                                candidate["type"] != "equipment"
                                or candidate["detection_source"]
                                == "equipment_registry_match"
                            ),
                            "reason": "The candidate is relevant to this review step.",
                        }
                        for candidate in step["reference_candidates"]
                    ],
                    "summary": "Actual supports Expected.",
                }
                for step in steps
            ]
        }
    elif "HTML Evidence Review" in system_content:
        output = {
            "assessments": [
                {
                    "review_step": step["review_step"],
                    "status": "pass",
                    "description_coverage": "supported",
                    "expected_coverage": "supported",
                    "actual_coverage": "supported",
                    "result_consistency": "consistent",
                    "release_consistency": (
                        "not_checked"
                        if step["automation_release"]["status"] == "disabled"
                        else "matched"
                    ),
                    "reviewed_report_ids": [
                        report["report_id"] for report in step["reports"]
                    ],
                    "evidence": [
                        {
                            "report_id": step["reports"][0]["report_id"],
                            "block_id": step["reports"][0]["blocks"][0]["block_id"],
                            "quote": step["reports"][0]["blocks"][0]["text"],
                            "supports": [
                                "description",
                                "expected",
                                "actual",
                                "result",
                            ],
                        }
                    ],
                    "reason": "The report supports the Step and its passing result.",
                }
                for step in steps
            ]
        }
    elif "Evidence Intent Review" in system_content:
        output = {
            "decisions": [
                {
                    "review_step": step["review_step"],
                    "intent": "image_evidence",
                    "confidence": 0.97,
                    "reason": "Actual explicitly identifies review evidence.",
                }
                for step in steps
            ]
        }
    else:
        return None
    return StubResponse(json.dumps(output, ensure_ascii=False))


def add_job(db: Session, *, description: str, expected: str, actual: str) -> ReviewJob:
    snapshot = {
        "run": {
            "id": "42",
            "status": "Passed",
            "execution-date": "2026-08-01",
            "execution-time": "10:30:00",
            "steps": [
                {
                    "step-order": "1",
                    "name": "Equipment setup",
                    "status": "Passed",
                    "descriptionText": description,
                    "expectedText": expected,
                    "actualText": actual,
                    "execution-date": "2026-08-01",
                    "execution-time": "10:31:00",
                }
            ],
        }
    }
    run = AlmRun(
        run_id=42,
        run_status="Passed",
        source_hash="a" * 64,
        review_hash="b" * 64,
        raw_json=json.dumps(snapshot),
    )
    db.add(run)
    db.flush()
    revision = RunRevision(
        run_id=run.run_id,
        revision_number=1,
        source_hash=run.source_hash,
        review_hash=run.review_hash,
        snapshot_json=json.dumps(snapshot),
    )
    db.add(revision)
    db.flush()
    run.current_revision_id = revision.id
    job = ReviewJob(run_id=run.run_id, revision_id=revision.id, status="queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def configure_review(db: Session) -> None:
    db.add(
        AiConfig(
            id=1,
            base_url="http://ai.test/v1/chat/completions",
            model_name="test-model",
            enabled=True,
        )
    )
    db.add(
        PromptVersion(
            name="test",
            template="Review this content: {{RUN_CONTENT}}",
            is_active=True,
        )
    )
    db.commit()


def test_review_plan_routes_text_report_and_equipment_steps() -> None:
    content = {
        "steps": [
            {
                "review_step": 1,
                "evidence_profile": {
                    "actual_paths": [
                        {"raw": r"\\server\reports\result.html", "kind": "html"}
                    ]
                },
            },
            {"review_step": 2, "evidence_profile": {"actual_paths": []}},
        ]
    }
    plan = _build_review_plan(
        content,
        [
            {"review_step": 1, "status": "not_applicable"},
            {"review_step": 2, "status": "manual"},
        ],
        [
            OpenQuestion(
                review_step=1,
                kind="name_mapping",
                description="",
                expected="",
                actual="",
            )
        ],
    )

    assert plan.text_steps == (1, 2)
    assert [(item.review_step, item.paths) for item in plan.report_requests] == [
        (1, (r"\\server\reports\result.html",))
    ]
    assert plan.equipment_steps == (2, 1)


def test_review_plan_always_includes_explicit_html_even_when_text_routing_rejects_it() -> None:
    report = r"\\server\reports\result.html"
    content = {
        "steps": [
            {
                "review_step": 1,
                "evidence_profile": {
                    "actual_paths": [
                        {"raw": report, "kind": "html", "route_requested": False}
                    ],
                    "routing": {"actions": []},
                },
            }
        ]
    }

    plan = _build_review_plan(content, [])

    assert [(item.review_step, item.paths) for item in plan.report_requests] == [
        (1, (report,))
    ]


def test_html_ai_citations_must_match_supplied_report_content() -> None:
    skill_input = {
        "steps": [
            {
                "review_step": 1,
                "automation_release": {
                    "status": "disabled",
                },
                "reports": [
                    {
                        "report_id": "report-1",
                        "content_truncated": False,
                        "blocks": [
                            {"block_id": "block-1", "text": "Observed result: Passed"}
                        ],
                    }
                ],
            }
        ]
    }
    assessment = {
        "status": "fail",
        "release_consistency": "not_checked",
        "reviewed_report_ids": ["report-1"],
        "evidence": [
            {
                "report_id": "report-1",
                "block_id": "block-1",
                "quote": "Observed result: Failed",
            }
        ],
    }

    with pytest.raises(SkillFailure, match="cited text outside"):
        _validate_html_assessment(skill_input, assessment)


def test_html_ai_citation_may_select_exact_lines_in_original_order() -> None:
    skill_input = {
        "steps": [
            {
                "review_step": 1,
                "automation_release": {
                    "status": "disabled",
                },
                "reports": [
                    {
                        "report_id": "report-1",
                        "content_truncated": False,
                        "blocks": [
                            {
                                "block_id": "summary",
                                "text": "Passed: 9\nManual: 0\nFailed: 0",
                            }
                        ],
                    }
                ],
            }
        ]
    }
    assessment = {
        "status": "pass",
        "description_coverage": "supported",
        "expected_coverage": "supported",
        "actual_coverage": "supported",
        "result_consistency": "consistent",
        "release_consistency": "not_checked",
        "reviewed_report_ids": ["report-1"],
        "evidence": [
            {
                "report_id": "report-1",
                "block_id": "summary",
                "quote": "Passed: 9\nFailed: 0",
            }
        ],
    }

    _validate_html_assessment(skill_input, assessment)


def test_html_ai_citation_may_quote_an_exact_excerpt_of_a_long_line() -> None:
    skill_input = {
        "steps": [
            {
                "review_step": 1,
                "automation_release": {"status": "disabled"},
                "reports": [
                    {
                        "report_id": "report-1",
                        "content_truncated": False,
                        "blocks": [
                            {
                                "block_id": "result-1",
                                "text": (
                                    "expect: Press 0 and verify Colon:350,10. "
                                    "The displayed values match the previous step."
                                ),
                            }
                        ],
                    }
                ],
            }
        ]
    }
    assessment = {
        "status": "pass",
        "description_coverage": "supported",
        "expected_coverage": "supported",
        "actual_coverage": "supported",
        "result_consistency": "consistent",
        "release_consistency": "not_checked",
        "reviewed_report_ids": ["report-1"],
        "evidence": [
            {
                "report_id": "report-1",
                "block_id": "result-1",
                "quote": "expect: Press 0 and verify Colon:350,10.",
            }
        ],
    }

    _validate_html_assessment(skill_input, assessment)


def test_html_quote_compaction_keeps_ordered_exact_line_excerpts() -> None:
    quote = "\n".join(
        [
            "description: " + "a" * 500,
            "expect: " + "b" * 500,
            "actual: " + "c" * 500,
            "status: Pass",
        ]
    )

    compacted = _compact_html_quote(quote)

    assert len(compacted) <= 400
    compacted_lines = compacted.splitlines()
    source_lines = quote.splitlines()
    assert len(compacted_lines) == len(source_lines)
    assert all(
        source_line.startswith(compacted_line)
        for source_line, compacted_line in zip(
            source_lines, compacted_lines, strict=True
        )
    )
    assert compacted_lines[-1] == "status: Pass"


def test_html_quote_compaction_handles_many_short_lines() -> None:
    quote = "\n".join([*(f"line-{index}" for index in range(500)), "status: Pass"])

    compacted = _compact_html_quote(quote)

    assert len(compacted) <= 400
    assert compacted.splitlines()[-1] == "status: Pass"


def test_html_skill_batches_cover_every_block_without_truncation() -> None:
    reports = [
        {
            "report_id": f"report-{report_index}",
            "source_path": rf"\\server\report-{report_index}.html",
            "filename": f"report-{report_index}.html",
            "sha256": f"{report_index:x}" * 64,
            "content_truncated": False,
            "blocks": [
                {
                    "block_id": f"block-{block_index}",
                    "text": f"report {report_index} block {block_index} "
                    + "x" * 1900,
                }
                for block_index in range(1, 5)
            ],
        }
        for report_index in range(1, 9)
    ]
    skill_input = {
        "review_mode": "final",
        "batch_index": 1,
        "batch_count": 1,
        "steps": [
            {
                "review_step": 1,
                "description": "Review all automation reports.",
                "expected": "Every required result passes.",
                "actual": "All reports passed.",
                "automation_release": {"status": "disabled"},
                "batch_observations": [],
                "reports": reports,
            }
        ],
    }

    batches = _html_skill_batch_inputs(skill_input, char_budget=10_000)

    assert len(batches) > 1
    assert all(batch["review_mode"] == "evidence_batch" for batch in batches)
    assert all(batch["batch_count"] == len(batches) for batch in batches)
    assert all(
        not report["content_truncated"]
        for batch in batches
        for report in batch["steps"][0]["reports"]
    )
    sent_blocks = [
        (report["report_id"], block["block_id"])
        for batch in batches
        for report in batch["steps"][0]["reports"]
        for block in report["blocks"]
    ]
    expected_blocks = [
        (report["report_id"], block["block_id"])
        for report in reports
        for block in report["blocks"]
    ]
    assert sent_blocks == expected_blocks
    assert all(
        sum(
            len(block["text"])
            for report in batch["steps"][0]["reports"]
            for block in report["blocks"]
        )
        <= 10_000
        for batch in batches
    )


def test_html_review_batches_finish_with_one_synthesis_call(monkeypatch) -> None:
    reports = [
        {
            "report_id": f"report-{report_index}",
            "source_path": rf"\\server\report-{report_index}.html",
            "filename": f"report-{report_index}.html",
            "sha256": f"{report_index:x}" * 64,
            "content_truncated": False,
            "blocks": [
                {
                    "block_id": f"block-{block_index}",
                    "text": f"Result {block_index}: Passed " + "x" * 7500,
                }
                for block_index in range(1, 5)
            ],
        }
        for report_index in range(1, 3)
    ]
    skill_input = {
        "review_mode": "final",
        "batch_index": 1,
        "batch_count": 1,
        "steps": [
            {
                "review_step": 1,
                "description": "Review both reports.",
                "expected": "All results pass.",
                "actual": "Both reports passed.",
                "automation_release": {"status": "disabled"},
                "batch_observations": [],
                "reports": reports,
            }
        ],
    }
    calls = []

    def run_skill(_ctx, request, *, assessment_validator):
        calls.append((request, assessment_validator))
        step = request["steps"][0]
        report = step["reports"][0]
        block = report["blocks"][0] if report["blocks"] else None
        assessment = {
            "review_step": 1,
            "status": "pass",
            "description_coverage": "supported",
            "expected_coverage": "supported",
            "actual_coverage": "supported",
            "result_consistency": "consistent",
            "release_consistency": "not_checked",
            "reviewed_report_ids": [
                item["report_id"] for item in step["reports"]
            ],
            "evidence": (
                [
                    {
                        "report_id": report["report_id"],
                        "block_id": block["block_id"],
                        "quote": block["text"][:100],
                        "supports": ["result"],
                    }
                ]
                if block
                else []
            ),
            "reason": "All supplied observations support the result.",
        }
        return {"status": "completed", "ai_calls": 1}, assessment

    monkeypatch.setattr("app.services.reviews._run_html_review_skill", run_skill)

    traces, assessment = _run_html_review_batches(None, skill_input)  # type: ignore[arg-type]

    assert len(calls) > 2
    assert [call[0]["review_mode"] for call in calls[:-1]] == [
        "evidence_batch"
    ] * (len(calls) - 1)
    final_input = calls[-1][0]
    assert final_input["review_mode"] == "final"
    assert len(final_input["steps"][0]["batch_observations"]) == len(calls) - 1
    assert all(not report["blocks"] for report in final_input["steps"][0]["reports"])
    assert len(traces) == len(calls)
    assert assessment["status"] == "pass"


def test_html_ai_verdict_must_cite_every_supplied_report() -> None:
    skill_input = {
        "steps": [
            {
                "review_step": 1,
                "automation_release": {
                    "status": "disabled",
                },
                "reports": [
                    {
                        "report_id": report_id,
                        "content_truncated": False,
                        "blocks": [
                            {"block_id": "summary", "text": "Result: Passed"}
                        ],
                    }
                    for report_id in ("report-1", "report-2")
                ],
            }
        ]
    }
    assessment = {
        "status": "pass",
        "description_coverage": "supported",
        "expected_coverage": "supported",
        "actual_coverage": "supported",
        "result_consistency": "consistent",
        "release_consistency": "not_checked",
        "reviewed_report_ids": ["report-1", "report-2"],
        "evidence": [
            {
                "report_id": "report-1",
                "block_id": "summary",
                "quote": "Result: Passed",
            }
        ],
    }

    with pytest.raises(SkillFailure, match="every supplied report"):
        _validate_html_assessment(skill_input, assessment)


def test_html_ai_cannot_override_deterministic_release_mismatch() -> None:
    skill_input = {
        "steps": [
            {
                "review_step": 1,
                "automation_release": {"status": "mismatch"},
                "reports": [],
            }
        ]
    }
    assessment = {"release_consistency": "matched"}

    with pytest.raises(SkillFailure, match="contradicted"):
        _validate_html_assessment(skill_input, assessment)


def test_html_ai_keeps_actual_name_mismatch_separate_from_release_match() -> None:
    skill_input = {
        "steps": [
            {
                "review_step": 1,
                "automation_release": {
                    "status": "mismatch",
                    "failure_code": "actual_name_html_mismatch",
                    "script_name_match": "exact",
                },
                "reports": [],
            }
        ]
    }

    _validate_html_assessment(
        skill_input,
        {
            "status": "manual",
            "release_consistency": "matched",
            "reviewed_report_ids": [],
            "evidence": [],
        },
    )


def test_html_ai_cannot_pass_with_uncertain_release_consistency() -> None:
    skill_input = {
        "steps": [
            {
                "review_step": 1,
                "automation_release": {"status": "needs_ai"},
                "reports": [
                    {
                        "report_id": "report-1",
                        "content_truncated": False,
                        "blocks": [
                            {"block_id": "summary", "text": "Result: Passed"}
                        ],
                    }
                ],
            }
        ]
    }
    assessment = {
        "status": "pass",
        "description_coverage": "supported",
        "expected_coverage": "supported",
        "actual_coverage": "supported",
        "result_consistency": "consistent",
        "release_consistency": "uncertain",
        "reviewed_report_ids": ["report-1"],
        "evidence": [
            {
                "report_id": "report-1",
                "block_id": "summary",
                "quote": "Result: Passed",
            }
        ],
    }

    with pytest.raises(SkillFailure, match="complete consistent coverage"):
        _validate_html_assessment(skill_input, assessment)


def test_truncated_html_content_cannot_be_ai_qualified() -> None:
    skill_input = {
        "steps": [
            {
                "review_step": 1,
                "automation_release": {
                    "status": "disabled",
                },
                "reports": [
                    {
                        "report_id": "report-1",
                        "content_truncated": True,
                        "blocks": [
                            {"block_id": "block-1", "text": "Observed result: Passed"}
                        ],
                    }
                ],
            }
        ]
    }
    assessment = {
        "status": "pass",
        "description_coverage": "supported",
        "expected_coverage": "supported",
        "actual_coverage": "supported",
        "result_consistency": "consistent",
        "release_consistency": "not_checked",
        "reviewed_report_ids": ["report-1"],
        "evidence": [
            {
                "report_id": "report-1",
                "block_id": "block-1",
                "quote": "Observed result: Passed",
            }
        ],
    }

    with pytest.raises(SkillFailure, match="complete consistent coverage"):
        _validate_html_assessment(skill_input, assessment)


def test_equipment_candidate_without_step_evidence_has_no_source_field() -> None:
    step = {
        "description": "Perform the scan with the head phantom.",
        "expected": "The scan succeeds.",
        "actual": "The result was recorded.",
    }
    previous_equipment = {
        "equipment_id": "PHSZ-RD-VV-0-0119",
        "description": "Anthropomorphic phantom (Full Body)- Sandy",
        "model_number": "PBU-60",
        "serial_number": "11A-09",
    }

    assert _equipment_source_field(step, previous_equipment) is None


def test_process_job_exact_match_uses_only_main_ai_and_persists_equipment(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        configure_review(db)
        db.add(
            EquipmentRegistry(
                equipment_id="PCCSY-RD-CT-1-0175",
                description="ECG simulator",
                serial_number="00850540007089",
                calibration_date=date(2025, 10, 29),
                calibration_due_date=date(2026, 10, 28),
            )
        )
        db.commit()
        job = add_job(
            db,
            description="Record the SN and Calibration Date of the ECG simulator",
            expected="ECG simulator SN:__; Calibration Date:__",
            actual=(
                "ECG simulator SN: PCCSY-RD-CT-1-0175; "
                "Calibration Date From: 2025/10/29 to 2026/10/28"
            ),
        )
        calls = []

        def post(*args, **kwargs):
            request = kwargs["json"]
            calls.append(request)
            response = semantic_skill_response(request)
            assert response is not None
            return response

        monkeypatch.setattr("app.services.reviews.httpx.post", post)

        result = process_job(db, job)
        step_result = json.loads(result.step_results_json)[0]

        assert len(calls) == 1
        assert "ALM Text Review and Evidence Planning" in (
            calls[0]["messages"][0]["content"]
        )
        assert result.verdict == "qualified"
        pipeline = json.loads(result.pipeline_json)
        assert pipeline["total_ai_calls"] == 1
        assert pipeline["stages"]["text_review"]["ai_calls"] == 1
        assert pipeline["stages"]["equipment_review"]["ai_calls"] == 0
        assert step_result["equipment"]["status"] == "pass"
        assert step_result["equipment"]["matches"][0]["equipment_id"] == (
            "PCCSY-RD-CT-1-0175"
        )


def test_process_job_reuses_clear_first_pass_equipment_role(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        configure_review(db)
        job = add_job(
            db,
            description="Record the PIM SN",
            expected="PIM SN:__",
            actual="PIM SN: CN52105243",
        )
        calls = []

        def post(*args, **kwargs):
            request = kwargs["json"]
            calls.append(request)
            messages = request["messages"]
            if (
                messages[0]["role"] == "system"
                and "Equipment Role Review" in messages[0]["content"]
            ):
                return StubResponse(
                    json.dumps(
                        {
                            "decisions": [
                                {
                                    "review_step": 1,
                                    "role": "dut_or_other",
                                    "required": False,
                                    "selected_equipment_ids": [],
                                    "reason": "PIM is the product under test.",
                                }
                            ]
                        }
                    )
                )
            response = semantic_skill_response(request)
            assert response is not None
            return response

        monkeypatch.setattr("app.services.reviews.httpx.post", post)

        result = process_job(db, job)
        step_result = json.loads(result.step_results_json)[0]

        assert len(calls) == 1
        assert "ALM Text Review and Evidence Planning" in (
            calls[0]["messages"][0]["content"]
        )
        pipeline = json.loads(result.pipeline_json)
        assert pipeline["total_ai_calls"] == 1
        assert pipeline["stages"]["equipment_review"]["ai_calls"] == 0
        assert pipeline["stages"]["equipment_review"][
            "first_pass_resolved_steps"
        ] == 1
        assert pipeline["stages"]["equipment_review"]["skill"]["status"] == (
            "not_applicable"
        )
        assert result.verdict == "qualified"
        assert step_result["equipment"]["status"] == "not_applicable"


def test_process_job_uses_equipment_skill_when_first_pass_is_uncertain(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        configure_review(db)
        job = add_job(
            db,
            description="Record the PIM SN",
            expected="PIM SN:__",
            actual="PIM SN: CN52105243",
        )
        calls = []

        def post(*args, **kwargs):
            request = kwargs["json"]
            calls.append(request)
            system_content = request["messages"][0]["content"]
            if "ALM Text Review and Evidence Planning" in system_content:
                skill_input = json.loads(request["messages"][1]["content"])
                return StubResponse(
                    json.dumps(
                        {
                            "assessments": [
                                {
                                    "review_step": step["review_step"],
                                    "applicability": "applicable",
                                    "findings": [],
                                    "reference_decisions": [
                                        {
                                            "candidate_id": candidate[
                                                "candidate_id"
                                            ],
                                            "role": "uncertain",
                                            "requires_check": True,
                                            "reason": "The equipment role is unclear.",
                                        }
                                        for candidate in step[
                                            "reference_candidates"
                                        ]
                                    ],
                                    "summary": "Actual supports Expected.",
                                }
                                for step in skill_input["steps"]
                            ]
                        }
                    )
                )
            if "Equipment Role Review" in system_content:
                return StubResponse(
                    json.dumps(
                        {
                            "decisions": [
                                {
                                    "review_step": 1,
                                    "role": "dut_or_other",
                                    "required": False,
                                    "selected_equipment_ids": [],
                                    "reason": "PIM is the product under test.",
                                }
                            ]
                        }
                    )
                )
            raise AssertionError(f"Unexpected Skill request: {system_content[:80]}")

        monkeypatch.setattr("app.services.reviews.httpx.post", post)

        result = process_job(db, job)
        pipeline = json.loads(result.pipeline_json)

        assert len(calls) == 2
        assert "Equipment Role Review" in calls[1]["messages"][0]["content"]
        assert pipeline["total_ai_calls"] == 2
        assert pipeline["stages"]["equipment_review"]["ai_calls"] == 1
        assert pipeline["stages"]["equipment_review"][
            "first_pass_resolved_steps"
        ] == 0
        assert result.verdict == "qualified"


def test_process_job_sends_explicit_html_report_to_ai_review(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        configure_review(db)
        workspace = default_workspace(db)
        workspace.equipment_review_enabled = False
        db.add(
            EvidenceConfig(
                workspace_id=workspace.id,
                allowed_network_root=r"\\server\approved",
                external_evidence_review_enabled=True,
            )
        )
        db.commit()
        job = add_job(
            db,
            description="Review the automation report",
            expected="The automation report results are Passed",
            actual=r"Report: \\server\approved\automation\result.html",
        )
        run = db.get(AlmRun, job.run_id)
        assert run is not None
        run.workspace_id = workspace.id
        run.alm_run_id = 42
        job.workspace_id = workspace.id
        db.commit()
        calls = []

        def post(*args, **kwargs):
            request = kwargs["json"]
            calls.append(request)
            response = semantic_skill_response(request)
            assert response is not None
            return response

        monkeypatch.setattr("app.services.reviews.httpx.post", post)
        monkeypatch.setattr(
            "app.services.reviews.HtmlEvidenceResolver.resolve",
            lambda *_args: HtmlEvidenceResult(
                status="ready",
                size_bytes=128,
                sha256="c" * 64,
                blocks=(
                    HtmlEvidenceBlock(
                        block_id="block-1",
                        text="Automation report result: Passed",
                    ),
                ),
            ),
        )

        result = process_job(db, job)
        pipeline = json.loads(result.pipeline_json)

        assert len(calls) == 2
        assert result.verdict == "qualified"
        assert pipeline["plan"]["report_steps"] == [1]
        report_stage = pipeline["stages"]["report_review"]
        assert report_stage["status"] == "completed"
        assert report_stage["ai_calls"] == 1
        assert report_stage["reports"] == 1
        assert report_stage["result_statuses"] == {"ready": 1}
        assert report_stage["assessment_statuses"] == {"pass": 1}


def test_process_job_supplies_release_record_to_html_review(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        configure_review(db)
        workspace = default_workspace(db)
        workspace.equipment_review_enabled = False
        db.add(
            EvidenceConfig(
                workspace_id=workspace.id,
                allowed_network_root=r"\\server\approved",
                automation_release_project_name="Earth_Kylin",
                external_evidence_review_enabled=True,
            )
        )
        db.commit()
        job = add_job(
            db,
            description="Review the automation report",
            expected="The released automation test passes",
            actual=(
                "Execute automated test scripts Name: CT-NMP.SRS.Fun.321_34941, "
                "which specific reference to D002513563 Automation Test script "
                "validation documentation RevB.\n"
                    r"Report: \\server\approved\automation\CT-NMP.SRS.Fun.321_34941.html"
            ),
        )
        run = db.get(AlmRun, job.run_id)
        assert run is not None
        run.workspace_id = workspace.id
        run.test_id = 34941
        job.workspace_id = workspace.id
        db.commit()
        releases = (
            AutomationReleaseRecord(
                release_id=1295,
                testcase_id="34941",
                project_name="Earth_Kylin",
                script_name="CT-NMP.SRS.Fun.321_34941",
                file_path="//server/scripts/CT-NMP.SRS.Fun.321_34941.yaml",
                baseline_name="Earth Formal D002446336 Rev B",
                release_time="2026-07-24 17:17:00",
                release_version="Earth_Kylin-24-r1",
                version_number="24",
                release_revision=1,
                release_version_format="project-version-revision-v1",
                document_number="D002513563",
                document_revision="B",
                report_link="",
            ),
        )
        release_calls = []
        html_inputs = []

        def load_releases(_db, project_name, testcase_id):
            release_calls.append((project_name, testcase_id))
            return releases

        def post(*args, **kwargs):
            request = kwargs["json"]
            system_content = request["messages"][0]["content"]
            if "HTML Evidence Review" in system_content:
                html_inputs.append(json.loads(request["messages"][1]["content"]))
            response = semantic_skill_response(request)
            assert response is not None
            return response

        monkeypatch.setattr("app.services.reviews.load_automation_releases", load_releases)
        monkeypatch.setattr("app.services.reviews.httpx.post", post)
        monkeypatch.setattr(
            "app.services.reviews.HtmlEvidenceResolver.resolve",
            lambda *_args: HtmlEvidenceResult(
                status="ready",
                size_bytes=128,
                sha256="c" * 64,
                blocks=(HtmlEvidenceBlock("block-1", "Automation result: Passed"),),
            ),
        )

        result = process_job(db, job)
        step_result = json.loads(result.step_results_json)[0]

        assert release_calls == [("Earth_Kylin", "34941")]
        release_input = html_inputs[0]["steps"][0]["automation_release"]
        assert release_input["status"] == "matched"
        assert release_input["actual_name_match"] == "exact"
        assert release_input["script_name_match"] == "exact"
        assert release_input["selected_release"]["release_id"] == 1295
        assert step_result["automation_release"]["status"] == "matched"
        assert step_result["automation_release"]["ai_consistency"] == "matched"
        assert result.verdict == "qualified"


def test_process_job_marks_ai_release_script_mismatch_distinctly(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        configure_review(db)
        workspace = default_workspace(db)
        workspace.equipment_review_enabled = False
        db.add(
            EvidenceConfig(
                workspace_id=workspace.id,
                allowed_network_root=r"\\server\approved",
                automation_release_project_name="Earth_Kylin",
                external_evidence_review_enabled=True,
            )
        )
        db.commit()
        job = add_job(
            db,
            description="Review the automation report",
            expected="The released automation test passes",
            actual=(
                "Execute automated test scripts Name: CT-NMP.SRS.Fun.321_34941, "
                "which specific reference to D002513563 Automation Test script "
                "validation documentation RevB.\n"
                r"Report: \\server\approved\automation\CT-NMP.SRS.Fun.321_34941.html"
            ),
        )
        run = db.get(AlmRun, job.run_id)
        assert run is not None
        run.workspace_id = workspace.id
        run.test_id = 34941
        job.workspace_id = workspace.id
        db.commit()
        releases = (
            AutomationReleaseRecord(
                release_id=1295,
                testcase_id="34941",
                project_name="Earth_Kylin",
                script_name="UNRELATED.Released.Script_34941",
                file_path="//server/scripts/UNRELATED.Released.Script_34941.yaml",
                baseline_name="Earth Formal D002446336 Rev B",
                release_time="2026-07-24 17:17:00",
                release_version="Earth_Kylin-24-r1",
                version_number="24",
                release_revision=1,
                release_version_format="project-version-revision-v1",
                document_number="D002513563",
                document_revision="B",
                report_link="",
            ),
        )

        def post(*args, **kwargs):
            request = kwargs["json"]
            response = semantic_skill_response(request)
            assert response is not None
            if "HTML Evidence Review" not in request["messages"][0]["content"]:
                return response
            payload = response.json()["choices"][0]["message"]["content"]
            output = json.loads(payload)
            output["assessments"][0]["status"] = "fail"
            output["assessments"][0]["release_consistency"] = "mismatched"
            output["assessments"][0]["reason"] = (
                "The HTML script does not match the released script."
            )
            return StubResponse(json.dumps(output))

        monkeypatch.setattr(
            "app.services.reviews.load_automation_releases",
            lambda *_args: releases,
        )
        monkeypatch.setattr("app.services.reviews.httpx.post", post)
        monkeypatch.setattr(
            "app.services.reviews.HtmlEvidenceResolver.resolve",
            lambda *_args: HtmlEvidenceResult(
                status="ready",
                size_bytes=128,
                sha256="c" * 64,
                blocks=(HtmlEvidenceBlock("block-1", "Automation result: Passed"),),
            ),
        )

        result = process_job(db, job)
        step_result = json.loads(result.step_results_json)[0]
        release_result = step_result["automation_release"]

        assert result.verdict == "unqualified"
        assert release_result["status"] == "needs_ai"
        assert release_result["script_name_match"] == "mismatch"
        assert release_result["ai_consistency"] == "mismatched"
        assert release_result["failure_code"] == "release_script_mismatch"


def test_process_job_uses_first_pass_evidence_routing_without_shadow_call(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        configure_review(db)
        workspace = default_workspace(db)
        workspace.equipment_review_enabled = False
        db.add(
            EvidenceConfig(
                workspace_id=workspace.id,
            )
        )
        db.commit()
        job = add_job(
            db,
            description="Archive the review evidence.",
            expected="Review evidence is available.",
            actual=r"Review material is stored in \\server\approved\case-42",
        )
        run = db.get(AlmRun, job.run_id)
        assert run is not None
        run.workspace_id = workspace.id
        run.alm_run_id = 42
        job.workspace_id = workspace.id
        db.commit()
        calls = []

        def post(*args, **kwargs):
            request = kwargs["json"]
            calls.append(request)
            response = semantic_skill_response(request)
            assert response is not None
            return response

        monkeypatch.setattr("app.services.reviews.httpx.post", post)
        monkeypatch.setattr("app.services.skill_runner.httpx.post", post)

        result = process_job(db, job)
        pipeline = json.loads(result.pipeline_json)

        assert len(calls) == 1
        assert pipeline["total_ai_calls"] == 1
        assert pipeline["stages"]["routing"]["ai_calls"] == 0
        assert "skill_shadow" not in pipeline["stages"]["routing"]
        assert pipeline["stages"]["routing"]["decision_skill"]["skill_id"] == (
            "alm-text-review"
        )
        assert pipeline["stages"]["routing"]["steps"][0]["intent"] == (
            "image_evidence"
        )


def test_disabled_equipment_review_skips_disambiguation_and_marks_not_applicable(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        configure_review(db)
        workspace = Workspace(
            name="No equipment",
            slug="no-equipment",
            equipment_review_enabled=False,
        )
        db.add(workspace)
        db.flush()
        job = add_job(
            db,
            description="Record the PIM SN",
            expected="PIM SN:__",
            actual="PIM SN: CN52105243",
        )
        run = db.get(AlmRun, job.run_id)
        assert run is not None
        run.workspace_id = workspace.id
        run.alm_run_id = 42
        job.workspace_id = workspace.id
        db.commit()
        calls = []

        def post(*args, **kwargs):
            request = kwargs["json"]
            calls.append(request)
            response = semantic_skill_response(request)
            assert response is not None
            return response

        monkeypatch.setattr("app.services.reviews.httpx.post", post)

        result = process_job(db, job)
        step_result = json.loads(result.step_results_json)[0]
        criteria = json.loads(result.criteria_json)

        assert len(calls) == 1
        assert result.verdict == "qualified"
        assert step_result["equipment"]["code"] == "disabled_by_configuration"
        assert criteria["equipment_traceability"]["status"] == "not_applicable"


def test_disabling_both_specialist_controls_runs_text_review_only(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        configure_review(db)
        workspace = default_workspace(db)
        workspace.equipment_review_enabled = False
        db.add(
            EvidenceConfig(
                workspace_id=workspace.id,
                allowed_network_root=r"\\server\approved",
            external_evidence_review_enabled=False,
            )
        )
        db.commit()
        job = add_job(
            db,
            description="Record the PIM SN and review the automation report.",
            expected="PIM SN and Passed report results are recorded.",
            actual=(
                "PIM SN: CN52105243; Report: "
                r"\\server\approved\automation\result.html"
            ),
        )
        run = db.get(AlmRun, job.run_id)
        assert run is not None
        run.workspace_id = workspace.id
        run.alm_run_id = 42
        job.workspace_id = workspace.id
        db.commit()
        calls = []

        def post(*args, **kwargs):
            request = kwargs["json"]
            calls.append(request)
            system_content = request["messages"][0]["content"]
            assert "ALM Text Review and Evidence Planning" in system_content
            skill_input = json.loads(request["messages"][1]["content"])
            return StubResponse(
                json.dumps(
                    {
                        "assessments": [
                            {
                                "review_step": step["review_step"],
                                "applicability": "applicable",
                                "findings": [],
                                "reference_decisions": [
                                    {
                                        "candidate_id": candidate["candidate_id"],
                                        "role": (
                                            "uncertain"
                                            if candidate["type"] == "equipment"
                                            else "result_evidence"
                                        ),
                                        "requires_check": True,
                                        "reason": "The candidate may require specialist review.",
                                    }
                                    for candidate in step["reference_candidates"]
                                ],
                                "summary": "The ALM text supports Expected.",
                            }
                            for step in skill_input["steps"]
                        ]
                    }
                )
            )

        monkeypatch.setattr("app.services.reviews.httpx.post", post)
        monkeypatch.setattr(
            "app.services.reviews.HtmlEvidenceResolver.resolve",
            lambda *_args: pytest.fail("text-only mode parsed an HTML report"),
        )
        monkeypatch.setattr(
            "app.services.reviews.NetworkImageResolver.resolve",
            lambda *_args: pytest.fail("text-only mode read image evidence"),
        )

        result = process_job(db, job)
        pipeline = json.loads(result.pipeline_json)
        criteria = json.loads(result.criteria_json)

        assert len(calls) == 1
        assert pipeline["external_evidence_review_enabled"] is False
        assert pipeline["equipment_review_enabled"] is False
        assert pipeline["total_ai_calls"] == 1
        assert pipeline["plan"] == {
            "text_steps": [1],
            "report_steps": [1],
            "equipment_steps": [],
        }
        assert pipeline["stages"]["routing"]["ai_calls"] == 0
        assert pipeline["stages"]["routing"]["steps"][0]["intent"] == "html_report"
        assert pipeline["stages"]["image_review"]["status"] == "disabled"
        assert pipeline["stages"]["report_review"]["status"] == "disabled"
        assert pipeline["stages"]["equipment_review"]["status"] == "disabled"
        assert criteria["path_validation"]["status"] == "manual"
        assert criteria["automation_results"]["status"] == "not_applicable"
        assert criteria["equipment_traceability"]["status"] == "not_applicable"
