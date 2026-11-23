"""Stage 7 (`aggregation`): deterministic path/evidence/release capability guards and final verdict."""
from __future__ import annotations

from typing import Any

from app.models import EvidenceConfig
from app.services.equipment_pipeline import apply_equipment_verdicts
from app.services.evidence import (
    CAPABILITIES,
    analyze_html_filename_anomalies,
    analyze_html_path_sequences,
    html_filename_testcase_ids,
    html_path_testcase_ids,
    is_html_report_path,
    validate_network_evidence_path,
)
from app.services.image_evidence import ImageEvidenceResult
from app.services.review_types import (
    PreparedImageEvidence,
    ReviewContext,
    _append_step_issue,
    _evidence_actions,
    _recalculate_result,
    _routed_paths,
)
from app.services.stage_image_review import (
    _IMAGE_EVIDENCE_ISSUES,
    _image_audit_metadata,
)


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
    temporary_evidence_used = False
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
            manual_summary = (
                "无法可靠确定证据用途。"
                if routing.get("intent") == "uncertain"
                else "引用的外部文件类型暂不支持自动解析，需人工复核。"
            )
            _append_step_issue(
                parsed,
                step_result["review_step"],
                "manual",
                "path",
                manual_summary,
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
                    "images": [_image_audit_metadata(image) for image in evidence.images],
                    **(
                        {"corrupt_images": list(evidence.corrupt_images)}
                        if evidence.corrupt_images else {}
                    ),
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
            if evidence.status == "ready" and evidence.corrupt_images:
                _append_step_issue(
                    parsed, step_result["review_step"], "manual", "screenshot",
                    _IMAGE_EVIDENCE_ISSUES["invalid_image"][2],
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
                    if "load_images" not in actions or path.get("kind") == "html":
                        continue
                    evidence = ImageEvidenceResult(
                        status="not_checked", source_kind="not_read"
                    )
                step_result["image_evidence"].append(
                    {
                        "source_path": path["raw"],
                        "source_kind": evidence.source_kind,
                        "status": evidence.status,
                        **(
                            {"corrupt_images": list(evidence.corrupt_images)}
                            if evidence.corrupt_images else {}
                        ),
                        "images": [
                            _image_audit_metadata(image) for image in evidence.images
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
                if evidence.status == "ready" and evidence.corrupt_images:
                    _append_step_issue(
                        parsed, step_result["review_step"], "manual", "screenshot",
                        _IMAGE_EVIDENCE_ISSUES["invalid_image"][2],
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
            if evidence.source_kind == "local_html_fallback":
                temporary_evidence_used = True
            step_result["html_evidence"].append(
                {
                    "source_path": path["raw"],
                    "source_kind": evidence.source_kind,
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
        external_enabled = bool(
            prepared_evidence and prepared_evidence.external_review_enabled
        )
        if (
            external_enabled
            and readable_html_count == html_path_count
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
            external_enabled
            and results_criterion["status"] == "not_applicable"
        ):
            results_criterion.update(
                status="manual",
                summary="部分自动化 HTML 报告未完成 AI 评审。",
            )
        if external_enabled or results_criterion["status"] != "not_applicable":
            results_criterion["evidence"] = (
                f"已读取 {readable_html_count}/{html_path_count} 个 HTML 文件；"
                f"AI 已评审 {ai_reviewed_html_count} 个报告引用；"
                f"通过的步骤评审 {passed_html_step_count} 个。"
            )
    result["temporary_evidence_used"] = temporary_evidence_used
    return result


def aggregate_review_pipeline(ctx: ReviewContext) -> dict[str, Any]:
    with_equipment = apply_equipment_verdicts(ctx, ctx.text_result)
    return _apply_capability_guards(
        with_equipment,
        ctx.content,
        ctx.evidence_config,
        ctx.evidence,
        ctx.automation_release_assessments,
        equipment_enabled=ctx.equipment_enabled,
    )
