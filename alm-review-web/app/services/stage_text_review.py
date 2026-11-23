"""Stage 2 (`text_review`): Step applicability, Expected-vs-Actual, and language review."""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.hashing import normalize_text
from app.models import AiConfig
from app.services.ai_transport import ai_headers, completion_url, skill_failure
from app.services.review_types import (
    TEXT_BATCH_CHAR_BUDGET,
    TEXT_BATCH_MAX_STEPS,
    ReviewContext,
    _clip_step_text,
    _recalculate_result,
)
from app.services.skill_runner import SkillFailure, skill_runner
from app.services.stage_routing import (
    _routes_result_evidence,
    routed_evidence_references,
)

# Whether the cited evidence really answers Expected is decided by the later
# path/image/HTML stages, so the text pass must not pre-empt them.
_EVIDENCE_SUPERSEDED_CODES = {"actual_insufficient", "evidence_reference_missing"}
_ADS_REFERENCE_RE = re.compile(r"\bADS\s*[:：#-]\s*[A-Za-z0-9]+\b", re.IGNORECASE)
_PD_REFERENCE_RE = re.compile(
    r"\bPD(?:\s*(?:No\.?|Number))?\s*[:：#-]\s*[A-Za-z0-9.-]*\d[A-Za-z0-9.-]*\b",
    re.IGNORECASE,
)
_DEFECT_TICKET_RE = re.compile(
    r"(?:\b(?:PD(?:\s*(?:No\.?|Number))?|ADS|CQ|BUG|DEF)\s*[:：#-]\s*[A-Za-z0-9.-]*\d[A-Za-z0-9.-]*"
    r"|\bCTD\d{5,})\b",
    re.IGNORECASE,
)


def _text_skill_steps(content: dict[str, Any]) -> list[dict[str, Any]]:
    requested = set(content.get("review_plan", {}).get("text_steps", []))
    return [
        step for step in content.get("steps", [])
        if int(step["review_step"]) in requested
    ]


