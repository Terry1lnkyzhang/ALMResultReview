"""Stage 5 (`report_review`): HTML report resolution, BM25 selection, batching, and citation verification."""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import PureWindowsPath
from typing import Any

import httpx

from app.models import EvidenceConfig
from app.services.ai_transport import ai_headers, completion_url, skill_failure
from app.services.equipment_pipeline import collect_html_reported_equipment
from app.services.html_evidence import (
    HtmlEvidenceResolver,
    HtmlEvidenceResult,
    select_relevant_blocks,
)
from app.services.review_types import (
    HTML_SKILL_CHAR_BUDGET,
    ReportReviewRequest,
    ReviewContext,
    ReviewPlan,
    _clip_step_text,
)
from app.services.skill_runner import SkillFailure, skill_runner


def _run_report_pipeline(
    plan: ReviewPlan,
    evidence_config: EvidenceConfig | None,
) -> dict[int, dict[str, HtmlEvidenceResult]]:
    if not evidence_config or not evidence_config.external_evidence_review_enabled:
        return {}
    resolver = HtmlEvidenceResolver()
    cache: dict[str, HtmlEvidenceResult] = {}
    resolved_by_step: dict[int, dict[str, HtmlEvidenceResult]] = {}
    for request in plan.report_requests:
        step_map: dict[str, HtmlEvidenceResult] = {}
        for path in request.paths:
            key = path.strip().casefold()
            if key not in cache:
                cache[key] = resolver.resolve(
                    path,
                    evidence_config.allowed_network_root,
                    evidence_config.local_html_fallback_root,
                )
            step_map[path] = cache[key]
        resolved_by_step[request.review_step] = step_map
    return resolved_by_step


def _html_skill_input(
    ctx: ReviewContext,
    request: ReportReviewRequest,
) -> dict[str, Any] | None:
    step = next(
        item
        for item in ctx.content.get("steps", [])
        if int(item["review_step"]) == request.review_step
    )
    step_results = ctx.evidence.html_results.get(request.review_step, {})
    ready = [
        (index, path, step_results.get(path))
        for index, path in enumerate(request.paths, start=1)
        if step_results.get(path) is not None
        and step_results[path].status == "ready"
        and step_results[path].blocks
    ]
    if not ready:
        return None
    reports = []
    for index, path, result in ready:
        assert result is not None
        reports.append(
            {
                "report_id": f"step-{request.review_step}-report-{index}",
                "source_path": path,
                "filename": PureWindowsPath(path).name,
                "sha256": result.sha256,
                "content_truncated": False,
                "blocks": [
                    {"block_id": block.block_id, "text": block.text}
                    for block in result.blocks
                ],
            }
        )
    return {
        "alm_run_status": str(ctx.content.get("run_status") or "Passed"),
        "review_mode": "final",
        "batch_index": 1,
        "batch_count": 1,
        "steps": [
            {
                "review_step": request.review_step,
                "alm_step_status": str(step.get("status") or "Passed"),
                "description": _clip_step_text(step.get("description"))[0],
                "expected": _clip_step_text(step.get("expected"))[0],
                "actual": _clip_step_text(step.get("actual"))[0],
                "automation_release": ctx.automation_release_assessments.get(
                    request.review_step,
                    {
                        "status": "disabled",
                        "project_name": "",
                        "testcase_id": str(ctx.content.get("test_id") or ""),
                        "candidate_count": 0,
                        "claimed_script_name": "",
                        "claimed_script_testcase_id": "",
                        "html_script_names": [],
                        "html_path_testcase_ids": [],
                        "html_path_testcase_match": "not_checked",
                        "actual_name_match": "not_checked",
                        "failure_code": "",
                        "claimed_document_number": "",
                        "claimed_document_revision": "",
                        "selected_release": None,
                        "script_name_match": "not_checked",
                        "document_match": "not_checked",
                        "reason": "未配置自动化发布记录校验。",
                    },
                ),
                "batch_observations": [],
                "reports": reports,
            }
        ],
    }


