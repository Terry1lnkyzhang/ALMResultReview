from __future__ import annotations

import logging
import signal
from threading import Event
from uuid import uuid4

from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.migrations import ensure_compatible_schema
from app.services.defaults import ensure_defaults
from app.services.review_policy import (
    adopt_legacy_workspace_policies,
    backfill_legacy_review_policy,
)
from app.services.scheduler import create_scheduler
from app.services.worker_lease import (
    acquire_worker_lease,
    fence_worker_lease,
    release_worker_lease,
    worker_lease_holder,
)
from app.services.worker_tasks import current_worker_id, update_worker_heartbeat

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    Base.metadata.create_all(engine)
    ensure_compatible_schema(engine)
    stop_event = Event()
    owner_token = str(uuid4())
    worker_id = current_worker_id()
    with SessionLocal() as db:
        ensure_defaults(db)
        backfill_legacy_review_policy(db)
        adopt_legacy_workspace_policies(db)
        if not acquire_worker_lease(
            db,
            owner_token,
            worker_id,
            get_settings().worker_singleton_lease_seconds,
        ):
            holder = worker_lease_holder(db)
            holder_label = (
                f"{holder.worker_id} on {holder.hostname} pid={holder.process_id}"
                if holder is not None
                else "another process"
            )
            raise RuntimeError(
                f"Another Worker already holds the singleton lease: {holder_label}."
            )
        update_worker_heartbeat(db, worker_id, status="starting")

    scheduler = create_scheduler(
        owner_token,
        force_worker=True,
        on_lease_lost=stop_event.set,
    )
    if scheduler is None:
        with SessionLocal() as db:
            release_worker_lease(db, owner_token)
        raise RuntimeError("Worker scheduler is disabled by APP_ROLE=web.")

    def request_stop(*_: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    logger.info("Starting ALM review worker id=%s", worker_id)
    scheduler.start()
    try:
        stop_event.wait()
    finally:
        scheduler.shutdown(wait=True)
        with SessionLocal() as db:
            if fence_worker_lease(db, owner_token):
                update_worker_heartbeat(db, worker_id, status="offline")
            release_worker_lease(db, owner_token)
        logger.info("ALM review worker stopped id=%s", worker_id)


if __name__ == "__main__":
    main()