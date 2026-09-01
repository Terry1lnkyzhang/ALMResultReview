from __future__ import annotations

import json
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic

from sqlalchemy import desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AlmRun,
    SyncConfig,
    SyncHistory,
    SyncJob,
    WorkerHeartbeat,
    Workspace,
    utcnow,
)
from app.services.alm import FolderCollectionProgress, collect_run, iter_folder_batches
from app.services.importer import ImportResult, import_batch, unchanged_run_check
from app.services.review_operations import queue_run_review
from app.services.workspaces import (
    resolve_workspace,
    workspace_evidence_config,
    workspace_sync_config,
)

MAX_JOB_ATTEMPTS = 3
FAILED_JOB_RETRY_BACKOFF_SECONDS = 60


@dataclass(frozen=True)
class SyncQueueResult:
    job: SyncJob
    created: bool


@dataclass(frozen=True)
class RunSyncQueueResult:
    matched: int
    queued: int
    already_active: int
    skipped: int


def current_worker_id() -> str:
    configured = get_settings().worker_id.strip()
    return configured or socket.gethostname()


def reap_abandoned_sync_jobs(db: Session) -> int:
    """Fail jobs left running by a stopped Worker so they stop blocking new ones."""
    now = utcnow()
    jobs = db.scalars(
        select(SyncJob).where(
            SyncJob.status == "running",
            SyncJob.lease_expires_at.is_not(None),
            SyncJob.lease_expires_at <= now,
            SyncJob.attempt_count >= MAX_JOB_ATTEMPTS,
        )
    ).all()
    for job in jobs:
        job.status = "failed"
        job.progress_stage = "failed"
        job.progress_message = "Worker stopped before the synchronization finished."
        job.error_message = (
            f"Abandoned after {MAX_JOB_ATTEMPTS} attempts; the Worker lease expired "
            "while the job was running."
        )
        job.active_key = None
        job.claimed_by = None
        job.lease_expires_at = None
        job.completed_at = now
    if jobs:
        db.commit()
    return len(jobs)


