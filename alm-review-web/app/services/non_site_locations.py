from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import NonSiteExecutionLocation

DEFAULT_NON_SITE_EXECUTION_LOCATIONS = ("Offline", "Laptop")


class NonSiteLocationValidationError(ValueError):
    pass


class NonSiteLocationNotFoundError(LookupError):
    pass


def normalized_non_site_location(value: str) -> str:
    return value.strip().casefold()


def list_non_site_locations(
    db: Session,
    *,
    enabled_only: bool = False,
) -> tuple[NonSiteExecutionLocation, ...]:
    statement = select(NonSiteExecutionLocation).order_by(
        NonSiteExecutionLocation.normalized_key,
        NonSiteExecutionLocation.id,
    )
    if enabled_only:
        statement = statement.where(NonSiteExecutionLocation.enabled.is_(True))
    return tuple(db.scalars(statement).all())


def enabled_non_site_location_keys(db: Session) -> frozenset[str]:
    return frozenset(
        item.normalized_key
        for item in list_non_site_locations(db, enabled_only=True)
    )


def create_non_site_location(
    db: Session,
    name: str,
) -> NonSiteExecutionLocation:
    display_name = name.strip()
    normalized_key = normalized_non_site_location(display_name)
    if not normalized_key:
        raise NonSiteLocationValidationError("Location mode is required.")
    if db.scalar(
        select(NonSiteExecutionLocation.id).where(
            NonSiteExecutionLocation.normalized_key == normalized_key
        )
    ):
        raise NonSiteLocationValidationError(
            f"Non-site execution location {display_name!r} already exists."
        )
    item = NonSiteExecutionLocation(
        name=display_name,
        normalized_key=normalized_key,
        enabled=True,
    )
    db.add(item)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise NonSiteLocationValidationError(
            f"Non-site execution location {display_name!r} already exists."
        ) from exc
    db.refresh(item)
    return item


def update_non_site_location(
    db: Session,
    location_id: int,
    *,
    name: str,
    enabled: bool,
) -> NonSiteExecutionLocation:
    item = db.get(NonSiteExecutionLocation, location_id)
    if item is None:
        raise NonSiteLocationNotFoundError(str(location_id))
    display_name = name.strip()
    normalized_key = normalized_non_site_location(display_name)
    if not normalized_key:
        raise NonSiteLocationValidationError("Location mode is required.")
    duplicate = db.scalar(
        select(NonSiteExecutionLocation.id).where(
            NonSiteExecutionLocation.normalized_key == normalized_key,
            NonSiteExecutionLocation.id != location_id,
        )
    )
    if duplicate is not None:
        raise NonSiteLocationValidationError(
            f"Non-site execution location {display_name!r} already exists."
        )
    item.name = display_name
    item.normalized_key = normalized_key
    item.enabled = enabled
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise NonSiteLocationValidationError(
            f"Non-site execution location {display_name!r} already exists."
        ) from exc
    db.refresh(item)
    return item


def non_site_location_policy_snapshot(db: Session) -> dict[str, object]:
    rows = [
        {
            "name": item.name,
            "normalized_key": item.normalized_key,
            "enabled": item.enabled,
        }
        for item in list_non_site_locations(db)
    ]
    serialized = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "row_count": len(rows),
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }