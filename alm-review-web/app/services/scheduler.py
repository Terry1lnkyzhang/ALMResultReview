from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import AiConfig, SyncConfig
from app.services.ai_transport import ai_endpoint_available, available_ai_configs
from app.services.reviews import (
    claim_next_review_job,
    process_claimed_review_job,
    reap_abandoned_review_jobs,
)
from app.services.worker_lease import (
    fence_worker_lease,
    owns_worker_lease,
    renew_worker_lease,
)
from app.services.worker_tasks import (
    current_worker_id,
    process_queued_sync_jobs,
    queue_sync_job,
    reap_abandoned_sync_jobs,
    update_worker_heartbeat,
)

logger = logging.getLogger(__name__)


def process_review_queue(
    *,
    limit: int,
    worker_id: str,
    lease_seconds: int,
    concurrency: int | None = None,
    endpoint_concurrency: Sequence[tuple[int, int]] | None = None,
    owner_token: str | None = None,
    lease_guard: Callable[[], bool] | None = None,
) -> tuple[int, int]:
    legacy_mode = endpoint_concurrency is None
    capacities = (
        ((0, max(1, min(4, concurrency or 1))),)
        if legacy_mode
        else tuple(
            (config_id, max(1, min(4, capacity)))
            for config_id, capacity in endpoint_concurrency
        )
    )
    available_slots = deque(
        config_id
        for config_id, capacity in capacities
        for _ in range(capacity)
    )
    worker_count = min(limit, len(available_slots))
    if worker_count == 0:
        return 0, 0

    def process_claimed(job_id: int) -> tuple[int, int]:
        with SessionLocal() as db:
            return (
                process_claimed_review_job(db, job_id, owner_token)
                if owner_token is not None
                else process_claimed_review_job(db, job_id)
            )

    completed = 0
    failed = 0
    claimed_count = 0
    claims_allowed = True
    poll_timeout = min(5.0, max(0.01, float(get_settings().worker_poll_seconds)))
    pending: dict[Future[tuple[int, int]], int] = {}
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="ai-review",
    ) as executor:
        while True:
            with SessionLocal() as db:
                while available_slots and claimed_count < limit:
                    if lease_guard is not None and not lease_guard():
                        claims_allowed = False
                        available_slots.clear()
                        break
                    config_id = available_slots.popleft()
                    job = (
                        claim_next_review_job(db, worker_id, lease_seconds)
                        if legacy_mode
                        else claim_next_review_job(
                            db,
                            worker_id,
                            lease_seconds,
                            ai_config_id=config_id,
                        )
                    )
                    if job is None:
                        available_slots.appendleft(config_id)
                        break
                    claimed_count += 1
                    pending[executor.submit(process_claimed, job.id)] = config_id
            if not pending:
                break
            # Also wake periodically so work queued after the last claim can use idle slots.
            done, _ = wait(
                pending,
                timeout=poll_timeout,
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                config_id = pending.pop(future)
                job_completed, job_failed = future.result()
                if claims_allowed and not job_failed:
                    available_slots.append(config_id)
                elif claims_allowed:
                    with SessionLocal() as db:
                        config = db.get(AiConfig, config_id)
                        if config is not None and ai_endpoint_available(config):
                            available_slots.append(config_id)
                completed += job_completed
                failed += job_failed
    return completed, failed


def scheduled_heartbeat(
    owner_token: str,
    on_lease_lost: Callable[[], None] | None = None,
) -> None:
    with SessionLocal() as db:
        if not renew_worker_lease(
            db,
            owner_token,
            get_settings().worker_singleton_lease_seconds,
        ):
            logger.error("Worker singleton lease was lost; stopping queue processing.")
            if on_lease_lost is not None:
                on_lease_lost()
            return
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


def run_sync_cycle(owner_token: str) -> None:
    settings = get_settings()

    def still_owns_lease() -> bool:
        with SessionLocal() as lease_db:
            return owns_worker_lease(lease_db, owner_token)

    with SessionLocal() as db:
        try:
            if not owns_worker_lease(db, owner_token):
                return
            reap_abandoned_sync_jobs(db)
            completed, failed = process_queued_sync_jobs(
                db,
                worker_id=current_worker_id(),
                lease_seconds=settings.worker_lease_seconds,
                limit=1,
                lease_guard=still_owns_lease,
                lease_fence=lambda: fence_worker_lease(db, owner_token),
            )
            if completed or failed:
                logger.info("Worker sync cycle sync=%s/%s", completed, failed)
        finally:
            db.rollback()


def run_worker_cycle(owner_token: str) -> None:
    settings = get_settings()
    worker_id = current_worker_id()

    def still_owns_lease() -> bool:
        with SessionLocal() as lease_db:
            return owns_worker_lease(lease_db, owner_token)

    with SessionLocal() as db:
        if not owns_worker_lease(db, owner_token):
            return
        update_worker_heartbeat(db, worker_id, status="working")
        try:
            reap_abandoned_review_jobs(db)
            ai_configs = available_ai_configs(db)
            endpoint_concurrency = tuple(
                (config.id, config.review_concurrency) for config in ai_configs
            )
            review_completed, review_failed = (
                process_review_queue(
                    limit=max(10, sum(capacity for _, capacity in endpoint_concurrency)),
                    endpoint_concurrency=endpoint_concurrency,
                    owner_token=owner_token,
                    lease_guard=still_owns_lease,
                    worker_id=worker_id,
                    lease_seconds=settings.worker_lease_seconds,
                )
                if endpoint_concurrency
                else (0, 0)
            )
            if review_completed or review_failed:
                logger.info(
                    "Worker cycle review=%s/%s",
                    review_completed,
                    review_failed,
                )
        finally:
            db.rollback()
            update_worker_heartbeat(db, worker_id, status="idle")


def configure_scheduler(
    scheduler: BackgroundScheduler,
    owner_token: str,
    on_lease_lost: Callable[[], None] | None = None,
) -> None:
    managed_job_ids = {
        "daily-alm-sync",
        "queued-ai-reviews",
        "worker-queue-poll",
        "worker-sync-poll",
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
        args=[owner_token],
        id="worker-queue-poll",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    # Separate pump so a long ALM synchronization cannot stall the review queue.
    scheduler.add_job(
        run_sync_cycle,
        "interval",
        seconds=max(1, get_settings().worker_poll_seconds),
        args=[owner_token],
        id="worker-sync-poll",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        scheduled_heartbeat,
        "interval",
        seconds=max(1, get_settings().worker_poll_seconds),
        args=[owner_token, on_lease_lost],
        id="worker-heartbeat",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


def create_scheduler(
    owner_token: str,
    force_worker: bool = False,
    on_lease_lost: Callable[[], None] | None = None,
) -> BackgroundScheduler | None:
    settings = get_settings()
    if force_worker:
        if settings.app_role == "web":
            return None
    elif settings.app_role != "combined" or not settings.scheduler_enabled:
        return None
    scheduler = BackgroundScheduler(timezone=settings.app_timezone)
    configure_scheduler(scheduler, owner_token, on_lease_lost)
    return scheduler