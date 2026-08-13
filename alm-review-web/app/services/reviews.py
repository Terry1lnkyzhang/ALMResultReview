from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import case, desc, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.hashing import review_payload
from app.models import (
    AiConfig,
    AlmRun,
    EquipmentRegistry,
    EvidenceConfig,
    ManualDecision,
    PromptVersion,
    ReviewJob,
    ReviewResult,
    RunRevision,
    utcnow,
)
from app.services.equipment_review import (
    analyze_equipment_steps,
    apply_equipment_disambiguation,
    equipment_disambiguation_prompt,
    parse_equipment_disambiguation,
)
from app.services.evidence import (
    CAPABILITIES,
    analyze_html_path_sequences,
    validate_network_evidence_path,
)
from app.services.html_evidence import HtmlEvidenceResolver, HtmlEvidenceResult
from app.services.image_evidence import (
    ImageEvidenceResult,
    NetworkImageResolver,
    image_transport_allowed,
)
from app.services.review_policy import current_review_policy_key
from app.services.workspaces import resolve_workspace, workspace_evidence_config

VALID_VERDICTS = {"qualified", "unqualified", "needs_manual_review"}
CRITERIA_NAMES = (
    "language_quality",
    "expected_vs_actual",
    "screenshot_evidence",
    "path_validation",
    "html_report_sequence",
    "automation_results",
    "automation_timing",
    "phantom_information",
    "equipment_traceability",
)
VALID_CRITERION_STATUSES = {"pass", "fail", "manual", "not_applicable"}
VALID_AI_ISSUE_TYPES = {"language", "expected_actual", "screenshot"}
VALID_WARNING_TYPES = {"minor_language"}
VALID_MANUAL_DECISIONS = {
    "needs_manual_review": {"confirmed_qualified", "confirmed_unqualified"},
    "unqualified": {"override_qualified"},
}

DEFAULT_JOB_LEASE_SECONDS = 15 * 60


@dataclass
class CurrentReview:
    result: ReviewResult | None
    manual_decision: ManualDecision | None
    final_status: str


@dataclass
class PreparedImageEvidence:
    network_enabled: bool
    image_review_enabled: bool
    transport_allowed: bool
    results: dict[int, dict[str, ImageEvidenceResult]]
    html_results: dict[int, dict[str, HtmlEvidenceResult]] = field(default_factory=dict)


def current_review(
    db: Session,
    run: AlmRun,
    policy_key: str | None = None,
) -> CurrentReview:
    if run.current_revision_id is None:
        return CurrentReview(None, None, "pending_review")
    active_policy_key = policy_key or current_review_policy_key(db, run.workspace_id)

    result = db.scalar(
        select(ReviewResult)
        .where(
            ReviewResult.run_id == run.run_id,
            ReviewResult.revision_id == run.current_revision_id,
            ReviewResult.source_hash == run.source_hash,
        )
        .order_by(
            desc(
                case(
                    (ReviewResult.review_policy_key == active_policy_key, 1),
                    else_=0,
                )
            ),
            desc(ReviewResult.completed_at),
            desc(ReviewResult.id),
        )
        .limit(1)
    )
    if result is None:
        failed_job = db.scalar(
            select(ReviewJob.id)
            .where(
                ReviewJob.revision_id == run.current_revision_id,
                ReviewJob.status == "failed",
            )
            .limit(1)
        )
        return CurrentReview(None, None, "review_failed" if failed_job else "pending_review")

    manual = db.scalar(
        select(ManualDecision)
        .where(
            ManualDecision.run_id == run.run_id,
            ManualDecision.revision_id == run.current_revision_id,
            ManualDecision.review_result_id == result.id,
            ManualDecision.source_hash == run.source_hash,
        )
        .order_by(desc(ManualDecision.created_at), desc(ManualDecision.id))
        .limit(1)
    )
    if result.verdict == "qualified":
        final_status = "qualified"
    elif result.verdict == "unqualified":
        is_overridden = manual and manual.decision == "override_qualified"
        final_status = "qualified" if is_overridden else "unqualified"
    elif manual and manual.decision == "confirmed_qualified":
        final_status = "qualified"
    elif manual and manual.decision == "confirmed_unqualified":
        final_status = "unqualified"
    else:
        final_status = "needs_manual_review"
    return CurrentReview(result, manual, final_status)


def save_manual_decision(
    db: Session,
    run: AlmRun,
    decision: str,
    operator: str,
    reason: str,
) -> ManualDecision:
    review = current_review(db, run)
    if review.result is None:
        raise ValueError("The current run revision has no completed AI review.")
    allowed = VALID_MANUAL_DECISIONS.get(review.result.verdict, set())
    if decision not in allowed:
        raise ValueError(
            f"Decision {decision!r} is not allowed for AI verdict {review.result.verdict!r}."
        )
    if not operator.strip() or not reason.strip():
        raise ValueError("Operator and reason are required.")

    manual = ManualDecision(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        revision_id=run.current_revision_id,
        review_result_id=review.result.id,
        decision=decision,
        operator=operator.strip(),
        reason=reason.strip(),
        source_hash=run.source_hash,
        original_ai_verdict=review.result.verdict,
    )
    db.add(manual)
    db.commit()
    db.refresh(manual)
    return manual


