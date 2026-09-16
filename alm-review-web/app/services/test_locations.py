from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Column, MetaData, String, Table, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import TestLocationIdentity, TestLocationVersion, utcnow

ATFRAMEWORK_SCHEMA = "atframeworkdb"

_metadata = MetaData()
TEST_LOCATION_TABLE = Table(
    "loadtestcasetestlocation",
    _metadata,
    Column("Item", String(250), primary_key=True),
    Column("Product", String(255), nullable=True),
    Column("DMS Version", String(255), key="Version", nullable=True),
    Column("DMS Coverage", String(255), key="Collimation", nullable=True),
    Column("Couch", String(255), key="Platform", nullable=True),
    Column("Computer", String(255), key="SystemConfig", nullable=True),
    schema=ATFRAMEWORK_SCHEMA,
)

_FIELD_LIMITS = {
    "item": 250,
    "product": 255,
    "version": 255,
    "collimation": 255,
    "platform": 255,
    "system_config": 255,
}
_COLUMN_BY_FIELD = {
    "item": TEST_LOCATION_TABLE.c.Item,
    "product": TEST_LOCATION_TABLE.c.Product,
    "version": TEST_LOCATION_TABLE.c.Version,
    "collimation": TEST_LOCATION_TABLE.c.Collimation,
    "platform": TEST_LOCATION_TABLE.c.Platform,
    "system_config": TEST_LOCATION_TABLE.c.SystemConfig,
}
_DATABASE_COLUMN_BY_FIELD = {
    "item": "Item",
    "product": "Product",
    "version": "Version",
    "collimation": "Collimation",
    "platform": "Platform",
    "system_config": "SystemConfig",
}


@dataclass(frozen=True)
class TestLocationRecord:
    item: str
    product: str
    version: str
    collimation: str
    platform: str
    system_config: str
    location_id: int | None = None
    valid_from: datetime | None = None
    version_count: int = 0

    def form_values(self) -> dict[str, str]:
        return asdict(self)


class TestLocationValidationError(ValueError):
    pass


class TestLocationNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class TestLocationHistory:
    location_id: int
    current_item: str
    retired_at: datetime | None
    retirement_reason: str
    retirement_recorded_at: datetime | None
    retirement_recorded_by: str
    versions: tuple[TestLocationVersion, ...]


@dataclass(frozen=True)
class TestLocationResolution:
    status: str
    version: TestLocationVersion | None


def current_business_time() -> datetime:
    return datetime.now(ZoneInfo(get_settings().app_timezone)).replace(tzinfo=None)


def _record(
    row: dict[str, object],
    *,
    location_id: int | None = None,
    valid_from: datetime | None = None,
    version_count: int = 0,
) -> TestLocationRecord:
    return TestLocationRecord(
        item=str(row["Item"]),
        product=str(row["Product"] or ""),
        version=str(row["Version"] or ""),
        collimation=str(row["Collimation"] or ""),
        platform=str(row["Platform"] or ""),
        system_config=str(row["SystemConfig"] or ""),
        location_id=location_id,
        valid_from=valid_from,
        version_count=version_count,
    )


def _normalized_values(values: dict[str, str]) -> dict[str, str | None]:
    normalized = {field: values.get(field, "").strip() for field in _FIELD_LIMITS}
    if not normalized["item"]:
        raise TestLocationValidationError("Item is required.")
    for field, limit in _FIELD_LIMITS.items():
        if len(normalized[field]) > limit:
            label = field.replace("_", " ").title()
            raise TestLocationValidationError(
                f"{label} must be {limit} characters or fewer."
            )
    return {
        _DATABASE_COLUMN_BY_FIELD[field]: value or None
        for field, value in normalized.items()
    }


def _history_values(values: dict[str, str | None]) -> dict[str, str]:
    return {
        "item": str(values["Item"]),
        "product": str(values["Product"] or ""),
        "version": str(values["Version"] or ""),
        "collimation": str(values["Collimation"] or ""),
        "platform": str(values["Platform"] or ""),
        "system_config": str(values["SystemConfig"] or ""),
    }


def _internal_row_values(row: object) -> dict[str, object]:
    return {column.key: row[column] for column in TEST_LOCATION_TABLE.c}


