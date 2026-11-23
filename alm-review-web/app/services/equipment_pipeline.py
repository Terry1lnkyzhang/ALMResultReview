"""Consolidated equipment review pipeline: initial deterministic registry analysis,
routing extraction/role absorption, HTML report phantom verification, second-pass
AI disambiguation, and final step/criterion guard application.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx

from app.models import AiConfig, EquipmentRegistry
from app.services.ai_transport import ai_headers, completion_url, skill_failure
from app.services.equipment_review import (
    Candidate,
    OpenQuestion,
    analyze_equipment_steps,
    apply_equipment_disambiguation,
    apply_extracted_equipment,
    apply_html_report_equipment,
    equipment_reference,
    merge_pending_names,
    registry_equipment_names,
    requirement_previous_equipment_ids,
)
from app.services.html_evidence import HtmlEvidenceResult, actual_phantom_codes
from app.services.review_types import (
    ReviewContext,
    _append_step_issue,
    _criterion,
    _recalculate_result,
)
from app.services.skill_runner import SkillFailure, skill_runner

# One request may not outgrow the equipment-role output budget.
EQUIPMENT_BATCH_MAX_STEPS = 8


def request_equipment_disambiguation(
    ai_config: AiConfig,
    questions: list[OpenQuestion],
    registry_names: list[str],
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    skill_input = {
        "registry_equipment_names": registry_names,
        "steps": [
            {
                "review_step": question.review_step,
                "description": question.description,
                "expected": question.expected,
                "actual": question.actual,
                "reported_identifiers": list(question.reported_identifiers),
                "extracted_device_names": list(question.device_names),
                "previously_matched_equipment_ids": list(
                    question.previously_matched_equipment_ids
                ),
                "candidate_equipment": [
                    candidate.as_dict() for candidate in question.candidates
                ],
            }
            for question in questions
        ]
    }
    trace = skill_runner.run(
        "equipment-role",
        skill_input,
        endpoint=completion_url(ai_config.base_url),
        model_name=ai_config.model_name,
        headers=ai_headers(ai_config),
        timeout_seconds=ai_config.timeout_seconds,
        granted_capabilities={
            "review.step_text",
            "equipment.registry.candidates",
            "equipment.registry.names",
            "equipment.previous_matches",
        },
        request_post=httpx.post,
    )
    if trace.get("status") != "completed":
        raise skill_failure(trace, "Equipment role Skill failed.")
    asked = {question.review_step: question for question in questions}
    allowed_names = set(registry_names)
    decisions: dict[int, dict[str, Any]] = {}
    for decision in trace["output"]["decisions"]:
        review_step = int(decision["review_step"])
        allowed = {
            candidate.equipment_id
            for candidate in asked[review_step].candidates
        }
        if any(
            identifier not in allowed
            for identifier in decision["selected_equipment_ids"]
        ):
            raise SkillFailure(
                "Equipment role Skill selected an unavailable equipment ID.",
                retryable=False,
            )
        if any(
            name not in allowed_names
            for name in decision.get("selected_equipment_names", [])
        ):
            raise SkillFailure(
                "Equipment role Skill selected a name outside the registry "
                "vocabulary.",
                retryable=False,
            )
        decisions[review_step] = {
            "role": decision["role"],
            "required": decision["required"],
            "selected_equipment_ids": decision["selected_equipment_ids"],
            "selected_equipment_names": decision.get("selected_equipment_names", []),
            "reason": decision["reason"],
        }
    return decisions, trace


def apply_first_pass_equipment_decisions(
    content: dict[str, Any],
    checks: list[dict[str, Any]],
    questions: list[OpenQuestion],
    equipment_registry: list[EquipmentRegistry],
) -> list[OpenQuestion]:
    registry_ids = {equipment_reference(item) for item in equipment_registry}
    steps = {
        int(step["review_step"]): step for step in content.get("steps", [])
    }
    checks_by_step = {
        int(check["review_step"]): check for check in checks
    }
    remaining: list[OpenQuestion] = []
    resolved: dict[int, dict[str, Any]] = {}
    for question in questions:
        review_step = question.review_step
        step = steps[review_step]
        candidates = {
            candidate["candidate_id"]: candidate
            for candidate in step.get("reference_candidates", [])
        }
        equipment_decisions = [
            (candidates[decision["candidate_id"]], decision)
            for decision in step.get("reference_decisions", [])
            if candidates[decision["candidate_id"]]["type"] == "equipment"
        ]
        if not equipment_decisions or any(
            decision["role"] == "uncertain"
            for _, decision in equipment_decisions
        ):
            remaining.append(question)
            continue
        controlled = [
            (candidate, decision)
            for candidate, decision in equipment_decisions
            if decision["role"] == "test_equipment"
        ]
        if controlled:
            previous_ids = set(question.previously_matched_equipment_ids)
            actual_ids = {
                candidate["value"]
                for candidate, _ in controlled
                if candidate.get("source_field") == "actual"
                and candidate["value"] in registry_ids
            }
            selected_ids = list(
                dict.fromkeys(
                    candidate["value"]
                    for candidate, _ in controlled
                    if candidate["value"] in registry_ids
                    and (
                        candidate.get("source_field") == "actual"
                        or candidate["value"] in previous_ids
                    )
                )
            )
            if not selected_ids and question.previously_matched_equipment_ids:
                remaining.append(question)
                continue
            if question.device_names and not actual_ids:
                # Only the registry vocabulary can still name this device.
                remaining.append(question)
                continue
            resolved[review_step] = {
                "role": "controlled_equipment",
                "required": bool(checks_by_step[review_step]["required"]),
                "selected_equipment_ids": selected_ids,
                "reason": " ".join(
                    decision["reason"] for _, decision in controlled
                )[:300],
            }
        else:
            if requirement_previous_equipment_ids(
                question.description,
                question.expected,
                question.previously_matched_equipment_ids,
                equipment_registry,
            ):
                remaining.append(question)
                continue
            resolved[review_step] = {
                "role": "dut_or_other",
                "required": False,
                "selected_equipment_ids": [],
                "reason": " ".join(
                    decision["reason"] for _, decision in equipment_decisions
                )[:300],
            }
    if resolved:
        apply_equipment_disambiguation(
            content,
            checks,
            resolved,
            equipment_registry,
        )
    return remaining


def absorb_first_pass(
    content: dict[str, Any],
    checks: list[dict[str, Any]],
    questions: list[OpenQuestion],
    equipment_registry: list[EquipmentRegistry],
) -> list[OpenQuestion]:
    """Consume the first pass `extracted_equipment` side channel."""
    resolved, pending_device_names = apply_extracted_equipment(
        content,
        checks,
        {
            int(step["review_step"]): step.get("extracted_equipment", [])
            for step in content.get("steps", [])
        },
        equipment_registry,
    )
    remaining = merge_pending_names(
        content,
        pending_device_names,
        equipment_registry,
        [item for item in questions if item.review_step not in resolved],
    )
    return apply_first_pass_equipment_decisions(
        content,
        checks,
        remaining,
        equipment_registry,
    )


def _equipment_batches(
    questions: list[OpenQuestion],
) -> list[list[OpenQuestion]]:
    return [
        questions[index:index + EQUIPMENT_BATCH_MAX_STEPS]
        for index in range(0, len(questions), EQUIPMENT_BATCH_MAX_STEPS)
    ]


def run_equipment_pipeline(
    ai_config: AiConfig,
    content: dict[str, Any],
    checks: list[dict[str, Any]],
    questions: list[OpenQuestion],
    equipment_registry: list[EquipmentRegistry],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not questions:
        return checks, {
            "skill_id": "equipment-role",
            "status": "not_applicable",
            "ai_calls": 0,
        }
    trace: dict[str, Any] = {
        "skill_id": "equipment-role",
        "status": "failed",
        "ai_calls": 0,
    }
    try:
        traces: list[dict[str, Any]] = []
        for batch in _equipment_batches(questions):
            needs_vocabulary = any(
                item.kind == "name_mapping"
                or item.previously_matched_equipment_ids
                for item in batch
            )
            decisions, batch_trace = request_equipment_disambiguation(
                ai_config,
                batch,
                registry_equipment_names(equipment_registry)
                if needs_vocabulary
                else [],
            )
            traces.append(batch_trace)
            apply_equipment_disambiguation(
                content,
                checks,
                decisions,
                equipment_registry,
            )
        trace = _merge_equipment_traces(traces)
    except Exception as exc:
        asked_steps = {item.review_step for item in questions}
        for check in checks:
            if check["review_step"] not in asked_steps:
                continue
            check["disambiguation_error"] = str(exc)[:300]
            check["status"] = "manual"
            check["code"] = "equipment_disambiguation_failed"
            check["summary"] = (
                "AI disambiguation of the equipment role failed; manual review needed."
            )
        trace["error"] = str(exc)[:1000]
    return checks, trace


def _merge_equipment_traces(traces: list[dict[str, Any]]) -> dict[str, Any]:
    if len(traces) == 1:
        return traces[0]
    merged = dict(traces[0])
    merged["ai_calls"] = sum(int(item.get("ai_calls", 0)) for item in traces)
    merged["batches"] = len(traces)
    merged["output"] = {
        "decisions": [
            decision
            for item in traces
            for decision in (item.get("output") or {}).get("decisions", [])
        ]
    }
    return merged


def prepare_equipment_context(ctx: ReviewContext) -> None:
    """Run deterministic equipment step analysis before routing."""
    if ctx.equipment_enabled:
        ctx.equipment_checks, ctx.open_questions = analyze_equipment_steps(
            ctx.content,
            ctx.equipment_registry,
        )


def absorb_routing_equipment(ctx: ReviewContext) -> None:
    """Fold routing-stage equipment extractions and role decisions into context."""
    if not ctx.equipment_enabled:
        return
    original_question_steps = {
        question.review_step for question in ctx.open_questions
    }
    ctx.open_questions = absorb_first_pass(
        ctx.content,
        ctx.equipment_checks,
        ctx.open_questions,
        ctx.equipment_registry,
    )
    remaining_question_steps = {
        question.review_step for question in ctx.open_questions
    }
    ctx.first_pass_resolved_steps = len(
        original_question_steps - remaining_question_steps
    )


def collect_html_reported_equipment(
    review_step: int,
    report_paths: dict[str, str],
    html_results: dict[int, dict[str, HtmlEvidenceResult]],
    assessment: dict[str, Any],
    reported_by_step: dict[int, list[dict[str, str]]],
) -> None:
    """Extract verified `Phantom Code` entries from cited `Actual:` HTML blocks."""
    reported_equipment: list[dict[str, str]] = []
    for citation in assessment.get("evidence", []):
        if "actual" not in citation.get("supports", []):
            continue
        source_path = report_paths.get(citation.get("report_id", ""))
        result = (
            html_results.get(review_step, {}).get(source_path)
            if source_path
            else None
        )
        if result is None:
            continue
        block = next(
            (
                item
                for item in result.blocks
                if item.block_id == citation.get("block_id")
            ),
            None,
        )
        if block is None:
            continue
        for label, identifier in actual_phantom_codes(block):
            reported_equipment.append(
                {
                    "equipment_id": identifier,
                    "source_path": source_path,
                    "block_id": block.block_id,
                    "label": label,
                }
            )
    if reported_equipment:
        reported_by_step[review_step] = list(
            {
                (
                    item["equipment_id"],
                    item["source_path"],
                    item["block_id"],
                ): item
                for item in reported_equipment
            }.values()
        )


def _enrich_questions_with_html_equipment(
    questions: list[OpenQuestion],
    reported_by_step: dict[int, list[dict[str, str]]],
    equipment_registry: list[EquipmentRegistry],
) -> list[OpenQuestion]:
    if not reported_by_step or not questions:
        return questions
    by_id = {
        item.equipment_id.casefold(): item
        for item in equipment_registry
        if item.equipment_id
    }
    enriched: list[OpenQuestion] = []
    for question in questions:
        entries = reported_by_step.get(question.review_step, [])
        if not entries:
            enriched.append(question)
            continue
        existing_ids = {candidate.equipment_id for candidate in question.candidates}
        extra_candidates = list(question.candidates)
        for entry in entries:
            identifier = (entry.get("equipment_id") or "").strip()
            matched = by_id.get(identifier.casefold())
            if matched is None:
                continue
            candidate = Candidate.of(matched)
            if candidate.equipment_id not in existing_ids:
                extra_candidates.append(candidate)
                existing_ids.add(candidate.equipment_id)
        enriched.append(replace(question, candidates=tuple(extra_candidates)))
    return enriched


def _downgrade_missing_equipment_for_unreadable_evidence(
    ctx: ReviewContext,
) -> None:
    """Align equipment state machine with text/report stages when routed evidence could not be read."""
    unreadable_statuses = {
        "denied",
        "unavailable",
        "too_large",
        "invalid",
        "no_visible_text",
        "outside_root",
        "transport_too_large",
        "budget_exhausted",
        "not_checked",
        "invalid_image",
    }
    external_enabled = bool(
        ctx.evidence_config and ctx.evidence_config.external_evidence_review_enabled
    )
    steps_by_number = {
        int(step["review_step"]): step for step in ctx.content.get("steps", [])
    }
    for check in ctx.equipment_checks:
        if check.get("code") != "equipment_missing" or check.get("status") != "fail":
            continue
        review_step = int(check["review_step"])
        step = steps_by_number.get(review_step, {})
        routed = [
            decision
            for decision in step.get("reference_decisions", [])
            if decision.get("requires_check")
            and decision.get("role") in {"result_evidence", "result_evidence_location"}
        ]
        if not routed:
            continue
        step_html = ctx.evidence.html_results.get(review_step, {})
        step_imgs = ctx.evidence.results.get(review_step, {})
        has_unreadable_evidence = (
            not external_enabled
            or any(res.status in unreadable_statuses for res in step_html.values())
            or any(res.status in unreadable_statuses for res in step_imgs.values())
        )
        if has_unreadable_evidence:
            check.update(
                status="manual",
                code="equipment_evidence_unavailable",
                summary=(
                    "该步骤将结果委托给外部证据，但外部证据当前无法读取或未启用，"
                    "无法确认其中是否记录了受控设备标识符，需人工复核。"
                ),
            )


def run_equipment_review_stage(ctx: ReviewContext) -> dict[str, Any]:
    if ctx.equipment_enabled:
        questions = _enrich_questions_with_html_equipment(
            ctx.open_questions,
            ctx.evidence.html_reported_equipment,
            ctx.equipment_registry,
        )
        ctx.equipment_checks, skill_trace = run_equipment_pipeline(
            ctx.ai_config,
            ctx.content,
            ctx.equipment_checks,
            questions,
            ctx.equipment_registry,
        )
        apply_html_report_equipment(
            ctx.content,
            ctx.equipment_checks,
            ctx.evidence.html_reported_equipment,
            ctx.equipment_registry,
        )
        _downgrade_missing_equipment_for_unreadable_evidence(ctx)
        status = "completed" if ctx.plan.equipment_steps else "not_applicable"
    else:
        skill_trace = {
            "skill_id": "equipment-role",
            "status": "disabled",
            "ai_calls": 0,
            "reason": "当前工作区已禁用设备台账校验。",
        }
        status = "disabled"
    return {
        "status": status,
        "ai_calls": int(skill_trace.get("ai_calls", 0)) if ctx.equipment_enabled else 0,
        "steps": len(ctx.plan.equipment_steps),
        "ambiguous_steps": len(ctx.open_questions),
        "first_pass_resolved_steps": ctx.first_pass_resolved_steps,
        "skill": skill_trace,
    }


def _apply_equipment_guards(
    parsed: dict[str, Any],
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    checks_by_step = {item["review_step"]: item for item in checks}
    for step_result in parsed["step_results"]:
        check = checks_by_step.get(step_result["review_step"])
        if check is None:
            continue
        if step_result.get("applicability") == "not_applicable":
            check = {
                **check,
                "status": "not_applicable",
                "code": "not_applicable",
                "summary": "主评审判定该步骤不适用，因此跳过设备台账校验。",
            }
        step_result["equipment"] = check
        for warning in check.get("warnings", []):
            if warning not in step_result["warnings"]:
                step_result["warnings"].append(warning)
        if check["status"] in {"fail", "manual"}:
            _append_step_issue(
                parsed,
                step_result["review_step"],
                check["status"],
                "equipment",
                check["summary"],
            )
    return _recalculate_result(parsed)


def _apply_disabled_equipment_guards(parsed: dict[str, Any]) -> dict[str, Any]:
    for step_result in parsed["step_results"]:
        step_result["equipment"] = {
            "review_step": step_result["review_step"],
            "status": "not_applicable",
            "code": "disabled_by_configuration",
            "summary": "当前工作区已禁用设备台账校验。",
            "matches": [],
            "warnings": [],
        }
    result = _recalculate_result(parsed)
    result["criteria"]["equipment_traceability"] = _criterion(
        "not_applicable",
        "当前工作区已禁用设备台账校验。",
    )
    return result


def apply_equipment_verdicts(ctx: ReviewContext, parsed: dict[str, Any]) -> dict[str, Any]:
    return (
        _apply_equipment_guards(parsed, ctx.equipment_checks)
        if ctx.equipment_enabled
        else _apply_disabled_equipment_guards(parsed)
    )
