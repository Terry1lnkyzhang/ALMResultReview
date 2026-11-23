from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, func, inspect, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import (
    TestLocationIdentity,
    TestLocationScheduledChange,
    TestLocationVersion,
    utcnow,
)
from app.services import test_locations
from app.services.test_locations import (
    TestLocationNotFoundError,
    TestLocationValidationError,
)

_FIELD_LIMITS = {
    "item": 250,
    "product": 255,
    "version": 255,
    "collimation": 255,
    "platform": 255,
    "system_config": 255,
}
_STALE_APPLYING_AFTER = timedelta(minutes=5)
ACTIVE_SCHEDULE_STATUSES = ("pending", "applying", "failed")


@dataclass(frozen=True)
class ScheduledLocationChangeView:
    id: int
    location_id: int
    current_item: str
    target_item: str
    product: str
    version: str
    collimation: str
    platform: str
    system_config: str
    effective_at: datetime
    change_reason: str
    status: str
    created_at: datetime
    created_by: str
    updated_at: datetime
    applied_at: datetime | None
    applied_version_id: int | None
    cancelled_at: datetime | None
    cancelled_by: str
    error_message: str

    def form_values(self) -> dict[str, str]:
        return {
            "item": self.target_item,
            "product": self.product,
            "version": self.version,
            "collimation": self.collimation,
            "platform": self.platform,
            "system_config": self.system_config,
        }


def _to_view(
    row: TestLocationScheduledChange,
    current_item: str,
) -> ScheduledLocationChangeView:
    return ScheduledLocationChangeView(
        id=row.id,
        location_id=row.location_id,
        current_item=current_item,
        target_item=row.target_item,
        product=row.product,
        version=row.version,
        collimation=row.collimation,
        platform=row.platform,
        system_config=row.system_config,
        effective_at=row.effective_at,
        change_reason=row.change_reason,
        status=row.status,
        created_at=row.created_at,
        created_by=row.created_by,
        updated_at=row.updated_at,
        applied_at=row.applied_at,
        applied_version_id=row.applied_version_id,
        cancelled_at=row.cancelled_at,
        cancelled_by=row.cancelled_by,
        error_message=row.error_message,
    )


def _normalize_schedule_values(values: dict[str, str]) -> dict[str, str]:
    normalized = {field: values.get(field, "").strip() for field in _FIELD_LIMITS}
    if not normalized["item"]:
        raise TestLocationValidationError("Item is required.")
    for field, limit in _FIELD_LIMITS.items():
        if len(normalized[field]) > limit:
            label = field.replace("_", " ").title()
            raise TestLocationValidationError(
                f"{label} must be {limit} characters or fewer."
            )
    return normalized


def _validate_reason(change_reason: str) -> str:
    normalized_reason = change_reason.strip()
    if not normalized_reason:
        raise TestLocationValidationError("Change reason is required.")
    if len(normalized_reason) > 2000:
        raise TestLocationValidationError(
            "Change reason must be 2000 characters or fewer."
        )
    return normalized_reason


def _resolve_active_identity(
    db: Session,
    *,
    location_id: int | None = None,
    original_item: str = "",
) -> tuple[TestLocationIdentity, TestLocationVersion]:
    test_locations.synchronize_test_location_baselines(db)
    identity: TestLocationIdentity | None = None
    if location_id is not None:
        identity = db.get(TestLocationIdentity, location_id)
    elif original_item.strip():
        identity = db.scalar(
            select(TestLocationIdentity).where(
                func.lower(TestLocationIdentity.current_item)
                == original_item.strip().casefold()
            )
        )
    lookup_label = original_item or str(location_id or "")
    if identity is None or identity.retired_at is not None:
        raise TestLocationNotFoundError(lookup_label)
    current_version = db.scalar(
        select(TestLocationVersion)
        .where(
            TestLocationVersion.location_id == identity.id,
            TestLocationVersion.valid_to.is_(None),
        )
        .order_by(TestLocationVersion.valid_from.desc(), TestLocationVersion.id.desc())
        .limit(1)
    )
    if current_version is None:
        raise TestLocationNotFoundError(lookup_label)
    return identity, current_version


