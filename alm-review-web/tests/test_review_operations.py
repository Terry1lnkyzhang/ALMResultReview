from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AlmRun,
    ManualDecision,
    ReviewJob,
    ReviewResult,
    RunRevision,
    RunStep,
    SyncJob,
    Workspace,
)
from app.services.review_operations import (
    cancel_queued_reviews,
    delete_run,
    latest_rereview_progress,
    queue_rereviews,
    workspace_review_progress,
)
from app.services.review_policy import current_review_policy_key
from app.services.review_status import is_force_qualified
from app.services.reviews import current_review, save_manual_decision


def add_reviewed_run(db: Session, run_id: int, verdict: str) -> AlmRun:
    run = AlmRun(
        run_id=run_id,
        run_status="Passed",
        source_hash=str(run_id).zfill(64),
        review_hash=str(run_id + 1).zfill(64),
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
    job = ReviewJob(run_id=run_id, revision_id=revision.id, status="completed")
    db.add(job)
    db.flush()
    db.add(
        ReviewResult(
            job_id=job.id,
            run_id=run_id,
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


def test_delete_run_leaves_no_orphaned_rows() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = add_reviewed_run(db, 1, "unqualified")
        kept = add_reviewed_run(db, 2, "qualified")
        revision_id = run.current_revision_id
        db.add(RunStep(revision_id=revision_id, step_id=10, name="Step 1"))
        db.add(SyncJob(run_id=1, status="queued"))
        result_id = db.scalar(select(ReviewResult.id).where(ReviewResult.run_id == 1))
        save_manual_decision(db, run, "override_qualified", "tester", "Checked by hand")
        db.commit()
        assert db.scalar(
            select(ManualDecision).where(ManualDecision.run_id == 1)
        ) is not None

        summary = delete_run(db, run)

        assert summary.run_id == 1
        assert (summary.revisions, summary.steps, summary.review_jobs) == (1, 1, 1)
        assert (summary.review_results, summary.manual_decisions) == (1, 1)
        assert summary.sync_jobs == 1
        assert db.get(AlmRun, 1) is None
        # Tables without a foreign key would silently keep orphans behind.
        for model, column in (
            (RunRevision, RunRevision.run_id),
            (ReviewJob, ReviewJob.run_id),
            (ReviewResult, ReviewResult.run_id),
            (ManualDecision, ManualDecision.run_id),
            (SyncJob, SyncJob.run_id),
        ):
            assert db.scalars(select(model).where(column == 1)).all() == []
        assert db.scalars(
            select(RunStep).where(RunStep.revision_id == revision_id)
        ).all() == []
        assert result_id is not None
        assert db.get(AlmRun, kept.run_id) is not None
        assert db.scalars(select(ReviewJob).where(ReviewJob.run_id == 2)).all() != []


def test_queue_rereviews_filters_final_status_and_preserves_history() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        add_reviewed_run(db, 1, "qualified")
        add_reviewed_run(db, 2, "unqualified")
        add_reviewed_run(db, 3, "needs_manual_review")

        result = queue_rereviews(db, "unqualified_manual")
        queued = db.scalars(
            select(ReviewJob)
            .where(ReviewJob.status == "queued")
            .order_by(ReviewJob.run_id)
        ).all()

        assert (result.matched, result.queued, result.already_active) == (2, 2, 0)
        assert [job.run_id for job in queued] == [2, 3]
        assert result.batch_id is not None
        assert {job.batch_id for job in queued} == {result.batch_id}
        progress = latest_rereview_progress(db)
        assert progress is not None
        assert (progress.total, progress.processed, progress.remaining) == (2, 0, 2)
        assert progress.percent == 0
        assert len(db.scalars(select(ReviewResult)).all()) == 3


def test_queue_rereviews_skips_run_with_active_job() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = add_reviewed_run(db, 2, "unqualified")
        db.add(
            ReviewJob(
                run_id=run.run_id,
                revision_id=run.current_revision_id,
                status="running",
            )
        )
        db.commit()

        result = queue_rereviews(db, "unqualified")

        assert (result.matched, result.queued, result.already_active) == (1, 0, 1)


def test_queue_all_rereviews_every_passed_run() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        add_reviewed_run(db, 1, "qualified")
        add_reviewed_run(db, 2, "unqualified")

        result = queue_rereviews(db, "all")

        assert (result.matched, result.queued) == (2, 2)


def test_queue_rereviews_never_touches_a_manually_resolved_run() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        forced = add_reviewed_run(db, 1, "unqualified")
        add_reviewed_run(db, 2, "unqualified")
        save_manual_decision(db, forced, "override_qualified", "tester", "Known tool defect")

        result = queue_rereviews(db, "all")
        queued = db.scalars(select(ReviewJob).where(ReviewJob.status == "queued")).all()

        assert (result.matched, result.queued, result.manually_resolved) == (1, 1, 1)
        assert [job.run_id for job in queued] == [2]
        assert is_force_qualified(current_review(db, forced))


def test_latest_rereview_progress_counts_terminal_jobs() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        add_reviewed_run(db, 1, "qualified")
        add_reviewed_run(db, 2, "unqualified")
        result = queue_rereviews(db, "all")
        jobs = db.scalars(
            select(ReviewJob)
            .where(ReviewJob.batch_id == result.batch_id)
            .order_by(ReviewJob.id)
        ).all()
        jobs[0].status = "completed"
        jobs[1].status = "failed"
        jobs[1].attempt_count = 3
        db.commit()

        progress = latest_rereview_progress(db)

        assert progress is not None
        assert (progress.total, progress.processed, progress.remaining) == (2, 2, 0)
        assert (progress.completed, progress.failed) == (1, 1)
        assert progress.percent == 100


def test_cancel_queued_reviews_removes_only_requested_waiting_jobs() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            (
                ReviewJob(workspace_id=1, run_id=1, revision_id=1, status="queued"),
                ReviewJob(workspace_id=1, run_id=2, revision_id=2, status="queued"),
                ReviewJob(workspace_id=1, run_id=3, revision_id=3, status="running"),
                ReviewJob(workspace_id=2, run_id=4, revision_id=4, status="queued"),
            )
        )
        db.commit()
        requested_job = db.scalar(
            select(ReviewJob).where(ReviewJob.workspace_id == 1, ReviewJob.run_id == 1)
        )
        assert requested_job is not None

        removed = cancel_queued_reviews(db, 1, requested_job.id)

        remaining = db.scalars(select(ReviewJob).order_by(ReviewJob.run_id)).all()
        assert removed == 1
        assert [(job.run_id, job.status) for job in remaining] == [
            (2, "queued"),
            (3, "running"),
            (4, "queued"),
        ]


def test_cancel_all_queued_reviews_preserves_running_and_other_workspaces() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            (
                ReviewJob(workspace_id=1, run_id=1, revision_id=1, status="queued"),
                ReviewJob(workspace_id=1, run_id=2, revision_id=2, status="queued"),
                ReviewJob(workspace_id=1, run_id=3, revision_id=3, status="running"),
                ReviewJob(workspace_id=2, run_id=4, revision_id=4, status="queued"),
            )
        )
        db.commit()

        removed = cancel_queued_reviews(db, 1)

        remaining = db.scalars(select(ReviewJob).order_by(ReviewJob.run_id)).all()
        assert removed == 2
        assert [(job.run_id, job.status) for job in remaining] == [
            (3, "running"),
            (4, "queued"),
        ]


def test_workspace_review_progress_counts_current_runs_once() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Project A", slug="project-a")
        db.add(workspace)
        db.flush()
        for run_id in (1, 2):
            run = AlmRun(
                workspace_id=workspace.id,
                run_id=run_id,
                run_status="Passed",
                source_hash=str(run_id).zfill(64),
                review_hash=str(run_id + 1).zfill(64),
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
        policy_key = current_review_policy_key(db, workspace.id)
        completed = ReviewJob(
            workspace_id=workspace.id,
            run_id=1,
            revision_id=1,
            status="completed",
        )
        queued = ReviewJob(
            workspace_id=workspace.id,
            run_id=2,
            revision_id=2,
            status="queued",
        )
        historical = ReviewJob(
            workspace_id=workspace.id,
            run_id=1,
            revision_id=1,
            status="completed",
        )
        db.add_all((completed, queued, historical))
        db.flush()
        db.add(
            ReviewResult(
                workspace_id=workspace.id,
                job_id=completed.id,
                run_id=1,
                revision_id=1,
                prompt_version_id=1,
                source_hash=str(1).zfill(64),
                review_policy_key=policy_key,
                model_name="test-model",
                verdict="qualified",
            )
        )
        db.commit()

        progress = workspace_review_progress(db, workspace.id)

        assert progress.total == 2
        assert progress.reviewed == 1
        assert progress.qualified == 1
        assert progress.pending == 1
        assert progress.queued == 1
        assert progress.percent == 50.0