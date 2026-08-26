"""Shared plumbing for calling the configured chat-completions endpoint."""
from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.models import AiConfig
from app.services.skill_runner import SkillFailure


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
