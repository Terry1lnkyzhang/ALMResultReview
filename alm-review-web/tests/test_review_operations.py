from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AlmRun, ReviewJob, ReviewResult, RunRevision
from app.services.review_operations import latest_rereview_progress, queue_rereviews
from app.services.review_policy import current_review_policy_key


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