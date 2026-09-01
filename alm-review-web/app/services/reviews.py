from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import PureWindowsPath
from typing import Any

import httpx
from sqlalchemy import case, desc, func, or_, select
from sqlalchemy.orm import Session

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
    Workspace,
    utcnow,
)
from app.services.ai_transport import ai_headers as _ai_headers
from app.services.ai_transport import completion_url as _completion_url
from app.services.ai_transport import skill_failure as _skill_failure
from app.services.automation_release import (
    assess_automation_release,
    load_automation_releases,
)
from app.services.equipment_pipeline import (
    absorb_first_pass as _absorb_first_pass_equipment,
)
from app.services.equipment_pipeline import (
    run_equipment_pipeline as _run_equipment_pipeline,
)
from app.services.equipment_review import OpenQuestion, analyze_equipment_steps
from app.services.evidence import (
    CAPABILITIES,
    analyze_html_filename_anomalies,
    analyze_html_path_sequences,
    html_filename_testcase_ids,
    html_path_testcase_ids,
    is_html_report_path,
    validate_network_evidence_path,
)
from app.services.html_evidence import HtmlEvidenceResolver, HtmlEvidenceResult
from app.services.image_evidence import (
    ImageEvidenceResult,
    NetworkImageResolver,
    ResolvedImage,
    embedded_image,
)
from app.services.review_pipeline import build_pipeline_trace
from app.services.review_policy import current_review_policy_key
from app.services.skill_runner import SkillFailure, skill_runner
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
_RESULT_EVIDENCE_ROLES = {"result_evidence", "result_evidence_location"}
# Whether the cited evidence really answers Expected is decided by the later
# path/image/HTML stages, so the text pass must not pre-empt them.
_EVIDENCE_SUPERSEDED_CODES = {"actual_insufficient", "evidence_reference_missing"}
# Prompt budgets that keep a single Skill call inside the model context window.
STEP_FIELD_CHAR_LIMIT = 6000
TEXT_BATCH_CHAR_BUDGET = 24000
TEXT_BATCH_MAX_STEPS = 15
HTML_SKILL_CHAR_BUDGET = 30000
VALID_MANUAL_DECISIONS = {
    "needs_manual_review": {"confirmed_qualified", "confirmed_unqualified"},
    "unqualified": {"override_qualified"},
}

DEFAULT_JOB_LEASE_SECONDS = 15 * 60
MAX_REVIEW_JOB_ATTEMPTS = 3
FAILED_JOB_RETRY_BACKOFF_SECONDS = 60


@dataclass
class CurrentReview:
    result: ReviewResult | None
    manual_decision: ManualDecision | None
    final_status: str


@dataclass
class PreparedImageEvidence:
    external_review_enabled: bool
    results: dict[int, dict[str, ImageEvidenceResult]]
    html_results: dict[int, dict[str, HtmlEvidenceResult]] = field(default_factory=dict)
    html_assessments: dict[int, dict[str, Any]] = field(default_factory=dict)
    image_skill_traces: list[dict[str, Any]] = field(default_factory=list)
    html_skill_traces: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ReportReviewRequest:
    review_step: int
    paths: tuple[str, ...]


@dataclass(frozen=True)
class ReviewPlan:
    text_steps: tuple[int, ...]
    report_requests: tuple[ReportReviewRequest, ...]
    equipment_steps: tuple[int, ...]


@dataclass
class ReviewContext:
    """Everything one review run carries between stages.

    Each stage reads what earlier stages left here and writes its own outputs
    back, so `execute_review` stays a plain list of stage calls.
    """

    ai_config: AiConfig
    content: dict[str, Any]
    evidence_config: EvidenceConfig | None
    equipment_enabled: bool
    equipment_registry: list[EquipmentRegistry] = field(default_factory=list)
    automation_release_assessments: dict[int, dict[str, Any]] = field(
        default_factory=dict
    )
    equipment_checks: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[OpenQuestion] = field(default_factory=list)
    text_result: dict[str, Any] = field(default_factory=dict)
    raw_response: str = ""
    plan: ReviewPlan = field(default_factory=lambda: ReviewPlan((), (), ()))
    evidence: PreparedImageEvidence = field(
        default_factory=lambda: PreparedImageEvidence(
            external_review_enabled=False,
            results={},
        )
    )
    first_pass_resolved_steps: int = 0


@dataclass(frozen=True)
class PipelineOutcome:
    parsed: dict[str, Any]
    pipeline: dict[str, Any]
    raw_response: str


def _evidence_actions(profile: dict[str, Any]) -> set[str]:
    routing = profile.get("routing")
    if isinstance(routing, dict):
        return set(routing.get("actions", []))

    paths = profile.get("actual_paths", [])
    actions: set[str] = set()
    if paths and profile.get("path_validation_required", True):
        actions.add("validate_path")
    if any(path.get("kind") == "html" for path in paths):
        actions.update(("validate_path", "parse_html_report"))
    if profile.get("screenshot_review_required") and any(
        path.get("kind") != "html" for path in paths
    ):
        actions.update(("validate_path", "load_images", "send_to_visual_ai"))
    if profile.get("attachment_declared"):
        actions.add("review_attachment")
    return actions


