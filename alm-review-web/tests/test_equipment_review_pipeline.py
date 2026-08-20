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
from app.services.html_evidence import HtmlEvidenceResult
from app.services.reviews import _build_review_plan, process_job
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
    )

    assert plan.text_steps == (1, 2)
    assert [(item.review_step, item.paths) for item in plan.report_requests] == [
        (1, (r"\\server\reports\result.html",))
    ]
    assert plan.equipment_steps == (2,)


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


def test_process_job_routes_html_report_without_another_ai_call(monkeypatch) -> None:
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
            lambda *_args: HtmlEvidenceResult(status="pass", result_count=3),
        )

        result = process_job(db, job)
        pipeline = json.loads(result.pipeline_json)

        assert len(calls) == 1
        assert result.verdict == "qualified"
        assert pipeline["plan"]["report_steps"] == [1]
        assert pipeline["stages"]["report_review"] == {
            "status": "completed",
            "ai_calls": 0,
            "reports": 1,
            "result_statuses": {"pass": 1},
        }


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
