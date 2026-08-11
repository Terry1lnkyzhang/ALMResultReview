from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from app.models import AlmRun, ReviewJob
from app.services.review_policy import current_review_policy_key
from app.services.reviews import current_review
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
    matched_runs = [
        run
        for run in runs
        if target_statuses is None
        or current_review(db, run, policy_key).final_status in target_statuses
    ]
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
                workspace_id=workspace.id,
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
        batch_id=batch_id,
    )


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