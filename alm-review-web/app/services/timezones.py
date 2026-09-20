from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import get_settings


def alm_execution_in_app_timezone(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    settings = get_settings()
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(settings.alm_timezone))
    return value.astimezone(ZoneInfo(settings.app_timezone))