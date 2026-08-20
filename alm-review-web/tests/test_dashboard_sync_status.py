from datetime import datetime

from app.web import templates


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
    assert "42 Passed Runs selected" in rendered


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