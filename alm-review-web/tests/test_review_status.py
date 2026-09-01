import re

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AlmRun,
    EquipmentRegistry,
    EvidenceConfig,
    ManualDecision,
    ReviewJob,
    ReviewResult,
    RunRevision,
    Workspace,
)
from app.services.review_operations import active_run_review_job
from app.services.review_policy import (
    adopt_legacy_workspace_policies,
    current_review_policy_key,
)
from app.services.review_status import (
    current_reviews,
    is_force_qualified,
    review_update_reasons,
)
from app.services.reviews import current_review, save_manual_decision
from app.web import _matches_status


def prepare_run(db: Session, verdict: str, warnings_json: str | None = None) -> AlmRun:
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
            warnings_json=warnings_json,
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


def test_manual_decision_requires_a_reason_but_not_an_operator() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = prepare_run(db, "unqualified")
        with pytest.raises(ValueError, match="reason is required"):
            save_manual_decision(db, run, "override_qualified", "10.0.0.1", "   ")

        manual = save_manual_decision(db, run, "override_qualified", "  ", "Known tool defect")

        assert manual.operator == "unknown"
        assert manual.reason == "Known tool defect"


def test_force_qualified_is_a_lens_over_qualified_runs() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = prepare_run(db, "unqualified")
        assert not is_force_qualified(current_review(db, run))

        save_manual_decision(db, run, "override_qualified", "10.0.0.1", "Known tool defect")
        review = current_review(db, run)
        item = {"final_status": review.final_status, "force_qualified": is_force_qualified(review)}

        assert review.final_status == "qualified"
        assert _matches_status(item, "qualified")
        assert _matches_status(item, "force_qualified")
        assert not _matches_status(item, "unqualified")


def test_confirmed_unqualified_is_not_force_qualified() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = prepare_run(db, "needs_manual_review")
        save_manual_decision(db, run, "confirmed_unqualified", "10.0.0.1", "Evidence missing")

        assert not is_force_qualified(current_review(db, run))


