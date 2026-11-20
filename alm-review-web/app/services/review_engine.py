"""Master pipeline orchestrator executing declared stages in exact TOML order."""
from __future__ import annotations

from typing import Any

from app.services.equipment_pipeline import (
    absorb_routing_equipment,
    prepare_equipment_context,
    run_equipment_review_stage,
)
from app.services.equipment_review import OpenQuestion
from app.services.evidence import is_html_report_path
from app.services.review_pipeline import build_pipeline_trace
from app.services.review_types import (
    PipelineOutcome,
    ReportReviewRequest,
    ReviewContext,
    ReviewPlan,
)
from app.services.stage_aggregation import aggregate_review_pipeline
from app.services.stage_image_review import (
    _prepare_image_evidence,
    run_image_review_stage,
)
from app.services.stage_location_review import run_location_review_stage
from app.services.stage_report_review import run_report_review_stage
from app.services.stage_routing import (
    _attach_reference_candidates,
    finalize_routing_after_applicability,
    run_routing_stage,
)
from app.services.stage_text_review import run_text_review_stage


def _build_review_plan(
    content: dict[str, Any],
    equipment_checks: list[dict[str, Any]],
    open_questions: list[OpenQuestion] | None = None,
) -> ReviewPlan:
    text_steps = tuple(int(step["review_step"]) for step in content.get("steps", []))
    report_requests = tuple(
        ReportReviewRequest(
            review_step=int(step["review_step"]),
            paths=tuple(
                path["raw"]
                for path in step["evidence_profile"].get("actual_paths", [])
                if is_html_report_path(path)
            ),
        )
        for step in content.get("steps", [])
        if step.get("text_applicability") != "not_applicable"
        and any(
            is_html_report_path(path)
            for path in step["evidence_profile"].get("actual_paths", [])
        )
    )
    equipment_steps = tuple(
        dict.fromkeys(
            [
                int(check["review_step"])
                for check in equipment_checks
                if check.get("status") != "not_applicable"
            ]
            + [question.review_step for question in open_questions or []]
        )
    )
    return ReviewPlan(
        text_steps=text_steps,
        report_requests=report_requests,
        equipment_steps=equipment_steps,
    )


def _prepare_stage(ctx: ReviewContext) -> None:
    """Deterministic registry and candidate detection before the pipeline starts."""
    prepare_equipment_context(ctx)
    _attach_reference_candidates(
        ctx.content,
        ctx.equipment_checks,
        ctx.open_questions,
    )
    ctx.content["review_plan"] = {
        "text_steps": [
            int(step["review_step"]) for step in ctx.content.get("steps", [])
        ],
        "report_steps": [],
        "equipment_steps": [],
    }


def _plan_specialist_passes(ctx: ReviewContext) -> None:
    """Turn routing and applicability answers into specialist stage work lists."""
    absorb_routing_equipment(ctx)
    ctx.plan = _build_review_plan(
        ctx.content,
        ctx.equipment_checks,
        ctx.open_questions,
    )
    ctx.content["review_plan"] = {
        "text_steps": list(ctx.plan.text_steps),
        "report_steps": [
            request.review_step for request in ctx.plan.report_requests
        ],
        "equipment_steps": list(ctx.plan.equipment_steps),
    }
    ctx.evidence = _prepare_image_evidence(ctx.content, ctx.evidence_config)
    external = ctx.evidence.external_review_enabled
    ctx.content["review_capabilities"].update(
        {
            "path_access": external,
            "folder_scan": external,
            "image_review": external,
            "html_review": bool(ctx.plan.report_requests and external),
        }
    )
    ctx.content["external_evidence_phase"] = "active" if external else "deferred"


def execute_review(ctx: ReviewContext) -> PipelineOutcome:
    """Run one review end to end in declared stage order (`review_pipeline.toml`)."""
    _prepare_stage(ctx)
    routing_trace = run_routing_stage(ctx)
    text_trace = run_text_review_stage(ctx)
    finalize_routing_after_applicability(ctx, routing_trace)
    location_trace = run_location_review_stage(ctx)
    _plan_specialist_passes(ctx)
    image_trace = run_image_review_stage(ctx)
    report_trace = run_report_review_stage(ctx)
    equipment_trace = run_equipment_review_stage(ctx)
    parsed = aggregate_review_pipeline(ctx)
    pipeline = build_pipeline_trace(
        gates={
            "external_evidence_review_enabled": (
                ctx.evidence.external_review_enabled
            ),
            "equipment_review_enabled": ctx.equipment_enabled,
            "location_review_enabled": ctx.location_enabled,
        },
        plan=ctx.content["review_plan"],
        stages={
            "routing": routing_trace,
            "text_review": text_trace,
            "location_review": location_trace,
            "image_review": image_trace,
            "report_review": report_trace,
            "equipment_review": equipment_trace,
            "aggregation": {"status": "completed", "ai_calls": 0},
        },
    )
    return PipelineOutcome(
        parsed=parsed,
        pipeline=pipeline,
        raw_response=ctx.raw_response,
    )