def queue_sync_job(
    db: Session,
    requested_by: str = "web",
    workspace_id: int | None = None,
    full_refresh: bool = False,
    run_id: int | None = None,
) -> SyncQueueResult:
    workspace = resolve_workspace(db, workspace_id)
    reap_abandoned_sync_jobs(db)
    active_key = (
        f"alm-sync:{workspace.id}"
        if run_id is None
        else f"alm-run-sync:{workspace.id}:{run_id}"
    )
    active = db.scalar(select(SyncJob).where(SyncJob.active_key == active_key))
    if active is not None:
        return SyncQueueResult(active, False)
    job = SyncJob(
        workspace_id=workspace.id,
        active_key=active_key,
        run_id=run_id,
        status="queued",
        full_refresh=full_refresh,
        requested_by=requested_by[:128],
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        active = db.scalar(select(SyncJob).where(SyncJob.active_key == active_key))
        if active is None:
            raise
        return SyncQueueResult(active, False)
    db.refresh(job)
    return SyncQueueResult(job, True)


def queue_run_sync_jobs(
    db: Session,
    run_ids: Sequence[int],
    workspace_id: int | None = None,
    requested_by: str = "web",
) -> RunSyncQueueResult:
    """Refresh each Run from ALM; the Worker queues its AI review after the import."""
    workspace = resolve_workspace(db, workspace_id)
    if not run_ids:
        return RunSyncQueueResult(0, 0, 0, 0)
    runs = db.scalars(
        select(AlmRun)
        .where(
            AlmRun.run_id.in_(run_ids),
            AlmRun.workspace_id == workspace.id,
        )
        .order_by(AlmRun.run_id)
    ).all()
    queued = 0
    already_active = 0
    skipped = 0
    for run in runs:
        if run.test_instance_id is None:
            # Without a test instance the Worker cannot pull the Run back from ALM.
            skipped += 1
            continue
        result = queue_sync_job(
            db,
            requested_by=requested_by,
            workspace_id=workspace.id,
            run_id=run.run_id,
        )
        if result.created:
            queued += 1
        else:
            already_active += 1
    return RunSyncQueueResult(len(runs), queued, already_active, skipped)


def claim_next_sync_job(
    db: Session,
    worker_id: str,
    lease_seconds: int,
) -> SyncJob | None:
    now = utcnow()
    retry_after = now - timedelta(seconds=FAILED_JOB_RETRY_BACKOFF_SECONDS)
    job = db.scalar(
        select(SyncJob)
        .outerjoin(Workspace, Workspace.id == SyncJob.workspace_id)
        .where(
            or_(
                SyncJob.status == "queued",
                (
                    (SyncJob.status == "failed")
                    & (
                        SyncJob.completed_at.is_(None)
                        | (SyncJob.completed_at <= retry_after)
                    )
                ),
                (
                    (SyncJob.status == "running")
                    & (SyncJob.lease_expires_at.is_not(None))
                    & (SyncJob.lease_expires_at <= now)
                ),
            ),
            SyncJob.attempt_count < MAX_JOB_ATTEMPTS,
            or_(Workspace.id.is_(None), Workspace.sync_queue_paused.is_(False)),
        )
        .order_by(
            desc(func.coalesce(Workspace.queue_priority, 0)),
            SyncJob.created_at,
            SyncJob.id,
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        db.rollback()
        return None
    job.status = "running"
    job.progress_stage = "connecting"
    job.progress_message = "Connecting to ALM"
    job.folders_discovered = 0
    job.folders_processed = 0
    job.test_sets_discovered = 0
    job.runs_discovered = 0
    job.runs_skipped = 0
    job.claimed_by = worker_id
    job.lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))
    job.started_at = now
    job.completed_at = None
    job.attempt_count += 1
    job.error_message = ""
    db.commit()
    db.refresh(job)
    return job


def _fail_sync_history(db: Session, history_id: int, exc: Exception) -> None:
    failed_history = db.get(SyncHistory, history_id)
    if failed_history is not None:
        failed_history.status = "failed"
        failed_history.error_message = str(exc)[:2000]
        failed_history.completed_at = utcnow()
        db.commit()


def _finish_sync_job(
    db: Session,
    job_id: int,
    history: SyncHistory,
    result: ImportResult,
    message: str,
) -> None:
    history.status = "completed"
    history.discovered_runs = result.discovered_runs
    history.new_runs = result.new_runs
    history.changed_runs = result.changed_runs
    history.unchanged_runs = result.unchanged_runs
    history.completed_at = utcnow()
    completed_job = db.get(SyncJob, job_id)
    if completed_job is None:
        raise ValueError("Sync job disappeared while it was running.")
    completed_job.status = "completed"
    completed_job.progress_stage = "completed"
    completed_job.progress_message = message
    completed_job.cursor_json = None
    completed_job.active_key = None
    completed_job.claimed_by = None
    completed_job.lease_expires_at = None
    completed_job.completed_at = utcnow()
    db.commit()


def _process_run_sync_job(
    db: Session,
    job: SyncJob,
    workspace_id: int,
    config: SyncConfig,
) -> ImportResult:
    """Refresh one Run from ALM and queue its AI review once the import lands."""
    run_row = db.get(AlmRun, job.run_id)
    if run_row is None:
        raise ValueError(f"Run {job.run_id} no longer exists.")
    if run_row.test_instance_id is None:
        raise ValueError("The Run has no ALM test instance reference.")
    label = run_row.alm_run_id or run_row.run_id
    job.progress_stage = "collecting"
    job.progress_message = f"Refreshing ALM Run {label}"
    db.commit()
    result = ImportResult()
    history = SyncHistory(
        workspace_id=workspace_id,
        sync_config_id=config.id,
        source=f"alm:run:{label}",
        status="running",
    )
    db.add(history)
    db.commit()
    history_id = history.id
    try:
        evidence_config = workspace_evidence_config(db, workspace_id)
        data = collect_run(
            config,
            run_row.test_instance_id,
            str(run_row.folder_id or ""),
            run_row.folder_path,
            include_image_attachments=bool(
                evidence_config and evidence_config.external_evidence_review_enabled
            ),
        )
        import_batch(db, data, workspace_id, result)
        db.commit()
        queue_run_review(db, run_row)
    except Exception as exc:
        db.rollback()
        _fail_sync_history(db, history_id, exc)
        raise
    _finish_sync_job(db, job.id, history, result, f"Run {label} refreshed")
    return result


def process_sync_job(db: Session, job: SyncJob) -> ImportResult:
    workspace = resolve_workspace(db, job.workspace_id)
    config = workspace_sync_config(db, workspace.id)
    if config is None:
        raise ValueError("No ALM synchronization scope is configured.")
    if job.run_id is not None:
        return _process_run_sync_job(db, job, workspace.id, config)
    evidence_config = workspace_evidence_config(db, workspace.id)
    source = f"alm:folder:{config.folder_id}"
    last_progress_update = 0.0

    def persist_progress(progress: FolderCollectionProgress, force: bool) -> None:
        nonlocal last_progress_update
        now = monotonic()
        if not force and now - last_progress_update < 1:
            return
        active_job = db.get(SyncJob, job.id)
        if active_job is None:
            return
        active_job.progress_stage = progress.stage
        active_job.progress_message = progress.message[:1500]
        active_job.folders_discovered = progress.folders_discovered
        active_job.folders_processed = progress.folders_processed
        active_job.test_sets_discovered = progress.test_sets_discovered
        active_job.runs_discovered = progress.runs_discovered
        active_job.runs_skipped = progress.runs_skipped
        active_job.lease_expires_at = utcnow() + timedelta(
            seconds=max(1, get_settings().worker_lease_seconds)
        )
        update_worker_heartbeat(
            db,
            active_job.claimed_by or current_worker_id(),
            status="working",
            current_job_type="sync",
            current_job_id=active_job.id,
        )
        last_progress_update = now

    completed_folder_ids: list[str] = []
    if job.cursor_json:
        try:
            completed_folder_ids = list(json.loads(job.cursor_json))
        except (TypeError, ValueError):
            completed_folder_ids = []
    result = ImportResult()
    history = SyncHistory(
        workspace_id=workspace.id,
        sync_config_id=config.id,
        source=source,
        status="running",
    )
    db.add(history)
    db.commit()
    try:
        batches = iter_folder_batches(
            config,
            progress_callback=persist_progress,
            completed_folder_ids=completed_folder_ids,
            is_unchanged_run=(
                None
                if job.full_refresh
                else unchanged_run_check(db, workspace.id)
            ),
            include_image_attachments=bool(
                evidence_config and evidence_config.external_evidence_review_enabled
            ),
        )
        for batch in batches:
            import_batch(
                db,
                {"users": batch.users, "records": batch.records},
                workspace.id,
                result,
            )
            completed_folder_ids.append(batch.folder_id)
            active_job = db.get(SyncJob, job.id)
            if active_job is not None:
                active_job.cursor_json = json.dumps(completed_folder_ids)
            # Commit per folder so an interrupted synchronization can resume.
            db.commit()
    except Exception as exc:
        db.rollback()
        _fail_sync_history(db, history.id, exc)
        raise

    _finish_sync_job(db, job.id, history, result, "Synchronization complete")
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
                failed_job.progress_stage = "failed"
                failed_job.progress_message = str(exc)[:1500]
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