"""Shared plumbing for calling the configured chat-completions endpoint."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AiConfig, utcnow
from app.services.skill_runner import SkillFailure

ENDPOINT_FAILURE_THRESHOLD = 3
ENDPOINT_COOLDOWN_SECONDS = 60
_AUTH_FAILURE_MARKERS = ("HTTP 401", "HTTP 403", "401 Unauthorized", "403 Forbidden")


def completion_url(configured_url: str) -> str:
    url = configured_url.rstrip("/")
    return url if url.endswith("/chat/completions") else f"{url}/chat/completions"


def ai_headers(ai_config: AiConfig) -> dict[str, str]:
    api_key = (ai_config.api_key or "").strip() or get_settings().ai_api_key.strip()
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def skill_failure(trace: dict[str, Any], fallback: str) -> SkillFailure:
    return SkillFailure(
        trace.get("error") or fallback,
        retryable=bool(trace.get("retryable", True)),
    )


def ai_endpoint_available(ai_config: AiConfig) -> bool:
    if not ai_config.enabled or ai_config.health_status == "auth_error":
        return False
    return ai_config.cooldown_until is None or ai_config.cooldown_until <= utcnow()


def ai_endpoint_health_status(ai_config: AiConfig) -> str:
    if not ai_config.enabled:
        return "disabled"
    if ai_config.health_status == "cooldown" and ai_endpoint_available(ai_config):
        return "recovering"
    return ai_config.health_status


def available_ai_configs(db: Session) -> list[AiConfig]:
    configs = db.scalars(
        select(AiConfig)
        .where(AiConfig.enabled.is_(True))
        .order_by(AiConfig.id)
    ).all()
    return [config for config in configs if ai_endpoint_available(config)]


def record_ai_endpoint_success(db: Session, ai_config_id: int | None) -> None:
    if ai_config_id is None:
        return
    config = db.scalar(
        select(AiConfig)
        .where(AiConfig.id == ai_config_id)
        .with_for_update()
    )
    if config is None:
        return
    config.health_status = "healthy"
    config.consecutive_failures = 0
    config.cooldown_until = None
    config.last_error = ""
    config.last_success_at = utcnow()
    db.commit()


def record_ai_endpoint_failure(
    db: Session,
    ai_config_id: int | None,
    exc: Exception,
) -> None:
    if ai_config_id is None or not isinstance(exc, SkillFailure):
        return
    config = db.scalar(
        select(AiConfig)
        .where(AiConfig.id == ai_config_id)
        .with_for_update()
    )
    if config is None:
        return
    error = str(exc)[:2000]
    config.last_error = error
    config.last_failure_at = utcnow()
    if any(marker in error for marker in _AUTH_FAILURE_MARKERS):
        config.health_status = "auth_error"
        config.cooldown_until = None
        config.consecutive_failures += 1
    elif getattr(exc, "retryable", False) is True:
        config.consecutive_failures += 1
        if config.consecutive_failures >= ENDPOINT_FAILURE_THRESHOLD:
            config.health_status = "cooldown"
            config.cooldown_until = utcnow() + timedelta(
                seconds=ENDPOINT_COOLDOWN_SECONDS
            )
    db.commit()
