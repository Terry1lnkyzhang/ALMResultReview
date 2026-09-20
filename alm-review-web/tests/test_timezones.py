from datetime import datetime

from app.services.timezones import alm_execution_in_app_timezone


def test_alm_execution_time_uses_jerusalem_summer_time() -> None:
    converted = alm_execution_in_app_timezone(datetime(2026, 9, 20, 3, 40, 11))

    assert converted is not None
    assert converted.isoformat() == "2026-09-20T08:40:11+08:00"


def test_alm_execution_time_uses_jerusalem_winter_time() -> None:
    converted = alm_execution_in_app_timezone(datetime(2026, 12, 20, 3, 40, 11))

    assert converted is not None
    assert converted.isoformat() == "2026-12-20T09:40:11+08:00"


def test_missing_alm_execution_time_stays_missing() -> None:
    assert alm_execution_in_app_timezone(None) is None