def _validate_change_metadata(
    effective_at: datetime,
    change_reason: str,
    *,
    earliest: datetime | None = None,
    now: datetime | None = None,
) -> str:
    normalized_reason = change_reason.strip()
    if not normalized_reason:
        raise TestLocationValidationError("Change reason is required.")
    if len(normalized_reason) > 2000:
        raise TestLocationValidationError(
            "Change reason must be 2000 characters or fewer."
        )
    current_time = now or current_business_time()
    if effective_at > current_time + timedelta(seconds=60):
        raise TestLocationValidationError(
            "Effective time cannot be in the future because the AT Framework "
            "record changes immediately."
        )
    if earliest is not None and effective_at <= earliest:
        raise TestLocationValidationError(
            "Effective time must be later than the current version start."
        )
    return normalized_reason


def _current_version(
    db: Session,
    location_id: int,
) -> TestLocationVersion | None:
    return db.scalar(
        select(TestLocationVersion)
        .where(
            TestLocationVersion.location_id == location_id,
            TestLocationVersion.valid_to.is_(None),
        )
        .order_by(TestLocationVersion.valid_from.desc(), TestLocationVersion.id.desc())
        .limit(1)
    )


def _identity_for_current_item(
    db: Session,
    item: str,
) -> TestLocationIdentity | None:
    return db.scalar(
        select(TestLocationIdentity).where(
            func.lower(TestLocationIdentity.current_item) == item.casefold()
        )
    )


def _identity_for_any_item(
    db: Session,
    item: str,
) -> TestLocationIdentity | None:
    return db.scalar(
        select(TestLocationIdentity)
        .join(
            TestLocationVersion,
            TestLocationVersion.location_id == TestLocationIdentity.id,
        )
        .where(func.lower(TestLocationVersion.item) == item.casefold())
        .order_by(TestLocationVersion.valid_from.desc())
        .limit(1)
    )


def synchronize_test_location_baselines(
    db: Session,
    *,
    baseline_at: datetime | None = None,
    recorded_by: str = "system",
) -> int:
    rows = list(db.execute(select(TEST_LOCATION_TABLE)).mappings())
    if not rows:
        return 0
    baseline_time = baseline_at or current_business_time()
    existing = {
        identity.current_item.casefold(): identity
        for identity in db.scalars(select(TestLocationIdentity)).all()
    }
    inserted = 0
    for row in rows:
        item = str(row["Item"])
        if item.casefold() in existing:
            continue
        identity = TestLocationIdentity(current_item=item)
        db.add(identity)
        db.flush()
        db.add(
            TestLocationVersion(
                location_id=identity.id,
                valid_from=baseline_time,
                recorded_by=recorded_by,
                change_reason=(
                    "Baseline captured when test-location history was enabled; "
                    "configuration before this time is unknown."
                ),
                operation="baseline",
                **_history_values(_internal_row_values(row)),
            )
        )
        existing[item.casefold()] = identity
        inserted += 1
    if inserted:
        db.commit()
    return inserted


def list_test_locations(db: Session, query: str = "") -> list[TestLocationRecord]:
    synchronize_test_location_baselines(db)
    statement = select(TEST_LOCATION_TABLE)
    normalized_query = query.strip().casefold()
    if normalized_query:
        search_value = f"%{normalized_query}%"
        statement = statement.where(
            or_(
                *(
                    func.lower(func.coalesce(column, "")).like(search_value)
                    for column in _COLUMN_BY_FIELD.values()
                )
            )
        )
    rows = list(db.execute(
        statement.order_by(
            func.lower(TEST_LOCATION_TABLE.c.Item),
            TEST_LOCATION_TABLE.c.Item,
        )
    ).mappings())
    identities = {
        identity.current_item.casefold(): identity
        for identity in db.scalars(select(TestLocationIdentity)).all()
    }
    version_counts = dict(
        db.execute(
            select(TestLocationVersion.location_id, func.count())
            .group_by(TestLocationVersion.location_id)
        ).all()
    )
    current_versions = {
        version.location_id: version
        for version in db.scalars(
            select(TestLocationVersion).where(TestLocationVersion.valid_to.is_(None))
        ).all()
    }
    records = []
    for row in rows:
        identity = identities.get(str(row["Item"]).casefold())
        current_version = current_versions.get(identity.id) if identity else None
        records.append(
            _record(
                _internal_row_values(row),
                location_id=identity.id if identity else None,
                valid_from=current_version.valid_from if current_version else None,
                version_count=version_counts.get(identity.id, 0) if identity else 0,
            )
        )
    return records