def _parse_response(content: str, expected_steps: list[int] | None = None) -> dict[str, Any]:
    value = content.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        value = "\n".join(lines[1:-1]).strip()
    parsed: Any = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("AI response must be a JSON object.")
    reviewed_steps = parsed.get("reviewed_steps")
    if not isinstance(reviewed_steps, list) or not all(
        isinstance(step, int) and not isinstance(step, bool) for step in reviewed_steps
    ):
        raise ValueError("AI response must contain an integer reviewed_steps array.")
    if len(reviewed_steps) != len(set(reviewed_steps)):
        raise ValueError("AI response contains duplicate reviewed_steps.")
    required_steps = expected_steps if expected_steps is not None else reviewed_steps
    if reviewed_steps != required_steps:
        raise ValueError(
            f"AI reviewed_steps {reviewed_steps!r} do not match expected steps "
            f"{required_steps!r}."
        )

    raw_not_applicable_steps = parsed.get("not_applicable_steps", [])
    if not isinstance(raw_not_applicable_steps, list) or not all(
        isinstance(step, int) and not isinstance(step, bool)
        for step in raw_not_applicable_steps
    ):
        raise ValueError("AI response not_applicable_steps must be an integer array.")
    if len(raw_not_applicable_steps) != len(set(raw_not_applicable_steps)):
        raise ValueError("AI response contains duplicate not_applicable_steps.")
    unknown_not_applicable_steps = set(raw_not_applicable_steps) - set(required_steps)
    if unknown_not_applicable_steps:
        raise ValueError(
            "AI not_applicable_steps reference unknown steps "
            f"{sorted(unknown_not_applicable_steps)!r}."
        )
    not_applicable_steps = set(raw_not_applicable_steps)

    raw_issues = parsed.get("issues")
    raw_warnings = parsed.get("warnings")
    if not isinstance(raw_issues, list) or not isinstance(raw_warnings, list):
        raise ValueError("AI response must contain issues and warnings arrays.")

    step_results = {
        step: {
            "review_step": step,
            "applicability": (
                "not_applicable" if step in not_applicable_steps else "applicable"
            ),
            "status": "pass",
            "summary": "",
            "issues": [],
            "warnings": [],
        }
        for step in required_steps
    }
    seen_issues: set[tuple[int, str]] = set()
    for issue in raw_issues:
        if not isinstance(issue, dict):
            raise ValueError("Each AI issue must be a JSON object.")
        step = issue.get("step")
        status = str(issue.get("status", "")).strip().lower()
        issue_type = str(issue.get("type", "")).strip().lower()
        summary = str(issue.get("summary", "")).strip()
        if step not in step_results:
            raise ValueError(f"AI issue references unknown step {step!r}.")
        if status not in {"fail", "manual"}:
            raise ValueError(f"Invalid AI issue status {status!r}.")
        if issue_type not in VALID_AI_ISSUE_TYPES:
            raise ValueError(f"Invalid AI issue type {issue_type!r}.")
        if not summary:
            raise ValueError("AI issue summary cannot be empty.")
        key = (step, issue_type)
        if key in seen_issues:
            raise ValueError(f"Duplicate AI issue for step {step!r} and type {issue_type!r}.")
        seen_issues.add(key)
        step_results[step]["issues"].append(
            {"status": status, "type": issue_type, "summary": summary[:200]}
        )

    seen_warnings: set[tuple[int, str, str]] = set()
    for warning in raw_warnings:
        if not isinstance(warning, dict):
            raise ValueError("Each AI warning must be a JSON object.")
        step = warning.get("step")
        warning_type = str(warning.get("type", "")).strip().lower()
        summary = str(warning.get("summary", "")).strip()
        if step not in step_results:
            raise ValueError(f"AI warning references unknown step {step!r}.")
        if warning_type not in VALID_WARNING_TYPES:
            raise ValueError(f"Invalid AI warning type {warning_type!r}.")
        if not summary:
            raise ValueError("AI warning summary cannot be empty.")
        key = (step, warning_type, summary)
        if key in seen_warnings:
            continue
        seen_warnings.add(key)
        step_results[step]["warnings"].append(
            {"type": warning_type, "summary": summary[:200]}
        )

    result = {
        "model_summary": str(parsed.get("summary", "")).strip()[:500],
        "step_results": list(step_results.values()),
        "warnings": [
            {"step": item["review_step"], **warning}
            for item in step_results.values()
            for warning in item["warnings"]
        ],
    }
    return _recalculate_result(result)