def _routed_paths(profile: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        path
        for path in profile.get("actual_paths", [])
        if path.get("route_requested", True)
    ]


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
    if not reason.strip():
        raise ValueError("A reason is required.")

    manual = ManualDecision(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        revision_id=run.current_revision_id,
        review_result_id=review.result.id,
        decision=decision,
        operator=operator.strip() or "unknown",
        reason=reason.strip(),
        source_hash=run.source_hash,
        original_ai_verdict=review.result.verdict,
    )
    db.add(manual)
    db.commit()
    db.refresh(manual)
    return manual


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
        "language_quality": _criterion(
            "pass", "未发现影响结论的语言问题。"
        ),
        "expected_vs_actual": _criterion("pass", "Actual 已回应 Expected。"),
        "screenshot_evidence": _criterion(
            "not_applicable", "未检测到截图证据要求。"
        ),
        "path_validation": _criterion(
            "not_applicable", "Actual 中未检测到外部路径。"
        ),
        "html_report_sequence": _criterion(
            "not_applicable", "未检测到自动化 HTML 报告序列。"
        ),
        "automation_results": _criterion(
            "not_applicable", "未检测到自动化 HTML 报告结果。"
        ),
        "automation_timing": _criterion(
            "not_applicable", "日期评审规则尚未启用。"
        ),
        "phantom_information": _criterion(
            "not_applicable", "未检测到参考数据要求。"
        ),
        "equipment_traceability": _criterion(
            "not_applicable", "没有需要台账校验的受控设备。"
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
        "html_filename": "html_report_sequence",
        "html_testcase_id": "automation_results",
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
        step_result["summary"] = "; ".join(issue["summary"] for issue in issues)
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
                    criterion["summary"] += "; " + issue["summary"]

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
                    f"已核验 {matched_count} 台设备；标识符和执行日期均与台账一致。"
                ),
            )
        equipment_criterion["evidence"] = (
            f"已检查 {len(equipment_results)} 个步骤；"
            f"失败 {equipment_statuses.count('fail')} 个，"
            f"需人工复核 {equipment_statuses.count('manual')} 个。"
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
            f"步骤 {issue['step']}：{issue['summary']}" for issue in all_issues[:6]
        ]
        if len(all_issues) > 6:
            displayed.append(f"另有 {len(all_issues) - 6} 个问题")
        issue_summary = "；".join(displayed)
    elif all_warnings:
        issue_summary = f"评审通过，但有 {len(all_warnings)} 条警告。"
    else:
        issue_summary = parsed.get("model_summary") or (
            "未发现语言或语义问题。"
        )
    parsed.update(
        {
            "verdict": verdict,
            "issue_summary": issue_summary,
            "criteria": criteria,
            "warnings": all_warnings,
        }
    )
    return parsed


# Image evidence statuses that raise an issue, mapped to severity, issue type, and text.
# Any status missing here is deliberately silent; `tests/test_image_evidence.py` pins the set.
_IMAGE_EVIDENCE_ISSUES: dict[str, tuple[str, str, str]] = {
    "missing": ("fail", "path", "配置的证据路径不存在。"),
    "no_images": ("fail", "screenshot", "证据文件夹中没有可评审的图像。"),
    "no_usable_images": (
        "fail",
        "screenshot",
        "证据文件夹中没有可评审的图像。",
    ),
    "no_matching_images": (
        "fail",
        "screenshot",
        "证据文件夹中存在图像，但没有文件名与该步骤匹配。",
    ),
    "ambiguous_step_mapping": (
        "manual",
        "screenshot",
        "多个步骤共用一个证据文件夹，且图像文件名不含步骤标记，无法可靠匹配。",
    ),
    "denied": ("manual", "path", "证据文件夹暂时无法读取。"),
    "unavailable": ("manual", "path", "证据文件夹暂时无法读取。"),
    "outside_root": (
        "manual",
        "path",
        "证据路径包含链接或重解析点，无法确认路径始终位于批准的根目录内。",
    ),
    "transport_too_large": (
        "manual",
        "screenshot",
        "证据图像存在，但超过当前 AI 接口可接受的大小。",
    ),
}