@pytest.mark.parametrize(
    ("verdict", "warnings_json", "expected"),
    [
        ("qualified", '[{"step": 1, "type": "minor_language", "summary": "Typo"}]', True),
        ("unqualified", '[{"step": 2, "type": "equipment_status", "summary": "Active"}]', True),
        ("qualified", "[]", False),
        ("qualified", None, False),
    ],
)
def test_warning_is_a_lens_over_every_final_status(
    verdict: str,
    warnings_json: str | None,
    expected: bool,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = prepare_run(db, verdict, warnings_json)
        policy_key = current_review_policy_key(db)
        single = current_review(db, run, policy_key)
        batched = current_reviews(db, [run], policy_key)[run.run_id]

        assert single.has_warning is expected
        assert batched.has_warning is expected

        item = {
            "final_status": batched.final_status,
            "force_qualified": False,
            "has_warning": batched.has_warning,
        }
        assert _matches_status(item, "warning") is expected
        assert _matches_status(item, verdict)


def test_latest_result_remains_visible_after_review_policy_changes() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = prepare_run(db, "qualified")
        result = db.scalar(select(ReviewResult).where(ReviewResult.run_id == run.run_id))
        assert result is not None
        result.review_policy_key = "outdated"
        db.commit()

        review = current_review(db, run)

        assert review.result is result
        assert review.final_status == "qualified"


def test_existing_result_stays_visible_while_a_re_review_is_queued() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = prepare_run(db, "qualified")
        db.add(
            ReviewJob(
                run_id=run.run_id,
                revision_id=run.current_revision_id,
                status="queued",
            )
        )
        db.commit()

        assert current_review(db, run).final_status == "qualified"
        assert active_run_review_job(db, run).status == "queued"


def test_review_update_reasons_describe_detectable_setting_changes() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = prepare_run(db, "qualified")
        result = db.scalar(select(ReviewResult).where(ReviewResult.run_id == run.run_id))
        assert result is not None
        result.review_policy_key = "earlier-policy"
        result.prompt_version_id = 4
        result.model_name = "earlier-model"
        db.commit()

        reasons = review_update_reasons(result, "current-policy", 5, "current-model")

        assert reasons == (
            "提示词版本已从 4 更新为 5",
            "AI 模型已从 earlier-model 更新为 current-model",
            "评审规则、证据设置或设备台账已更新",
        )


def test_current_policy_result_does_not_recommend_review_update() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = prepare_run(db, "qualified")
        result = db.scalar(select(ReviewResult).where(ReviewResult.run_id == run.run_id))
        assert result is not None

        assert review_update_reasons(
            result,
            result.review_policy_key,
            result.prompt_version_id,
            result.model_name,
        ) == ()


def test_current_reviews_batches_mixed_statuses() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        policy_key = current_review_policy_key(db)
        for run_id in range(1, 5):
            run = AlmRun(
                run_id=run_id,
                source_hash=str(run_id) * 64,
                review_hash="b" * 64,
                raw_json="{}",
            )
            db.add(run)
            db.flush()
            revision = RunRevision(
                run_id=run_id,
                revision_number=1,
                source_hash=run.source_hash,
                review_hash=run.review_hash,
                snapshot_json="{}",
            )
            db.add(revision)
            db.flush()
            run.current_revision_id = revision.id
            if run_id in (1, 2):
                job = ReviewJob(
                    run_id=run_id,
                    revision_id=revision.id,
                    status="completed",
                )
                db.add(job)
                db.flush()
                result = ReviewResult(
                    job_id=job.id,
                    run_id=run_id,
                    revision_id=revision.id,
                    prompt_version_id=1,
                    source_hash=run.source_hash,
                    review_policy_key=policy_key,
                    model_name="test-model",
                    verdict="qualified" if run_id == 1 else "needs_manual_review",
                )
                db.add(result)
                db.flush()
                if run_id == 2:
                    db.add(
                        ManualDecision(
                            run_id=run_id,
                            revision_id=revision.id,
                            review_result_id=result.id,
                            decision="confirmed_unqualified",
                            operator="operator",
                            reason="Evidence checked",
                            source_hash=run.source_hash,
                            original_ai_verdict=result.verdict,
                        )
                    )
            elif run_id == 3:
                db.add(
                    ReviewJob(
                        run_id=run_id,
                        revision_id=revision.id,
                        status="failed",
                    )
                )
        db.commit()
        runs = db.scalars(select(AlmRun).order_by(AlmRun.run_id)).all()

        statements = []

        def count_statement(*_args) -> None:
            statements.append(1)

        event.listen(engine, "before_cursor_execute", count_statement)
        try:
            reviews = current_reviews(db, runs, policy_key)
        finally:
            event.remove(engine, "before_cursor_execute", count_statement)

        assert [reviews[run_id].final_status for run_id in range(1, 5)] == [
            "qualified",
            "unqualified",
            "review_failed",
            "pending_review",
        ]
        assert len(statements) == 3


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


def test_workspace_project_change_updates_review_policy() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(
            name="Project context",
            slug="project-context",
            project="earth_kylin",
        )
        db.add(workspace)
        db.commit()
        first_key = current_review_policy_key(db, workspace.id)

        workspace.project = "kunpeng"
        db.commit()

        assert current_review_policy_key(db, workspace.id) != first_key


def test_equipment_review_switch_updates_authoritative_policy() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(
            name="Equipment review",
            slug="equipment-review",
            equipment_review_enabled=True,
        )
        db.add(workspace)
        db.commit()
        first_key = current_review_policy_key(db, workspace.id)

        workspace.equipment_review_enabled = False
        db.commit()

        assert current_review_policy_key(db, workspace.id) != first_key


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


def test_disabled_equipment_review_excludes_registry_from_workspace_policy() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        from app.models import Workspace

        workspace = Workspace(
            name="No equipment",
            slug="no-equipment",
            equipment_review_enabled=False,
        )
        equipment = EquipmentRegistry(
            equipment_id="PCCSY-RD-CT-1-0175",
            description="ECG simulator",
            serial_number="00850540007089",
        )
        db.add_all((workspace, equipment))
        db.commit()
        first_key = current_review_policy_key(db, workspace.id)

        equipment.serial_number = "00850540007090"
        db.commit()

        assert current_review_policy_key(db, workspace.id) == first_key


def test_every_verdict_module_is_hashed_into_the_review_policy() -> None:
    from app.services.review_policy import _REVIEW_POLICY_FILES

    # Transport and orchestration cannot change a verdict, so they stay out.
    exempt = {"ai_transport", "review_policy", "workspaces", "worker_tasks"}
    hashed = {path.stem for path in _REVIEW_POLICY_FILES if path.suffix == ".py"}
    imported = {
        match.group(1)
        for path in _REVIEW_POLICY_FILES
        if path.suffix == ".py"
        for match in re.finditer(
            r"^from app\.services\.(\w+) import", path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    }

    assert imported - hashed - exempt == set()


def test_legacy_workspace_policy_is_adopted_only_once() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        from app.models import Workspace

        workspace = Workspace(
            name="Migrated",
            slug="migrated",
            legacy_policy_adopted=False,
        )
        db.add(workspace)
        db.flush()
        run = prepare_run(db, "qualified")
        run.workspace_id = workspace.id
        result = db.scalar(select(ReviewResult).where(ReviewResult.run_id == run.run_id))
        assert result is not None
        result.workspace_id = workspace.id
        result.review_policy_key = "legacy"
        db.commit()

        first_policy = current_review_policy_key(db, workspace.id)
        assert adopt_legacy_workspace_policies(db) == 1
        assert result.review_policy_key == first_policy

        workspace.equipment_review_enabled = False
        db.commit()
        assert current_review_policy_key(db, workspace.id) != first_policy
        assert adopt_legacy_workspace_policies(db) == 0
        assert result.review_policy_key == first_policy