def _completion_url(configured_url: str) -> str:
    url = configured_url.rstrip("/")
    return url if url.endswith("/chat/completions") else f"{url}/chat/completions"


def _manual_criterion(summary: str, evidence: str) -> dict[str, str]:
    return {"status": "manual", "summary": summary, "evidence": evidence}


def _criterion(status: str, summary: str, evidence: str = "") -> dict[str, str]:
    return {"status": status, "summary": summary, "evidence": evidence}


def _append_step_issue(
    parsed: dict[str, Any],
    step: int,
    status: str,
    issue_type: str,
    summary: str,
) -> None:
    result = next(item for item in parsed["step_results"] if item["review_step"] == step)
    if not any(issue["type"] == issue_type for issue in result["issues"]):
        result["issues"].append(
            {"status": status, "type": issue_type, "summary": summary}
        )


def _recalculate_result(parsed: dict[str, Any]) -> dict[str, Any]:
    criteria = {
        "language_quality": _criterion("pass", "未发现影响结论的语言问题。"),
        "expected_vs_actual": _criterion("pass", "Actual 可以回答 Expected。"),
        "screenshot_evidence": _criterion("not_applicable", "未检测到截图证据要求。"),
        "path_validation": _criterion("not_applicable", "Actual 中未检测到外部路径。"),
        "html_report_sequence": _criterion(
            "not_applicable", "未检测到自动化 HTML 报告序列。"
        ),
        "automation_results": _criterion(
            "not_applicable", "未检测到自动化 HTML 报告结果。"
        ),
        "automation_timing": _criterion("not_applicable", "日期审核规则尚未启用。"),
        "phantom_information": _criterion("not_applicable", "未检测到参考数据要求。"),
        "equipment_traceability": _criterion(
            "not_applicable", "未检测到需要台账核验的受控设备。"
        ),
    }
    criterion_by_type = {
        "language": "language_quality",
        "expected_actual": "expected_vs_actual",
        "screenshot": "screenshot_evidence",
        "html": "screenshot_evidence",
        "folder": "screenshot_evidence",
        "path": "path_validation",
        "html_sequence": "html_report_sequence",
        "automation_result": "automation_results",
        "reference_data": "phantom_information",
        "equipment": "equipment_traceability",
    }
    severity = {"not_applicable": 0, "pass": 1, "manual": 2, "fail": 3}
    all_issues = []
    all_warnings = []
    for step_result in parsed["step_results"]:
        issues = step_result["issues"]
        statuses = [issue["status"] for issue in issues]
        step_result["status"] = max(statuses, key=severity.get) if statuses else "pass"
        step_result["summary"] = "；".join(issue["summary"] for issue in issues)
        all_issues.extend(
            {"step": step_result["review_step"], **issue} for issue in issues
        )
        all_warnings.extend(
            {"step": step_result["review_step"], **warning}
            for warning in step_result["warnings"]
        )
        for issue in issues:
            criterion_name = criterion_by_type[issue["type"]]
            criterion = criteria[criterion_name]
            if severity[issue["status"]] > severity[criterion["status"]]:
                criterion["status"] = issue["status"]
                criterion["summary"] = issue["summary"]
            elif severity[issue["status"]] == severity[criterion["status"]]:
                if issue["summary"] not in criterion["summary"]:
                    criterion["summary"] += "；" + issue["summary"]

    equipment_results = [
        item["equipment"]
        for item in parsed["step_results"]
        if "equipment" in item
        and item["equipment"].get("status") != "not_applicable"
    ]
    if equipment_results:
        equipment_criterion = criteria["equipment_traceability"]
        equipment_statuses = [item["status"] for item in equipment_results]
        equipment_status = max(equipment_statuses, key=severity.get)
        if equipment_status == "pass":
            matched_count = sum(len(item.get("matches", [])) for item in equipment_results)
            equipment_criterion.update(
                status="pass",
                summary=(
                    f"已核验 {matched_count} 台设备，设备标识及执行日期符合台账。"
                ),
            )
        equipment_criterion["evidence"] = (
            f"共检查 {len(equipment_results)} 个 Step；"
            f"Fail {equipment_statuses.count('fail')}，"
            f"Manual {equipment_statuses.count('manual')}。"
        )

    step_statuses = {item["status"] for item in parsed["step_results"]}
    if "fail" in step_statuses:
        verdict = "unqualified"
    elif "manual" in step_statuses:
        verdict = "needs_manual_review"
    else:
        verdict = "qualified"
    if all_issues:
        displayed = [
            f"Step {issue['step']}: {issue['summary']}" for issue in all_issues[:6]
        ]
        if len(all_issues) > 6:
            displayed.append(f"另有 {len(all_issues) - 6} 个问题")
        issue_summary = "；".join(displayed)
    elif all_warnings:
        issue_summary = f"审核通过，发现 {len(all_warnings)} 条警告。"
    else:
        issue_summary = parsed.get("model_summary") or "未发现语言或语义问题。"
    parsed.update(
        {
            "verdict": verdict,
            "issue_summary": issue_summary,
            "criteria": criteria,
            "warnings": all_warnings,
        }
    )
    return parsed