def _html_skill_batch_inputs(
    skill_input: dict[str, Any],
    *,
    char_budget: int = HTML_SKILL_CHAR_BUDGET,
) -> list[dict[str, Any]]:
    """Split complete report blocks into ordered, non-overlapping AI inputs."""
    step = skill_input["steps"][0]
    common_step = {
        key: value
        for key, value in step.items()
        if key not in {"batch_observations", "reports"}
    }
    report_batches: list[list[dict[str, Any]]] = []
    current_reports: list[dict[str, Any]] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current_reports, current_chars
        if current_reports:
            report_batches.append(current_reports)
            current_reports = []
            current_chars = 0

    for report in step["reports"]:
        report_base = {
            key: value for key, value in report.items() if key != "blocks"
        }
        for block in report["blocks"]:
            block_chars = len(block["text"])
            if current_reports and current_chars + block_chars > char_budget:
                flush()
            if (
                not current_reports
                or current_reports[-1]["report_id"] != report["report_id"]
            ):
                current_reports.append({**report_base, "blocks": []})
            current_reports[-1]["blocks"].append(block)
            current_chars += block_chars
            if block_chars >= char_budget:
                flush()
    flush()

    for reports in report_batches:
        for report in reports:
            report["content_truncated"] = False

    batch_count = len(report_batches)
    return [
        {
            "alm_run_status": str(skill_input.get("alm_run_status") or "Passed"),
            "review_mode": "evidence_batch",
            "batch_index": batch_index,
            "batch_count": batch_count,
            "steps": [
                {
                    **common_step,
                    "batch_observations": [],
                    "reports": reports,
                }
            ],
        }
        for batch_index, reports in enumerate(report_batches, start=1)
    ]


def _html_block_stats(reports: list[dict[str, Any]]) -> tuple[int, int]:
    return (
        sum(len(report["blocks"]) for report in reports),
        sum(len(block["text"]) for report in reports for block in report["blocks"]),
    )


