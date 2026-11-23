from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import PROJECT_DIR
from app.database import get_db
from app.models import NonSiteExecutionLocation
from app.models import TestLocationIdentity as LocationIdentity
from app.models import TestLocationScheduledChange as LocationScheduledChange
from app.models import TestLocationVersion as LocationVersion
from app.services import test_locations
from app.services.location_review import location_config_policy_snapshot
from app.services.review_policy import _review_implementation_hash
from app.services.test_location_schedules import (
    apply_due_scheduled_location_changes,
    apply_scheduled_location_change_now,
    cancel_scheduled_location_change,
    create_scheduled_location_change,
    list_scheduled_location_changes,
    update_scheduled_location_change,
)
from app.services.test_locations import (
    TEST_LOCATION_TABLE,
    create_test_location,
    get_test_location,
    get_test_location_history,
    resolve_test_location_at,
    update_test_location,
)
from app.services.test_locations import (
    TestLocationValidationError as LocationValidationError,
)
from app.web import router


def _setup_connection():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    connection = engine.connect()
    connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS atframeworkdb")
    LocationIdentity.__table__.create(connection)
    LocationVersion.__table__.create(connection)
    LocationScheduledChange.__table__.create(connection)
    NonSiteExecutionLocation.__table__.create(connection)
    TEST_LOCATION_TABLE.create(connection)
    return connection


def _setup_client():
    connection = _setup_connection()

    def override_get_db():
        with Session(bind=connection) as db:
            yield db

    app = FastAPI()
    app.mount(
        "/static",
        StaticFiles(directory=PROJECT_DIR / "app" / "static"),
        name="static",
    )
    app.include_router(router)
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app, follow_redirects=False), connection


def test_chained_scheduled_changes_preserve_live_and_policy_until_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 11, 20, 16, 0, 0)
    monkeypatch.setattr(test_locations, "current_business_time", lambda: now)
    connection = _setup_connection()
    try:
        with Session(bind=connection) as db:
            bay09 = create_test_location(
                db,
                {
                    "item": "SY Bay09(CHESS-SCIM-0338)",
                    "product": "IncisiveCT V7.0",
                    "version": "V2",
                    "collimation": "4cm",
                    "platform": "Incisive STD",
                    "system_config": "Incisive G4 STD",
                },
                effective_at=datetime(2026, 11, 18, 1, 0, 0),
                change_reason="Baseline for 11/19-11/21",
            )
            baseline_snapshot = location_config_policy_snapshot(db)

            s1 = create_scheduled_location_change(
                db,
                {
                    **bay09.form_values(),
                    "system_config": "CT5300 G4 STD",
                },
                original_item=bay09.item,
                effective_at=datetime(2026, 11, 22, 0, 0, 0),
                change_reason="11/22 single-day PC switch",
            )
            s2 = create_scheduled_location_change(
                db,
                {
                    **bay09.form_values(),
                    "system_config": "CT 5300/Incisive CT G5 Prem",
                },
                original_item=bay09.item,
                effective_at=datetime(2026, 11, 23, 0, 0, 0),
                change_reason="11/23-11/25 PC switch",
            )

            assert s1.status == "pending"
            assert s2.status == "pending"
            assert apply_due_scheduled_location_changes(db) == (0, 0)
            live_before = get_test_location(db, bay09.item)
            assert live_before is not None
            assert live_before.system_config == "Incisive G4 STD"
            assert location_config_policy_snapshot(db) == baseline_snapshot

            # Advance to 2026-11-22: only s1 applies; s2 remains pending.
            now = datetime(2026, 11, 22, 0, 0, 5)
            assert apply_due_scheduled_location_changes(db) == (1, 0)
            live_nov22 = get_test_location(db, bay09.item)
            assert live_nov22 is not None
            assert live_nov22.system_config == "CT5300 G4 STD"
            pending_after_nov22 = list_scheduled_location_changes(
                db, statuses=("pending",)
            )
            assert [item.id for item in pending_after_nov22] == [s2.id]

            # Advance to 2026-11-23: s2 applies cleanly.
            now = datetime(2026, 11, 23, 0, 1, 0)
            assert apply_due_scheduled_location_changes(db) == (1, 0)
            live_nov23 = get_test_location(db, bay09.item)
            assert live_nov23 is not None
            assert live_nov23.system_config == "CT 5300/Incisive CT G5 Prem"

            r_nov20 = resolve_test_location_at(
                db, "CHESS-SCIM-0338", datetime(2026, 11, 20, 12, 0, 0)
            )
            r_nov22 = resolve_test_location_at(
                db, "CHESS-SCIM-0338", datetime(2026, 11, 22, 12, 0, 0)
            )
            r_nov24 = resolve_test_location_at(
                db, "CHESS-SCIM-0338", datetime(2026, 11, 24, 12, 0, 0)
            )
            assert r_nov20.version is not None
            assert r_nov20.version.system_config == "Incisive G4 STD"
            assert r_nov22.version is not None
            assert r_nov22.version.system_config == "CT5300 G4 STD"
            assert r_nov24.version is not None
            assert r_nov24.version.system_config == "CT 5300/Incisive CT G5 Prem"
    finally:
        connection.close()


