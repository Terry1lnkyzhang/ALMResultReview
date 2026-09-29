from datetime import datetime
from types import SimpleNamespace

import pytest

from app.web import templates


def _idle_run_sync_batch() -> dict[str, int | bool]:
    return {
        "active": False,
        "total": 0,
        "done": 0,
        "queued": 0,
        "running": 0,
        "failed": 0,
        "percent": 0,
    }


def test_dashboard_uses_workspace_review_progress_and_latest_alm_action() -> None:
    template = templates.get_template("dashboard.html")
    source, _, _ = templates.env.loader.get_source(templates.env, template.name)

    assert "Review new &amp; changed Runs" in source
    assert "Re-run review" in source
    assert "AI REVIEW" in source
    assert "data-queue-status" in source
    assert "queue-strip" not in source
    assert "data-progress-qualified" not in source
    assert "LATEST RE-REVIEW" not in source


def test_dashboard_workload_lists_every_tester() -> None:
    template = templates.get_template("dashboard.html")
    source, _, _ = templates.env.loader.get_source(templates.env, template.name)

    assert "for code1_id, label, count in tester_counts %}" in source
    assert "tester_counts[:10]" not in source


def test_dashboard_reports_failed_reviews_per_run_not_per_job() -> None:
    template = templates.get_template("dashboard.html")
    source, _, _ = templates.env.loader.get_source(templates.env, template.name)

    assert "review_job_counts.get('failed'" not in source
    assert "Retry {{ review_progress.review_failed }} failed Runs" in source
    assert "<strong data-review-failed>{{ review_progress.review_failed or 0 }}</strong>" in source


def test_dashboard_shows_completed_sync_time() -> None:
    template = templates.get_template("dashboard.html")
    completed_at = datetime(2026, 8, 14, 16, 25, 30)

    rendered = template.render(
        request=type(
            "Request",
            (),
            {"url_for": lambda self, name, **params: f"/static{params['path']}"},
        )(),
        current_workspace=type("Workspace", (), {"id": 7, "name": "Project A"})(),
        workspaces=[],
        worker_heartbeat=None,
        worker_online=False,
        latest_sync_job=type(
            "SyncJob",
            (),
            {
                "status": "completed",
                "completed_at": completed_at,
                "runs_discovered": 42,
                "error_message": "",
            },
        )(),
        sync_display_at=completed_at,
        active_sync_job=None,
        run_sync_batch=_idle_run_sync_batch(),
        review_job_counts={},
        review_update_count=0,
        review_progress=type("Progress", (), {"total": 0})(),
        total_runs=0,
        status_counts={},
        tester_counts=[],
        owner_counts=[],
        max_tester_count=1,
        max_owner_count=1,
        selected_status="all",
        selected_tester="all",
        selected_owner="all",
        query="",
        status_labels={},
        runs=[],
    )

    assert "data-sync-status>COMPLETED<" in rendered
    assert "Completed 2026-08-14 16:25:30" in rendered
    assert "42 Passed/Failed Runs selected" in rendered


def test_dashboard_distinguishes_queued_sync_from_review_jobs() -> None:
    template = templates.get_template("dashboard.html")
    queued_sync = type(
        "SyncJob",
        (),
        {
            "id": 4,
            "status": "queued",
            "progress_stage": "queued",
            "progress_message": "",
            "folders_processed": 0,
            "folders_discovered": 0,
            "test_sets_discovered": 0,
            "runs_discovered": 0,
        },
    )()

    rendered = template.render(
        request=type(
            "Request",
            (),
            {"url_for": lambda self, name, **params: f"/static{params['path']}"},
        )(),
        current_workspace=type("Workspace", (), {"id": 5, "name": "Kunpeng0810"})(),
        workspaces=[],
        worker_heartbeat=type("Heartbeat", (), {"status": "working", "worker_id": "YY292380"})(),
        worker_online=False,
        latest_sync_job=queued_sync,
        sync_display_at=None,
        active_sync_job=queued_sync,
        run_sync_batch=_idle_run_sync_batch(),
        review_job_counts={"queued": 1},
        review_update_count=0,
        review_progress=type("Progress", (), {"total": 0})(),
        total_runs=0,
        status_counts={},
        tester_counts=[],
        owner_counts=[],
        max_tester_count=1,
        max_owner_count=1,
        selected_status="all",
        selected_tester="all",
        selected_owner="all",
        query="",
        status_labels={},
        runs=[],
    )

    assert "data-sync-status>QUEUED<" in rendered
    assert "<strong data-review-queued>1</strong> queued" in rendered
    assert "Worker is offline; queued jobs will wait." in rendered


@pytest.mark.parametrize(
    ("schedule", "expected_text", "excluded_text"),
    [
        (
            SimpleNamespace(
                enabled=True, auto_review_after_sync=True,
                schedule_hour=22, schedule_minute=30,
            ),
            "每天 22:30（Asia/Shanghai）自动同步",
            "当前工作区未启用每日自动同步",
        ),
        (
            SimpleNamespace(
                enabled=True, auto_review_after_sync=False,
                schedule_hour=2, schedule_minute=5,
            ),
            "当前没有开启定时同步后的自动 AI 评审",
            "定时同步完成后还会自动排入符合条件的 AI 评审",
        ),
        (
            None,
            "当前工作区未启用每日自动同步",
            "每天 22:30（Asia/Shanghai）自动同步",
        ),
        (
            SimpleNamespace(
                enabled=False, auto_review_after_sync=False,
                schedule_hour=22, schedule_minute=30,
            ),
            "当前工作区未启用每日自动同步",
            "每天 22:30（Asia/Shanghai）自动同步",
        ),
    ],
)
def test_workspace_sync_requires_two_explanations_and_shows_real_schedule(
    schedule: SimpleNamespace | None, expected_text: str, excluded_text: str
) -> None:
    template = templates.get_template("dashboard.html")
    source, _, _ = templates.env.loader.get_source(templates.env, template.name)
    assert 'data-confirm-workspace-sync="incremental"' in source
    assert 'data-confirm-workspace-sync="full"' in source
    assert 'name="full_refresh" value="true"' in source
    assert "explanation.showModal()" in source
    assert "warning.showModal()" in source
    assert "form.requestSubmit()" in source
    assert "onsubmit=\"return confirm('Re-read every Run" not in source

    rendered = template.render(
        request=SimpleNamespace(url_for=lambda name, **params: f"/static{params['path']}"),
        current_workspace=SimpleNamespace(id=7, name="Project A"),
        dashboard_sync_config=schedule,
        app_timezone="Asia/Shanghai",
        workspaces=[],
        worker_heartbeat=None,
        worker_online=False,
        latest_sync_job=None,
        sync_display_at=None,
        active_sync_job=None,
        run_sync_batch=_idle_run_sync_batch(),
        review_job_counts={},
        review_update_count=0,
        review_progress=SimpleNamespace(total=0),
        total_runs=0,
        status_counts={},
        tester_counts=[],
        owner_counts=[],
        max_tester_count=1,
        max_owner_count=1,
        selected_status="all",
        selected_tester="all",
        selected_owner="all",
        query="",
        status_labels={},
        runs=[],
    )
    assert expected_text in rendered
    assert excluded_text not in rendered
    assert "增量同步整个范围" in rendered
    assert "重新读取整个范围" in rendered
    assert "不建议在白天工作高峰启动" in rendered
    assert "先在下方 Runs 按 Actual tester 等条件筛选" in rendered
    assert "只刷新筛选出的 Run 并评审" in rendered
    assert "仅评审筛选出的本地 Run，不重新读取 ALM" in rendered
    assert "资源占用 · 第二步" in rendered