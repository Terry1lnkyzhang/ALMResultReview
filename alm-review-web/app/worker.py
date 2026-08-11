from __future__ import annotations

import logging
import signal
from threading import Event

from app.database import Base, SessionLocal, engine
from app.migrations import ensure_compatible_schema
from app.services.defaults import ensure_defaults
from app.services.review_policy import (
    adopt_legacy_workspace_policies,
    backfill_legacy_review_policy,
)
from app.services.scheduler import create_scheduler, run_worker_cycle
from app.services.worker_tasks import current_worker_id, update_worker_heartbeat

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    Base.metadata.create_all(engine)
    ensure_compatible_schema(engine)
    with SessionLocal() as db:
        ensure_defaults(db)
        backfill_legacy_review_policy(db)
        adopt_legacy_workspace_policies(db)
        update_worker_heartbeat(db, current_worker_id(), status="starting")

    scheduler = create_scheduler(force_worker=True)
    if scheduler is None:
        raise RuntimeError("Worker scheduler is disabled by APP_ROLE=web.")

    stop_event = Event()

    def request_stop(*_: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    worker_id = current_worker_id()
    logger.info("Starting ALM review worker id=%s", worker_id)
    run_worker_cycle()
    scheduler.start()
    try:
        stop_event.wait()
    finally:
        scheduler.shutdown(wait=True)
        with SessionLocal() as db:
            update_worker_heartbeat(db, worker_id, status="offline")
        logger.info("ALM review worker stopped id=%s", worker_id)


if __name__ == "__main__":
    main()