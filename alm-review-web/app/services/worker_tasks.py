from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import SyncConfig, SyncHistory, SyncJob, WorkerHeartbeat, utcnow
from app.services.alm import collect_folder
from app.services.importer import ImportResult, import_data

SYNC_ACTIVE_KEY = "alm-sync"
MAX_JOB_ATTEMPTS = 3


@dataclass(frozen=True)
class SyncQueueResult:
    job: SyncJob
    created: bool


def current_worker_id() -> str:
    configured = get_settings().worker_id.strip()
    return configured or socket.gethostname()


def queue_sync_job(db: Session, requested_by: str = "web") -> SyncQueueResult:
    active = db.scalar(select(SyncJob).where(SyncJob.active_key == SYNC_ACTIVE_KEY))
    if active is not None:
        return SyncQueueResult(active, False)
    job = SyncJob(
        active_key=SYNC_ACTIVE_KEY,
        status="queued",
        requested_by=requested_by[:128],
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        active = db.scalar(select(SyncJob).where(SyncJob.active_key == SYNC_ACTIVE_KEY))
        if active is None:
            raise
        return SyncQueueResult(active, False)
    db.refresh(job)
    return SyncQueueResult(job, True)


def claim_next_sync_job(
    db: Session,
    worker_id: str,
    lease_seconds: int,
) -> SyncJob | None:
    now = utcnow()
    job = db.scalar(
        select(SyncJob)
        .where(
            or_(
                SyncJob.status.in_(("queued", "failed")),
                (
                    (SyncJob.status == "running")
                    & (SyncJob.lease_expires_at.is_not(None))
                    & (SyncJob.lease_expires_at <= now)
                ),
            ),
            SyncJob.attempt_count < MAX_JOB_ATTEMPTS,
        )
        .order_by(SyncJob.created_at, SyncJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        db.rollback()
        return None
    job.status = "running"
    job.claimed_by = worker_id
    job.lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))
    job.started_at = now
    job.completed_at = None
    job.attempt_count += 1
    job.error_message = ""
    db.commit()
    db.refresh(job)
    return job


def process_sync_job(db: Session, job: SyncJob) -> ImportResult:
    config = db.scalar(select(SyncConfig).order_by(SyncConfig.id).limit(1))
    if config is None:
        raise ValueError("No ALM synchronization scope is configured.")
    source = f"alm:folder:{config.folder_id}"
    try:
        data = collect_folder(config)
    except Exception as exc:
        db.rollback()
        db.add(
            SyncHistory(
                sync_config_id=config.id,
                source=source,
                status="failed",
                error_message=str(exc)[:2000],
                completed_at=utcnow(),
            )
        )
        db.commit()
        raise
    result = import_data(data, db, source=source)
    completed_job = db.get(SyncJob, job.id)
    if completed_job is None:
        raise ValueError("Sync job disappeared while it was running.")
    completed_job.status = "completed"
    completed_job.active_key = None
    completed_job.claimed_by = None
    completed_job.lease_expires_at = None
    completed_job.completed_at = utcnow()
    db.commit()
    return result


def process_queued_sync_jobs(
    db: Session,
    worker_id: str,
    lease_seconds: int,
    limit: int = 1,
) -> tuple[int, int]:
    completed = 0
    failed = 0
    for _ in range(limit):
        job = claim_next_sync_job(db, worker_id, lease_seconds)
        if job is None:
            break
        try:
            process_sync_job(db, job)
            completed += 1
        except Exception as exc:
            db.rollback()
            failed_job = db.get(SyncJob, job.id)
            if failed_job is not None:
                failed_job.status = "failed"
                failed_job.error_message = str(exc)[:2000]
                failed_job.claimed_by = None
                failed_job.lease_expires_at = None
                failed_job.completed_at = utcnow()
                if failed_job.attempt_count >= MAX_JOB_ATTEMPTS:
                    failed_job.active_key = None
                db.commit()
            failed += 1
    return completed, failed


def update_worker_heartbeat(
    db: Session,
    worker_id: str,
    status: str | None = "idle",
    current_job_type: str = "",
    current_job_id: int | None = None,
) -> None:
    heartbeat = db.get(WorkerHeartbeat, worker_id)
    if heartbeat is None:
        heartbeat = WorkerHeartbeat(
            worker_id=worker_id,
            hostname=socket.gethostname(),
            status=status or "idle",
        )
        db.add(heartbeat)
    if status is not None:
        heartbeat.status = status
        heartbeat.current_job_type = current_job_type
        heartbeat.current_job_id = current_job_id
    heartbeat.last_seen_at = utcnow()
    db.commit()