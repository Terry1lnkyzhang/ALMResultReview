import json
from datetime import date
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AiConfig,
    AlmRun,
    EquipmentRegistry,
    PromptVersion,
    ReviewJob,
    RunRevision,
)
from app.services.reviews import process_job


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
            "summary": "未发现语言或语义问题。",
        }
    )


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
            calls.append(kwargs["json"])
            return StubResponse(main_review_response())

        monkeypatch.setattr("app.services.reviews.httpx.post", post)

        result = process_job(db, job)
        step_result = json.loads(result.step_results_json)[0]

        assert len(calls) == 1
        assert result.verdict == "qualified"
        assert step_result["equipment"]["status"] == "pass"
        assert step_result["equipment"]["matches"][0]["equipment_id"] == (
            "PCCSY-RD-CT-1-0175"
        )


def test_process_job_calls_disambiguation_only_for_ambiguous_role(monkeypatch) -> None:
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
            prompt = request["messages"][0]["content"]
            if "You classify equipment references" in prompt:
                return StubResponse(
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
                    )
                )
            return StubResponse(main_review_response())

        monkeypatch.setattr("app.services.reviews.httpx.post", post)

        result = process_job(db, job)
        step_result = json.loads(result.step_results_json)[0]

        assert len(calls) == 2
        assert "You classify equipment references" in calls[0]["messages"][0]["content"]
        assert result.verdict == "qualified"
        assert step_result["equipment"]["status"] == "not_applicable"
