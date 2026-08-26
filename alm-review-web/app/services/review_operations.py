from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import delete, desc, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AlmRun,
    ManualDecision,
    ReviewJob,
    ReviewResult,
    RunRevision,
    RunStep,
    SyncJob,
)
from app.services.review_policy import current_review_policy_key
from app.services.review_status import current_reviews, is_force_qualified
from app.services.reviews import MAX_REVIEW_JOB_ATTEMPTS, current_review
from app.services.workspaces import resolve_workspace

REREVIEW_SCOPES = {
    "all": None,
    "unqualified": {"unqualified"},
    "manual": {"needs_manual_review"},
    "unqualified_manual": {"unqualified", "needs_manual_review"},
}


@dataclass(frozen=True)
class RereviewQueueResult:
    matched: int
    queued: int
    already_active: int
    manually_resolved: int
    batch_id: str | None


@dataclass(frozen=True)
class RereviewProgress:
    batch_id: str
    created_at: datetime
    total: int
    processed: int
    completed: int
    remaining: int
    queued: int
    running: int
    retrying: int
    failed: int
    skipped: int
    percent: float


@dataclass(frozen=True)
class WorkspaceReviewProgress:
    total: int
    reviewed: int
    qualified: int
    force_qualified: int
    unqualified: int
    manual: int
    pending: int
    review_failed: int
    queued: int
    running: int
    percent: float


def workspace_review_progress(
    db: Session,
    workspace_id: int | None = None,
) -> WorkspaceReviewProgress:
    workspace = resolve_workspace(db, workspace_id)
    runs = db.scalars(
        select(AlmRun).where(
            AlmRun.workspace_id == workspace.id,
            AlmRun.run_status == "Passed",
            AlmRun.current_revision_id.is_not(None),
        )
    ).all()
    reviews = current_reviews(
        db,
        runs,
        current_review_policy_key(db, workspace.id),
    )
    status_counts = {
        status: sum(review.final_status == status for review in reviews.values())
        for status in (
            "qualified",
            "unqualified",
            "needs_manual_review",
            "pending_review",
            "review_failed",
        )
    }
    current_revision_ids = [
        run.current_revision_id
        for run in runs
        if run.current_revision_id is not None
    ]
    job_counts = dict(
        db.execute(
            select(ReviewJob.status, func.count())
            .where(
                ReviewJob.workspace_id == workspace.id,
                ReviewJob.revision_id.in_(current_revision_ids),
                ReviewJob.status.in_(("queued", "running")),
            )
            .group_by(ReviewJob.status)
        ).all()
    )
    reviewed = (
        status_counts["qualified"]
        + status_counts["unqualified"]
        + status_counts["needs_manual_review"]
    )
    total = len(runs)
    return WorkspaceReviewProgress(
        total=total,
        reviewed=reviewed,
        qualified=status_counts["qualified"],
        force_qualified=sum(is_force_qualified(review) for review in reviews.values()),
        unqualified=status_counts["unqualified"],
        manual=status_counts["needs_manual_review"],
        pending=status_counts["pending_review"],
        review_failed=status_counts["review_failed"],
        queued=job_counts.get("queued", 0),
        running=job_counts.get("running", 0),
        percent=round(reviewed * 100 / total, 1) if total else 0.0,
    )


def queue_run_review(db: Session, run: AlmRun) -> ReviewJob | None:
    """Queue the AI review for one Run, reusing an active or retryable job."""
    if run.current_revision_id is None:
        return None
    job = db.scalar(
        select(ReviewJob)
        .where(
            ReviewJob.run_id == run.run_id,
            ReviewJob.revision_id == run.current_revision_id,
            ReviewJob.status.in_(("queued", "running")),
        )
        .order_by(desc(ReviewJob.id))
        .limit(1)
    )
    if job is not None:
        return job
    job = db.scalar(
        select(ReviewJob)
        .where(
            ReviewJob.run_id == run.run_id,
            ReviewJob.revision_id == run.current_revision_id,
            ReviewJob.status == "failed",
            ReviewJob.attempt_count < MAX_REVIEW_JOB_ATTEMPTS,
        )
        .order_by(desc(ReviewJob.id))
        .limit(1)
    )
    if job is None:
        job = ReviewJob(
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            revision_id=run.current_revision_id,
            status="queued",
        )
        db.add(job)
    else:
        job.status = "queued"
        job.error_message = ""
        job.completed_at = None
    db.commit()
    return job


def active_run_review_job(db: Session, run: AlmRun) -> ReviewJob | None:
    """The queued or running job for the Run's current revision, if any."""
    if run.current_revision_id is None:
        return None
    return db.scalar(
        select(ReviewJob)
        .where(
            ReviewJob.run_id == run.run_id,
            ReviewJob.revision_id == run.current_revision_id,
            ReviewJob.status.in_(("queued", "running")),
        )
        .order_by(desc(ReviewJob.id))
        .limit(1)
    )