def _apply_capability_guards(
    parsed: dict[str, Any],
    content: dict[str, Any],
    evidence_config: EvidenceConfig | None = None,
    prepared_evidence: PreparedImageEvidence | None = None,
) -> dict[str, Any]:
    allowed_root = evidence_config.allowed_network_root if evidence_config else ""
    html_path_count = 0
    checked_html_count = 0
    passed_html_result_count = 0
    checked_result_value_count = 0
    sequence_count = 0
    continuous_sequence_count = 0
    for content_step, step_result in zip(
        content.get("steps", []), parsed["step_results"], strict=True
    ):
        step_result["step_order"] = content_step.get("order") or step_result["review_step"]
        step_result["step_name"] = content_step.get("name") or ""
        if step_result.get("applicability") == "not_applicable":
            continue
        profile = content_step["evidence_profile"]
        paths = profile["actual_paths"]
        html_paths = [path for path in paths if path.get("kind") == "html"]
        html_path_count += len(html_paths)
        html_sequences = analyze_html_path_sequences(paths)
        sequence_count += len(html_sequences)
        continuous_sequence_count += sum(
            sequence["status"] == "pass" for sequence in html_sequences
        )
        step_result["html_sequences"] = html_sequences
        for sequence in html_sequences:
            if sequence["status"] != "fail":
                continue
            missing_numbers = sequence["missing_numbers"]
            missing_suffixes = [
                "无后缀起始文件" if number == 1 else f"_{number}.html"
                for number in missing_numbers
            ]
            _append_step_issue(
                parsed,
                step_result["review_step"],
                "fail",
                "html_sequence",
                "自动化 HTML 报告序号不连续，缺少 "
                + "、".join(missing_suffixes)
                + "。",
            )
        step_evidence = (
            prepared_evidence.results.get(step_result["review_step"], {})
            if prepared_evidence
            else {}
        )
        step_html_evidence = (
            prepared_evidence.html_results.get(step_result["review_step"], {})
            if prepared_evidence
            else {}
        )
        step_result["image_evidence"] = []
        step_result["html_evidence"] = []
        for path in paths:
            path_status = validate_network_evidence_path(path["raw"], allowed_root)
            if path_status in {"not_unc", "outside_root"}:
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    "fail",
                    "path",
                    "证据不是配置根目录下的绝对网络路径。",
                )
            elif path_status == "root_not_configured":
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    "manual",
                    "path",
                    "尚未配置允许访问的网络证据根目录。",
                )
            elif not prepared_evidence or not prepared_evidence.network_enabled:
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    "manual",
                    "path",
                    "受控网络证据读取能力尚未启用。",
                )
            else:
                evidence = step_evidence.get(path["raw"])
                if evidence is None:
                    continue
                step_result["image_evidence"].append(
                    {
                        "source_path": path["raw"],
                        "status": evidence.status,
                        "images": [
                            {
                                "name": image.relative_name,
                                "media_type": image.media_type,
                                "size_bytes": image.size_bytes,
                                "width": image.width,
                                "height": image.height,
                                "sha256": image.sha256,
                            }
                            for image in evidence.images
                        ],
                    }
                )
                if evidence.status == "missing":
                    _append_step_issue(
                        parsed,
                        step_result["review_step"],
                        "fail",
                        "path",
                        "配置的证据路径不存在。",
                    )
                elif evidence.status in {"no_images", "no_usable_images"}:
                    _append_step_issue(
                        parsed,
                        step_result["review_step"],
                        "fail",
                        "screenshot",
                        "证据目录中没有可审核的图片。",
                    )
                elif evidence.status == "no_matching_images":
                    _append_step_issue(
                        parsed,
                        step_result["review_step"],
                        "fail",
                        "screenshot",
                        "证据目录中有图片，但文件名未匹配当前 Step。",
                    )
                elif evidence.status == "ambiguous_step_mapping":
                    _append_step_issue(
                        parsed,
                        step_result["review_step"],
                        "manual",
                        "screenshot",
                        "多个 Step 共用同一证据目录，图片文件名未标明 Step，无法可靠匹配。",
                    )
                elif evidence.status in {"denied", "unavailable"}:
                    _append_step_issue(
                        parsed,
                        step_result["review_step"],
                        "manual",
                        "path",
                        "证据目录暂时无法读取，需人工确认。",
                    )
                elif evidence.status == "transport_too_large":
                    _append_step_issue(
                        parsed,
                        step_result["review_step"],
                        "manual",
                        "screenshot",
                        "证据图片存在，但总量超过当前 AI 端点的处理范围，需人工确认。",
                    )

        for path in html_paths:
            evidence = step_html_evidence.get(path["raw"])
            if evidence is None:
                if prepared_evidence and prepared_evidence.network_enabled:
                    _append_step_issue(
                        parsed,
                        step_result["review_step"],
                        "manual",
                        "automation_result",
                        "自动化 HTML 报告未完成解析，需人工确认。",
                    )
                continue
            checked_html_count += 1
            checked_result_value_count += evidence.result_count
            if evidence.status == "pass":
                passed_html_result_count += 1
            step_result["html_evidence"].append(
                {
                    "source_path": path["raw"],
                    "status": evidence.status,
                    "result_count": evidence.result_count,
                    "non_passed_values": list(evidence.non_passed_values),
                    "detail": evidence.detail,
                }
            )
            if evidence.status == "fail":
                values = ", ".join(evidence.non_passed_values[:4])
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    "fail",
                    "automation_result",
                    f"自动化报告结果并非全部 Passed：{values}。",
                )
            elif evidence.status == "result_row_missing":
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    "fail",
                    "automation_result",
                    "自动化报告未找到 Result (Passed/Failed) 结果行。",
                )
            elif evidence.status == "missing":
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    "fail",
                    "automation_result",
                    "自动化 HTML 报告文件不存在。",
                )
            elif evidence.status in {
                "denied",
                "unavailable",
                "too_large",
                "invalid",
                "outside_root",
            }:
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    "manual",
                    "automation_result",
                    "自动化 HTML 报告无法可靠解析，需人工确认。",
                )

        screenshot_required = profile["screenshot_review_required"]
        has_evidence_reference = bool(paths or profile["attachment_declared"])
        if screenshot_required and not has_evidence_reference:
            _append_step_issue(
                parsed,
                step_result["review_step"],
                "fail",
                "screenshot",
                "Expected 要求截图，但未提供截图或证据路径。",
            )
        elif screenshot_required and profile["attachment_declared"] and not paths:
            _append_step_issue(
                parsed,
                step_result["review_step"],
                "manual",
                "screenshot",
                "ALM 附件图片审核尚未启用，需人工确认。",
            )
        elif screenshot_required and prepared_evidence:
            has_ready_images = any(
                evidence.status == "ready" and evidence.images
                for evidence in step_evidence.values()
            )
            if has_ready_images and not prepared_evidence.image_review_enabled:
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    "manual",
                    "screenshot",
                    "图片发送给 AI 的功能尚未启用。",
                )
            elif has_ready_images and not prepared_evidence.transport_allowed:
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    "manual",
                    "screenshot",
                    "AI 端点为 HTTP，图片传输未获批准。",
                )

        if profile["reference_lookup_required"] and not CAPABILITIES.reference_lookup:
            _append_step_issue(
                parsed,
                step_result["review_step"],
                "manual",
                "reference_data",
                "参考数据尚未配置，设备或模体信息需人工确认。",
            )
    result = _recalculate_result(parsed)
    if html_path_count:
        sequence_criterion = result["criteria"]["html_report_sequence"]
        if sequence_criterion["status"] == "not_applicable":
            sequence_criterion.update(
                status="pass",
                summary="自动化 HTML 报告文件名连续。",
            )
        sequence_criterion["evidence"] = (
            f"共 {html_path_count} 个 HTML 文件；"
            f"连续序列 {continuous_sequence_count}/{sequence_count}。"
        )

        results_criterion = result["criteria"]["automation_results"]
        if (
            checked_html_count == html_path_count
            and results_criterion["status"] == "not_applicable"
        ):
            results_criterion.update(
                status="pass",
                summary="所有自动化报告结果均为 Passed。",
            )
        elif (
            checked_html_count < html_path_count
            and results_criterion["status"] == "not_applicable"
        ):
            results_criterion.update(
                status="manual",
                summary="部分自动化 HTML 报告尚未读取，需人工确认。",
            )
        results_criterion["evidence"] = (
            f"已解析 {checked_html_count}/{html_path_count} 个 HTML 文件；"
            f"全 Passed 文件 {passed_html_result_count}；"
            f"共核对 {checked_result_value_count} 个结果值。"
        )
    return result


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
                "summary": "主审核判定该 Step 不适用，跳过设备台账核验。",
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
            "summary": "设备台账校验已在当前 Workspace 配置中关闭。",
            "matches": [],
            "warnings": [],
        }
    result = _recalculate_result(parsed)
    result["criteria"]["equipment_traceability"] = _criterion(
        "not_applicable",
        "设备台账校验已在当前 Workspace 配置中关闭。",
    )
    return result


