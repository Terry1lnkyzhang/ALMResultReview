from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import WorkerLease, utcnow
from app.services.worker_lease import (
    acquire_worker_lease,
    fence_worker_lease,
    owns_worker_lease,
    release_worker_lease,
    renew_worker_lease,
)


def test_only_one_worker_can_hold_the_singleton_lease() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(WorkerLease(id=1))
        db.commit()

        assert acquire_worker_lease(db, "owner-a", "worker-a", 30) is True
        assert acquire_worker_lease(db, "owner-b", "worker-b", 30) is False
        assert owns_worker_lease(db, "owner-a") is True
        assert fence_worker_lease(db, "owner-a") is True
        db.rollback()
        assert fence_worker_lease(db, "owner-b") is False
        db.rollback()
        assert renew_worker_lease(db, "owner-a", 30) is True
        assert release_worker_lease(db, "owner-b") is False
        assert release_worker_lease(db, "owner-a") is True
        assert acquire_worker_lease(db, "owner-b", "worker-b", 30) is True


def test_expired_worker_lease_can_be_taken_over_but_not_renewed() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            WorkerLease(
                id=1,
                owner_token="owner-a",
                worker_id="worker-a",
                lease_expires_at=utcnow() - timedelta(seconds=1),
            )
        )
        db.commit()

        assert renew_worker_lease(db, "owner-a", 30) is False
        assert acquire_worker_lease(db, "owner-b", "worker-b", 30) is True
        assert owns_worker_lease(db, "owner-a") is False
        assert owns_worker_lease(db, "owner-b") is True