def test_scheduled_changes_catch_up_in_chronological_order_and_guard_out_of_order_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 11, 20, 10, 0, 0)
    monkeypatch.setattr(test_locations, "current_business_time", lambda: now)
    connection = _setup_connection()
    try:
        with Session(bind=connection) as db:
            loc = create_test_location(
                db,
                {
                    "item": "ROOM-1",
                    "product": "CT",
                    "version": "V2",
                    "collimation": "4cm",
                    "platform": "Noah",
                    "system_config": "PC-A",
                },
                effective_at=datetime(2026, 11, 19, 0, 0, 0),
                change_reason="Initial",
            )
            first = create_scheduled_location_change(
                db,
                {**loc.form_values(), "system_config": "PC-B"},
                original_item="ROOM-1",
                effective_at=datetime(2026, 11, 22, 0, 0, 0),
                change_reason="Day 1 switch",
            )
            second = create_scheduled_location_change(
                db,
                {**loc.form_values(), "system_config": "PC-C"},
                original_item="ROOM-1",
                effective_at=datetime(2026, 11, 23, 0, 0, 0),
                change_reason="Day 2 switch",
            )

            # Cannot apply second out-of-order while first is still pending.
            with pytest.raises(LocationValidationError, match="earlier scheduled"):
                apply_scheduled_location_change_now(db, second.id)

            # Duplicate timestamp on same location is rejected.
            with pytest.raises(LocationValidationError, match="already uses"):
                create_scheduled_location_change(
                    db,
                    {**loc.form_values(), "system_config": "PC-D"},
                    original_item="ROOM-1",
                    effective_at=datetime(2026, 11, 22, 0, 0, 0),
                    change_reason="Duplicate timestamp",
                )

            # Simulate server offline until 11/24: both catch up in exact order.
            now = datetime(2026, 11, 24, 8, 0, 0)
            assert apply_due_scheduled_location_changes(db) == (2, 0)
            history = get_test_location_history(db, item="ROOM-1")
            assert history is not None
            assert [v.system_config for v in history.versions] == [
                "PC-C",
                "PC-B",
                "PC-A",
            ]
            assert first.id != second.id
    finally:
        connection.close()