def _request_equipment_disambiguation(
    ai_config: AiConfig,
    ambiguous: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    headers = {}
    api_key = get_settings().ai_api_key
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = httpx.post(
        _completion_url(ai_config.base_url),
        json={
            "model": ai_config.model_name,
            "temperature": 0,
            "max_tokens": 1024,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {
                    "role": "user",
                    "content": equipment_disambiguation_prompt(ambiguous),
                }
            ],
        },
        headers=headers,
        timeout=ai_config.timeout_seconds,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Equipment disambiguation did not return JSON.")
    return parse_equipment_disambiguation(content, ambiguous)


def _prepare_image_evidence(
    content: dict[str, Any],
    evidence_config: EvidenceConfig | None,
    ai_config: AiConfig,
) -> PreparedImageEvidence:
    network_enabled = bool(
        evidence_config and evidence_config.network_evidence_enabled
    )
    image_review_enabled = bool(
        evidence_config and evidence_config.image_review_enabled
    )
    transport_allowed = bool(
        evidence_config
        and image_transport_allowed(
            ai_config.base_url,
            allow_insecure=evidence_config.allow_insecure_image_transport,
        )
    )
    results: dict[int, dict[str, ImageEvidenceResult]] = {}
    html_results: dict[int, dict[str, HtmlEvidenceResult]] = {}
    if not network_enabled:
        return PreparedImageEvidence(
            network_enabled=False,
            image_review_enabled=image_review_enabled,
            transport_allowed=transport_allowed,
            results=results,
            html_results=html_results,
        )

    remaining_run_images = 12
    remaining_run_bytes = 15 * 1024 * 1024
    path_review_steps: dict[str, set[int]] = {}
    for step in content.get("steps", []):
        step_html_results: dict[str, HtmlEvidenceResult] = {}
        for path in step["evidence_profile"]["actual_paths"]:
            if path.get("kind") != "html":
                continue
            step_html_results[path["raw"]] = HtmlEvidenceResolver().resolve(
                path["raw"], evidence_config.allowed_network_root
            )
        html_results[step["review_step"]] = step_html_results

        if not step["evidence_profile"]["screenshot_review_required"]:
            continue
        for path in step["evidence_profile"]["actual_paths"]:
            path_review_steps.setdefault(path["raw"].casefold(), set()).add(
                step["review_step"]
            )
    for step in content.get("steps", []):
        if not step["evidence_profile"]["screenshot_review_required"]:
            continue
        remaining_step_images = 4
        remaining_step_bytes = 10 * 1024 * 1024
        step_order = str(step.get("order") or "").strip()
        matching_step_numbers = {
            int(step_order) if step_order.isdecimal() else step["review_step"]
        }
        step_results: dict[str, ImageEvidenceResult] = {}
        for path in step["evidence_profile"]["actual_paths"]:
            if path.get("kind") == "html":
                continue
            if (
                remaining_step_images <= 0
                or remaining_run_images <= 0
                or remaining_step_bytes <= 0
                or remaining_run_bytes <= 0
            ):
                break
            resolver = NetworkImageResolver(
                max_depth=2,
                max_images=min(remaining_step_images, remaining_run_images),
                max_total_bytes=min(remaining_step_bytes, remaining_run_bytes),
                matching_step_numbers=matching_step_numbers,
                require_step_marker=(
                    len(path_review_steps.get(path["raw"].casefold(), set())) > 1
                ),
            )
            result = resolver.resolve(
                path["raw"], evidence_config.allowed_network_root
            )
            if (
                result.status == "ready"
                and (
                    sum(image.size_bytes for image in result.images) > 4 * 1024 * 1024
                    or sum(image.width * image.height for image in result.images)
                    > 12_000_000
                )
            ):
                result = ImageEvidenceResult(
                    status="transport_too_large",
                    images=result.images,
                    detail="Image evidence exceeds the AI endpoint transport budget.",
                )
            step_results[path["raw"]] = result
            image_count = len(result.images)
            image_bytes = sum(image.size_bytes for image in result.images)
            remaining_step_images -= image_count
            remaining_run_images -= image_count
            remaining_step_bytes -= image_bytes
            remaining_run_bytes -= image_bytes
        results[step["review_step"]] = step_results
    return PreparedImageEvidence(
        network_enabled=True,
        image_review_enabled=image_review_enabled,
        transport_allowed=transport_allowed,
        results=results,
        html_results=html_results,
    )


def _review_message(
    prompt_text: str,
    snapshot: dict[str, Any],
    prepared_evidence: PreparedImageEvidence | None = None,
) -> str | list[dict[str, Any]]:
    send_network_images = bool(
        prepared_evidence
        and prepared_evidence.image_review_enabled
        and prepared_evidence.transport_allowed
    )
    if not CAPABILITIES.image_review and not send_network_images:
        return prompt_text
    attachment_images = [
        attachment
        for step in (snapshot.get("run") or {}).get("steps") or []
        for attachment in step.get("attachmentContents") or []
        if attachment.get("data_url")
    ] if CAPABILITIES.image_review else []
    network_images = [
        (review_step, source_path, image)
        for review_step, step_results in (
            prepared_evidence.results.items() if prepared_evidence else []
        )
        for source_path, result in step_results.items()
        if result.status == "ready"
        for image in result.images
    ]
    if not attachment_images and not network_images:
        return prompt_text
    image_rules = (
        "\n\n图片证据审核规则：图片按 review_step 标注。检查图片是否清晰且能证明对应 "
        "Expected；若图片明确不支持或反驳 Expected，添加 status=fail、type=screenshot "
        "的 issue；若图片不可辨认或无法确定，添加 status=manual、type=screenshot 的 "
        "issue；证据充分则不要添加 screenshot issue。screenshot 是本次图片审核允许的扩展 "
        "issue type，优先于模板中的类型限制。不得把图片证据用于其他 Step。"
    )
    content: list[dict[str, Any]] = [
        {"type": "text", "text": prompt_text + image_rules}
    ]
    for attachment in attachment_images:
        content.append(
            {
                "type": "text",
                "text": f"ALM screenshot attachment: {attachment.get('name', 'image')}",
            }
        )
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": attachment["data_url"]},
            }
        )
    for review_step, source_path, image in network_images:
        content.append(
            {
                "type": "text",
                "text": (
                    f"Network evidence for review_step {review_step}: "
                    f"{image.relative_name} (source: {source_path})"
                ),
            }
        )
        content.append(
            {"type": "image_url", "image_url": {"url": image.data_url}}
        )
    return content


