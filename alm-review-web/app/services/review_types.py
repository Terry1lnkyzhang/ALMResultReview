"""Shared data types, constants, and criterion recalculation for the review pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models import AiConfig, EquipmentRegistry, EvidenceConfig
from app.services.equipment_review import OpenQuestion
from app.services.html_evidence import HtmlEvidenceResult
from app.services.image_evidence import ImageEvidenceResult

VALID_VERDICTS = {"qualified", "unqualified", "needs_manual_review"}
CRITERIA_NAMES = (
    "location_consistency",
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

# Prompt budgets that keep a single Skill call inside the model context window.
STEP_FIELD_CHAR_LIMIT = 6000
TEXT_BATCH_CHAR_BUDGET = 24000
TEXT_BATCH_MAX_STEPS = 6
HTML_SKILL_CHAR_BUDGET = 30000


@dataclass
class PreparedImageEvidence:
    external_review_enabled: bool
    results: dict[int, dict[str, ImageEvidenceResult]]
    html_results: dict[int, dict[str, HtmlEvidenceResult]] = field(default_factory=dict)
    html_assessments: dict[int, dict[str, Any]] = field(default_factory=dict)
    html_reported_equipment: dict[int, list[dict[str, str]]] = field(
        default_factory=dict
    )
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
    location_enabled: bool = True
    project: str = ""
    workspace_name: str = ""
    equipment_registry: list[EquipmentRegistry] = field(default_factory=list)
    automation_release_assessments: dict[int, dict[str, Any]] = field(
        default_factory=dict
    )
    location_assessment: dict[str, Any] = field(default_factory=dict)
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


def _clip_step_text(value: Any) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= STEP_FIELD_CHAR_LIMIT:
        return text, False
    marker = (
        f"\n[TRUNCATED: the ALM text has {len(text)} characters; "
        f"only the first {STEP_FIELD_CHAR_LIMIT} are supplied]"
    )
    return text[:STEP_FIELD_CHAR_LIMIT] + marker, True


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
        "location_consistency": _criterion(
            "not_applicable", "测试位置配置评审不可用。"
        ),
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

    location_review = parsed.get("location_review")
    location_failure = ""
    location_manual = ""
    if isinstance(location_review, dict):
        location_status = str(location_review.get("status") or "uncertain")
        criterion_status = {
            "disabled": "not_applicable",
            "not_applicable": "not_applicable",
            "pass": "pass",
            "manual": "manual",
            "fail": "fail",
            "uncertain": "fail",
            "ready": "fail",
        }.get(location_status, "fail")
        location_reason = str(
            location_review.get("reason") or "测试位置评审未完成。"
        )
        criteria["location_consistency"] = _criterion(
            criterion_status,
            location_reason,
            (
                f"Location: {location_review.get('alm_location') or '-'}; "
                f"parent: {location_review.get('parent_name') or '-'}"
            ),
        )
        if criterion_status == "fail":
            location_failure = location_reason
        elif criterion_status == "manual":
            location_manual = location_reason

    if any(
        item.get("equipment", {}).get("code") == "disabled_by_configuration"
        for item in parsed["step_results"]
    ):
        criteria["equipment_traceability"] = _criterion(
            "not_applicable",
            "当前工作区已禁用设备台账校验。",
        )
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
    if location_failure or "fail" in step_statuses:
        verdict = "unqualified"
    elif location_manual or "manual" in step_statuses:
        verdict = "needs_manual_review"
    else:
        verdict = "qualified"
    if location_failure or location_manual:
        displayed = [f"测试位置：{location_failure or location_manual}"]
        displayed.extend(
            f"步骤 {issue['step']}：{issue['summary']}" for issue in all_issues[:5]
        )
        if len(all_issues) > 5:
            displayed.append(f"另有 {len(all_issues) - 5} 个问题")
        issue_summary = "；".join(displayed)
    elif all_issues:
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
