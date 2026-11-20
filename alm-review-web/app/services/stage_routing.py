"""Stage 1 (`routing`): candidate detection, `evidence-routing` Skill execution,
reference role validation/fallback, and downstream evidence routing.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx

from app.models import AiConfig, EquipmentRegistry
from app.services.ai_transport import ai_headers, completion_url, skill_failure
from app.services.equipment_review import OpenQuestion
from app.services.review_types import (
    TEXT_BATCH_CHAR_BUDGET,
    TEXT_BATCH_MAX_STEPS,
    ReviewContext,
    _clip_step_text,
)
from app.services.skill_runner import skill_runner

_RESULT_EVIDENCE_ROLES = {"result_evidence", "result_evidence_location"}


def _equipment_source_field(
    step: dict[str, Any],
    equipment: dict[str, Any],
) -> str | None:
    identifiers = [
        str(equipment.get(key) or "").strip().casefold()
        for key in ("equipment_id", "description", "model_number", "serial_number")
    ]
    identifiers = [value for value in identifiers if len(value) >= 4]
    for field_name in ("actual", "description", "expected"):
        text = str(step.get(field_name) or "").casefold()
        if any(value in text for value in identifiers):
            return field_name
    return None


def _attach_reference_candidates(
    content: dict[str, Any],
    equipment_checks: list[dict[str, Any]],
    open_questions: list[OpenQuestion],
) -> None:
    checks_by_step = {
        int(check["review_step"]): check for check in equipment_checks
    }
    questions_by_step = {
        question.review_step: question for question in open_questions
    }
    for step in content.get("steps", []):
        review_step = int(step["review_step"])
        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def add_candidate(
            candidate_type: str,
            value: str,
            source_field: str,
            detection_source: str,
        ) -> None:
            normalized_value = value.strip()
            key = (candidate_type, normalized_value.casefold())
            if not normalized_value or key in seen:
                return
            seen.add(key)
            candidates.append(
                {
                    "candidate_id": "",
                    "type": candidate_type,
                    "value": normalized_value,
                    "source_field": source_field,
                    "detection_source": detection_source,
                }
            )

        for path in step.get("evidence_profile", {}).get("actual_paths", []):
            path_type = {
                "image": "image",
                "html": "html_report",
                "file": "file",
                "folder_or_unknown": "folder_or_unknown",
            }.get(path.get("kind"), "file")
            add_candidate(
                path_type,
                str(path.get("raw") or ""),
                "actual",
                (
                    "path_without_extension"
                    if path_type == "folder_or_unknown"
                    else "path_extension"
                ),
            )
        if step.get("attachment_declared"):
            attachments = step.get("attachment_contents", [])
            for value in (
                [str(item.get("name") or "ALM attachment") for item in attachments]
                or ["ALM attachment (content unavailable)"]
            ):
                add_candidate(
                    "image" if attachments else "file",
                    value,
                    "attachment",
                    "alm_attachment",
                )

        check = checks_by_step.get(review_step, {})
        question = questions_by_step.get(review_step)
        equipment_rows = [
            *check.get("matches", []),
            *(
                [candidate.as_dict() for candidate in question.candidates]
                if question
                else []
            ),
        ]
        for equipment in sorted(
            equipment_rows,
            key=lambda item: str(item.get("equipment_id") or "").casefold(),
        ):
            source_field = _equipment_source_field(step, equipment)
            if source_field is None:
                continue
            add_candidate(
                "equipment",
                str(
                    equipment.get("registry_reference")
                    or equipment.get("equipment_id")
                    or ""
                ),
                source_field,
                "equipment_registry_match",
            )
        for identifier in [
            *check.get("reported_identifiers", []),
            *check.get("unknown_identifiers", []),
        ]:
            add_candidate(
                "equipment",
                str(identifier),
                "actual",
                "equipment_identifier",
            )

        for index, candidate in enumerate(candidates, start=1):
            candidate["candidate_id"] = f"step-{review_step}-ref-{index}"
        step["reference_candidates"] = candidates


def _validate_reference_decision(
    candidate: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    allowed_roles = {
        "equipment": {"test_equipment", "dut_or_other", "unrelated", "uncertain"},
        "image": {"result_evidence", "reference_document", "unrelated", "uncertain"},
        "html_report": {
            "result_evidence",
            "reference_document",
            "unrelated",
            "uncertain",
        },
        "file": {"result_evidence", "reference_document", "unrelated", "uncertain"},
        "folder_or_unknown": {
            "result_evidence",
            "result_evidence_location",
            "reference_document",
            "unrelated",
            "uncertain",
        },
    }
    role = decision["role"]
    if role not in allowed_roles[candidate["type"]]:
        raise ValueError(
            f"Reference {candidate['candidate_id']} of type {candidate['type']} "
            f"cannot use role {role}."
        )
    expected_check = role in {
        "test_equipment",
        "result_evidence",
        "result_evidence_location",
        "uncertain",
    }
    if bool(decision["requires_check"]) != expected_check:
        raise ValueError(
            f"Reference {candidate['candidate_id']} with role {role} must set "
            f"requires_check={str(expected_check).lower()}."
        )


def _validate_routing_references(
    validated_input: dict[str, Any],
    validated_output: dict[str, Any],
) -> None:
    decisions_by_step = {
        int(item["review_step"]): item
        for item in validated_output["decisions"]
    }
    errors: list[str] = []
    for step in validated_input["steps"]:
        review_step = int(step["review_step"])
        candidates = step.get("reference_candidates", [])
        candidate_ids = [item["candidate_id"] for item in candidates]
        decisions = decisions_by_step[review_step]["reference_decisions"]
        decision_ids = [item["candidate_id"] for item in decisions]
        duplicate_candidates = sorted(
            identifier
            for identifier in set(candidate_ids)
            if candidate_ids.count(identifier) > 1
        )
        duplicate_decisions = sorted(
            identifier
            for identifier in set(decision_ids)
            if decision_ids.count(identifier) > 1
        )
        missing = sorted(set(candidate_ids) - set(decision_ids))
        extra = sorted(set(decision_ids) - set(candidate_ids))
        if duplicate_candidates:
            errors.append(
                f"Step {review_step} supplied duplicate reference candidate(s): "
                + ", ".join(duplicate_candidates)
            )
        if duplicate_decisions:
            errors.append(
                f"Step {review_step} duplicated reference decision(s): "
                + ", ".join(duplicate_decisions)
            )
        if missing:
            errors.append(
                f"Step {review_step} omitted reference decision(s): "
                + ", ".join(missing)
            )
        if extra:
            errors.append(
                f"Step {review_step} invented reference decision(s): "
                + ", ".join(extra)
            )
        candidates_by_id = {
            item["candidate_id"]: item for item in candidates
        }
        for decision in decisions:
            candidate = candidates_by_id.get(decision["candidate_id"])
            if candidate is None:
                continue
            try:
                _validate_reference_decision(candidate, decision)
            except ValueError as exc:
                errors.append(f"Step {review_step}: {exc}")
    if errors:
        raise ValueError(" ".join(errors))


def _fallback_missing_routing_references(
    validated_input: dict[str, Any],
    validated_output: dict[str, Any],
    _error: str,
) -> dict[str, Any]:
    decisions_by_step = {
        int(item["review_step"]): {**item}
        for item in validated_output["decisions"]
    }
    fallback_used = False
    for step in validated_input["steps"]:
        review_step = int(step["review_step"])
        candidates = step.get("reference_candidates", [])
        candidate_ids = [item["candidate_id"] for item in candidates]
        decisions = [
            {**item}
            for item in decisions_by_step[review_step]["reference_decisions"]
        ]
        decision_ids = [item["candidate_id"] for item in decisions]
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError(
                f"Step {review_step} returned duplicate reference decisions."
            )
        extra = sorted(set(decision_ids) - set(candidate_ids))
        if extra:
            raise ValueError(
                f"Step {review_step} invented reference decision(s): "
                + ", ".join(extra)
            )
        candidates_by_id = {
            item["candidate_id"]: item for item in candidates
        }
        for decision in decisions:
            _validate_reference_decision(
                candidates_by_id[decision["candidate_id"]],
                decision,
            )
        for candidate_id in candidate_ids:
            if candidate_id in decision_ids:
                continue
            fallback_used = True
            decisions.append(
                {
                    "candidate_id": candidate_id,
                    "role": "uncertain",
                    "requires_check": True,
                    "reason": "模型修复后仍遗漏了该引用，需要人工复核。",
                }
            )
        decisions_by_step[review_step] = {
            **decisions_by_step[review_step],
            "reference_decisions": decisions,
        }
    if not fallback_used:
        raise ValueError(
            "The evidence routing output is invalid and cannot be safely completed."
        )
    return {
        **validated_output,
        "decisions": [
            decisions_by_step[int(item["review_step"])]
            for item in validated_output["decisions"]
        ],
    }


def _routes_result_evidence(step: dict[str, Any]) -> bool:
    return bool(step.get("attachment_contents")) or any(
        decision["requires_check"]
        and decision["role"] in _RESULT_EVIDENCE_ROLES
        for decision in step.get("reference_decisions", [])
    )


def routed_evidence_references(step: dict[str, Any]) -> list[str]:
    candidates = {
        candidate["candidate_id"]: candidate
        for candidate in step.get("reference_candidates", [])
    }
    references = [
        str(item.get("name") or "ALM attachment")
        for item in step.get("attachment_contents", [])
    ]
    for decision in step.get("reference_decisions", []):
        if (
            decision["requires_check"]
            and decision["role"] in _RESULT_EVIDENCE_ROLES
            and (candidate := candidates.get(decision["candidate_id"])) is not None
        ):
            references.append(str(candidate["value"]))
    return list(dict.fromkeys(references))


def _apply_reference_routing(
    content: dict[str, Any],
    *,
    decision_source: str = "evidence-routing",
) -> None:
    for step in content.get("steps", []):
        profile = step.setdefault("evidence_profile", {})
        profile.setdefault("actual_paths", [])
        candidates = {
            candidate["candidate_id"]: candidate
            for candidate in step.get("reference_candidates", [])
        }
        attachment_contents = step.get("attachment_contents", [])
        actions = {"review_attachment"} if step.get("attachment_declared") else set()
        routed_types: set[str] = set()
        reasons: list[str] = []
        if attachment_contents:
            actions.update(("load_images", "send_to_visual_ai"))
            routed_types.add("image_evidence")
            reasons.append("ALM 步骤附件已强制转交图像证据评审。")
        paths_by_value = {
            str(path.get("raw") or "").casefold(): path
            for path in profile.get("actual_paths", [])
        }
        for path in paths_by_value.values():
            path["route_requested"] = False
        if step.get("text_applicability") == "not_applicable":
            profile["screenshot_review_required"] = False
            profile["path_validation_required"] = False
            profile["routing"] = {
                "intent": "none",
                "triggers": profile.get("routing", {}).get("triggers", []),
                "actions": [],
                "decision_source": decision_source,
                "confidence": None,
                "reason": "该步骤不适用，因此未执行外部证据检查。",
                "manual_required": False,
            }
            continue
        for decision in step.get("reference_decisions", []):
            candidate = candidates.get(decision["candidate_id"])
            if candidate is None or candidate["source_field"] == "attachment":
                continue
            if candidate["type"] == "equipment" or not decision["requires_check"]:
                continue
            path = paths_by_value.get(candidate["value"].casefold())
            if path is not None:
                path["route_requested"] = True
            reasons.append(decision["reason"])
            if decision["role"] == "uncertain":
                actions.add("manual_review")
                continue
            if candidate["type"] == "html_report":
                actions.update(("validate_path", "parse_html_report"))
                routed_types.add("html_report")
            elif candidate["type"] in {"image", "folder_or_unknown"}:
                actions.update(("validate_path", "load_images", "send_to_visual_ai"))
                routed_types.add("image_evidence")
            elif candidate["type"] == "file":
                actions.update(("validate_path", "manual_review"))
                routed_types.add("file_evidence")

        triggers = profile.get("routing", {}).get("triggers", [])
        profile["screenshot_review_required"] = bool(
            step.get("attachment_declared")
            or "image_evidence" in routed_types
        )
        profile["path_validation_required"] = "validate_path" in actions
        if "manual_review" in actions:
            intent = "uncertain"
        elif len(routed_types) > 1:
            intent = "mixed_evidence"
        elif routed_types:
            intent = next(iter(routed_types))
        elif step.get("attachment_declared"):
            intent = "image_evidence"
        else:
            intent = "none"
        source = (
            decision_source
            if step.get("reference_decisions")
            else "deterministic"
        )
        profile["routing"] = {
            "intent": intent,
            "triggers": triggers,
            "actions": sorted(actions),
            "decision_source": source,
            "confidence": None,
            "reason": "；".join(dict.fromkeys(reasons)) or (
                "未请求外部专项检查。"
            ),
            "manual_required": "manual_review" in actions,
        }


def _routing_batches(
    payloads: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = 0
    for payload in payloads:
        size = len(json.dumps(payload, ensure_ascii=False))
        if current and (
            current_size + size > TEXT_BATCH_CHAR_BUDGET
            or len(current) >= TEXT_BATCH_MAX_STEPS
        ):
            batches.append(current)
            current = []
            current_size = 0
        current.append(payload)
        current_size += size
    if current:
        batches.append(current)
    return batches


def _run_evidence_routing_skill(
    ai_config: AiConfig,
    content: dict[str, Any],
    *,
    equipment_enabled: bool = False,
    equipment_registry: Sequence[EquipmentRegistry] = (),
) -> list[dict[str, Any]]:
    extract_unlabelled_equipment = bool(equipment_enabled and equipment_registry)
    all_steps = content.get("steps", [])
    payloads: list[dict[str, Any]] = []
    for step in all_steps:
        candidates = step.get("reference_candidates", [])
        if not candidates and not extract_unlabelled_equipment:
            step.setdefault("reference_decisions", [])
            step.setdefault("extracted_equipment", [])
            continue
        payloads.append(
            {
                "review_step": int(step["review_step"]),
                "description": _clip_step_text(step.get("description"))[0],
                "expected": _clip_step_text(step.get("expected"))[0],
                "actual": _clip_step_text(step.get("actual"))[0],
                "reference_candidates": candidates,
            }
        )

    traces: list[dict[str, Any]] = []
    decisions_by_step: dict[int, dict[str, Any]] = {}
    for batch in _routing_batches(payloads):
        trace = skill_runner.run(
            "evidence-routing",
            {"steps": batch},
            endpoint=completion_url(ai_config.base_url),
            model_name=ai_config.model_name,
            headers=ai_headers(ai_config),
            timeout_seconds=ai_config.timeout_seconds,
            granted_capabilities={
                "review.step_text",
                "evidence.path_metadata",
                "equipment.registry.candidates",
            },
            request_post=httpx.post,
            output_validator=_validate_routing_references,
            output_fallback=_fallback_missing_routing_references,
        )
        traces.append(trace)
        if trace.get("status") != "completed":
            raise skill_failure(trace, "evidence-routing Skill failed.")
        decisions_by_step.update(
            {
                int(item["review_step"]): item
                for item in trace["output"]["decisions"]
            }
        )

    for step in all_steps:
        review_step = int(step["review_step"])
        decision = decisions_by_step.get(review_step)
        if decision is not None:
            step["reference_decisions"] = decision["reference_decisions"]
            step["extracted_equipment"] = decision.get("extracted_equipment", [])

    _apply_reference_routing(content)
    return traces


def _routing_step_summaries(content: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "review_step": step["review_step"],
            "paths": step["evidence_profile"]["actual_paths"],
            **step["evidence_profile"].get("routing", {}),
        }
        for step in content.get("steps", [])
    ]


def run_routing_stage(ctx: ReviewContext) -> dict[str, Any]:
    traces = _run_evidence_routing_skill(
        ctx.ai_config,
        ctx.content,
        equipment_enabled=ctx.equipment_enabled,
        equipment_registry=ctx.equipment_registry,
    )
    ctx.content["routing_skill_traces"] = traces
    if traces:
        raw_outputs = json.loads(ctx.raw_response) if ctx.raw_response else {}
        raw_outputs["evidence-routing"] = [trace["output"] for trace in traces]
        ctx.raw_response = json.dumps(
            raw_outputs,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return {
        "status": "completed",
        "ai_calls": sum(int(trace.get("ai_calls", 0)) for trace in traces),
        "skills": traces,
        "steps": _routing_step_summaries(ctx.content),
    }


def finalize_routing_after_applicability(
    ctx: ReviewContext,
    routing_trace: dict[str, Any],
) -> None:
    """Clear external specialist routes on Steps that `text_review` marked N/A."""
    if any(
        step.get("text_applicability") == "not_applicable"
        for step in ctx.content.get("steps", [])
    ):
        _apply_reference_routing(ctx.content)
        routing_trace["steps"] = _routing_step_summaries(ctx.content)
