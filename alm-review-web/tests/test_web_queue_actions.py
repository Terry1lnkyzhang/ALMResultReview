from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi import Request
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AiConfig,
    AlmRun,
    EvidenceConfig,
    ManualDecision,
    ReviewJob,
    ReviewResult,
    RunRevision,
    RunStep,
    SyncConfig,
    SyncJob,
    WorkerHeartbeat,
    Workspace,
)
from app.web import (
    _current_sync_job,
    _run_sync_batch_progress,
    cancel_review_jobs,
    create_workspace,
    delete_run,
    import_snapshot,
    process_reviews,
    queue_status,
    retry_failed_reviews,
    review_progress,
    review_run_now,
    save_configuration,
    sync_alm,
    update_workspace_queue,
)


def test_import_snapshot_requires_configured_path(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        monkeypatch.setattr(
            "app.web.get_settings", lambda: SimpleNamespace(import_path=None)
        )

        response = import_snapshot(db)

        assert response.status_code == 303
        assert response.headers["location"] == (
            "/?message=Snapshot%20import%20path%20is%20not%20configured."
            "&message_kind=error"
        )


def test_sync_action_only_creates_a_database_job() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            SyncConfig(
                name="Testing",
                server_url="http://alm.example.test",
                domain="domain",
                project="project",
                folder_id=5172,
            )
        )
        db.commit()

        response = sync_alm(db)

        job = db.scalar(select(SyncJob))
        assert response.status_code == 303
        assert job is not None
        assert job.status == "queued"
        assert job.requested_by == "web"


def test_review_action_only_creates_a_database_job() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = AlmRun(
            run_id=42,
            source_hash="a" * 64,
            review_hash="b" * 64,
            raw_json="{}",
        )
        db.add(run)
        db.flush()
        revision = RunRevision(
            run_id=run.run_id,
            revision_number=1,
            source_hash=run.source_hash,
            review_hash=run.review_hash,
            snapshot_json="{}",
        )
        db.add(revision)
        db.flush()
        run.current_revision_id = revision.id
        db.add(AiConfig(id=1, enabled=True))
        db.commit()

        response = review_run_now(run.run_id, False, db)

        job = db.scalar(select(ReviewJob))
        assert response.status_code == 303
        assert job is not None
        assert job.run_id == run.run_id
        assert job.revision_id == revision.id
        assert job.status == "queued"


def test_delete_run_removes_all_local_run_history() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Project A", slug="project-a")
        db.add(workspace)
        db.flush()
        run = AlmRun(
            run_id=42,
            alm_run_id=155510,
            workspace_id=workspace.id,
            source_hash="a" * 64,
            review_hash="b" * 64,
            raw_json="{}",
        )
        db.add(run)
        db.flush()
        revision = RunRevision(
            run_id=run.run_id,
            revision_number=1,
            source_hash=run.source_hash,
            review_hash=run.review_hash,
            snapshot_json="{}",
        )
        db.add(revision)
        db.flush()
        run.current_revision_id = revision.id
        db.add(RunStep(revision_id=revision.id, step_order=1))
        review_job = ReviewJob(
            workspace_id=workspace.id,
            run_id=run.run_id,
            revision_id=revision.id,
            status="completed",
        )
        db.add(review_job)
        db.flush()
        result = ReviewResult(
            workspace_id=workspace.id,
            job_id=review_job.id,
            run_id=run.run_id,
            revision_id=revision.id,
            prompt_version_id=1,
            source_hash=run.source_hash,
            model_name="test-model",
            verdict="qualified",
        )
        db.add(result)
        db.flush()
        db.add_all(
            (
                ManualDecision(
                    workspace_id=workspace.id,
                    run_id=run.run_id,
                    revision_id=revision.id,
                    review_result_id=result.id,
                    decision="confirmed_qualified",
                    operator="tester",
                    reason="Reviewed",
                    source_hash=run.source_hash,
                    original_ai_verdict="qualified",
                ),
                SyncJob(
                    workspace_id=workspace.id,
                    run_id=run.run_id,
                    status="completed",
                ),
            )
        )
        db.commit()

        response = delete_run(run.run_id, "155510", db)

        assert response.status_code == 303
        assert response.headers["location"].startswith(
            f"/?workspace={workspace.id}&message=Run%20155510"
        )
        for model in (
            AlmRun,
            RunRevision,
            RunStep,
            ReviewJob,
            ReviewResult,
            ManualDecision,
            SyncJob,
        ):
            assert db.scalar(select(model)) is None