def _select_html_blocks(
    skill_input: dict[str, Any],
    *,
    char_budget: int = HTML_SKILL_CHAR_BUDGET,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Send only Step-relevant report blocks once the full reports outgrow one call."""
    step = skill_input["steps"][0]
    total_blocks, total_chars = _html_block_stats(step["reports"])
    if total_chars <= char_budget:
        return skill_input, None
    reports = select_relevant_blocks(
        "\n".join(
            str(step.get(field_name) or "")
            for field_name in ("description", "expected", "actual")
        ),
        step["reports"],
        char_budget,
    )
    selected_blocks, selected_chars = _html_block_stats(reports)
    return (
        {**skill_input, "steps": [{**step, "reports": reports}]},
        {
            "total_blocks": total_blocks,
            "selected_blocks": selected_blocks,
            "total_chars": total_chars,
            "selected_chars": selected_chars,
        },
    )


def _guard_selected_html_fail(
    skill_input: dict[str, Any],
    assessment: dict[str, Any],
) -> dict[str, Any]:
    """A gap in pre-selected blocks does not prove the report lacks that content."""
    step = skill_input["steps"][0]
    coverages = [
        assessment[field_name]
        for field_name in (
            "description_coverage",
            "expected_coverage",
            "actual_coverage",
        )
    ]
    if (
        assessment["status"] != "fail"
        or not any(report.get("omitted_block_count") for report in step["reports"])
        or "not_found" not in coverages
        or "contradicted" in coverages
        or assessment["result_consistency"] == "inconsistent"
        or assessment["release_consistency"] == "mismatched"
        or step["automation_release"].get("failure_code")
    ):
        return assessment
    return {
        **assessment,
        "status": "manual",
        "reason": (
            "未找到的内容可能位于程序未选取的报告片段，改为人工复核。"
            + assessment["reason"]
        )[:1000],
    }


def _html_final_input(
    skill_input: dict[str, Any],
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    step = skill_input["steps"][0]
    return {
        "alm_run_status": str(skill_input.get("alm_run_status") or "Passed"),
        "review_mode": "final",
        "batch_index": max(1, len(observations)),
        "batch_count": max(1, len(observations)),
        "steps": [
            {
                **{
                    key: value
                    for key, value in step.items()
                    if key not in {"batch_observations", "reports"}
                },
                "batch_observations": observations,
                "reports": [
                    {
                        **{
                            key: value
                            for key, value in report.items()
                            if key != "blocks"
                        },
                        "content_truncated": False,
                        "omitted_block_count": 0,
                        "blocks": [],
                    }
                    for report in step["reports"]
                ],
            }
        ],
    }


_ELLIPSIS_SPLIT_RE = re.compile(r"(?:\.{3,}|…+)")


def _quote_line_matches(quote_line: str, block_line: str) -> tuple[bool, bool]:
    """Return `(matched, used_ellipsis)` for one normalized quote line vs block line."""
    if quote_line in block_line:
        return True, False
    if "..." not in quote_line and "…" not in quote_line:
        return False, False
    segments = [
        segment.strip()
        for segment in _ELLIPSIS_SPLIT_RE.split(quote_line)
        if segment.strip()
    ]
    if len(segments) < 2 or sum(len(segment) for segment in segments) < 10:
        return False, False
    cursor = 0
    for segment in segments:
        pos = block_line.find(segment, cursor)
        if pos < 0:
            return False, False
        cursor = pos + len(segment)
    return True, True


def _validate_html_citations(
    reports: list[dict[str, Any]],
    assessment: dict[str, Any],
) -> None:
    blocks = {
        (report["report_id"], block["block_id"]): block["text"]
        for report in reports
        for block in report["blocks"]
    }
    for citation in assessment["evidence"]:
        block_text = blocks.get((citation["report_id"], citation["block_id"]))
        raw_quote_lines = [
            line.strip()
            for line in citation["quote"].splitlines()
            if line.strip()
        ]
        quote_lines = [" ".join(line.split()).casefold() for line in raw_quote_lines]
        raw_block_lines = [
            line.strip() for line in (block_text or "").splitlines() if line.strip()
        ]
        block_lines = [
            " ".join(line.split()).casefold() for line in raw_block_lines
        ]
        next_index = 0
        unmatched_line = ""
        normalized_lines: list[str] = []
        replaced_ellipsis = False
        for raw_quote_line, quote_line in zip(
            raw_quote_lines, quote_lines, strict=True
        ):
            matching_index = None
            used_ellipsis = False
            for index in range(next_index, len(block_lines)):
                matched, ellipsis_hit = _quote_line_matches(
                    quote_line, block_lines[index]
                )
                if matched:
                    matching_index = index
                    used_ellipsis = ellipsis_hit
                    break
            if matching_index is None:
                next_index = -1
                unmatched_line = raw_quote_line
                break
            if used_ellipsis:
                replaced_ellipsis = True
                normalized_lines.append(raw_block_lines[matching_index][:400])
            else:
                normalized_lines.append(raw_quote_line)
            next_index = matching_index + 1
        if quote_lines and next_index >= 0 and replaced_ellipsis:
            citation["quote"] = "\n".join(normalized_lines)
        if quote_lines and next_index < 0:
            source_matches: list[tuple[int, str]] = []
            used_indexes: set[int] = set()
            for raw_quote_line, quote_line in zip(
                raw_quote_lines, quote_lines, strict=True
            ):
                matching_index = None
                used_ellipsis = False
                for index, block_line in enumerate(block_lines):
                    if index in used_indexes:
                        continue
                    matched, ellipsis_hit = _quote_line_matches(
                        quote_line, block_line
                    )
                    if matched:
                        matching_index = index
                        used_ellipsis = ellipsis_hit
                        break
                if matching_index is None:
                    break
                used_indexes.add(matching_index)
                source_matches.append(
                    (
                        matching_index,
                        raw_block_lines[matching_index][:400]
                        if used_ellipsis
                        else raw_quote_line,
                    )
                )
            if len(source_matches) == len(quote_lines):
                citation["quote"] = "\n".join(
                    quote_line for _, quote_line in sorted(source_matches)
                )
                continue
        if not quote_lines or next_index < 0:
            detail = (
                "empty quote"
                if not quote_lines
                else f"unmatched line {unmatched_line[:200]!r}"
            )
            raise SkillFailure(
                "HTML evidence Skill cited text outside the supplied report block: "
                f"report_id={citation['report_id']!r}, "
                f"block_id={citation['block_id']!r}, {detail} "
                f"(actual block text: {(block_text or '')[:240]!r}).",
                retryable=False,
            )


def _compact_html_quote(quote: str, max_chars: int = 400) -> str:
    if len(quote) <= max_chars:
        return quote
    lines = [line.strip() for line in quote.splitlines() if line.strip()]
    if not lines:
        return quote[:max_chars]
    max_lines = max(1, min(20, (max_chars + 1) // 2))
    if len(lines) > max_lines:
        lines = [*lines[: max_lines - 1], lines[-1]]
    text_budget = max_chars - len(lines) + 1
    base_chars, remainder = divmod(text_budget, len(lines))
    excerpts = []
    for index, line in enumerate(lines):
        line_budget = base_chars + (1 if index < remainder else 0)
        excerpts.append(line[:line_budget].rstrip())
    return "\n".join(excerpts)


def _reuse_verified_html_citations(
    observations: list[dict[str, Any]],
    assessment: dict[str, Any],
) -> dict[str, Any]:
    verified_citations: dict[tuple[str, str], dict[str, Any]] = {}
    for observation in observations:
        for citation in observation.get("evidence", []):
            key = (citation.get("report_id", ""), citation.get("block_id", ""))
            quote = citation.get("quote", "")
            if all(key) and quote:
                verified_citations.setdefault(
                    key,
                    {
                        "report_id": key[0],
                        "block_id": key[1],
                        "quote": _compact_html_quote(quote),
                        "supports": list(citation.get("supports", [])),
                    },
                )
    reused: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    unverified: list[tuple[str, str]] = []
    for citation in assessment.get("evidence", []):
        key = (citation.get("report_id", ""), citation.get("block_id", ""))
        if key not in verified_citations:
            unverified.append(key)
        elif key not in seen:
            reused.append(dict(verified_citations[key]))
            seen.add(key)
    if unverified:
        raise SkillFailure(
            "HTML final synthesis cited report/block IDs not verified in evidence "
            f"batches: {unverified[:8]!r}.",
            retryable=False,
        )
    cited_reports = {citation["report_id"] for citation in reused}
    appended_reports: list[str] = []
    if assessment.get("status") in {"pass", "fail"}:
        for citation in verified_citations.values():
            report_id = citation["report_id"]
            if report_id not in cited_reports:
                reused.append(dict(citation))
                cited_reports.add(report_id)
                appended_reports.append(report_id)
    assessment["evidence"] = reused
    return {"reused": len(seen), "appended_report_ids": appended_reports}


def _manual_html_assessment(skill_input: dict[str, Any]) -> dict[str, Any]:
    step = skill_input["steps"][0]
    release = step["automation_release"]
    release_consistency = {
        "disabled": "not_checked",
        "matched": "matched",
        "mismatch": "mismatched",
        "not_found": "mismatched",
    }.get(release["status"], "uncertain")
    if release.get("failure_code") in {
        "html_path_testcase_id_mismatch",
        "actual_name_missing",
        "actual_name_html_mismatch",
    }:
        release_consistency = {
            "exact": "matched",
            "compatible": "matched",
            "not_checked": "uncertain",
        }.get(release.get("script_name_match"), release_consistency)
    return {
        "review_step": step["review_step"],
        "status": "manual",
        "description_coverage": "uncertain",
        "expected_coverage": "uncertain",
        "actual_coverage": "uncertain",
        "result_consistency": "uncertain",
        "release_consistency": release_consistency,
        "reviewed_report_ids": [],
        "evidence": [],
        "reason": "HTML 证据 AI 输出未通过格式或引用校验，未采信模型结论；需人工复核。",
    }


def _validate_html_assessment(
    skill_input: dict[str, Any],
    assessment: dict[str, Any],
) -> None:
    automation_release = skill_input["steps"][0]["automation_release"]
    release_status = automation_release["status"]
    required_release_consistency = {
        "disabled": "not_checked",
        "matched": "matched",
        "mismatch": "mismatched",
        "not_found": "mismatched",
        "incomplete": "uncertain",
        "unavailable": "uncertain",
    }.get(release_status)
    if automation_release.get("failure_code") in {
        "html_path_testcase_id_mismatch",
        "actual_name_missing",
        "actual_name_html_mismatch",
    }:
        allowed_release_consistencies = {
            "exact": {"matched"},
            "compatible": {"matched"},
            "not_checked": (
                {"uncertain", "mismatched"}
                if automation_release.get("selected_release") is None
                else {"uncertain"}
            ),
        }.get(automation_release.get("script_name_match"))
    else:
        allowed_release_consistencies = (
            {required_release_consistency}
            if required_release_consistency is not None
            else None
        )
    if (
        allowed_release_consistencies is not None
        and assessment["release_consistency"] not in allowed_release_consistencies
    ):
        raise SkillFailure(
            "HTML evidence Skill contradicted the automation release lookup.",
            retryable=False,
        )
    reports = skill_input["steps"][0]["reports"]
    report_ids = [report["report_id"] for report in reports]
    reviewed_ids = assessment["reviewed_report_ids"]
    if len(reviewed_ids) != len(set(reviewed_ids)) or set(reviewed_ids) != set(
        report_ids
    ):
        raise SkillFailure(
            "HTML evidence Skill did not exactly cover supplied reports.",
            retryable=False,
        )
    _validate_html_citations(reports, assessment)
    if assessment["status"] in {"pass", "fail"}:
        cited_report_ids = {
            citation["report_id"] for citation in assessment["evidence"]
        }
        if cited_report_ids != set(report_ids):
            raise SkillFailure(
                "HTML evidence Skill returned a verdict without cited evidence "
                "for every supplied report.",
                retryable=False,
            )
    if assessment["status"] == "pass" and (
        any(report["content_truncated"] for report in reports)
        or any(
            assessment[field_name] != "supported"
            for field_name in (
                "description_coverage",
                "expected_coverage",
                "actual_coverage",
            )
        )
        or assessment["result_consistency"] != "consistent"
        or (
            skill_input["steps"][0]["automation_release"]["status"]
            != "disabled"
            and assessment["release_consistency"] != "matched"
        )
    ):
        raise SkillFailure(
            "HTML evidence Skill returned pass without complete consistent coverage.",
            retryable=False,
        )


def _validate_html_batch_assessment(
    skill_input: dict[str, Any],
    assessment: dict[str, Any],
) -> None:
    reports = skill_input["steps"][0]["reports"]
    report_ids = [report["report_id"] for report in reports]
    reviewed_ids = assessment["reviewed_report_ids"]
    if len(reviewed_ids) != len(set(reviewed_ids)) or set(reviewed_ids) != set(
        report_ids
    ):
        raise SkillFailure(
            "HTML evidence batch did not cover every supplied report fragment.",
            retryable=False,
        )
    _validate_html_citations(reports, assessment)


def _run_html_review_skill(
    ctx: ReviewContext,
    skill_input: dict[str, Any],
    *,
    assessment_validator: Callable[[dict[str, Any], dict[str, Any]], None],
) -> tuple[dict[str, Any], dict[str, Any]]:
    def validate_output(
        validated_input: dict[str, Any],
        validated_output: dict[str, Any],
    ) -> None:
        for citation in validated_output["assessments"][0]["evidence"]:
            citation["quote"] = _compact_html_quote(citation["quote"])
        assessment_validator(
            validated_input,
            validated_output["assessments"][0],
        )

    trace = skill_runner.run(
        "html-evidence-review",
        skill_input,
        endpoint=completion_url(ctx.ai_config.base_url),
        model_name=ctx.ai_config.model_name,
        headers=ai_headers(ctx.ai_config),
        timeout_seconds=ctx.ai_config.timeout_seconds,
        granted_capabilities={
            "review.step_text",
            "evidence.automation_release.metadata",
            "evidence.html.metadata",
            "evidence.html.content",
        },
        request_post=httpx.post,
        output_validator=validate_output,
    )
    if trace.get("status") != "completed":
        if trace.get("failure_kind") == "invalid_output":
            trace["manual_fallback"] = "invalid_output"
            return trace, _manual_html_assessment(skill_input)
        raise skill_failure(trace, "HTML evidence Skill failed.")
    assessment = trace["output"]["assessments"][0]
    return trace, assessment


def _run_html_review_batches(
    ctx: ReviewContext,
    skill_input: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    batches = _html_skill_batch_inputs(skill_input)
    if len(batches) == 1:
        trace, assessment = _run_html_review_skill(
            ctx,
            skill_input,
            assessment_validator=_validate_html_assessment,
        )
        return [trace], assessment

    traces: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    for batch in batches:
        trace, observation = _run_html_review_skill(
            ctx,
            batch,
            assessment_validator=_validate_html_batch_assessment,
        )
        traces.append(trace)
        if trace.get("manual_fallback"):
            return traces, _manual_html_assessment(skill_input)
        observations.append(observation)

    final_input = _html_final_input(skill_input, observations)
    citation_recovery: dict[str, Any] = {}

    def validate_final(
        _validated_input: dict[str, Any],
        assessment: dict[str, Any],
    ) -> None:
        citation_recovery.update(
            _reuse_verified_html_citations(observations, assessment)
        )
        _validate_html_assessment(skill_input, assessment)

    final_trace, assessment = _run_html_review_skill(
        ctx,
        final_input,
        assessment_validator=validate_final,
    )
    if final_trace.get("manual_fallback"):
        assessment = _manual_html_assessment(skill_input)
    elif citation_recovery:
        final_trace["citation_recovery"] = citation_recovery
    traces.append(final_trace)
    return traces, assessment


def run_report_review_stage(ctx: ReviewContext) -> dict[str, Any]:
    ctx.evidence.html_results = _run_report_pipeline(ctx.plan, ctx.evidence_config)
    for request in ctx.plan.report_requests:
        skill_input = _html_skill_input(ctx, request)
        if skill_input is None:
            continue
        skill_input, selection = _select_html_blocks(skill_input)
        traces, assessment = _run_html_review_batches(ctx, skill_input)
        if selection is not None:
            traces[0]["html_block_selection"] = selection
            assessment = _guard_selected_html_fail(skill_input, assessment)
        ctx.evidence.html_skill_traces.extend(traces)
        ctx.evidence.html_assessments[request.review_step] = assessment
        report_paths = {
            f"step-{request.review_step}-report-{index}": path
            for index, path in enumerate(request.paths, start=1)
        }
        collect_html_reported_equipment(
            request.review_step,
            report_paths,
            ctx.evidence.html_results,
            assessment,
            ctx.evidence.html_reported_equipment,
        )
    if not ctx.evidence.external_review_enabled:
        status = "disabled"
    elif ctx.plan.report_requests:
        status = "completed"
    else:
        status = "not_applicable"
    report_statuses = [
        result.status
        for step_results in ctx.evidence.html_results.values()
        for result in step_results.values()
    ]
    return {
        "status": status,
        "ai_calls": sum(
            int(trace.get("ai_calls", 0))
            for trace in ctx.evidence.html_skill_traces
        ),
        "skills": ctx.evidence.html_skill_traces,
        "reports": sum(len(request.paths) for request in ctx.plan.report_requests),
        "result_statuses": {
            status_name: report_statuses.count(status_name)
            for status_name in sorted(set(report_statuses))
        },
        "assessment_statuses": {
            status_name: sum(
                assessment["status"] == status_name
                for assessment in ctx.evidence.html_assessments.values()
            )
            for status_name in sorted(
                {
                    assessment["status"]
                    for assessment in ctx.evidence.html_assessments.values()
                }
            )
        },
    }