def _validate_schedule_target(
    db: Session,
    *,
    identity: TestLocationIdentity,
    current_version: TestLocationVersion,
    target_item: str,
    effective_at: datetime,
    current_time: datetime,
    exclude_schedule_id: int | None = None,
) -> None:
    if effective_at.tzinfo is not None:
        raise TestLocationValidationError(
            "Effective time must use the application's local time."
        )
    if effective_at <= current_time:
        raise TestLocationValidationError(
            "Scheduled effective time must be in the future. "
            "Use Edit for immediate changes."
        )
    if effective_at <= current_version.valid_from:
        raise TestLocationValidationError(
            "Scheduled effective time must be later than the current version start."
        )

    duplicate_stmt = select(TestLocationScheduledChange.id).where(
        TestLocationScheduledChange.location_id == identity.id,
        TestLocationScheduledChange.status.in_(("pending", "applying")),
        TestLocationScheduledChange.effective_at == effective_at,
    )
    if exclude_schedule_id is not None:
        duplicate_stmt = duplicate_stmt.where(
            TestLocationScheduledChange.id != exclude_schedule_id
        )
    if db.scalar(duplicate_stmt.limit(1)) is not None:
        raise TestLocationValidationError(
            "Another scheduled change for this location already uses this effective time."
        )

    other_current = db.scalar(
        select(TestLocationIdentity.id).where(
            func.lower(TestLocationIdentity.current_item) == target_item.casefold(),
            TestLocationIdentity.id != identity.id,
        )
    )
    if other_current is not None:
        raise TestLocationValidationError(f"Item {target_item!r} already exists.")

    other_history = db.scalar(
        select(TestLocationVersion.location_id)
        .where(
            func.lower(TestLocationVersion.item) == target_item.casefold(),
            TestLocationVersion.location_id != identity.id,
        )
        .limit(1)
    )
    if other_history is not None:
        raise TestLocationValidationError(
            f"Item {target_item!r} belongs to another location's history."
        )

    other_scheduled_stmt = select(TestLocationScheduledChange.id).where(
        func.lower(TestLocationScheduledChange.target_item) == target_item.casefold(),
        TestLocationScheduledChange.location_id != identity.id,
        TestLocationScheduledChange.status.in_(("pending", "applying")),
    )
    if exclude_schedule_id is not None:
        other_scheduled_stmt = other_scheduled_stmt.where(
            TestLocationScheduledChange.id != exclude_schedule_id
        )
    if db.scalar(other_scheduled_stmt.limit(1)) is not None:
        raise TestLocationValidationError(
            f"Item {target_item!r} is already reserved by another scheduled change."
        )


