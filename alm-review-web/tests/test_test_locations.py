from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import PROJECT_DIR
from app.database import get_db
from app.models import (
    NonSiteExecutionLocation,
)
from app.models import (
    TestLocationIdentity as LocationIdentity,
)
from app.models import (
    TestLocationVersion as LocationVersion,
)
from app.services.test_locations import (
    TEST_LOCATION_TABLE,
    add_earlier_test_location_version,
    create_test_location,
    delete_test_location,
    get_test_location,
    get_test_location_history,
    list_test_locations,
    resolve_test_location_at,
    synchronize_test_location_baselines,
    update_test_location,
)
from app.services.test_locations import (
    TestLocationNotFoundError as LocationNotFoundError,
)
from app.services.test_locations import (
    TestLocationValidationError as LocationValidationError,
)
from app.web import router


def test_test_location_table_uses_current_atframework_column_names() -> None:
    assert [column.name for column in TEST_LOCATION_TABLE.c] == [
        "Item",
        "Product",
        "DMS Version",
        "DMS Coverage",
        "Couch",
        "Computer",
    ]


def _test_client() -> tuple[TestClient, object]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    connection = engine.connect()
    connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS atframeworkdb")
    LocationIdentity.__table__.create(connection)
    LocationVersion.__table__.create(connection)
    NonSiteExecutionLocation.__table__.create(connection)
    TEST_LOCATION_TABLE.create(connection)

    def override_get_db():
        with Session(bind=connection) as db:
            yield db

    app = FastAPI()
    app.mount("/static", StaticFiles(directory=PROJECT_DIR / "app" / "static"), name="static")
    app.include_router(router)
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app, follow_redirects=False), connection


def test_test_location_crud_and_search() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS atframeworkdb")
        LocationIdentity.__table__.create(connection)
        LocationVersion.__table__.create(connection)
        TEST_LOCATION_TABLE.create(connection)
        with Session(bind=connection) as db:
            created = create_test_location(
                db,
                {
                    "item": "CT-ROOM-01",
                    "product": "Spectral CT",
                    "version": "R12",
                    "collimation": "40 mm",
                    "platform": "Kylin",
                    "system_config": "Dual monitor",
                },
            )

            assert created.item == "CT-ROOM-01"
            assert [record.item for record in list_test_locations(db, "kylin")] == [
                "CT-ROOM-01"
            ]

            updated = update_test_location(
                db,
                "CT-ROOM-01",
                {
                    **created.form_values(),
                    "item": "CT-ROOM-02",
                    "version": "R13",
                },
            )

            assert get_test_location(db, "CT-ROOM-01") is None
            assert updated.item == "CT-ROOM-02"
            assert get_test_location(db, "CT-ROOM-02") == updated

            delete_test_location(db, "CT-ROOM-02")

            assert list_test_locations(db) == []


def test_test_location_rejects_invalid_or_duplicate_items() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS atframeworkdb")
        LocationIdentity.__table__.create(connection)
        LocationVersion.__table__.create(connection)
        TEST_LOCATION_TABLE.create(connection)
        with Session(bind=connection) as db:
            values = {
                "item": "ROOM-A",
                "product": "CT",
                "version": "",
                "collimation": "",
                "platform": "",
                "system_config": "",
            }
            create_test_location(db, values)

            try:
                create_test_location(db, values)
            except LocationValidationError as exc:
                assert "already exists" in str(exc)
            else:
                raise AssertionError("Duplicate Item was accepted")

            try:
                create_test_location(db, {**values, "item": ""})
            except LocationValidationError as exc:
                assert str(exc) == "Item is required."
            else:
                raise AssertionError("Blank Item was accepted")

            try:
                delete_test_location(db, "MISSING")
            except LocationNotFoundError:
                pass
            else:
                raise AssertionError("Missing Item deletion was accepted")


