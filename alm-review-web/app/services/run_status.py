from __future__ import annotations

from typing import Any

REVIEWABLE_RUN_STATUSES = ("Passed", "Failed")
_REVIEWABLE_RUN_STATUS_BY_KEY = {
    status.casefold(): status for status in REVIEWABLE_RUN_STATUSES
}


def normalize_reviewable_run_status(value: Any) -> str | None:
    return _REVIEWABLE_RUN_STATUS_BY_KEY.get(str(value or "").strip().casefold())


def is_reviewable_run_status(value: Any) -> bool:
    return normalize_reviewable_run_status(value) is not None