def create_scheduled_location_change(
    db: Session,
    values: dict[str, str],
    *,
    effective_at: datetime,
    change_reason: str,
    original_item: str = "",
    location_id: int | None = None,
    created_by: str = "local",
    now: datetime | None = None,
) -> ScheduledLocationChangeView:
    identity, current_version = _resolve_active_identity(
        db,
        location_id=location_id,
        original_item=original_item,
    )
    normalized = _normalize_schedule_values(values)
    reason = _validate_reason(change_reason)
    current_time = now or test_locations.current_business_time()
    _validate_schedule_target(
        db,
        identity=identity,
        current_version=current_version,
        target_item=normalized["item"],
        effective_at=effective_at,
        current_time=current_time,
    )
    schedule = TestLocationScheduledChange(
        location_id=identity.id,
        target_item=normalized["item"],
        product=normalized["product"],
        version=normalized["version"],
        collimation=normalized["collimation"],
        platform=normalized["platform"],
        system_config=normalized["system_config"],
        effective_at=effective_at,
        change_reason=reason,
        status="pending",
        created_by=created_by,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return _to_view(schedule, identity.current_item)


def update_scheduled_location_change(
    db: Session,
    schedule_id: int,
    values: dict[str, str],
    *,
    effective_at: datetime,
    change_reason: str,
    updated_by: str = "local",
    now: datetime | None = None,
) -> ScheduledLocationChangeView:
    schedule = db.get(TestLocationScheduledChange, schedule_id)
    if schedule is None:
        raise TestLocationNotFoundError(str(schedule_id))
    if schedule.status not in {"pending", "failed"}:
        raise TestLocationValidationError(
            f"Cannot edit a {schedule.status} scheduled change."
        )
    identity, current_version = _resolve_active_identity(
        db,
        location_id=schedule.location_id,
    )
    normalized = _normalize_schedule_values(values)
    reason = _validate_reason(change_reason)
    current_time = now or test_locations.current_business_time()
    _validate_schedule_target(
        db,
        identity=identity,
        current_version=current_version,
        target_item=normalized["item"],
        effective_at=effective_at,
        current_time=current_time,
        exclude_schedule_id=schedule.id,
    )
    schedule.target_item = normalized["item"]
    schedule.product = normalized["product"]
    schedule.version = normalized["version"]
    schedule.collimation = normalized["collimation"]
    schedule.platform = normalized["platform"]
    schedule.system_config = normalized["system_config"]
    schedule.effective_at = effective_at
    schedule.change_reason = reason
    schedule.status = "pending"
    schedule.error_message = ""
    schedule.created_by = updated_by or schedule.created_by
    schedule.updated_at = utcnow()
    db.commit()
    db.refresh(schedule)
    return _to_view(schedule, identity.current_item)


def cancel_scheduled_location_change(
    db: Session,
    schedule_id: int,
    *,
    cancelled_by: str = "local",
) -> ScheduledLocationChangeView:
    schedule = db.get(TestLocationScheduledChange, schedule_id)
    if schedule is None:
        raise TestLocationNotFoundError(str(schedule_id))
    if schedule.status not in {"pending", "failed"}:
        raise TestLocationValidationError(
            f"Cannot cancel a {schedule.status} scheduled change."
        )
    identity = db.get(TestLocationIdentity, schedule.location_id)
    current_item = identity.current_item if identity is not None else schedule.target_item
    schedule.status = "cancelled"
    schedule.cancelled_at = utcnow()
    schedule.cancelled_by = cancelled_by
    schedule.updated_at = utcnow()
    db.commit()
    db.refresh(schedule)
    return _to_view(schedule, current_item)


def get_scheduled_location_change(
    db: Session,
    schedule_id: int,
) -> ScheduledLocationChangeView | None:
    row = db.execute(
        select(TestLocationScheduledChange, TestLocationIdentity.current_item)
        .join(
            TestLocationIdentity,
            TestLocationIdentity.id == TestLocationScheduledChange.location_id,
        )
        .where(TestLocationScheduledChange.id == schedule_id)
    ).one_or_none()
    if row is None:
        return None
    schedule, current_item = row
    return _to_view(schedule, current_item)


def list_scheduled_location_changes(
    db: Session,
    *,
    location_id: int | None = None,
    statuses: Sequence[str] | None = None,
    limit: int | None = None,
) -> tuple[ScheduledLocationChangeView, ...]:
    if not inspect(db.connection()).has_table(
        TestLocationScheduledChange.__tablename__
    ):
        return ()
    active_rank = case(
        (TestLocationScheduledChange.status.in_(ACTIVE_SCHEDULE_STATUSES), 0),
        else_=1,
    )
    stmt = (
        select(TestLocationScheduledChange, TestLocationIdentity.current_item)
        .join(
            TestLocationIdentity,
            TestLocationIdentity.id == TestLocationScheduledChange.location_id,
        )
        .order_by(
            active_rank.asc(),
            TestLocationScheduledChange.effective_at.asc(),
            TestLocationScheduledChange.id.asc(),
        )
    )
    if location_id is not None:
        stmt = stmt.where(TestLocationScheduledChange.location_id == location_id)
    if statuses is not None:
        stmt = stmt.where(TestLocationScheduledChange.status.in_(tuple(statuses)))
    if limit is not None:
        stmt = stmt.limit(limit)
    return tuple(
        _to_view(schedule, current_item)
        for schedule, current_item in db.execute(stmt).all()
    )


def _recover_stale_applying_schedules(db: Session) -> None:
    cutoff = utcnow() - _STALE_APPLYING_AFTER
    stale_rows = db.scalars(
        select(TestLocationScheduledChange).where(
            TestLocationScheduledChange.status == "applying",
            TestLocationScheduledChange.updated_at <= cutoff,
        )
    ).all()
    if not stale_rows:
        return
    for row in stale_rows:
        existing_version = db.scalar(
            select(TestLocationVersion)
            .where(
                TestLocationVersion.location_id == row.location_id,
                TestLocationVersion.valid_from == row.effective_at,
            )
            .limit(1)
        )
        if existing_version is not None:
            row.status = "applied"
            row.applied_at = existing_version.recorded_at
            row.applied_version_id = existing_version.id
            row.error_message = ""
        else:
            row.status = "pending"
        row.updated_at = utcnow()
    db.commit()


def _apply_claimed_schedule(
    db: Session,
    schedule_id: int,
    *,
    effective_at_override: datetime | None = None,
    recorded_by_override: str | None = None,
) -> ScheduledLocationChangeView:
    schedule = db.get(TestLocationScheduledChange, schedule_id)
    if schedule is None:
        raise TestLocationNotFoundError(str(schedule_id))
    identity = db.get(TestLocationIdentity, schedule.location_id)
    if identity is None or identity.retired_at is not None:
        raise TestLocationNotFoundError(schedule.target_item)

    effective_time = effective_at_override or schedule.effective_at
    actor = recorded_by_override or schedule.created_by or "scheduler"
    values = {
        "item": schedule.target_item,
        "product": schedule.product,
        "version": schedule.version,
        "collimation": schedule.collimation,
        "platform": schedule.platform,
        "system_config": schedule.system_config,
    }
    test_locations.update_test_location(
        db,
        identity.current_item,
        values,
        effective_at=effective_time,
        change_reason=schedule.change_reason,
        recorded_by=actor,
    )
    applied_version = db.scalar(
        select(TestLocationVersion)
        .where(
            TestLocationVersion.location_id == schedule.location_id,
            TestLocationVersion.valid_from == effective_time,
        )
        .order_by(TestLocationVersion.id.desc())
        .limit(1)
    )
    refreshed_schedule = db.get(TestLocationScheduledChange, schedule_id)
    refreshed_identity = db.get(TestLocationIdentity, schedule.location_id)
    assert refreshed_schedule is not None
    refreshed_schedule.effective_at = effective_time
    refreshed_schedule.status = "applied"
    refreshed_schedule.applied_at = utcnow()
    refreshed_schedule.applied_version_id = (
        applied_version.id if applied_version is not None else None
    )
    refreshed_schedule.error_message = ""
    refreshed_schedule.updated_at = utcnow()
    db.commit()
    db.refresh(refreshed_schedule)
    current_item = (
        refreshed_identity.current_item
        if refreshed_identity is not None
        else refreshed_schedule.target_item
    )
    return _to_view(refreshed_schedule, current_item)


def _cleanup_aborted_session(db: Session) -> None:
    if not db.is_active or db.new or db.dirty or db.deleted:
        db.rollback()


def apply_scheduled_location_change_now(
    db: Session,
    schedule_id: int,
    *,
    applied_by: str = "local",
    now: datetime | None = None,
) -> ScheduledLocationChangeView:
    schedule = db.get(TestLocationScheduledChange, schedule_id)
    if schedule is None:
        raise TestLocationNotFoundError(str(schedule_id))
    if schedule.status not in {"pending", "failed"}:
        raise TestLocationValidationError(
            f"Cannot apply a {schedule.status} scheduled change."
        )
    identity, current_version = _resolve_active_identity(
        db,
        location_id=schedule.location_id,
    )
    earlier_pending = db.scalar(
        select(TestLocationScheduledChange.id)
        .where(
            TestLocationScheduledChange.location_id == identity.id,
            TestLocationScheduledChange.id != schedule.id,
            TestLocationScheduledChange.status.in_(("pending", "applying")),
            (
                (TestLocationScheduledChange.effective_at < schedule.effective_at)
                | (
                    (TestLocationScheduledChange.effective_at == schedule.effective_at)
                    & (TestLocationScheduledChange.id < schedule.id)
                )
            ),
        )
        .limit(1)
    )
    if earlier_pending is not None:
        raise TestLocationValidationError(
            "Apply or cancel earlier scheduled changes for this location first."
        )

    current_time = now or test_locations.current_business_time()
    if (
        schedule.effective_at <= current_time
        and schedule.effective_at > current_version.valid_from
    ):
        effective_time = schedule.effective_at
    else:
        effective_time = max(
            current_time,
            current_version.valid_from + timedelta(seconds=1),
        )

    claimed = db.execute(
        update(TestLocationScheduledChange)
        .where(
            TestLocationScheduledChange.id == schedule_id,
            TestLocationScheduledChange.status.in_(("pending", "failed")),
        )
        .values(status="applying", updated_at=utcnow())
    )
    db.commit()
    if claimed.rowcount != 1:
        raise TestLocationValidationError(
            "Scheduled change is already being processed."
        )

    try:
        return _apply_claimed_schedule(
            db,
            schedule_id,
            effective_at_override=effective_time,
            recorded_by_override=applied_by,
        )
    except (TestLocationValidationError, TestLocationNotFoundError) as exc:
        _cleanup_aborted_session(db)
        db.execute(
            update(TestLocationScheduledChange)
            .where(TestLocationScheduledChange.id == schedule_id)
            .values(status="failed", error_message=str(exc), updated_at=utcnow())
        )
        db.commit()
        raise
    except Exception:
        _cleanup_aborted_session(db)
        db.execute(
            update(TestLocationScheduledChange)
            .where(TestLocationScheduledChange.id == schedule_id)
            .values(status="pending", updated_at=utcnow())
        )
        db.commit()
        raise


def apply_due_scheduled_location_changes(
    db: Session,
    *,
    now: datetime | None = None,
) -> tuple[int, int]:
    if not inspect(db.connection()).has_table(
        TestLocationScheduledChange.__tablename__
    ):
        return 0, 0
    _recover_stale_applying_schedules(db)
    current_time = now or test_locations.current_business_time()
    due_ids = list(
        db.scalars(
            select(TestLocationScheduledChange.id)
            .where(
                TestLocationScheduledChange.status == "pending",
                TestLocationScheduledChange.effective_at <= current_time,
            )
            .order_by(
                TestLocationScheduledChange.effective_at.asc(),
                TestLocationScheduledChange.id.asc(),
            )
        ).all()
    )
    if not due_ids:
        return 0, 0

    applied_count = 0
    failed_count = 0
    for schedule_id in due_ids:
        claimed = db.execute(
            update(TestLocationScheduledChange)
            .where(
                TestLocationScheduledChange.id == schedule_id,
                TestLocationScheduledChange.status == "pending",
            )
            .values(status="applying", updated_at=utcnow())
        )
        db.commit()
        if claimed.rowcount != 1:
            continue

        try:
            _apply_claimed_schedule(db, schedule_id)
            applied_count += 1
        except (TestLocationValidationError, TestLocationNotFoundError) as exc:
            _cleanup_aborted_session(db)
            db.execute(
                update(TestLocationScheduledChange)
                .where(TestLocationScheduledChange.id == schedule_id)
                .values(status="failed", error_message=str(exc), updated_at=utcnow())
            )
            db.commit()
            failed_count += 1
        except SQLAlchemyError as exc:
            _cleanup_aborted_session(db)
            db.execute(
                update(TestLocationScheduledChange)
                .where(TestLocationScheduledChange.id == schedule_id)
                .values(
                    status="pending",
                    error_message=f"Temporary database error (will retry): {exc}"[:500],
                    updated_at=utcnow(),
                )
            )
            db.commit()
    return applied_count, failed_count