def test_conflicting_manual_edit_marks_schedule_failed_and_edit_requeues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 11, 20, 10, 0, 0)
    monkeypatch.setattr(test_locations, "current_business_time", lambda: now)
    connection = _setup_connection()
    try:
        with Session(bind=connection) as db:
            loc = create_test_location(
                db,
                {
                    "item": "ROOM-X",
                    "product": "CT",
                    "version": "V6",
                    "collimation": "2cm",
                    "platform": "Noah",
                    "system_config": "PC-1",
                },
                effective_at=datetime(2026, 11, 19, 0, 0, 0),
                change_reason="Initial",
            )
            sched = create_scheduled_location_change(
                db,
                {**loc.form_values(), "system_config": "PC-2"},
                original_item="ROOM-X",
                effective_at=datetime(2026, 11, 22, 0, 0, 0),
                change_reason="Scheduled swap",
            )

            # Operator manually edits at 11/22 08:00 before scheduler runs.
            now = datetime(2026, 11, 22, 8, 30, 0)
            update_test_location(
                db,
                "ROOM-X",
                {**loc.form_values(), "system_config": "PC-MANUAL"},
                effective_at=datetime(2026, 11, 22, 8, 0, 0),
                change_reason="Manual emergency change",
            )

            assert apply_due_scheduled_location_changes(db) == (0, 1)
            failed_items = list_scheduled_location_changes(db, statuses=("failed",))
            assert len(failed_items) == 1
            assert "later than the current version start" in failed_items[0].error_message

            # Editing the failed schedule to a new future time re-queues it as pending.
            requeued = update_scheduled_location_change(
                db,
                sched.id,
                {**loc.form_values(), "system_config": "PC-2"},
                effective_at=datetime(2026, 11, 23, 0, 0, 0),
                change_reason="Rescheduled after manual intervention",
            )
            assert requeued.status == "pending"
            assert requeued.error_message == ""

            now = datetime(2026, 11, 23, 0, 1, 0)
            assert apply_due_scheduled_location_changes(db) == (1, 0)
            current = get_test_location(db, "ROOM-X")
            assert current is not None
            assert current.system_config == "PC-2"

            # Cancelled schedules cannot be applied.
            extra = create_scheduled_location_change(
                db,
                {**loc.form_values(), "system_config": "PC-3"},
                original_item="ROOM-X",
                effective_at=datetime(2026, 11, 25, 0, 0, 0),
                change_reason="Optional swap",
            )
            cancelled = cancel_scheduled_location_change(db, extra.id)
            assert cancelled.status == "cancelled"
    finally:
        connection.close()


def test_scheduled_test_location_web_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 11, 20, 16, 0, 0)
    monkeypatch.setattr(test_locations, "current_business_time", lambda: now)
    client, connection = _setup_client()
    try:
        create_resp = client.post(
            "/ops/test-locations/new",
            data={
                "item": "SY Bay02(CHESS-CAST-10003)",
                "product": "CT5300 V7.0",
                "version": "V2",
                "collimation": "2cm",
                "platform": "Enhance Incisive STD",
                "system_config": "CT 5300/Incisive CT G4 Prem",
                "effective_at": "2026-11-19T00:00:00",
                "change_reason": "Initial",
            },
        )
        assert create_resp.status_code == 303

        form_page = client.get(
            "/ops/test-locations/schedule",
            params={"item": "SY Bay02(CHESS-CAST-10003)"},
        )
        assert form_page.status_code == 200
        assert "Schedule future configuration change" in form_page.text

        sched_resp = client.post(
            "/ops/test-locations/schedule",
            data={
                "original_item": "SY Bay02(CHESS-CAST-10003)",
                "item": "SY Bay02(CHESS-CAST-10003)",
                "product": "CT5300 V7.0",
                "version": "V2",
                "collimation": "2cm",
                "platform": "Enhance Incisive STD",
                "system_config": "CT Tenara G5 Prem",
                "effective_at": "2026-11-23T00:00:00",
                "change_reason": "Planned PC swap",
            },
        )
        assert sched_resp.status_code == 303

        registry_page = client.get("/ops/test-locations")
        assert registry_page.status_code == 200
        assert "1 queued" in registry_page.text
        assert "1 scheduled" in registry_page.text
        assert "CT Tenara G5 Prem" in registry_page.text

        edit_page = client.get("/ops/test-locations/schedule/1/edit")
        assert edit_page.status_code == 200
        assert 'value="CT Tenara G5 Prem"' in edit_page.text

        update_resp = client.post(
            "/ops/test-locations/schedule/1/edit",
            data={
                "item": "SY Bay02(CHESS-CAST-10003)",
                "product": "CT5300 V7.0",
                "version": "V2",
                "collimation": "2cm",
                "platform": "Enhance Incisive STD",
                "system_config": "CT Tenara G5 Prem",
                "effective_at": "2026-11-23T08:00:00",
                "change_reason": "Planned morning PC swap",
            },
        )
        assert update_resp.status_code == 303

        # Early manual Apply now works from web UI.
        now = datetime(2026, 11, 22, 23, 30, 0)
        apply_resp = client.post("/ops/test-locations/schedule/1/apply-now")
        assert apply_resp.status_code == 303

        after_page = client.get("/ops/test-locations")
        assert "0 queued" in after_page.text
        assert "CT Tenara G5 Prem" in after_page.text
    finally:
        client.close()
        connection.close()


def test_scheduled_changes_module_does_not_alter_review_policy_files() -> None:
    assert len(_review_implementation_hash()) == 64