def test_test_location_routes_cover_crud_and_search() -> None:
    client, connection = _test_client()
    try:
        create_response = client.post(
            "/ops/test-locations/new",
            data={
                "item": "ROOM/A 01",
                "product": "Spectral CT",
                "version": "R12",
                "collimation": "40 mm",
                "platform": "Kylin",
                "system_config": "Dual monitor",
                "effective_at": "2026-08-01T08:00",
                "change_reason": "Initial controlled record",
            },
        )
        assert create_response.status_code == 303
        assert create_response.headers["location"].startswith("/ops/test-locations?")

        list_response = client.get("/ops/test-locations?query=kylin")
        assert list_response.status_code == 200
        assert "ROOM/A 01" in list_response.text
        assert "Spectral CT" in list_response.text
        assert "14 test locations" not in list_response.text

        edit_page = client.get("/ops/test-locations/edit", params={"item": "ROOM/A 01"})
        assert edit_page.status_code == 200
        assert 'value="ROOM/A 01"' in edit_page.text

        update_response = client.post(
            "/ops/test-locations/edit",
            data={
                "original_item": "ROOM/A 01",
                "item": "ROOM/B 02",
                "product": "Spectral CT",
                "version": "R13",
                "collimation": "40 mm",
                "platform": "Kylin",
                "system_config": "Dual monitor",
                "effective_at": "2026-08-02T08:00",
                "change_reason": "Room renamed after scanner upgrade",
            },
        )
        assert update_response.status_code == 303

        history_response = client.get(
            "/ops/test-locations/history",
            params={"location_id": 1},
        )
        assert history_response.status_code == 200
        assert "Add earlier version" in history_response.text

        backfill_page = client.get(
            "/ops/test-locations/history/backfill",
            params={"location_id": 1},
        )
        assert backfill_page.status_code == 200
        assert 'value="ROOM/A 01"' in backfill_page.text
        backfill_response = client.post(
            "/ops/test-locations/history/backfill",
            data={
                "location_id": "1",
                "item": "ROOM/OLD 00",
                "product": "Spectral CT",
                "version": "R10",
                "collimation": "20 mm",
                "platform": "Dragon",
                "system_config": "Single monitor",
                "effective_at": "2025-01-01T08:00",
                "change_reason": "Recovered from the 2025 configuration workbook",
            },
        )
        assert backfill_response.status_code == 303
        assert backfill_response.headers["location"].startswith(
            "/ops/test-locations/history?location_id=1"
        )
        current_item = connection.execute(
            select(TEST_LOCATION_TABLE.c.Item)
        ).scalar_one()
        backfill_version = connection.execute(
            select(LocationVersion.item, LocationVersion.valid_to).where(
                LocationVersion.operation == "backfill"
            )
        ).one()
        assert current_item == "ROOM/B 02"
        assert backfill_version.item == "ROOM/OLD 00"
        assert backfill_version.valid_to == datetime(2026, 8, 1, 8, 0)

        retire_response = client.post(
            "/ops/test-locations/retire",
            data={
                "item": "ROOM/B 02",
                "effective_at": "2026-08-03T08:00",
                "change_reason": "Location decommissioned",
            },
        )
        assert retire_response.status_code == 303
        assert client.get("/ops/test-locations").text.count("ROOM/B 02") == 0
    finally:
        client.close()
        connection.close()


def test_non_site_execution_location_routes_cover_create_update_and_duplicates() -> None:
    client, connection = _test_client()
    try:
        create_response = client.post(
            "/ops/test-locations/non-site",
            data={"name": "Offline"},
        )
        assert create_response.status_code == 303

        duplicate_response = client.post(
            "/ops/test-locations/non-site",
            data={"name": " offline "},
        )
        assert duplicate_response.status_code == 303
        assert "message_kind=error" in duplicate_response.headers["location"]

        page = client.get("/ops/test-locations")
        assert page.status_code == 200
        assert "Non-site execution modes" in page.text
        assert 'value="Offline"' in page.text
        assert 'name="enabled" value="true" checked' in page.text

        update_response = client.post(
            "/ops/test-locations/non-site/1",
            data={"name": "Remote workstation"},
        )
        assert update_response.status_code == 303
        row = connection.execute(
            select(
                NonSiteExecutionLocation.name,
                NonSiteExecutionLocation.normalized_key,
                NonSiteExecutionLocation.enabled,
            )
        ).one()
        assert row.name == "Remote workstation"
        assert row.normalized_key == "remote workstation"
        assert row.enabled is False

        updated_page = client.get("/ops/test-locations")
        assert 'value="Remote workstation"' in updated_page.text
        assert 'name="enabled" value="true" checked' not in updated_page.text
    finally:
        client.close()
        connection.close()


