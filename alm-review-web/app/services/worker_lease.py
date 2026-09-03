from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models import WorkerLease


class WorkerLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerLeaseHolder:
    owner_token: str
    worker_id: str
    hostname: str
    process_id: int | None
    lease_expires_at: datetime | None


def _database_now(db: Session) -> datetime:
    value = db.scalar(select(func.current_timestamp()))
    if not isinstance(value, datetime):
        raise RuntimeError("Database did not return a valid current timestamp.")
    return value


def worker_lease_holder(db: Session) -> WorkerLeaseHolder | None:
    lease = db.get(WorkerLease, 1)
    if lease is None or not lease.owner_token:
        return None
    return WorkerLeaseHolder(
        owner_token=lease.owner_token,
        worker_id=lease.worker_id,
        hostname=lease.hostname,
        process_id=lease.process_id,
        lease_expires_at=lease.lease_expires_at,
    )


def acquire_worker_lease(
    db: Session,
    owner_token: str,
    worker_id: str,
    lease_seconds: int,
) -> bool:
    now = _database_now(db)
    result = db.execute(
        update(WorkerLease)
        .where(
            WorkerLease.id == 1,
            or_(
                WorkerLease.owner_token.is_(None),
                WorkerLease.lease_expires_at.is_(None),
                WorkerLease.lease_expires_at <= now,
            ),
        )
        .values(
            owner_token=owner_token,
            worker_id=worker_id,
            hostname=socket.gethostname(),
            process_id=os.getpid(),
            acquired_at=now,
            lease_expires_at=now + timedelta(seconds=max(5, lease_seconds)),
            last_seen_at=now,
        )
    )
    acquired = result.rowcount == 1
    if acquired:
        db.commit()
    else:
        db.rollback()
    return acquired


def renew_worker_lease(db: Session, owner_token: str, lease_seconds: int) -> bool:
    now = _database_now(db)
    result = db.execute(
        update(WorkerLease)
        .where(
            WorkerLease.id == 1,
            WorkerLease.owner_token == owner_token,
            WorkerLease.lease_expires_at > now,
        )
        .values(
            lease_expires_at=now + timedelta(seconds=max(5, lease_seconds)),
            last_seen_at=now,
        )
    )
    renewed = result.rowcount == 1
    if renewed:
        db.commit()
    else:
        db.rollback()
    return renewed


def owns_worker_lease(db: Session, owner_token: str) -> bool:
    now = _database_now(db)
    return bool(
        db.scalar(
            select(WorkerLease.id).where(
                WorkerLease.id == 1,
                WorkerLease.owner_token == owner_token,
                WorkerLease.lease_expires_at > now,
            )
        )
    )


def fence_worker_lease(db: Session, owner_token: str) -> bool:
    now = _database_now(db)
    return bool(
        db.scalar(
            select(WorkerLease.id)
            .where(
                WorkerLease.id == 1,
                WorkerLease.owner_token == owner_token,
                WorkerLease.lease_expires_at > now,
            )
            .with_for_update()
        )
    )


def release_worker_lease(db: Session, owner_token: str) -> bool:
    result = db.execute(
        update(WorkerLease)
        .where(
            WorkerLease.id == 1,
            WorkerLease.owner_token == owner_token,
        )
        .values(
            owner_token=None,
            worker_id="",
            hostname="",
            process_id=None,
            acquired_at=None,
            lease_expires_at=None,
            last_seen_at=None,
        )
    )
    released = result.rowcount == 1
    if released:
        db.commit()
    else:
        db.rollback()
    return released