def delete_local_run(db: Session, run: AlmRun) -> None:
    """Delete a Run and its local history once no job can still use it."""
    active_review = db.scalar(
        select(ReviewJob.id).where(
            ReviewJob.run_id == run.run_id,
            ReviewJob.status.in_(("queued", "running")),
        )
    )
    active_sync = db.scalar(
        select(SyncJob.id).where(
            SyncJob.run_id == run.run_id,
            SyncJob.status.in_(("queued", "running")),
        )
    )
    if active_review is not None or active_sync is not None:
        raise ValueError(
            "This Run cannot be deleted while an ALM refresh or AI review is "
            "queued or running. Remove or finish the active job first."
        )

    revision_ids = list(
        db.scalars(select(RunRevision.id).where(RunRevision.run_id == run.run_id))
    )
    db.execute(delete(ManualDecision).where(ManualDecision.run_id == run.run_id))
    db.execute(delete(ReviewResult).where(ReviewResult.run_id == run.run_id))
    db.execute(delete(ReviewJob).where(ReviewJob.run_id == run.run_id))
    db.execute(delete(SyncJob).where(SyncJob.run_id == run.run_id))
    if revision_ids:
        db.execute(delete(RunStep).where(RunStep.revision_id.in_(revision_ids)))
    db.execute(delete(RunRevision).where(RunRevision.run_id == run.run_id))
    db.delete(run)
    db.commit()


def cancel_queued_reviews(
    db: Session,
    workspace_id: int,
    job_id: int | None = None,
) -> int:
    statement = delete(ReviewJob).where(
        ReviewJob.workspace_id == workspace_id,
        ReviewJob.status == "queued",
    )
    if job_id is not None:
        statement = statement.where(ReviewJob.id == job_id)
    result = db.execute(statement)
    db.commit()
    return result.rowcount or 0


def queue_rereviews(
    db: Session,
    scope: str,
    workspace_id: int | None = None,
) -> RereviewQueueResult:
    if scope not in REREVIEW_SCOPES:
        raise ValueError(f"Unknown re-review scope {scope!r}.")
    include_legacy = workspace_id is None
    workspace = resolve_workspace(db, workspace_id)
    policy_key = current_review_policy_key(db, workspace.id)
    target_statuses = REREVIEW_SCOPES[scope]
    runs = db.scalars(
        select(AlmRun)
        .where(
            or_(
                AlmRun.workspace_id == workspace.id,
                include_legacy and AlmRun.workspace_id.is_(None),
            ),
            AlmRun.run_status == "Passed",
            AlmRun.current_revision_id.is_not(None),
        )
        .order_by(AlmRun.run_id)
    ).all()
    matched_runs = []
    manually_resolved = 0
    for run in runs:
        review = current_review(db, run, policy_key)
        if target_statuses is not None and review.final_status not in target_statuses:
            continue
        if review.manual_decision is not None:
            # An operator already ruled on this revision; a new AI verdict would
            # replace the result the decision is attached to and silently drop it.
            manually_resolved += 1
            continue
        matched_runs.append(run)
    return _queue_rereview_batch(db, workspace.id, matched_runs, manually_resolved)


def queue_rereviews_for_run_ids(
    db: Session,
    run_ids: Sequence[int],
    workspace_id: int | None = None,
) -> RereviewQueueResult:
    """Queue a re-review for an explicit set of Runs, e.g. a dashboard filter."""
    include_legacy = workspace_id is None
    workspace = resolve_workspace(db, workspace_id)
    policy_key = current_review_policy_key(db, workspace.id)
    if not run_ids:
        return RereviewQueueResult(0, 0, 0, 0, None)
    runs = db.scalars(
        select(AlmRun)
        .where(
            AlmRun.run_id.in_(run_ids),
            or_(
                AlmRun.workspace_id == workspace.id,
                include_legacy and AlmRun.workspace_id.is_(None),
            ),
            AlmRun.run_status == "Passed",
            AlmRun.current_revision_id.is_not(None),
        )
        .order_by(AlmRun.run_id)
    ).all()
    matched_runs = []
    manually_resolved = 0
    for run in runs:
        review = current_review(db, run, policy_key)
        if review.manual_decision is not None:
            manually_resolved += 1
            continue
        matched_runs.append(run)
    return _queue_rereview_batch(db, workspace.id, matched_runs, manually_resolved)