def _apply_capability_guards(
    parsed: dict[str, Any],
    content: dict[str, Any],
    evidence_config: EvidenceConfig | None = None,
    prepared_evidence: PreparedImageEvidence | None = None,
    automation_release_assessments: dict[int, dict[str, Any]] | None = None,
    equipment_enabled: bool = False,
) -> dict[str, Any]:
    allowed_root = evidence_config.allowed_network_root if evidence_config else ""
    release_assessments = automation_release_assessments or {}
    html_path_count = 0
    readable_html_count = 0
    ai_reviewed_html_count = 0
    passed_html_step_count = 0
    html_assessment_statuses: list[str] = []
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
        routing = profile.get("routing", {})
        actions = _evidence_actions(profile)
        routed_paths = (
            _routed_paths(profile)
            if "validate_path" in actions
            else []
        )
        html_paths = [
            path
            for path in profile.get("actual_paths", [])
            if is_html_report_path(path)
        ]
        paths = []
        seen_paths: set[str] = set()
        for path in [*routed_paths, *html_paths]:
            key = str(path.get("raw") or "").casefold()
            if key and key not in seen_paths:
                paths.append(path)
                seen_paths.add(key)
        html_path_count += len(html_paths)
        html_sequences = analyze_html_path_sequences(html_paths)
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
                "无编号后缀的基础文件" if number == 1 else f"_{number}.html"
                for number in missing_numbers
            ]
            _append_step_issue(
                parsed,
                step_result["review_step"],
                "fail",
                "html_sequence",
                "自动化 HTML 报告编号不连续；缺少 "
                + ", ".join(missing_suffixes)
                + "。",
            )
        filename_anomalies = analyze_html_filename_anomalies(html_paths)
        if filename_anomalies:
            markers = ", ".join(
                sorted({item["marker"] for item in filename_anomalies})
            )
            _append_step_issue(
                parsed,
                step_result["review_step"],
                "fail",
                "html_filename",
                f"HTML 报告文件名使用了不合格的后缀或标记：{markers}。",
            )
        alm_testcase_id = str(content.get("test_id") or "").strip()
        if alm_testcase_id:
            html_testcase_ids = []
            html_path_ids = []
            for path in html_paths:
                filename_testcase_ids = html_filename_testcase_ids(path["raw"])
                status = (
                    "pass"
                    if alm_testcase_id in filename_testcase_ids
                    else "fail"
                )
                html_testcase_ids.append(
                    {
                        "source_path": path["raw"],
                        "alm_testcase_id": alm_testcase_id,
                        "filename_testcase_ids": filename_testcase_ids,
                        "status": status,
                    }
                )
                if status == "fail":
                    summary = (
                        "HTML 报告文件名中未找到 5 位或 6 位 Testcase ID。"
                        if not filename_testcase_ids
                        else (
                            "HTML 报告文件名中的 Testcase ID "
                            f"{', '.join(filename_testcase_ids)} 与 ALM Test ID "
                            f"{alm_testcase_id} 不一致。"
                        )
                    )
                    _append_step_issue(
                        parsed,
                        step_result["review_step"],
                        "fail",
                        "html_testcase_id",
                        summary,
                    )
                path_testcase_ids = html_path_testcase_ids(path["raw"])
                path_status = (
                    "not_checked"
                    if not path_testcase_ids
                    else "pass"
                    if all(value == alm_testcase_id for value in path_testcase_ids)
                    else "fail"
                )
                html_path_ids.append(
                    {
                        "source_path": path["raw"],
                        "alm_testcase_id": alm_testcase_id,
                        "path_testcase_ids": path_testcase_ids,
                        "status": path_status,
                    }
                )
                if path_status == "fail":
                    _append_step_issue(
                        parsed,
                        step_result["review_step"],
                        "fail",
                        "html_testcase_id",
                        "HTML 报告路径中的 Testcase ID 目录 "
                        f"{', '.join(path_testcase_ids)} 与 ALM Test ID "
                        f"{alm_testcase_id} 不一致。",
                    )
            step_result["html_testcase_ids"] = html_testcase_ids
            step_result["html_path_testcase_ids"] = html_path_ids
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
        step_result["evidence_routing"] = routing
        if routing.get("manual_required"):
            _append_step_issue(
                parsed,
                step_result["review_step"],
                "manual",
                "path",
                "无法可靠确定证据用途。",
            )
        attachment_evidence = {
            source: evidence
            for source, evidence in step_evidence.items()
            if source.startswith("ALM attachment:")
        }
        for source, evidence in attachment_evidence.items():
            step_result["image_evidence"].append(
                {
                    "source_path": source,
                    "source_kind": "alm_attachment",
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
            issue = _IMAGE_EVIDENCE_ISSUES.get(evidence.status)
            if issue is not None:
                issue_status, issue_type, issue_summary = issue
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    issue_status,
                    issue_type,
                    issue_summary,
                )
        for path in paths:
            path_status = validate_network_evidence_path(path["raw"], allowed_root)
            if path_status in {"not_unc", "outside_root"}:
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    "fail",
                    "path",
                    "该证据不是已配置根目录下的绝对网络路径。",
                )
            elif path_status == "root_not_configured":
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    "manual",
                    "path",
                    "未配置允许的网络证据根目录。",
                )
            elif not prepared_evidence or not prepared_evidence.external_review_enabled:
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    "manual",
                    "path",
                    "未启用受控网络证据读取。",
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
                issue = _IMAGE_EVIDENCE_ISSUES.get(evidence.status)
                if issue is not None:
                    issue_status, issue_type, issue_summary = issue
                    _append_step_issue(
                        parsed,
                        step_result["review_step"],
                        issue_status,
                        issue_type,
                        issue_summary,
                    )

        for path in html_paths:
            evidence = step_html_evidence.get(path["raw"])
            if evidence is None:
                if prepared_evidence and prepared_evidence.external_review_enabled:
                    _append_step_issue(
                        parsed,
                        step_result["review_step"],
                        "manual",
                        "automation_result",
                        "未解析自动化 HTML 报告。",
                    )
                continue
            if evidence.status == "ready":
                readable_html_count += 1
            step_result["html_evidence"].append(
                {
                    "source_path": path["raw"],
                    "status": evidence.status,
                    "size_bytes": evidence.size_bytes,
                    "sha256": evidence.sha256,
                    "block_count": len(evidence.blocks),
                    "detail": evidence.detail,
                }
            )
            if evidence.status == "missing":
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
                "no_visible_text",
                "outside_root",
            }:
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    "manual",
                    "automation_result",
                    "无法可靠解析自动化 HTML 报告。",
                )

        html_assessment = (
            prepared_evidence.html_assessments.get(step_result["review_step"])
            if prepared_evidence
            else None
        )
        if html_assessment is not None:
            step_result["html_ai_review"] = html_assessment
            reviewed_count = len(html_assessment["reviewed_report_ids"])
            ai_reviewed_html_count += reviewed_count
            html_assessment_statuses.append(html_assessment["status"])
            if html_assessment["status"] == "pass":
                passed_html_step_count += 1
            elif html_assessment["status"] in {"fail", "manual"}:
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    html_assessment["status"],
                    "automation_result",
                    html_assessment["reason"][:200],
                )
        elif any(
            evidence is not None and evidence.status == "ready"
            for evidence in step_html_evidence.values()
        ):
            _append_step_issue(
                parsed,
                step_result["review_step"],
                "manual",
                "automation_result",
                "可读取的自动化 HTML 报告未经过 AI 评审。",
            )

        release_assessment = release_assessments.get(step_result["review_step"])
        if release_assessment is not None:
            release_result = dict(release_assessment)
            if html_assessment is not None:
                release_result["ai_consistency"] = html_assessment[
                    "release_consistency"
                ]
                if (
                    release_assessment["status"] == "needs_ai"
                    and html_assessment["release_consistency"] == "mismatched"
                ):
                    release_result["failure_code"] = "release_script_mismatch"
            step_result["automation_release"] = release_result
            release_status = release_assessment["status"]
            if release_status in {"mismatch", "not_found"}:
                issue_status = "fail"
            elif release_status in {"incomplete", "unavailable"}:
                issue_status = "manual"
            elif release_status == "needs_ai" and html_assessment is not None:
                issue_status = {
                    "matched": "pass",
                    "mismatched": "fail",
                    "uncertain": "manual",
                    "not_checked": "manual",
                }[html_assessment["release_consistency"]]
            else:
                issue_status = "pass"
            if issue_status in {"fail", "manual"}:
                summary = release_assessment["reason"]
                if release_status == "needs_ai" and html_assessment is not None:
                    summary = html_assessment["reason"]
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    issue_status,
                    "automation_result",
                    summary[:200],
                )

        screenshot_required = profile["screenshot_review_required"]
        has_evidence_reference = bool(paths or profile["attachment_declared"])
        if screenshot_required and not has_evidence_reference:
            _append_step_issue(
                parsed,
                step_result["review_step"],
                "fail",
                "screenshot",
                "Expected 要求截图，但 Actual 未提供截图或证据路径。",
            )
        elif (
            screenshot_required
            and profile["attachment_declared"]
            and not paths
            and not attachment_evidence
        ):
            _append_step_issue(
                parsed,
                step_result["review_step"],
                "manual",
                "screenshot",
                (
                    "ALM 附件中没有可评审的图片，或附件内容无法读取。"
                    if prepared_evidence
                    and prepared_evidence.external_review_enabled
                    else "未启用 ALM 附件图像评审。"
                ),
            )
        elif screenshot_required and prepared_evidence:
            has_ready_images = any(
                evidence.status == "ready" and evidence.images
                for evidence in step_evidence.values()
            )
            if has_ready_images and not prepared_evidence.external_review_enabled:
                _append_step_issue(
                    parsed,
                    step_result["review_step"],
                    "manual",
                    "screenshot",
                    "未启用向 AI 接口发送图像。",
                )
        if (
            profile["reference_lookup_required"]
            and not CAPABILITIES.reference_lookup
            and not equipment_enabled
        ):
            _append_step_issue(
                parsed,
                step_result["review_step"],
                "manual",
                "reference_data",
                "未配置参考数据，因此设备或模体信息需要人工确认。",
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
            f"共 {html_path_count} 个 HTML 文件；连续序列 "
            f"{continuous_sequence_count}/{sequence_count}。"
        )

        results_criterion = result["criteria"]["automation_results"]
        if (
            readable_html_count == html_path_count
            and ai_reviewed_html_count == html_path_count
            and html_assessment_statuses
            and all(status == "pass" for status in html_assessment_statuses)
            and results_criterion["status"] == "not_applicable"
        ):
            results_criterion.update(
                status="pass",
                summary=(
                    "自动化报告覆盖对应的 ALM 步骤，并支持一致的通过结论。"
                ),
            )
        elif (
            results_criterion["status"] == "not_applicable"
        ):
            results_criterion.update(
                status="manual",
                summary="部分自动化 HTML 报告未完成 AI 评审。",
            )
        results_criterion["evidence"] = (
            f"已读取 {readable_html_count}/{html_path_count} 个 HTML 文件；"
            f"AI 已评审 {ai_reviewed_html_count} 个报告引用；"
            f"通过的步骤评审 {passed_html_step_count} 个。"
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
        if any(
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


def _run_report_pipeline(
    plan: ReviewPlan,
    evidence_config: EvidenceConfig | None,
) -> dict[int, dict[str, HtmlEvidenceResult]]:
    if not evidence_config or not evidence_config.external_evidence_review_enabled:
        return {}
    resolver = HtmlEvidenceResolver()
    return {
        request.review_step: {
            path: resolver.resolve(
                path,
                evidence_config.allowed_network_root,
                evidence_config.local_html_fallback_root,
            )
            for path in request.paths
        }
        for request in plan.report_requests
    }


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
        "review_mode": "final",
        "batch_index": 1,
        "batch_count": 1,
        "steps": [
            {
                "review_step": request.review_step,
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
        ]
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


def _html_final_input(
    skill_input: dict[str, Any],
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    step = skill_input["steps"][0]
    return {
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
                        "blocks": [],
                    }
                    for report in step["reports"]
                ],
            }
        ],
    }


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
        quote_lines = [
            " ".join(line.split()).casefold()
            for line in citation["quote"].splitlines()
            if line.strip()
        ]
        block_lines = [
            " ".join(line.split()).casefold()
            for line in (block_text or "").splitlines()
            if line.strip()
        ]
        next_index = 0
        for quote_line in quote_lines:
            matching_index = next(
                (
                    index
                    for index in range(next_index, len(block_lines))
                    if quote_line in block_lines[index]
                ),
                None,
            )
            if matching_index is None:
                next_index = -1
                break
            next_index = matching_index + 1
        if not quote_lines or next_index < 0:
            raise SkillFailure(
                "HTML evidence Skill cited text outside the supplied report block.",
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
        required_release_consistency = {
            "exact": "matched",
            "compatible": "matched",
            "not_checked": "uncertain",
        }.get(automation_release.get("script_name_match"))
    if (
        required_release_consistency is not None
        and assessment["release_consistency"] != required_release_consistency
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
        endpoint=_completion_url(ctx.ai_config.base_url),
        model_name=ctx.ai_config.model_name,
        headers=_ai_headers(ctx.ai_config),
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
        raise _skill_failure(trace, "HTML evidence Skill failed.")
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
        observations.append(observation)

    final_input = _html_final_input(skill_input, observations)

    def validate_final(
        _validated_input: dict[str, Any],
        assessment: dict[str, Any],
    ) -> None:
        _validate_html_assessment(skill_input, assessment)

    final_trace, assessment = _run_html_review_skill(
        ctx,
        final_input,
        assessment_validator=validate_final,
    )
    traces.append(final_trace)
    return traces, assessment


def _prepare_image_evidence(
    content: dict[str, Any],
    evidence_config: EvidenceConfig | None,
) -> PreparedImageEvidence:
    external_review_enabled = bool(
        evidence_config and evidence_config.external_evidence_review_enabled
    )
    results: dict[int, dict[str, ImageEvidenceResult]] = {}
    if not external_review_enabled:
        return PreparedImageEvidence(
            external_review_enabled=False,
            results=results,
        )

    remaining_run_images = 12
    remaining_run_bytes = 15 * 1024 * 1024
    path_review_steps: dict[str, set[int]] = {}
    for step in content.get("steps", []):
        actions = _evidence_actions(step["evidence_profile"])
        if "load_images" not in actions and "review_attachment" not in actions:
            continue
        for path in _routed_paths(step["evidence_profile"]):
            path_review_steps.setdefault(path["raw"].casefold(), set()).add(
                step["review_step"]
            )
    for step in content.get("steps", []):
        actions = _evidence_actions(step["evidence_profile"])
        if "load_images" not in actions and "review_attachment" not in actions:
            continue
        remaining_step_images = 4
        remaining_step_bytes = 10 * 1024 * 1024
        step_order = str(step.get("order") or "").strip()
        matching_step_numbers = {
            int(step_order) if step_order.isdecimal() else step["review_step"]
        }
        step_results: dict[str, ImageEvidenceResult] = {}
        for attachment in step.get("attachment_contents", []):
            if (
                remaining_step_images <= 0
                or remaining_run_images <= 0
                or remaining_step_bytes <= 0
                or remaining_run_bytes <= 0
            ):
                break
            attachment_id = str(attachment.get("attachment_id") or "").strip()
            source = f"ALM attachment:{attachment_id or 'unknown'}"
            image = embedded_image(
                name=str(attachment.get("name") or source),
                media_type=str(attachment.get("mime_type") or ""),
                data_url=str(attachment.get("data_url") or ""),
                expected_sha256=str(attachment.get("sha256") or ""),
                max_image_bytes=min(
                    5 * 1024 * 1024,
                    remaining_step_bytes,
                    remaining_run_bytes,
                ),
            )
            step_results[source] = ImageEvidenceResult(
                status="ready" if image else "no_usable_images",
                images=(image,) if image else (),
                skipped_invalid=0 if image else 1,
            )
            if image:
                remaining_step_images -= 1
                remaining_run_images -= 1
                remaining_step_bytes -= image.size_bytes
                remaining_run_bytes -= image.size_bytes
        for path in _routed_paths(step["evidence_profile"]):
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
        external_review_enabled=True,
        results=results,
    )


def _image_review_batches(
    prepared_evidence: PreparedImageEvidence,
    batch_size: int = 4,
) -> list[list[tuple[int, str, ResolvedImage]]]:
    batches: list[list[tuple[int, str, ResolvedImage]]] = []
    for review_step, step_results in prepared_evidence.results.items():
        step_images = [
            (review_step, source_path, image)
            for source_path, result in step_results.items()
            if result.status == "ready"
            for image in result.images
        ]
        batches.extend(
            step_images[index : index + batch_size]
            for index in range(0, len(step_images), batch_size)
        )
    return batches


def _run_image_review_skill(
    ai_config: AiConfig,
    batch: list[tuple[int, str, ResolvedImage]],
    content: dict[str, Any],
) -> dict[str, Any]:
    step_by_number = {
        int(step["review_step"]): step for step in content.get("steps", [])
    }
    images_by_step: dict[int, list[dict[str, Any]]] = {}
    media_parts: list[dict[str, Any]] = []
    allowed_media: dict[int, set[str]] = {}
    for index, (review_step, source_path, image) in enumerate(batch, start=1):
        media_id = f"step-{review_step}-{index}-{image.sha256[:12]}"
        images_by_step.setdefault(review_step, []).append(
            {
                "media_id": media_id,
                "source_path": source_path,
                "relative_name": image.relative_name,
                "mime_type": image.media_type,
                "sha256": image.sha256,
                "width": image.width or None,
                "height": image.height or None,
            }
        )
        allowed_media.setdefault(review_step, set()).add(media_id)
        media_parts.extend(
            [
                {"type": "text", "text": f"media_id: {media_id}"},
                {"type": "image_url", "image_url": {"url": image.data_url}},
            ]
        )
    skill_input = {
        "steps": [
            {
                "review_step": review_step,
                "description": _clip_step_text(
                    step_by_number[review_step].get("description")
                )[0],
                "expected": _clip_step_text(
                    step_by_number[review_step].get("expected")
                )[0],
                "actual": _clip_step_text(
                    step_by_number[review_step].get("actual")
                )[0],
                "images": images,
            }
            for review_step, images in images_by_step.items()
        ]
    }
    trace = skill_runner.run(
        "image-evidence-review",
        skill_input,
        endpoint=_completion_url(ai_config.base_url),
        model_name=ai_config.model_name,
        headers=_ai_headers(ai_config),
        timeout_seconds=ai_config.timeout_seconds,
        granted_capabilities={
            "review.step_text",
            "evidence.image.metadata",
            "evidence.image.content",
        },
        media_parts=media_parts,
        request_post=httpx.post,
    )
    if trace.get("status") != "completed":
        raise _skill_failure(trace, "Image evidence Skill failed.")
    for assessment in trace["output"]["assessments"]:
        review_step = int(assessment["review_step"])
        observed = assessment["observed_media_ids"]
        if len(observed) != len(set(observed)) or set(observed) != allowed_media[
            review_step
        ]:
            raise SkillFailure(
                "Image evidence Skill did not exactly cover supplied media.",
                retryable=False,
            )
    return trace


def _merge_image_skill_trace(
    text_result: dict[str, Any],
    trace: dict[str, Any],
) -> None:
    for assessment in trace["output"]["assessments"]:
        if assessment["status"] == "pass":
            continue
        target = next(
            step
            for step in text_result["step_results"]
            if step["review_step"] == assessment["review_step"]
        )
        if not any(item["type"] == "screenshot" for item in target["issues"]):
            target["issues"].append(
                {
                    "status": assessment["status"],
                    "type": "screenshot",
                    "summary": assessment["reason"][:200],
                }
            )


def _text_skill_steps(content: dict[str, Any]) -> list[dict[str, Any]]:
    requested = set(content.get("review_plan", {}).get("text_steps", []))
    return [
        step for step in content.get("steps", [])
        if int(step["review_step"]) in requested
    ]


def _clip_step_text(value: Any) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= STEP_FIELD_CHAR_LIMIT:
        return text, False
    marker = (
        f"\n[TRUNCATED: the ALM text has {len(text)} characters; "
        f"only the first {STEP_FIELD_CHAR_LIMIT} are supplied]"
    )
    return text[:STEP_FIELD_CHAR_LIMIT] + marker, True


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


def _validate_text_references(
    validated_input: dict[str, Any],
    validated_output: dict[str, Any],
) -> None:
    assessments = {
        int(item["review_step"]): item
        for item in validated_output["assessments"]
    }
    errors: list[str] = []
    for step in validated_input["steps"]:
        review_step = int(step["review_step"])
        candidates = step.get("reference_candidates", [])
        candidate_ids = [item["candidate_id"] for item in candidates]
        decisions = assessments[review_step]["reference_decisions"]
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


def _fallback_missing_text_references(
    validated_input: dict[str, Any],
    validated_output: dict[str, Any],
    _error: str,
) -> dict[str, Any]:
    assessments = {
        int(item["review_step"]): {**item}
        for item in validated_output["assessments"]
    }
    fallback_used = False
    for step in validated_input["steps"]:
        review_step = int(step["review_step"])
        candidates = step.get("reference_candidates", [])
        candidate_ids = [item["candidate_id"] for item in candidates]
        decisions = [
            {**item}
            for item in assessments[review_step]["reference_decisions"]
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
        assessments[review_step] = {
            **assessments[review_step],
            "reference_decisions": decisions,
        }
    if not fallback_used:
        raise ValueError(
            "The text review output is invalid and cannot be safely completed."
        )
    return {
        **validated_output,
        "assessments": [
            assessments[int(item["review_step"])]
            for item in validated_output["assessments"]
        ],
    }


def _routes_result_evidence(step: dict[str, Any]) -> bool:
    return bool(step.get("attachment_contents")) or any(
        decision["requires_check"]
        and decision["role"] in _RESULT_EVIDENCE_ROLES
        for decision in step.get("reference_decisions", [])
    )


def _suppressed_finding(finding: dict[str, Any], cause: str) -> dict[str, str]:
    return {
        "code": finding["code"],
        "severity": finding["severity"],
        "summary": finding["reason"][:200],
        "cause": cause,
    }


def _apply_reference_routing(content: dict[str, Any]) -> None:
    for step in content.get("steps", []):
        profile = step["evidence_profile"]
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
                "decision_source": "alm-text-review",
                "confidence": None,
                "reason": "该步骤不适用，因此未执行外部证据检查。",
                "manual_required": False,
            }
            continue
        for decision in step.get("reference_decisions", []):
            candidate = candidates[decision["candidate_id"]]
            if candidate["source_field"] == "attachment":
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
        profile["routing"] = {
            "intent": intent,
            "triggers": triggers,
            "actions": sorted(actions),
            "decision_source": "alm-text-review",
            "confidence": None,
            "reason": "；".join(dict.fromkeys(reasons)) or (
                "未请求外部专项检查。"
            ),
            "manual_required": "manual_review" in actions,
        }


def _run_text_semantic_skills(
    ai_config: AiConfig,
    content: dict[str, Any],
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
                "description": description,
                "expected": expected,
                "actual": actual,
                "actual_format": step.get("actual_format", {}),
                "numbered_comparison": step.get("numbered_comparison", []),
                "reference_candidates": step.get("reference_candidates", []),
            }
        )

    traces: list[dict[str, Any]] = []
    assessments: dict[int, dict[str, Any]] = {}
    for batch in _text_skill_batches(payloads):
        trace = skill_runner.run(
            "alm-text-review",
            {"steps": batch},
            endpoint=_completion_url(ai_config.base_url),
            model_name=ai_config.model_name,
            headers=_ai_headers(ai_config),
            timeout_seconds=ai_config.timeout_seconds,
            granted_capabilities={
                "review.step_text",
                "review.text_format",
                "review.numbered_comparison",
                "evidence.path_metadata",
                "equipment.registry.candidates",
            },
            request_post=httpx.post,
            output_validator=_validate_text_references,
            output_fallback=_fallback_missing_text_references,
        )
        traces.append(trace)
        if trace.get("status") != "completed":
            raise _skill_failure(trace, "alm-text-review Skill failed.")
        assessments.update(
            {
                int(item["review_step"]): item
                for item in trace["output"]["assessments"]
            }
        )

    for step in steps:
        review_step = int(step["review_step"])
        decisions = assessments[review_step]["reference_decisions"]
        step["reference_decisions"] = decisions
        step["text_applicability"] = assessments[review_step]["applicability"]
        step["extracted_equipment"] = assessments[review_step].get(
            "extracted_equipment", []
        )

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
        evidence_routed = _routes_result_evidence(steps_by_number[review_step])
        suppressed: list[dict[str, str]] = []
        for finding in assessment["findings"]:
            code = finding["code"]
            severity = finding["severity"]
            if assessment["applicability"] == "not_applicable":
                suppressed.append(
                    _suppressed_finding(finding, "step_not_applicable")
                )
                continue
            if code in _EVIDENCE_SUPERSEDED_CODES and evidence_routed:
                suppressed.append(
                    _suppressed_finding(finding, "result_evidence_routed")
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
            target["issues"].append(
                {
                    "status": severity,
                    "type": issue_type,
                    "summary": finding["reason"][:200],
                }
            )
        if suppressed:
            target["suppressed_findings"] = suppressed
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


def _aggregate_review_pipeline(ctx: ReviewContext) -> dict[str, Any]:
    guarded = _apply_capability_guards(
        ctx.text_result,
        ctx.content,
        ctx.evidence_config,
        ctx.evidence,
        ctx.automation_release_assessments,
        equipment_enabled=ctx.equipment_enabled,
    )
    return (
        _apply_equipment_guards(guarded, ctx.equipment_checks)
        if ctx.equipment_enabled
        else _apply_disabled_equipment_guards(guarded)
    )


def _prepare_stage(ctx: ReviewContext) -> None:
    """Deterministic registry work the first AI call already needs as input."""
    if ctx.equipment_enabled:
        ctx.equipment_checks, ctx.open_questions = analyze_equipment_steps(
            ctx.content,
            ctx.equipment_registry,
        )
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


def _text_review_stage(ctx: ReviewContext) -> dict[str, Any]:
    ctx.text_result, traces = _run_text_semantic_skills(ctx.ai_config, ctx.content)
    ctx.content["text_skill_traces"] = traces
    ctx.raw_response = json.dumps(
        {trace["skill_id"]: trace["output"] for trace in traces},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "status": "completed",
        "ai_calls": len(traces),
        "steps": len(ctx.content["review_plan"]["text_steps"]),
        "skills": traces,
    }


def _routing_stage(ctx: ReviewContext) -> dict[str, Any]:
    # Reference roles arrive inside the text pass answer, so this stage consumes
    # that answer instead of spending an AI call of its own.
    _apply_reference_routing(ctx.content)
    return {
        "status": "completed",
        "ai_calls": 0,
        "decision_skill": {
            "skill_id": "alm-text-review",
            "status": "completed",
            "mode": "authoritative",
            "ai_calls": 0,
            "affects_routing": True,
            "affects_verdict": True,
            "reason": "第一轮文本评审返回候选引用角色和专项检查请求。",
        },
        "steps": [
            {
                "review_step": step["review_step"],
                "paths": step["evidence_profile"]["actual_paths"],
                **step["evidence_profile"].get("routing", {}),
            }
            for step in ctx.content.get("steps", [])
        ],
    }


def _plan_specialist_passes(ctx: ReviewContext) -> None:
    """Turn the first pass answer into the work list for the specialist stages."""
    if ctx.equipment_enabled:
        original_question_steps = {
            question.review_step for question in ctx.open_questions
        }
        ctx.open_questions = _absorb_first_pass_equipment(
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


def _image_review_stage(ctx: ReviewContext) -> dict[str, Any]:
    ai_calls = 0
    for batch in _image_review_batches(ctx.evidence):
        trace = _run_image_review_skill(ctx.ai_config, batch, ctx.content)
        ctx.evidence.image_skill_traces.append(trace)
        _merge_image_skill_trace(ctx.text_result, trace)
        ai_calls += 1
    ctx.text_result = _recalculate_result(ctx.text_result)
    if not ctx.evidence.external_review_enabled:
        status = "disabled"
    elif ai_calls:
        status = "completed"
    else:
        status = "not_applicable"
    return {
        "status": status,
        "ai_calls": ai_calls,
        "skills": ctx.evidence.image_skill_traces,
    }


def _report_review_stage(ctx: ReviewContext) -> dict[str, Any]:
    ctx.evidence.html_results = _run_report_pipeline(ctx.plan, ctx.evidence_config)
    for request in ctx.plan.report_requests:
        skill_input = _html_skill_input(ctx, request)
        if skill_input is None:
            continue
        traces, assessment = _run_html_review_batches(ctx, skill_input)
        ctx.evidence.html_skill_traces.extend(traces)
        ctx.evidence.html_assessments[request.review_step] = assessment
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


def _equipment_review_stage(ctx: ReviewContext) -> dict[str, Any]:
    if ctx.equipment_enabled:
        ctx.equipment_checks, skill_trace = _run_equipment_pipeline(
            ctx.ai_config,
            ctx.content,
            ctx.equipment_checks,
            ctx.open_questions,
            ctx.equipment_registry,
        )
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
        # The second pass is chunked, so only the Skill trace knows the call count.
        "ai_calls": int(skill_trace.get("ai_calls", 0)) if ctx.equipment_enabled else 0,
        "steps": len(ctx.plan.equipment_steps),
        "ambiguous_steps": len(ctx.open_questions),
        "first_pass_resolved_steps": ctx.first_pass_resolved_steps,
        "skill": skill_trace,
    }


def execute_review(ctx: ReviewContext) -> PipelineOutcome:
    """Run one review end to end; the caller owns the Job and the database."""
    _prepare_stage(ctx)
    text_trace = _text_review_stage(ctx)
    routing_trace = _routing_stage(ctx)
    _plan_specialist_passes(ctx)
    image_trace = _image_review_stage(ctx)
    report_trace = _report_review_stage(ctx)
    equipment_trace = _equipment_review_stage(ctx)
    parsed = _aggregate_review_pipeline(ctx)
    pipeline = build_pipeline_trace(
        gates={
            "external_evidence_review_enabled": (
                ctx.evidence.external_review_enabled
            ),
            "equipment_review_enabled": ctx.equipment_enabled,
        },
        plan=ctx.content["review_plan"],
        stages={
            "routing": routing_trace,
            "text_review": text_trace,
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



def test_ai_connection(config: AiConfig) -> str:
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
        headers=_ai_headers(config),
        timeout=config.timeout_seconds,
    )
    response.raise_for_status()
    content = str(response.json()["choices"][0]["message"]["content"]).strip()
    if '"status":"ok"' not in content.replace(" ", ""):
        raise ValueError(f"Unexpected AI response: {content[:300]}")
    return content


class ManualDecisionLock(Exception):
    """Raised when a Run was already resolved by an operator, so re-reviewing it would
    orphan that decision. Enforced at execution time because a stale Web deployment can
    still enqueue jobs that bypass the queueing-side guard."""


def manual_decision_locks_run(db: Session, run: AlmRun) -> ManualDecision | None:
    return db.scalar(
        select(ManualDecision)
        .where(
            ManualDecision.run_id == run.run_id,
            ManualDecision.revision_id == run.current_revision_id,
            ManualDecision.source_hash == run.source_hash,
        )
        .order_by(desc(ManualDecision.created_at), desc(ManualDecision.id))
        .limit(1)
    )


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

    manual = manual_decision_locks_run(db, run)
    if manual is not None:
        job.status = "cancelled"
        job.completed_at = utcnow()
        job.error_message = (
            f"Skipped: {manual.operator} already resolved this revision as {manual.decision}."
        )
        db.commit()
        raise ManualDecisionLock(job.error_message)

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
    equipment_registry: list[EquipmentRegistry] = []
    if workspace.equipment_review_enabled:
        equipment_statement = select(EquipmentRegistry).order_by(
            EquipmentRegistry.equipment_id,
            EquipmentRegistry.id,
        )
        if workspace.equipment_area_filter:
            equipment_statement = equipment_statement.where(
                EquipmentRegistry.subordinate_area == workspace.equipment_area_filter
            )
        equipment_registry = list(db.scalars(equipment_statement).all())
    content = review_payload(snapshot)
    evidence_config = workspace_evidence_config(db, workspace.id)
    release_assessments: dict[int, dict[str, Any]] = {}
    release_project = (
        evidence_config.automation_release_project_name
        if evidence_config
        else ""
    )
    release_testcase_id = str(content.get("test_id") or run.test_id or "")
    html_steps = [
        step
        for step in content.get("steps", [])
        if any(
            is_html_report_path(path)
            for path in step["evidence_profile"]["actual_paths"]
        )
    ]
    if html_steps and release_project:
        lookup_error = ""
        try:
            releases = load_automation_releases(
                db,
                release_project,
                release_testcase_id,
            )
        except Exception as exc:
            releases = ()
            lookup_error = str(exc)[:300]
        for step in html_steps:
            html_paths = tuple(
                path["raw"]
                for path in step["evidence_profile"]["actual_paths"]
                if is_html_report_path(path)
            )
            release_assessments[step["review_step"]] = assess_automation_release(
                actual=step["actual"],
                html_paths=html_paths,
                project_name=release_project,
                testcase_id=release_testcase_id,
                releases=releases,
                lookup_error=lookup_error,
            )
    ctx = ReviewContext(
        ai_config=ai_config,
        content=content,
        evidence_config=evidence_config,
        equipment_enabled=workspace.equipment_review_enabled,
        equipment_registry=equipment_registry,
        automation_release_assessments=release_assessments,
    )
    started = time.perf_counter()
    try:
        outcome = execute_review(ctx)
        parsed = outcome.parsed
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
            pipeline_json=json.dumps(outcome.pipeline, ensure_ascii=False),
            raw_response=outcome.raw_response,
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
            _mark_job_failed(failed_job, exc)
            db.commit()
        raise


def _mark_job_failed(job: ReviewJob, exc: Exception) -> None:
    job.status = "failed"
    job.error_message = str(exc)[:2000]
    job.claimed_by = None
    job.lease_expires_at = None
    job.completed_at = utcnow()
    if getattr(exc, "retryable", True) is False:
        # An identical retry would fail identically; stop burning attempts on it.
        job.attempt_count = MAX_REVIEW_JOB_ATTEMPTS


def reap_abandoned_review_jobs(db: Session) -> int:
    """Fail jobs left running by a stopped Worker so they stop blocking the queue."""
    now = utcnow()
    jobs = db.scalars(
        select(ReviewJob).where(
            ReviewJob.status == "running",
            ReviewJob.lease_expires_at.is_not(None),
            ReviewJob.lease_expires_at <= now,
            ReviewJob.attempt_count >= MAX_REVIEW_JOB_ATTEMPTS,
        )
    ).all()
    for job in jobs:
        job.status = "failed"
        job.error_message = (
            f"Abandoned after {MAX_REVIEW_JOB_ATTEMPTS} attempts; the Worker lease "
            "expired while the job was running."
        )
        job.claimed_by = None
        job.lease_expires_at = None
        job.completed_at = now
    if jobs:
        db.commit()
    return len(jobs)


def claim_next_review_job(
    db: Session,
    worker_id: str,
    lease_seconds: int = DEFAULT_JOB_LEASE_SECONDS,
) -> ReviewJob | None:
    now = utcnow()
    retry_after = now - timedelta(seconds=FAILED_JOB_RETRY_BACKOFF_SECONDS)
    job = db.scalar(
        select(ReviewJob)
        .outerjoin(Workspace, Workspace.id == ReviewJob.workspace_id)
        .where(
            or_(
                ReviewJob.status == "queued",
                (
                    (ReviewJob.status == "failed")
                    & or_(
                        ReviewJob.completed_at.is_(None),
                        ReviewJob.completed_at <= retry_after,
                    )
                ),
                (
                    (ReviewJob.status == "running")
                    & (ReviewJob.lease_expires_at.is_not(None))
                    & (ReviewJob.lease_expires_at <= now)
                ),
            ),
            ReviewJob.attempt_count < MAX_REVIEW_JOB_ATTEMPTS,
            or_(
                Workspace.id.is_(None),
                Workspace.review_queue_paused.is_(False),
            ),
        )
        .order_by(
            desc(func.coalesce(Workspace.queue_priority, 0)),
            ReviewJob.created_at,
            ReviewJob.id,
        )
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
        job_completed, job_failed = process_claimed_review_job(db, job.id)
        completed += job_completed
        failed += job_failed
    return completed, failed


def process_claimed_review_job(db: Session, job_id: int) -> tuple[int, int]:
    job = db.get(ReviewJob, job_id)
    if job is None or job.status != "running":
        return 0, 0
    try:
        process_job(db, job)
        return 1, 0
    except ManualDecisionLock:
        return 0, 0
    except Exception as exc:
        db.rollback()
        failed_job = db.get(ReviewJob, job_id)
        if failed_job is not None and failed_job.status == "running":
            _mark_job_failed(failed_job, exc)
            db.commit()
        return 0, 1