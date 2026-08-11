from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import SyncConfig
from app.services.reviews import process_queued_jobs
from app.services.worker_tasks import (
    current_worker_id,
    process_queued_sync_jobs,
    queue_sync_job,
    update_worker_heartbeat,
)

logger = logging.getLogger(__name__)


def scheduled_heartbeat() -> None:
    with SessionLocal() as db:
        update_worker_heartbeat(db, current_worker_id(), status=None)


def scheduled_sync() -> None:
    with SessionLocal() as db:
        configs = db.scalars(
            select(SyncConfig)
            .where(SyncConfig.enabled.is_(True))
            .order_by(SyncConfig.id)
        ).all()
        for config in configs:
            result = queue_sync_job(
                db,
                requested_by="scheduler",
                workspace_id=config.workspace_id,
            )
            if result.created:
                logger.info(
                    "Queued scheduled ALM synchronization workspace=%s job=%s",
                    config.workspace_id,
                    result.job.id,
                )


def queue_scheduled_workspace_sync(workspace_id: int) -> None:
    with SessionLocal() as db:
        config = db.scalar(
            select(SyncConfig).where(
                SyncConfig.workspace_id == workspace_id,
                SyncConfig.enabled.is_(True),
            )
        )
        if config is None:
            return
        result = queue_sync_job(
            db,
            requested_by="scheduler",
            workspace_id=workspace_id,
        )
        if result.created:
            logger.info(
                "Queued scheduled ALM synchronization workspace=%s job=%s",
                workspace_id,
                result.job.id,
            )


def run_worker_cycle() -> None:
    settings = get_settings()
    worker_id = current_worker_id()
    with SessionLocal() as db:
        update_worker_heartbeat(db, worker_id, status="working")
        try:
            sync_completed, sync_failed = process_queued_sync_jobs(
                db,
                worker_id=worker_id,
                lease_seconds=settings.worker_lease_seconds,
                limit=1,
            )
            review_completed, review_failed = process_queued_jobs(
                db,
                limit=10,
                worker_id=worker_id,
                lease_seconds=settings.worker_lease_seconds,
            )
            if sync_completed or sync_failed or review_completed or review_failed:
                logger.info(
                    "Worker cycle sync=%s/%s review=%s/%s",
                    sync_completed,
                    sync_failed,
                    review_completed,
                    review_failed,
                )
        finally:
            db.rollback()
            update_worker_heartbeat(db, worker_id, status="idle")


def configure_scheduler(scheduler: BackgroundScheduler) -> None:
    managed_job_ids = {
        "daily-alm-sync",
        "queued-ai-reviews",
        "worker-queue-poll",
        "worker-heartbeat",
    }
    managed_job_ids.update(
        job.id for job in scheduler.get_jobs() if job.id.startswith("daily-alm-sync:")
    )
    for job_id in managed_job_ids:
        if scheduler.get_job(job_id) is not None:
            scheduler.remove_job(job_id)

    with SessionLocal() as db:
        configs = db.scalars(
            select(SyncConfig)
            .where(SyncConfig.enabled.is_(True))
            .order_by(SyncConfig.id)
        ).all()
    for config in configs:
        scheduler.add_job(
            queue_scheduled_workspace_sync,
            "cron",
            hour=config.schedule_hour,
            minute=config.schedule_minute,
            args=[config.workspace_id],
            id=f"daily-alm-sync:{config.workspace_id}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    scheduler.add_job(
        run_worker_cycle,
        "interval",
        seconds=max(1, get_settings().worker_poll_seconds),
        id="worker-queue-poll",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        scheduled_heartbeat,
        "interval",
        seconds=max(1, get_settings().worker_poll_seconds),
        id="worker-heartbeat",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


def create_scheduler(force_worker: bool = False) -> BackgroundScheduler | None:
    settings = get_settings()
    if force_worker:
        if settings.app_role == "web":
            return None
    elif settings.app_role != "combined" or not settings.scheduler_enabled:
        return None
    scheduler = BackgroundScheduler(timezone=settings.app_timezone)
    configure_scheduler(scheduler)
    return scheduler