def _queue_rereview_batch(
    db: Session,
    workspace_id: int,
    matched_runs: Sequence[AlmRun],
    manually_resolved: int,
) -> RereviewQueueResult:
    active_revision_ids = set(
        db.scalars(
            select(ReviewJob.revision_id).where(
                ReviewJob.revision_id.in_(
                    run.current_revision_id for run in matched_runs
                ),
                ReviewJob.status.in_(("queued", "running")),
            )
        ).all()
    )
    runs_to_queue = [
        run for run in matched_runs if run.current_revision_id not in active_revision_ids
    ]
    batch_id = uuid4().hex if runs_to_queue else None
    for run in runs_to_queue:
        db.add(
            ReviewJob(
                workspace_id=workspace_id,
                batch_id=batch_id,
                run_id=run.run_id,
                revision_id=run.current_revision_id,
                status="queued",
            )
        )
    db.commit()
    return RereviewQueueResult(
        matched=len(matched_runs),
        queued=len(runs_to_queue),
        already_active=len(matched_runs) - len(runs_to_queue),
        manually_resolved=manually_resolved,
        batch_id=batch_id,
    )


def queue_failed_reviews(db: Session, workspace_id: int | None = None) -> int:
    workspace = resolve_workspace(db, workspace_id)
    policy_key = current_review_policy_key(db, workspace.id)
    runs = db.scalars(
        select(AlmRun).where(
            AlmRun.workspace_id == workspace.id,
            AlmRun.run_status == "Passed",
            AlmRun.current_revision_id.is_not(None),
        )
    ).all()
    failed_runs = [
        run
        for run in runs
        if current_review(db, run, policy_key).final_status == "review_failed"
    ]
    active_revision_ids = set(
        db.scalars(
            select(ReviewJob.revision_id).where(
                ReviewJob.revision_id.in_(
                    run.current_revision_id for run in failed_runs
                ),
                ReviewJob.status.in_(("queued", "running")),
            )
        ).all()
    )
    queued = 0
    for run in failed_runs:
        if run.current_revision_id in active_revision_ids:
            continue
        failed_job = db.scalar(
            select(ReviewJob)
            .where(
                ReviewJob.revision_id == run.current_revision_id,
                ReviewJob.status == "failed",
            )
            .order_by(desc(ReviewJob.id))
            .limit(1)
        )
        if failed_job is not None and failed_job.attempt_count < 3:
            failed_job.status = "queued"
            failed_job.error_message = ""
            failed_job.completed_at = None
        else:
            db.add(
                ReviewJob(
                    workspace_id=workspace.id,
                    run_id=run.run_id,
                    revision_id=run.current_revision_id,
                    status="queued",
                )
            )
        queued += 1
    db.commit()
    return queued


def latest_rereview_progress(
    db: Session,
    workspace_id: int | None = None,
) -> RereviewProgress | None:
    include_legacy = workspace_id is None
    workspace = resolve_workspace(db, workspace_id)
    batch_id = db.scalar(
        select(ReviewJob.batch_id)
        .where(
            or_(
                ReviewJob.workspace_id == workspace.id,
                include_legacy and ReviewJob.workspace_id.is_(None),
            ),
            ReviewJob.batch_id.is_not(None),
        )
        .order_by(ReviewJob.id.desc())
        .limit(1)
    )
    if batch_id is not None:
        jobs = db.scalars(
            select(ReviewJob)
            .where(ReviewJob.batch_id == batch_id)
            .order_by(ReviewJob.id)
        ).all()
    else:
        legacy_created_at = db.scalar(
            select(ReviewJob.created_at)
            .where(
                or_(
                    ReviewJob.workspace_id == workspace.id,
                    include_legacy and ReviewJob.workspace_id.is_(None),
                ),
                ReviewJob.batch_id.is_(None),
            )
            .group_by(ReviewJob.created_at)
            .having(func.count() > 1)
            .order_by(desc(ReviewJob.created_at))
            .limit(1)
        )
        if legacy_created_at is None:
            return None
        jobs = db.scalars(
            select(ReviewJob)
            .where(
                ReviewJob.batch_id.is_(None),
                ReviewJob.created_at == legacy_created_at,
            )
            .order_by(ReviewJob.id)
        ).all()
        batch_id = f"legacy-{legacy_created_at.isoformat()}"
    if not jobs:
        return None

    queued = sum(job.status == "queued" for job in jobs)
    running = sum(job.status == "running" for job in jobs)
    retrying = sum(
        job.status == "failed" and job.attempt_count < 3 for job in jobs
    )
    failed = sum(
        job.status == "failed" and job.attempt_count >= 3 for job in jobs
    )
    completed = sum(job.status == "completed" for job in jobs)
    skipped = sum(job.status in ("outdated", "superseded") for job in jobs)
    total = len(jobs)
    remaining = queued + running + retrying
    processed = total - remaining
    return RereviewProgress(
        batch_id=batch_id,
        created_at=jobs[0].created_at,
        total=total,
        processed=processed,
        completed=completed,
        remaining=remaining,
        queued=queued,
        running=running,
        retrying=retrying,
        failed=failed,
        skipped=skipped,
        percent=round(processed * 100 / total, 1),
    )