def get_test_location(db: Session, item: str) -> TestLocationRecord | None:
    synchronize_test_location_baselines(db)
    row = db.execute(
        select(TEST_LOCATION_TABLE).where(TEST_LOCATION_TABLE.c.Item == item)
    ).mappings().one_or_none()
    if row is None:
        return None
    identity = _identity_for_current_item(db, item)
    current_version = _current_version(db, identity.id) if identity else None
    version_count = (
        db.scalar(
            select(func.count())
            .select_from(TestLocationVersion)
            .where(TestLocationVersion.location_id == identity.id)
        )
        if identity
        else 0
    )
    return _record(
        _internal_row_values(row),
        location_id=identity.id if identity else None,
        valid_from=current_version.valid_from if current_version else None,
        version_count=version_count or 0,
    )


def create_test_location(
    db: Session,
    values: dict[str, str],
    *,
    effective_at: datetime | None = None,
    change_reason: str = "Initial test location",
    recorded_by: str = "local",
) -> TestLocationRecord:
    normalized = _normalized_values(values)
    item = str(normalized["Item"])
    effective_time = effective_at or current_business_time()
    reason = _validate_change_metadata(effective_time, change_reason)
    if get_test_location(db, item) is not None:
        raise TestLocationValidationError(f"Item {item!r} already exists.")
    if _identity_for_any_item(db, item) is not None:
        raise TestLocationValidationError(
            f"Item {item!r} has existing history and cannot be reused."
        )
    try:
        db.execute(TEST_LOCATION_TABLE.insert().values(**normalized))
        identity = TestLocationIdentity(current_item=item)
        db.add(identity)
        db.flush()
        db.add(
            TestLocationVersion(
                location_id=identity.id,
                valid_from=effective_time,
                recorded_by=recorded_by,
                change_reason=reason,
                operation="create",
                **_history_values(normalized),
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise TestLocationValidationError(f"Item {item!r} already exists.") from exc
    return _record(
        normalized,
        location_id=identity.id,
        valid_from=effective_time,
        version_count=1,
    )


def update_test_location(
    db: Session,
    original_item: str,
    values: dict[str, str],
    *,
    effective_at: datetime | None = None,
    change_reason: str = "Test location updated",
    recorded_by: str = "local",
) -> TestLocationRecord:
    synchronize_test_location_baselines(db)
    normalized = _normalized_values(values)
    item = str(normalized["Item"])
    identity = _identity_for_current_item(db, original_item)
    if identity is None or identity.retired_at is not None:
        raise TestLocationNotFoundError(original_item)
    current_version = _current_version(db, identity.id)
    if current_version is None:
        raise TestLocationNotFoundError(original_item)
    effective_time = effective_at or current_business_time()
    reason = _validate_change_metadata(
        effective_time,
        change_reason,
        earliest=current_version.valid_from,
    )
    if item != original_item and get_test_location(db, item) is not None:
        raise TestLocationValidationError(f"Item {item!r} already exists.")
    historical_identity = _identity_for_any_item(db, item)
    if historical_identity is not None and historical_identity.id != identity.id:
        raise TestLocationValidationError(
            f"Item {item!r} belongs to another location's history."
        )
    try:
        result = db.execute(
            TEST_LOCATION_TABLE.update()
            .where(TEST_LOCATION_TABLE.c.Item == original_item)
            .values(**normalized)
        )
        if result.rowcount != 1:
            db.rollback()
            raise TestLocationNotFoundError(original_item)
        current_version.valid_to = effective_time
        identity.current_item = item
        db.add(
            TestLocationVersion(
                location_id=identity.id,
                valid_from=effective_time,
                recorded_by=recorded_by,
                change_reason=reason,
                operation="update",
                **_history_values(normalized),
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise TestLocationValidationError(f"Item {item!r} already exists.") from exc
    version_count = db.scalar(
        select(func.count())
        .select_from(TestLocationVersion)
        .where(TestLocationVersion.location_id == identity.id)
    )
    return _record(
        normalized,
        location_id=identity.id,
        valid_from=effective_time,
        version_count=version_count or 0,
    )


def delete_test_location(
    db: Session,
    item: str,
    *,
    effective_at: datetime | None = None,
    change_reason: str = "Test location retired",
    recorded_by: str = "local",
) -> None:
    synchronize_test_location_baselines(db)
    identity = _identity_for_current_item(db, item)
    if identity is None or identity.retired_at is not None:
        raise TestLocationNotFoundError(item)
    current_version = _current_version(db, identity.id)
    if current_version is None:
        raise TestLocationNotFoundError(item)
    effective_time = effective_at or current_business_time()
    reason = _validate_change_metadata(
        effective_time,
        change_reason,
        earliest=current_version.valid_from,
    )
    result = db.execute(
        TEST_LOCATION_TABLE.delete().where(TEST_LOCATION_TABLE.c.Item == item)
    )
    if result.rowcount != 1:
        db.rollback()
        raise TestLocationNotFoundError(item)
    current_version.valid_to = effective_time
    identity.retired_at = effective_time
    identity.retirement_reason = reason
    identity.retirement_recorded_at = utcnow()
    identity.retirement_recorded_by = recorded_by
    db.commit()


def get_test_location_history(
    db: Session,
    *,
    location_id: int | None = None,
    item: str = "",
) -> TestLocationHistory | None:
    synchronize_test_location_baselines(db)
    identity = (
        db.get(TestLocationIdentity, location_id)
        if location_id is not None
        else _identity_for_current_item(db, item)
    )
    if identity is None and item:
        identity = db.scalar(
            select(TestLocationIdentity)
            .join(
                TestLocationVersion,
                TestLocationVersion.location_id == TestLocationIdentity.id,
            )
            .where(func.lower(TestLocationVersion.item) == item.casefold())
            .order_by(TestLocationVersion.valid_from.desc())
            .limit(1)
        )
    if identity is None:
        return None
    versions = tuple(
        db.scalars(
            select(TestLocationVersion)
            .where(TestLocationVersion.location_id == identity.id)
            .order_by(TestLocationVersion.valid_from.desc(), TestLocationVersion.id.desc())
        ).all()
    )
    return TestLocationHistory(
        location_id=identity.id,
        current_item=identity.current_item,
        retired_at=identity.retired_at,
        retirement_reason=identity.retirement_reason,
        retirement_recorded_at=identity.retirement_recorded_at,
        retirement_recorded_by=identity.retirement_recorded_by,
        versions=versions,
    )


def add_earlier_test_location_version(
    db: Session,
    location_id: int,
    values: dict[str, str],
    *,
    effective_at: datetime,
    change_reason: str,
    recorded_by: str = "local",
) -> TestLocationVersion:
    identity = db.get(TestLocationIdentity, location_id)
    if identity is None:
        raise TestLocationNotFoundError(str(location_id))
    earliest_version = db.scalar(
        select(TestLocationVersion)
        .where(TestLocationVersion.location_id == location_id)
        .order_by(TestLocationVersion.valid_from, TestLocationVersion.id)
        .limit(1)
    )
    if earliest_version is None:
        raise TestLocationNotFoundError(str(location_id))
    normalized = _normalized_values(values)
    reason = _validate_change_metadata(effective_at, change_reason)
    if effective_at >= earliest_version.valid_from:
        raise TestLocationValidationError(
            "Earlier version start must be before the earliest known version."
        )
    item = str(normalized["Item"])
    historical_identity = _identity_for_any_item(db, item)
    if historical_identity is not None and historical_identity.id != identity.id:
        raise TestLocationValidationError(
            f"Item {item!r} belongs to another location's history."
        )
    version = TestLocationVersion(
        location_id=identity.id,
        valid_from=effective_at,
        valid_to=earliest_version.valid_from,
        recorded_by=recorded_by,
        change_reason=reason,
        operation="backfill",
        **_history_values(normalized),
    )
    db.add(version)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise TestLocationValidationError(
            "A version already starts at this effective time."
        ) from exc
    return version


def resolve_test_location_at(
    db: Session,
    item: str,
    execution_at: datetime | None,
) -> TestLocationResolution:
    if execution_at is None:
        return TestLocationResolution(status="missing_execution_time", version=None)
    synchronize_test_location_baselines(db)
    version = db.scalar(
        select(TestLocationVersion)
        .where(
            func.lower(TestLocationVersion.item) == item.strip().casefold(),
            TestLocationVersion.valid_from <= execution_at,
            or_(
                TestLocationVersion.valid_to.is_(None),
                execution_at < TestLocationVersion.valid_to,
            ),
        )
        .order_by(TestLocationVersion.valid_from.desc(), TestLocationVersion.id.desc())
        .limit(1)
    )
    if version is not None:
        return TestLocationResolution(status="matched", version=version)
    known_version = db.scalar(
        select(TestLocationVersion)
        .where(func.lower(TestLocationVersion.item) == item.strip().casefold())
        .order_by(TestLocationVersion.valid_from.desc(), TestLocationVersion.id.desc())
        .limit(1)
    )
    if (
        known_version is not None
        and known_version.valid_to is not None
        and execution_at >= known_version.valid_to
    ):
        return TestLocationResolution(status="retired", version=None)
    return TestLocationResolution(
        status="history_unknown" if known_version is not None else "not_found",
        version=None,
    )
