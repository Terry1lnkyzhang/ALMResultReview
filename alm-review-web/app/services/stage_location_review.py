"""Stage 3 (`location_review`): parent-folder configuration vs. historical test location."""
from __future__ import annotations

import json
from typing import Any

import httpx

from app.services.ai_transport import ai_headers, completion_url, skill_failure
from app.services.location_review import (
    fallback_location_skill_output,
    validate_location_skill_output,
)
from app.services.review_types import ReviewContext, _recalculate_result
from app.services.skill_runner import skill_runner


def run_location_review_stage(ctx: ReviewContext) -> dict[str, Any]:
    assessment = dict(ctx.location_assessment)
    trace: dict[str, Any] | None = None
    if assessment.get("status") == "ready":
        skill_input = {
            "alm_location": assessment["alm_location"],
            "folder_path": assessment["folder_path"],
            "parent_name": assessment["parent_name"],
            "location_config": assessment["selected_config"],
        }
        trace = skill_runner.run(
            "location-consistency",
            skill_input,
            endpoint=completion_url(ctx.ai_config.base_url),
            model_name=ctx.ai_config.model_name,
            headers=ai_headers(ctx.ai_config),
            timeout_seconds=ctx.ai_config.timeout_seconds,
            granted_capabilities={
                "review.location.parent_name",
                "location.configuration.metadata",
            },
            request_post=httpx.post,
            output_validator=validate_location_skill_output,
            output_fallback=fallback_location_skill_output,
        )
        if trace.get("status") != "completed":
            raise skill_failure(trace, "Location consistency Skill failed.")
        semantic_review = trace["output"]
        assessment.update(
            status=semantic_review["status"],
            reason=semantic_review["reason"],
            semantic_review=semantic_review,
        )
        raw_outputs = json.loads(ctx.raw_response) if ctx.raw_response else {}
        raw_outputs["location-consistency"] = semantic_review
        ctx.raw_response = json.dumps(
            raw_outputs,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    ctx.location_assessment = assessment
    ctx.text_result["location_review"] = assessment
    ctx.text_result = _recalculate_result(ctx.text_result)
    return {
        "status": (
            "disabled" if assessment.get("status") == "disabled" else "completed"
        ),
        "ai_calls": int(trace.get("ai_calls", 0)) if trace else 0,
        "assessment": assessment,
        "skill": trace,
    }