def test_ai_connection(config: AiConfig) -> str:
    headers = {}
    api_key = get_settings().ai_api_key
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = httpx.post(
        _completion_url(config.base_url),
        json={
            "model": config.model_name,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": 'Return JSON only: {"status":"ok"}',
                }
            ],
        },
        headers=headers,
        timeout=config.timeout_seconds,
    )
    response.raise_for_status()
    content = str(response.json()["choices"][0]["message"]["content"]).strip()
    if '"status":"ok"' not in content.replace(" ", ""):
        raise ValueError(f"Unexpected AI response: {content[:300]}")
    return content


def process_job(db: Session, job: ReviewJob, allow_disabled: bool = False) -> ReviewResult:
    run = db.get(AlmRun, job.run_id)
    revision = db.get(RunRevision, job.revision_id)
    if run is None or revision is None:
        raise ValueError("Review job references missing run data.")
    workspace = resolve_workspace(db, run.workspace_id or job.workspace_id)
    policy_key = current_review_policy_key(db, workspace.id)
    if job.status == "completed":
        existing = db.scalar(select(ReviewResult).where(ReviewResult.job_id == job.id))
        if existing is None:
            raise ValueError("Completed review job has no result.")
        if existing.review_policy_key != policy_key:
            raise ValueError("Completed review job belongs to an outdated review policy.")
        return existing
    if run.current_revision_id != revision.id or run.source_hash != revision.source_hash:
        job.status = "outdated"
        job.completed_at = utcnow()
        db.commit()
        raise ValueError("Review job is outdated because the ALM run changed.")

    prompt = db.scalar(
        select(PromptVersion).where(PromptVersion.is_active.is_(True)).order_by(desc(PromptVersion.id))
    )
    ai_config = db.get(AiConfig, 1)
    if prompt is None or ai_config is None or (not ai_config.enabled and not allow_disabled):
        raise ValueError("AI review is not configured or is disabled.")

    if job.status != "running":
        job.status = "running"
        job.started_at = utcnow()
        job.attempt_count += 1
        job.error_message = ""
        db.commit()

    snapshot = json.loads(revision.snapshot_json)
    structured_content = review_payload(snapshot)
    equipment_checks: list[dict[str, Any]] = []
    if workspace.equipment_review_enabled:
        equipment_statement = select(EquipmentRegistry).order_by(
            EquipmentRegistry.equipment_id
        )
        if workspace.equipment_area_filter:
            equipment_statement = equipment_statement.where(
                EquipmentRegistry.subordinate_area == workspace.equipment_area_filter
            )
        equipment_registry = db.scalars(equipment_statement).all()
        equipment_checks, ambiguous_equipment = analyze_equipment_steps(
            structured_content,
            equipment_registry,
        )
    else:
        equipment_registry = []
        ambiguous_equipment = []
    if ambiguous_equipment:
        try:
            equipment_decisions = _request_equipment_disambiguation(
                ai_config,
                ambiguous_equipment,
            )
            apply_equipment_disambiguation(
                structured_content,
                equipment_checks,
                equipment_decisions,
                equipment_registry,
            )
        except Exception as exc:
            ambiguous_steps = {item["step"] for item in ambiguous_equipment}
            for check in equipment_checks:
                if check["review_step"] not in ambiguous_steps:
                    continue
                check["disambiguation_error"] = str(exc)[:300]
                check["status"] = "manual"
                check["code"] = "equipment_disambiguation_failed"
                check["summary"] = "设备角色 AI 消歧失败，需要人工审核。"
    evidence_config = workspace_evidence_config(db, workspace.id)
    prepared_evidence = _prepare_image_evidence(
        structured_content,
        evidence_config,
        ai_config,
    )
    structured_content["review_capabilities"].update(
        {
            "path_access": prepared_evidence.network_enabled,
            "folder_scan": prepared_evidence.network_enabled,
            "image_review": (
                prepared_evidence.image_review_enabled
                and prepared_evidence.transport_allowed
            ),
        }
    )
    structured_content["external_evidence_phase"] = (
        "active" if prepared_evidence.network_enabled else "deferred"
    )
    user_prompt = prompt.template.replace(
        "{{RUN_CONTENT}}",
        json.dumps(structured_content, ensure_ascii=False, indent=2),
    )
    request_body = {
        "model": ai_config.model_name,
        "temperature": 0,
        "max_tokens": 2048,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {
                "role": "user",
                "content": _review_message(
                    user_prompt,
                    snapshot,
                    prepared_evidence,
                ),
            }
        ],
    }
    started = time.perf_counter()
    try:
        headers = {}
        api_key = get_settings().ai_api_key
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        response = httpx.post(
            _completion_url(ai_config.base_url),
            json=request_body,
            headers=headers,
            timeout=ai_config.timeout_seconds,
        )
        response.raise_for_status()
        response_data = response.json()
        content = response_data["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("AI response did not contain review JSON.")
        expected_steps = [step["review_step"] for step in structured_content["steps"]]
        guarded = _apply_capability_guards(
                _parse_response(content, expected_steps),
                structured_content,
                evidence_config,
                prepared_evidence,
            )
        parsed = (
            _apply_equipment_guards(guarded, equipment_checks)
            if workspace.equipment_review_enabled
            else _apply_disabled_equipment_guards(guarded)
        )
        duration_ms = round((time.perf_counter() - started) * 1000)
        result = ReviewResult(
            workspace_id=workspace.id,
            job_id=job.id,
            run_id=run.run_id,
            revision_id=revision.id,
            prompt_version_id=prompt.id,
            source_hash=revision.source_hash,
            review_policy_key=policy_key,
            model_name=ai_config.model_name,
            verdict=parsed["verdict"],
            issue_summary=parsed["issue_summary"],
            criteria_json=json.dumps(parsed["criteria"], ensure_ascii=False),
            step_results_json=json.dumps(parsed["step_results"], ensure_ascii=False),
            warnings_json=json.dumps(parsed["warnings"], ensure_ascii=False),
            raw_response=content,
            duration_ms=duration_ms,
        )
        db.add(result)
        job.status = "completed"
        job.claimed_by = None
        job.lease_expires_at = None
        job.completed_at = utcnow()
        db.commit()
        db.refresh(result)
        return result
    except Exception as exc:
        db.rollback()
        failed_job = db.get(ReviewJob, job.id)
        if failed_job is not None:
            failed_job.status = "failed"
            failed_job.error_message = str(exc)[:2000]
            failed_job.claimed_by = None
            failed_job.lease_expires_at = None
            failed_job.completed_at = utcnow()
            db.commit()
        raise


def claim_next_review_job(
    db: Session,
    worker_id: str,
    lease_seconds: int = DEFAULT_JOB_LEASE_SECONDS,
) -> ReviewJob | None:
    now = utcnow()
    job = db.scalar(
        select(ReviewJob)
        .where(
            or_(
                ReviewJob.status.in_(("queued", "failed")),
                (
                    (ReviewJob.status == "running")
                    & (ReviewJob.lease_expires_at.is_not(None))
                    & (ReviewJob.lease_expires_at <= now)
                ),
            ),
            ReviewJob.attempt_count < 3,
        )
        .order_by(ReviewJob.created_at, ReviewJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        db.rollback()
        return None
    job.status = "running"
    job.claimed_by = worker_id
    job.lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))
    job.started_at = now
    job.completed_at = None
    job.attempt_count += 1
    job.error_message = ""
    db.commit()
    db.refresh(job)
    return job


def process_queued_jobs(
    db: Session,
    limit: int = 10,
    worker_id: str = "local-worker",
    lease_seconds: int = DEFAULT_JOB_LEASE_SECONDS,
) -> tuple[int, int]:
    ai_config = db.get(AiConfig, 1)
    if ai_config is None or not ai_config.enabled:
        return 0, 0
    completed = 0
    failed = 0
    for _ in range(limit):
        job = claim_next_review_job(db, worker_id, lease_seconds)
        if job is None:
            break
        try:
            process_job(db, job)
            completed += 1
        except Exception as exc:
            db.rollback()
            failed_job = db.get(ReviewJob, job.id)
            if failed_job is not None and failed_job.status == "running":
                failed_job.status = "failed"
                failed_job.error_message = str(exc)[:2000]
                failed_job.claimed_by = None
                failed_job.lease_expires_at = None
                failed_job.completed_at = utcnow()
                db.commit()
            failed += 1
    return completed, failed