def test_delete_run_requires_exact_display_id_confirmation() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = AlmRun(
            run_id=42,
            alm_run_id=155510,
            source_hash="a" * 64,
            review_hash="b" * 64,
            raw_json="{}",
        )
        db.add(run)
        db.commit()

        response = delete_run(run.run_id, "42", db)

        assert response.status_code == 303
        assert response.headers["location"].startswith("/runs/42?message=")
        assert "Nothing%20was%20deleted" in response.headers["location"]
        assert db.get(AlmRun, run.run_id) is not None


def test_delete_run_is_blocked_while_a_job_is_active() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = AlmRun(
            run_id=42,
            alm_run_id=155510,
            source_hash="a" * 64,
            review_hash="b" * 64,
            raw_json="{}",
        )
        db.add(run)
        db.flush()
        db.add(SyncJob(run_id=run.run_id, status="running"))
        db.commit()

        response = delete_run(run.run_id, "155510", db)

        assert response.status_code == 303
        assert response.headers["location"].startswith("/runs/42?message=")
        assert "cannot%20be%20deleted" in response.headers["location"]
        assert db.get(AlmRun, run.run_id) is not None


def test_process_reviews_queues_only_latest_alm_changes(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Project A", slug="project-a")
        db.add_all((workspace, AiConfig(id=1, enabled=True)))
        db.commit()
        called = []

        def queue_latest(_db, workspace_id):
            called.append(workspace_id)
            return SimpleNamespace(
                sync_id=7,
                changed=19,
                queued=9,
                already_reviewed=10,
                already_active=0,
            )

        monkeypatch.setattr("app.web.queue_latest_alm_changes", queue_latest)

        response = process_reviews(db, workspace.id)

        assert called == [workspace.id]
        assert response.status_code == 303
        assert "19%20review-content%20changes" in response.headers["location"]
        assert "9%20queued" in response.headers["location"]


def test_retry_failed_reviews_queues_only_current_failed_runs() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Project A", slug="project-a")
        db.add(workspace)
        db.flush()
        runs = []
        for run_id in (41, 42):
            run = AlmRun(
                workspace_id=workspace.id,
                run_id=run_id,
                run_status="Passed",
                source_hash=str(run_id) * 32,
                review_hash=str(run_id) * 32,
                raw_json="{}",
            )
            db.add(run)
            db.flush()
            revision = RunRevision(
                run_id=run.run_id,
                revision_number=1,
                source_hash=run.source_hash,
                review_hash=run.review_hash,
                snapshot_json="{}",
            )
            db.add(revision)
            db.flush()
            run.current_revision_id = revision.id
            runs.append(run)
        db.add_all(
            (
                AiConfig(id=1, enabled=True),
                ReviewJob(
                    workspace_id=workspace.id,
                    run_id=runs[0].run_id,
                    revision_id=runs[0].current_revision_id,
                    status="failed",
                    attempt_count=3,
                    error_message="AI timeout",
                ),
                ReviewJob(
                    workspace_id=workspace.id,
                    run_id=runs[1].run_id,
                    revision_id=runs[1].current_revision_id,
                    status="completed",
                ),
            )
        )
        db.commit()

        response = retry_failed_reviews(workspace.id, db)

        jobs = db.scalars(select(ReviewJob).order_by(ReviewJob.id)).all()
        assert response.status_code == 303
        assert response.headers["location"].startswith(f"/?workspace={workspace.id}")
        assert [(job.run_id, job.status) for job in jobs] == [
            (41, "failed"),
            (42, "completed"),
            (41, "queued"),
        ]


def test_create_workspace_creates_independent_sync_and_evidence_configs() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        response = create_workspace("Project B", db)

        workspace = db.scalar(select(Workspace).where(Workspace.slug == "project-b"))
        assert workspace is not None
        sync_config = db.scalar(
            select(SyncConfig).where(SyncConfig.workspace_id == workspace.id)
        )
        evidence_config = db.scalar(
            select(EvidenceConfig).where(EvidenceConfig.workspace_id == workspace.id)
        )

        assert response.status_code == 303
        assert sync_config is not None
        assert evidence_config is not None
        assert sync_config.id is not None
        assert evidence_config.id is not None


def test_create_workspace_copy_reuses_source_configuration() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        source = Workspace(
            name="Project A",
            slug="project-a",
            project="earth_kylin",
            equipment_review_enabled=False,
            equipment_area_filter="Lab 3",
            queue_priority=25,
        )
        db.add(source)
        db.flush()
        db.add(
            SyncConfig(
                workspace_id=source.id,
                name="Project A",
                server_url="http://alm.example/qcbin",
                domain="SY",
                project="sy_vnv",
                folder_id=1234,
                folder_path="Testing / A",
                schedule_hour=5,
                schedule_minute=30,
                enabled=True,
                auto_review_after_sync=True,
            )
        )
        db.add(
            EvidenceConfig(
                workspace_id=source.id,
                allowed_network_root="\\\\server\\share",
                local_html_fallback_root="D:\\cache",
                external_evidence_review_enabled=True,
            )
        )
        db.commit()

        create_workspace("Project B", db, mode="copy", source_workspace_id=source.id)

        workspace = db.scalar(select(Workspace).where(Workspace.slug == "project-b"))
        assert workspace is not None
        assert workspace.project == "earth_kylin"
        assert workspace.equipment_review_enabled is False
        assert workspace.equipment_area_filter == "Lab 3"
        assert workspace.queue_priority == 25
        sync_config = db.scalar(
            select(SyncConfig).where(SyncConfig.workspace_id == workspace.id)
        )
        assert sync_config is not None
        assert sync_config.server_url == "http://alm.example/qcbin"
        assert sync_config.domain == "SY"
        assert sync_config.project == "sy_vnv"
        assert sync_config.schedule_hour == 5
        assert sync_config.schedule_minute == 30
        assert sync_config.enabled is False
        assert sync_config.auto_review_after_sync is True
        assert sync_config.folder_id == 0
        assert sync_config.folder_path == ""
        evidence_config = db.scalar(
            select(EvidenceConfig).where(EvidenceConfig.workspace_id == workspace.id)
        )
        assert evidence_config is not None
        assert evidence_config.allowed_network_root == "\\\\server\\share"
        assert evidence_config.local_html_fallback_root == "D:\\cache"
        assert evidence_config.external_evidence_review_enabled is True


def test_pause_review_queue_preserves_existing_jobs() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Project A", slug="project-a")
        db.add(workspace)
        db.flush()
        db.add(
            ReviewJob(
                workspace_id=workspace.id,
                run_id=42,
                revision_id=7,
                status="queued",
            )
        )
        db.commit()

        response = update_workspace_queue(workspace.id, "pause-review", db)

        db.refresh(workspace)
        assert response.status_code == 303
        assert workspace.review_queue_paused
        assert db.scalar(select(ReviewJob)).status == "queued"


def test_cancel_review_jobs_removes_waiting_job_but_not_running_job() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Project A", slug="project-a")
        db.add(workspace)
        db.flush()
        queued_job = ReviewJob(
            workspace_id=workspace.id,
            run_id=41,
            revision_id=1,
            status="queued",
        )
        running_job = ReviewJob(
            workspace_id=workspace.id,
            run_id=42,
            revision_id=2,
            status="running",
        )
        db.add_all((queued_job, running_job))
        db.commit()

        response = cancel_review_jobs(workspace.id, queued_job.id, db)

        remaining = db.scalars(select(ReviewJob)).all()
        assert response.status_code == 303
        assert response.headers["location"].startswith(f"/?workspace={workspace.id}")
        assert [(job.run_id, job.status) for job in remaining] == [(42, "running")]


def test_cancel_all_review_jobs_removes_only_workspace_waiting_jobs() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        first = Workspace(name="Project A", slug="project-a")
        second = Workspace(name="Project B", slug="project-b")
        db.add_all((first, second))
        db.flush()
        db.add_all(
            (
                ReviewJob(
                    workspace_id=first.id,
                    run_id=41,
                    revision_id=1,
                    status="queued",
                ),
                ReviewJob(
                    workspace_id=first.id,
                    run_id=42,
                    revision_id=2,
                    status="running",
                ),
                ReviewJob(
                    workspace_id=second.id,
                    run_id=43,
                    revision_id=3,
                    status="queued",
                ),
            )
        )
        db.commit()

        response = cancel_review_jobs(first.id, None, db)

        remaining = db.scalars(select(ReviewJob).order_by(ReviewJob.run_id)).all()
        assert response.status_code == 303
        assert [(job.run_id, job.status) for job in remaining] == [
            (42, "running"),
            (43, "queued"),
        ]


def test_prioritize_workspace_resumes_review_and_sets_highest_priority() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        normal = Workspace(name="Normal", slug="normal", queue_priority=20)
        urgent = Workspace(
            name="Urgent",
            slug="urgent",
            queue_priority=0,
            review_queue_paused=True,
        )
        db.add_all((normal, urgent))
        db.commit()

        response = update_workspace_queue(urgent.id, "prioritize", db)

        db.refresh(urgent)
        assert response.status_code == 303
        assert not urgent.review_queue_paused
        assert urgent.queue_priority == 30


def test_queue_status_reports_live_counts_concurrency_and_worker(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Project A", slug="project-a")
        db.add(workspace)
        db.flush()
        db.add_all(
            (
                ReviewJob(
                    workspace_id=workspace.id,
                    run_id=41,
                    revision_id=1,
                    status="queued",
                ),
                ReviewJob(
                    workspace_id=workspace.id,
                    run_id=42,
                    revision_id=2,
                    status="running",
                    lease_expires_at=datetime.utcnow() + timedelta(minutes=1),
                ),
                ReviewJob(
                    workspace_id=workspace.id,
                    run_id=43,
                    revision_id=3,
                    status="failed",
                ),
                AiConfig(id=1, enabled=True, review_concurrency=4),
                WorkerHeartbeat(
                    worker_id="worker-one",
                    hostname="test-host",
                    status="idle",
                    last_seen_at=datetime.utcnow(),
                ),
            )
        )
        db.commit()
        monkeypatch.setattr(
            "app.web.get_settings",
            lambda: SimpleNamespace(worker_poll_seconds=5),
        )

        status = queue_status(workspace.id, db)

        assert status == {
            "queued": 1,
            "running": 1,
            "review_paused": False,
            "pending_run_sync": 0,
            "review_concurrency": 4,
                "endpoints": [
                    {
                        "id": 1,
                        "model_name": "qwen3",
                        "enabled": True,
                        "available": True,
                        "health_status": "healthy",
                        "running": 1,
                        "capacity": 4,
                        "last_error": "",
                    }
                ],
            "worker_online": True,
            "worker_status": "working",
            "worker_id": "worker-one",
        }


def test_run_sync_batch_progress_aggregates_the_batch_and_tracks_the_oldest_job() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Project A", slug="project-a")
        db.add(workspace)
        db.commit()
        queued_at = datetime(2026, 8, 25, 7, 26, 18)
        db.add(
            SyncJob(
                workspace_id=workspace.id,
                run_id=100,
                status="completed",
                created_at=queued_at - timedelta(hours=1),
            )
        )
        statuses = ["completed", "completed", "running", "queued", "queued", "failed"]
        for offset, job_status in enumerate(statuses):
            db.add(
                SyncJob(
                    workspace_id=workspace.id,
                    run_id=200 + offset,
                    status=job_status,
                    created_at=queued_at,
                )
            )
        db.commit()

        batch = _run_sync_batch_progress(db, workspace.id)

        assert batch == {
            "active": True,
            "total": 6,
            "done": 2,
            "queued": 2,
            "running": 1,
            "failed": 1,
            "percent": 33,
        }
        # The panel must follow the job the worker is on, not the newest row.
        assert _current_sync_job(db, workspace.id).run_id == 202


def test_run_sync_batch_progress_is_inactive_without_pending_runs() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Project A", slug="project-a")
        db.add(workspace)
        db.commit()
        db.add(SyncJob(workspace_id=workspace.id, run_id=100, status="completed"))
        db.commit()

        assert _run_sync_batch_progress(db, workspace.id)["active"] is False


def test_review_progress_reports_current_workspace_runs(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Project A", slug="project-a")
        db.add(workspace)
        db.commit()
        monkeypatch.setattr(
            "app.web.workspace_review_progress",
            lambda _db, _workspace_id: SimpleNamespace(
                total=10,
                reviewed=7,
                qualified=4,
                force_qualified=1,
                unqualified=2,
                manual=1,
                pending=2,
                review_failed=1,
                warning=3,
                queued=2,
                running=1,
                percent=70.0,
            ),
        )

        progress = review_progress(workspace.id, db)

        assert progress == {
            "available": True,
            "total": 10,
            "reviewed": 7,
            "qualified": 4,
            "force_qualified": 1,
            "unqualified": 2,
            "manual": 1,
            "pending": 2,
            "review_failed": 1,
            "warning": 3,
            "queued": 2,
            "running": 1,
            "percent": 70.0,
        }


def test_configuration_clamps_and_persists_review_concurrency() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Project A", slug="project-a")
        db.add(workspace)
        db.flush()
        db.add(
            SyncConfig(
                workspace_id=workspace.id,
                name="Project A",
                server_url="http://alm.example.test",
                domain="domain",
                project="project",
                folder_id=42,
            )
        )
        db.commit()
        request = Request(
            {
                "type": "http",
                "app": SimpleNamespace(state=SimpleNamespace()),
            }
        )

        def save(
            review_concurrency: int,
            ai_api_key: str = "",
            clear_ai_api_key: bool = False,
            schedule_time: str = "01:30",
            **secondary_config,
        ):
            return save_configuration(
                request=request,
                workspace_id=workspace.id,
                workspace_name="Project A",
                workspace_project="earth_kylin",
                server_url="http://alm.example.test",
                domain="domain",
                project="project",
                folder_id=42,
                folder_path="Root",
                schedule_time=schedule_time,
                sync_enabled=False,
                auto_review_after_sync=True,
                ai_base_url="http://ai.example.test/v1",
                model_name="test-model",
                timeout_seconds=120,
                ai_api_key=ai_api_key,
                clear_ai_api_key=clear_ai_api_key,
                review_concurrency=review_concurrency,
                ai_enabled=True,
                allowed_network_root="",
                local_html_fallback_root="",
                automation_release_project_name="Earth_Kylin",
                external_evidence_review_enabled=False,
                equipment_review_enabled=False,
                equipment_area_filter="",
                review_queue_paused=False,
                sync_queue_paused=False,
                queue_priority=0,
                prompt_name="Test prompt",
                prompt_template="Review {{RUN_CONTENT}}",
                db=db,
                **secondary_config,
            )

        assert save(10, ai_api_key="saved-key").status_code == 303
        ai_config = db.get(AiConfig, 1)
        assert ai_config.review_concurrency == 4
        assert ai_config.api_key == "saved-key"
        assert workspace.project == "earth_kylin"
        sync_config = db.scalar(
            select(SyncConfig).where(SyncConfig.workspace_id == workspace.id)
        )
        assert sync_config is not None
        assert sync_config.auto_review_after_sync is True
        assert sync_config.schedule_hour == 1
        assert sync_config.schedule_minute == 30
        assert save(2, schedule_time="25:00").status_code == 303
        assert sync_config.schedule_hour == 1
        assert sync_config.schedule_minute == 30
        evidence_config = db.scalar(
            select(EvidenceConfig).where(EvidenceConfig.workspace_id == workspace.id)
        )
        assert evidence_config is not None
        assert evidence_config.automation_release_project_name == "Earth_Kylin"
        assert save(0).status_code == 303
        assert ai_config.review_concurrency == 1
        assert ai_config.api_key == "saved-key"
        assert save(
            2,
            clear_ai_api_key=True,
        ).status_code == 303
        assert ai_config.api_key == ""
        assert save(
            2,
            ai_base_url_2="http://ai-two.example.test/v1",
            model_name_2="test-model-two",
            timeout_seconds_2=180,
            ai_api_key_2="secondary-key",
            review_concurrency_2=3,
            ai_enabled_2=True,
        ).status_code == 303
        secondary = db.get(AiConfig, 2)
        assert secondary is not None
        assert secondary.base_url == "http://ai-two.example.test/v1"
        assert secondary.model_name == "test-model-two"
        assert secondary.timeout_seconds == 180
        assert secondary.api_key == "secondary-key"
        assert secondary.review_concurrency == 3
        assert secondary.enabled is True
