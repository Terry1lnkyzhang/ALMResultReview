from pathlib import Path

import app.web as web
from app.web import templates


def test_configuration_exposes_skill_and_capability_controls() -> None:
    template = templates.get_template("configuration.html")
    source, _, _ = templates.env.loader.get_source(
        templates.env,
        template.name,
    )

    assert 'name="screenshot_terms"' not in source
    assert 'name="screenshot_exclusion_terms"' not in source
    assert 'name="ai_intent_classification_enabled"' not in source
    assert 'name="evidence_intent_skill_shadow_enabled"' not in source
    assert 'name="evidence_intent_confidence_threshold"' not in source
    assert 'name="specialist_reviews_enabled"' not in source
    assert "Enable downstream specialist reviews" not in source
    assert "Evidence candidate detection" not in source
    assert 'name="external_evidence_review_enabled"' in source
    assert "Enable external evidence review" in source
    assert "Directly controls approved HTML parsing" in source
    assert "Directly controls registry, calibration" in source
    assert "Read approved network evidence" not in source
    assert "Send evidence images to AI" not in source
    assert "Allow image transfer over HTTP" not in source
    assert "Enforced safety boundaries" in source
    assert "Enable AI Review processing" in source
    assert "It does not queue Reviews by itself" in source
    assert 'name="auto_review_after_sync"' in source
    assert "Auto-review after scheduled sync" in source
    assert "new or changed ALM Runs" in source
    assert "Review update recommended" in source
    assert 'type="time"' in source
    assert 'name="schedule_time"' in source
    assert 'name="schedule_hour"' not in source
    assert 'name="schedule_minute"' not in source
    assert "app_timezone" in source
    assert "Review processing &amp; Skills" in source
    assert "Global processing" in source
    assert "Workspace queue" in source
    assert "Skill capabilities" in source
    assert "Skill catalog" in source
    assert "skill_catalog" in source
    assert "required_capabilities" in source
    assert "skill.status == 'planned'" in source


def test_run_detail_exposes_image_stage_and_evidence_routing_trace() -> None:
    template = templates.get_template("run_detail.html")
    source, _, _ = templates.env.loader.get_source(
        templates.env,
        template.name,
    )

    assert "('image_review', '图像评审')" in source
    assert "证据路由记录" in source
    assert "匹配条件" in source
    assert "'html_report_equipment_id': 'HTML 报告设备 ID'" in source
    assert "route.get('actions', [])" in source
    assert "stage.get('steps')|length" in source
    assert "Evidence intent Skill" not in source
    assert "skill_shadow" not in source
    assert "Skill 执行记录" in source
    assert "review_skill_traces" in source
    assert "trace.get('capabilities', {}).get('granted', [])" in source
    assert "step_review.get('image_evidence', [])" in source
    assert "图像证据审计" in source
    assert "ALM 附件" in source
    assert "SHA-256" in source

    web_source = Path(web.__file__).read_text(encoding="utf-8")
    assert '"review_step_results": review_step_results' in web_source


def test_run_detail_exposes_guarded_local_delete_action() -> None:
    template = templates.get_template("run_detail.html")
    source, _, _ = templates.env.loader.get_source(
        templates.env,
        template.name,
    )

    assert 'action="/runs/{{ run.run_id }}/delete"' in source
    assert 'name="confirmation"' in source
    assert "data-confirm-run-id" in source
    assert "删除本地运行" in source
    assert "如果 ALM 中仍存在该运行" in source
    assert "data-delete-run-dialog" in source
    assert "dialog.showModal()" in source
    assert "window.prompt" not in source


def test_run_detail_keeps_review_job_status_and_errors_visible() -> None:
    template = templates.get_template("run_detail.html")
    source, _, _ = templates.env.loader.get_source(
        templates.env,
        template.name,
    )

    assert "latest_review_job.status == 'failed'" in source
    assert "latest_review_job_error" in source
    assert "最近一次评审没有生成新结果" in source
    assert "正在排队" in source
    assert "离开页面不会取消任务" in source
    assert "已加入评审队列" in source


def test_public_job_error_redacts_api_key_identifiers() -> None:
    error = (
        "Rate limit exceeded for api_key: "
        "48fc76828c923d2b8ffc483b8ccc5bd8ca48178ede53fd10a13158dabf5924ec. "
        "Current limit: 5"
    )

    public_error = web._public_job_error(error)

    assert public_error is not None
    assert "48fc7682" not in public_error
    assert "api_key: [REDACTED]. Current limit: 5" in public_error


def test_run_detail_uses_chinese_review_labels_without_changing_internal_codes() -> None:
    template = templates.get_template("run_detail.html")
    source, _, _ = templates.env.loader.get_source(
        templates.env,
        template.name,
    )

    assert "'qualified': '合格'" in source
    assert "'needs_manual_review': '需人工复核'" in source
    assert "'html_report': 'HTML 报告'" in source
    assert "评审准则" in source
    assert "执行证据" in source
    assert "Description（描述）" in source
    assert "step_review.get('status', 'pass')" in source
    assert "status-{{ review.final_status }}" in source