def _text_skill_batches(
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


def _suppressed_finding(finding: dict[str, Any], cause: str) -> dict[str, str]:
    return {
        "code": finding["code"],
        "severity": finding["severity"],
        "summary": finding["reason"][:200],
        "cause": cause,
    }


def _validate_text_skill_output(
    validated_input: dict[str, Any],
    validated_output: dict[str, Any],
) -> None:
    """Reject Passed-step assessments whose pre-check slots found defects with no findings."""
    steps_by_id = {
        int(step["review_step"]): step
        for step in validated_input.get("steps", [])
    }
    for assessment in validated_output.get("assessments", []):
        review_step = int(assessment["review_step"])
        step = steps_by_id.get(review_step, {})
        if (
            assessment.get("applicability") != "applicable"
            or str(step.get("alm_step_status") or "Passed").casefold() != "passed"
        ):
            continue
        findings = assessment.get("findings") or []
        has_substantive_finding = any(
            item.get("severity") in {"fail", "manual"} for item in findings
        )
        for bound_check in assessment.get("bound_checks") or []:
            if bound_check.get("within_bound") is False and not has_substantive_finding:
                raise ValueError(
                    f"Step {review_step} recorded within_bound=false for "
                    f"{bound_check.get('metric')!r} on a Passed Step without a "
                    "fail or manual finding."
                )
        for conclusion_check in assessment.get("conclusion_checks") or []:
            if (
                conclusion_check.get("matches_recorded_data") is False
                and not has_substantive_finding
            ):
                raise ValueError(
                    f"Step {review_step} recorded matches_recorded_data=false for "
                    f"{conclusion_check.get('claim_excerpt')!r} on a Passed Step "
                    "without a fail or manual finding."
                )
            if (
                conclusion_check.get("matches_expected_subject_and_unit") is False
                and not has_substantive_finding
            ):
                raise ValueError(
                    f"Step {review_step} recorded "
                    "matches_expected_subject_and_unit=false for "
                    f"{conclusion_check.get('claim_excerpt')!r} on a Passed Step "
                    "without a fail or manual finding."
                )


def _fallback_text_skill_output(
    validated_input: dict[str, Any],
    validated_output: dict[str, Any],
    _detail: str,
) -> dict[str, Any]:
    """Synthesize missing findings from structured pre-check slots if repair still omitted them."""
    steps_by_id = {
        int(step["review_step"]): step
        for step in validated_input.get("steps", [])
    }
    recovered: list[dict[str, Any]] = []
    for raw_assessment in validated_output.get("assessments", []):
        assessment = dict(raw_assessment)
        review_step = int(assessment["review_step"])
        step = steps_by_id.get(review_step, {})
        findings = [dict(item) for item in assessment.get("findings") or []]
        if (
            assessment.get("applicability") == "applicable"
            and str(step.get("alm_step_status") or "Passed").casefold() == "passed"
        ):
            for bound_check in assessment.get("bound_checks") or []:
                if bound_check.get("within_bound") is False and not any(
                    item.get("severity") in {"fail", "manual"} for item in findings
                ):
                    findings.append(
                        {
                            "code": "expected_actual_mismatch",
                            "severity": "fail",
                            "basis": "direct_step_text",
                            "reason": (
                                f"Actual 记录的 {bound_check.get('metric')} 数值 "
                                f"({bound_check.get('actual')}) 未满足本步限值要求 "
                                f"({bound_check.get('bound')})。"
                            )[:1000],
                        }
                    )
            for conclusion_check in assessment.get("conclusion_checks") or []:
                excerpt = str(conclusion_check.get("claim_excerpt") or "")[:120]
                if (
                    conclusion_check.get("matches_recorded_data") is False
                    and not any(
                        item.get("severity") in {"fail", "manual"}
                        for item in findings
                    )
                ):
                    findings.append(
                        {
                            "code": "expected_actual_mismatch",
                            "severity": "fail",
                            "basis": "direct_step_text",
                            "reason": (
                                f"Actual 中的结论表述 '{excerpt}' 与本步记录的数据结果矛盾。"
                            )[:1000],
                        }
                    )
                elif (
                    conclusion_check.get("matches_expected_subject_and_unit") is False
                    and not any(
                        item.get("severity") in {"fail", "manual"}
                        for item in findings
                    )
                ):
                    findings.append(
                        {
                            "code": "expected_actual_mismatch",
                            "severity": "fail",
                            "basis": "direct_step_text",
                            "reason": (
                                f"Actual 中的结论语句 '{excerpt}' 所称物理量或单位与本步要求不符。"
                            )[:1000],
                        }
                    )
        assessment["findings"] = findings
        recovered.append(assessment)
    return {**validated_output, "assessments": recovered}


def _run_text_semantic_skills(
    ai_config: AiConfig,
    content: dict[str, Any],
    project: str = "",
    workspace_name: str = "",
    location_assessment: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    steps = _text_skill_steps(content)
    truncated_steps: set[int] = set()
    payloads: list[dict[str, Any]] = []
    for step in steps:
        description, cut_description = _clip_step_text(step.get("description"))
        expected, cut_expected = _clip_step_text(step.get("expected"))
        actual, cut_actual = _clip_step_text(step.get("actual"))
        if cut_description or cut_expected or cut_actual:
            truncated_steps.add(int(step["review_step"]))
        payloads.append(
            {
                "review_step": int(step["review_step"]),
                "alm_step_status": str(step.get("status") or "Passed"),
                "description": description,
                "expected": expected,
                "actual": actual,
                "actual_format": {
                    "signals": list(
                        (step.get("actual_format") or {}).get("signals") or []
                    )
                },
                "numbered_comparison": step.get("numbered_comparison", []),
                "routed_evidence_references": routed_evidence_references(step),
            }
        )

    traces: list[dict[str, Any]] = []
    assessments: dict[int, dict[str, Any]] = {}
    execution_config = (
        location_assessment.get("selected_config")
        if location_assessment and location_assessment.get("status") == "ready"
        else None
    )
    for batch in _text_skill_batches(payloads):
        trace = skill_runner.run(
            "alm-text-review",
            {
                "project": project,
                "alm_run_status": str(content.get("run_status") or "Passed"),
                "execution_location_config": execution_config,
                "steps": batch,
            },
            endpoint=completion_url(ai_config.base_url),
            model_name=ai_config.model_name,
            headers=ai_headers(ai_config),
            timeout_seconds=ai_config.timeout_seconds,
            granted_capabilities={
                "review.step_text",
                "review.text_format",
                "review.numbered_comparison",
                "evidence.path_metadata",
                "location.configuration.metadata",
            },
            request_post=httpx.post,
            output_validator=_validate_text_skill_output,
            output_fallback=_fallback_text_skill_output,
        )
        traces.append(trace)
        if trace.get("status") != "completed":
            raise skill_failure(trace, "alm-text-review Skill failed.")
        assessments.update(
            {
                int(item["review_step"]): item
                for item in trace["output"]["assessments"]
            }
        )

    for step in steps:
        review_step = int(step["review_step"])
        step["text_applicability"] = assessments[review_step]["applicability"]

    step_results = {
        int(step["review_step"]): {
            "review_step": int(step["review_step"]),
            "applicability": assessments[int(step["review_step"])]["applicability"],
            "status": "pass",
            "summary": "",
            "issues": [],
            "warnings": [],
        }
        for step in steps
    }
    steps_by_number = {int(step["review_step"]): step for step in steps}
    for review_step, assessment in assessments.items():
        target = step_results[review_step]
        if assessment["applicability"] == "manual":
            target["issues"].append(
                {
                    "status": "manual",
                    "type": "expected_actual",
                    "summary": assessment["summary"][:200],
                }
            )
        step_source = steps_by_number[review_step]
        is_failed_step_with_ticket = (
            str(step_source.get("status") or "").casefold() == "failed"
            and bool(_DEFECT_TICKET_RE.search(normalize_text(step_source.get("actual"))))
        )
        evidence_routed = _routes_result_evidence(step_source)
        suppressed: list[dict[str, str]] = []
        for finding in assessment["findings"]:
            code = finding["code"]
            severity = finding["severity"]
            if code == "record_documentation_gap":
                target["issues"].append(
                    {
                        "status": "manual",
                        "type": "expected_actual",
                        "summary": finding["reason"][:200],
                    }
                )
                continue
            if assessment["applicability"] == "not_applicable":
                suppressed.append(
                    _suppressed_finding(finding, "step_not_applicable")
                )
                continue
            if code == "actual_insufficient" and is_failed_step_with_ticket:
                suppressed.append(
                    _suppressed_finding(finding, "failed_step_defect_ticket_recorded")
                )
                continue
            if code in _EVIDENCE_SUPERSEDED_CODES and evidence_routed:
                suppressed.append(
                    _suppressed_finding(finding, "result_evidence_routed")
                )
                continue
            if (
                code == "expected_actual_mismatch"
                and finding["basis"] == "reference_metadata_inference"
                and evidence_routed
            ):
                suppressed.append(
                    _suppressed_finding(finding, "reference_metadata_deferred")
                )
                continue
            if severity == "warning" and code != "language_quality":
                raise SkillFailure(
                    "ALM text review Skill returned a non-language warning.",
                    retryable=False,
                )
            if severity == "warning":
                warning = {
                    "type": "minor_language",
                    "summary": finding["reason"][:200],
                }
                if warning not in target["warnings"]:
                    target["warnings"].append(warning)
                continue
            issue_type = (
                "language" if code == "language_quality" else "expected_actual"
            )
            if (
                review_step in truncated_steps
                and severity == "fail"
                and code in {"actual_missing", "actual_insufficient"}
            ):
                severity = "manual"
            target["issues"].append(
                {
                    "status": severity,
                    "type": issue_type,
                    "summary": finding["reason"][:200],
                }
            )
        if suppressed:
            target["suppressed_findings"] = suppressed
    if workspace_name.casefold() == "earthformal02":
        for step in steps:
            review_step = int(step["review_step"])
            if (
                str(step.get("status") or "").casefold() != "failed"
                or step_results[review_step]["applicability"] == "not_applicable"
            ):
                continue
            actual = normalize_text(step.get("actual"))
            if _ADS_REFERENCE_RE.search(actual) and not _PD_REFERENCE_RE.search(actual):
                step_results[review_step]["issues"].append(
                    {
                        "status": "manual",
                        "type": "expected_actual",
                        "summary": (
                            "失败记录引用了 ADS 但未注明对应 PD，"
                            "请核对偏差追踪及正式记录用语。"
                        ),
                    }
                )
    for review_step in sorted(truncated_steps):
        step_results[review_step]["issues"].append(
            {
                "status": "manual",
                "type": "expected_actual",
                "summary": (
                    "步骤文本超过 AI 输入限制并已截断，请人工复核完整内容。"
                ),
            }
        )

    parsed = _recalculate_result(
        {
            "model_summary": "; ".join(
                assessments[int(step["review_step"])]["summary"]
                for step in steps
            ),
            "step_results": list(step_results.values()),
            "warnings": [],
        }
    )
    return parsed, traces


def run_text_review_stage(ctx: ReviewContext) -> dict[str, Any]:
    ctx.text_result, traces = _run_text_semantic_skills(
        ctx.ai_config,
        ctx.content,
        ctx.project,
        ctx.workspace_name,
        ctx.location_assessment,
    )
    ctx.content["text_skill_traces"] = traces
    raw_outputs = json.loads(ctx.raw_response) if ctx.raw_response else {}
    raw_outputs["alm-text-review"] = (
        traces[0]["output"]
        if len(traces) == 1
        else [trace["output"] for trace in traces]
    )
    ctx.raw_response = json.dumps(
        raw_outputs,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "status": "completed",
        "ai_calls": sum(int(trace.get("ai_calls", 0)) for trace in traces),
        "steps": len(ctx.content["review_plan"]["text_steps"]),
        "skills": traces,
    }
