import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AlmRun,
    EquipmentRegistry,
    EvidenceConfig,
    ReviewJob,
    ReviewResult,
    RunRevision,
)
from app.services.review_policy import current_review_policy_key
from app.services.reviews import current_review, save_manual_decision


def prepare_run(db: Session, verdict: str) -> AlmRun:
    run = AlmRun(
        run_id=42,
        source_hash="a" * 64,
        review_hash="b" * 64,
        raw_json="{}",
    )
    db.add(run)
    db.flush()
    revision = RunRevision(
        run_id=42,
        revision_number=1,
        source_hash=run.source_hash,
        review_hash=run.review_hash,
        snapshot_json="{}",
    )
    db.add(revision)
    db.flush()
    run.current_revision_id = revision.id
    job = ReviewJob(run_id=42, revision_id=revision.id, status="completed")
    db.add(job)
    db.flush()
    db.add(
        ReviewResult(
            job_id=job.id,
            run_id=42,
            revision_id=revision.id,
            prompt_version_id=1,
            source_hash=run.source_hash,
            review_policy_key=current_review_policy_key(db),
            model_name="test-model",
            verdict=verdict,
        )
    )
    db.commit()
    return run


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("qualified", "qualified"),
        ("unqualified", "unqualified"),
        ("needs_manual_review", "needs_manual_review"),
    ],
)
def test_ai_verdict_sets_initial_final_status(verdict: str, expected: str) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = prepare_run(db, verdict)
        assert current_review(db, run).final_status == expected


def test_manual_rules_allow_review_confirmation_and_unqualified_override() -> None:
    for verdict, decision in (
        ("needs_manual_review", "confirmed_qualified"),
        ("needs_manual_review", "confirmed_unqualified"),
        ("unqualified", "override_qualified"),
    ):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            run = prepare_run(db, verdict)
            save_manual_decision(db, run, decision, "operator", "Evidence checked")
            expected = "unqualified" if decision == "confirmed_unqualified" else "qualified"
            assert current_review(db, run).final_status == expected


def test_qualified_result_rejects_manual_decision() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = prepare_run(db, "qualified")
        with pytest.raises(ValueError, match="not allowed"):
            save_manual_decision(db, run, "override_qualified", "operator", "No reason")


def test_result_from_outdated_review_policy_is_not_current() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = prepare_run(db, "qualified")
        result = db.scalar(select(ReviewResult).where(ReviewResult.run_id == run.run_id))
        assert result is not None
        result.review_policy_key = "outdated"
        db.commit()

        review = current_review(db, run)

        assert review.result is None
        assert review.final_status == "pending_review"


def test_evidence_configuration_change_updates_review_policy() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        config = EvidenceConfig(id=1, allowed_network_root=r"\\server\first")
        db.add(config)
        db.commit()
        first_key = current_review_policy_key(db)

        config.allowed_network_root = r"\\server\second"
        db.commit()

        assert current_review_policy_key(db) != first_key


def test_equipment_registry_change_updates_review_policy() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        equipment = EquipmentRegistry(
            equipment_id="PCCSY-RD-CT-1-0175",
            description="ECG simulator",
            serial_number="00850540007089",
        )
        db.add(equipment)
        db.commit()
        first_key = current_review_policy_key(db)

        equipment.serial_number = "00850540007090"
        db.commit()

        assert current_review_policy_key(db) != first_key