def test_test_location_versions_resolve_by_execution_time_and_survive_retirement() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS atframeworkdb")
        LocationIdentity.__table__.create(connection)
        LocationVersion.__table__.create(connection)
        TEST_LOCATION_TABLE.create(connection)
        baseline_at = datetime(2026, 1, 1, 8, 0)
        changed_at = datetime(2026, 2, 1, 9, 30)
        retired_at = datetime(2026, 3, 1, 12, 0)
        with Session(bind=connection) as db:
            db.execute(
                TEST_LOCATION_TABLE.insert().values(
                    Item="ROOM-A",
                    Product="CT",
                    Version="R1",
                    Collimation=None,
                    Platform="Kylin",
                    SystemConfig=None,
                )
            )
            db.commit()

            assert synchronize_test_location_baselines(
                db,
                baseline_at=baseline_at,
            ) == 1
            assert resolve_test_location_at(
                db,
                "ROOM-A",
                baseline_at - timedelta(seconds=1),
            ).status == "history_unknown"

            add_earlier_test_location_version(
                db,
                1,
                {
                    "item": "ROOM-OLD",
                    "product": "CT",
                    "version": "R0",
                    "collimation": "20 mm",
                    "platform": "Dragon",
                    "system_config": "Single monitor",
                },
                effective_at=datetime(2025, 1, 1, 8, 0),
                change_reason="Recovered from the 2025 configuration workbook",
                recorded_by="review-admin",
            )
            assert resolve_test_location_at(
                db,
                "ROOM-OLD",
                datetime(2025, 6, 1, 8, 0),
            ).status == "matched"
            assert resolve_test_location_at(
                db,
                "ROOM-OLD",
                datetime(2024, 12, 31, 23, 59),
            ).status == "history_unknown"

            updated = update_test_location(
                db,
                "ROOM-A",
                {
                    "item": "ROOM-B",
                    "product": "CT",
                    "version": "R2",
                    "collimation": "40 mm",
                    "platform": "Kylin",
                    "system_config": "Dual monitor",
                },
                effective_at=changed_at,
                change_reason="Scanner upgraded and room renamed",
                recorded_by="review-admin",
            )

            assert updated.version_count == 3
            old_resolution = resolve_test_location_at(
                db,
                "ROOM-A",
                changed_at - timedelta(seconds=1),
            )
            new_resolution = resolve_test_location_at(db, "ROOM-B", changed_at)
            assert old_resolution.version is not None
            assert old_resolution.version.version == "R1"
            assert new_resolution.version is not None
            assert new_resolution.version.version == "R2"

            delete_test_location(
                db,
                "ROOM-B",
                effective_at=retired_at,
                change_reason="Location decommissioned",
                recorded_by="review-admin",
            )

            assert get_test_location(db, "ROOM-B") is None
            assert resolve_test_location_at(
                db,
                "ROOM-B",
                retired_at - timedelta(seconds=1),
            ).status == "matched"
            assert resolve_test_location_at(db, "ROOM-B", retired_at).status == "retired"
            history = get_test_location_history(db, item="ROOM-A")
            assert history is not None
            assert history.current_item == "ROOM-B"
            assert history.retired_at == retired_at
            assert history.retirement_reason == "Location decommissioned"
            assert [version.operation for version in history.versions] == [
                "update",
                "baseline",
                "backfill",
            ]

            try:
                create_test_location(
                    db,
                    {
                        "item": "ROOM-A",
                        "product": "Replacement CT",
                        "version": "R1",
                        "collimation": "",
                        "platform": "Kylin",
                        "system_config": "",
                    },
                    effective_at=retired_at + timedelta(seconds=1),
                    change_reason="Attempt to reuse an old alias",
                    recorded_by="review-admin",
                )
            except LocationValidationError as exc:
                assert "existing history" in str(exc)
            else:
                raise AssertionError("Historical Item alias was reused")


def test_earlier_version_rejects_overlap_and_another_locations_item() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS atframeworkdb")
        LocationIdentity.__table__.create(connection)
        LocationVersion.__table__.create(connection)
        TEST_LOCATION_TABLE.create(connection)
        baseline_at = datetime(2026, 1, 1, 8, 0)
        with Session(bind=connection) as db:
            for item in ("ROOM-A", "ROOM-B"):
                db.execute(
                    TEST_LOCATION_TABLE.insert().values(
                        Item=item,
                        Product="CT",
                        Version="R1",
                        Collimation=None,
                        Platform="Kylin",
                        SystemConfig=None,
                    )
                )
            db.commit()
            assert synchronize_test_location_baselines(
                db,
                baseline_at=baseline_at,
            ) == 2
            values = {
                "item": "ROOM-A-OLD",
                "product": "CT",
                "version": "R0",
                "collimation": "",
                "platform": "Dragon",
                "system_config": "",
            }

            try:
                add_earlier_test_location_version(
                    db,
                    1,
                    values,
                    effective_at=baseline_at,
                    change_reason="Invalid overlapping period",
                )
            except LocationValidationError as exc:
                assert "before the earliest known version" in str(exc)
            else:
                raise AssertionError("Overlapping earlier version was accepted")

            try:
                add_earlier_test_location_version(
                    db,
                    1,
                    {**values, "item": "ROOM-B"},
                    effective_at=datetime(2025, 1, 1, 8, 0),
                    change_reason="Invalid cross-location alias",
                )
            except LocationValidationError as exc:
                assert "another location's history" in str(exc)
            else:
                raise AssertionError("Another location's historical Item was accepted")
