"""Second-pass equipment resolution: everything between the registry analysis and
the final equipment verdict.

The first text pass carries `extracted_equipment` as a side channel for this
feature; `absorb_first_pass` is its only consumer.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.models import AiConfig, EquipmentRegistry
from app.services.ai_transport import ai_headers, completion_url, skill_failure
from app.services.equipment_review import (
    OpenQuestion,
    apply_equipment_disambiguation,
    apply_extracted_equipment,
    equipment_reference,
    merge_pending_names,
    registry_equipment_names,
    requirement_previous_equipment_ids,
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
