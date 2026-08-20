import json
from datetime import timedelta
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AlmRun,
    ReviewJob,
    SyncConfig,
    SyncJob,
    WorkerHeartbeat,
    Workspace,
    utcnow,
)
from app.services import reviews, scheduler, worker_tasks
from app.services.alm import FolderBatch, FolderCollectionProgress
from app.services.reviews import FAILED_JOB_RETRY_BACKOFF_SECONDS, claim_next_review_job
from app.services.skill_runner import SkillFailure
from app.services.worker_tasks import claim_next_sync_job, queue_sync_job
from app.web import sync_progress


def test_review_job_claim_is_exclusive_until_lease_expires() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(ReviewJob(run_id=42, revision_id=7, status="queued"))
        db.commit()

        first_claim = claim_next_review_job(db, "worker-one", lease_seconds=60)

        assert first_claim is not None
        assert first_claim.status == "running"
        assert first_claim.claimed_by == "worker-one"
        assert first_claim.attempt_count == 1
        assert claim_next_review_job(db, "worker-two", lease_seconds=60) is None

        first_claim.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

        recovered_claim = claim_next_review_job(db, "worker-two", lease_seconds=60)

        assert recovered_claim is not None
        assert recovered_claim.id == first_claim.id
        assert recovered_claim.claimed_by == "worker-two"
        assert recovered_claim.attempt_count == 2


def test_review_queue_concurrency_uses_independent_sessions(monkeypatch) -> None:
    sessions = []
    processing_sessions = []
    active_sessions = set()
    maximum_active = 0
    lock = Lock()
    barrier = Barrier(4)
    claimed_job_ids = iter(range(1, 5))

    class FakeSession:
        def __enter__(self):
            sessions.append(self)
            return self

        def __exit__(self, *_args):
            return None

    def claim_job(_db, worker_id, lease_seconds):
        assert worker_id == "worker-one"
        assert lease_seconds == 60
        job_id = next(claimed_job_ids, None)
        return SimpleNamespace(id=job_id) if job_id is not None else None

    def process_claimed(db, job_id):
        nonlocal maximum_active
        processing_sessions.append(db)
        with lock:
            active_sessions.add(id(db))
            maximum_active = max(maximum_active, len(active_sessions))
        barrier.wait(timeout=2)
        with lock:
            active_sessions.remove(id(db))
        return 1, 0

    monkeypatch.setattr(scheduler, "SessionLocal", FakeSession)
    monkeypatch.setattr(scheduler, "claim_next_review_job", claim_job)
    monkeypatch.setattr(scheduler, "process_claimed_review_job", process_claimed)

    completed, failed = scheduler.process_review_queue(
        limit=4,
        concurrency=4,
        worker_id="worker-one",
        lease_seconds=60,
    )

    assert (completed, failed) == (4, 0)
    assert len(processing_sessions) == 4
    assert len({id(session) for session in processing_sessions}) == 4
    assert maximum_active == 4


def test_review_queue_refills_a_free_slot_before_the_batch_finishes(monkeypatch) -> None:
    claimed_job_ids = iter(range(1, 5))
    later_jobs_started = Event()
    finished_fast_jobs = []
    lock = Lock()

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def claim_job(_db, _worker_id, _lease_seconds):
        job_id = next(claimed_job_ids, None)
        return SimpleNamespace(id=job_id) if job_id is not None else None

    def process_claimed(_db, job_id):
        if job_id == 1:
            assert later_jobs_started.wait(timeout=5)
            return 1, 0
        with lock:
            finished_fast_jobs.append(job_id)
            if len(finished_fast_jobs) == 3:
                later_jobs_started.set()
        return 1, 0

    monkeypatch.setattr(scheduler, "SessionLocal", FakeSession)
    monkeypatch.setattr(scheduler, "claim_next_review_job", claim_job)
    monkeypatch.setattr(scheduler, "process_claimed_review_job", process_claimed)

    completed, failed = scheduler.process_review_queue(
        limit=4,
        concurrency=2,
        worker_id="worker-one",
        lease_seconds=60,
    )

    assert (completed, failed) == (4, 0)
    assert finished_fast_jobs == [2, 3, 4]


def test_failed_review_job_is_retried_only_after_the_backoff() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        job = ReviewJob(
            run_id=42,
            revision_id=7,
            status="failed",
            attempt_count=1,
            completed_at=utcnow(),
        )
        db.add(job)
        db.commit()

        assert claim_next_review_job(db, "worker-one", lease_seconds=60) is None

        job.completed_at = utcnow() - timedelta(
            seconds=FAILED_JOB_RETRY_BACKOFF_SECONDS + 1
        )
        db.commit()

        retried = claim_next_review_job(db, "worker-one", lease_seconds=60)

        assert retried is not None
        assert retried.attempt_count == 2


def test_terminal_review_failure_is_not_retried(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(ReviewJob(run_id=42, revision_id=7, status="running", attempt_count=1))
        db.commit()

        def fail_terminally(_db, _job):
            raise SkillFailure("Model output violates the contract.", retryable=False)

        monkeypatch.setattr(reviews, "process_job", fail_terminally)

        assert reviews.process_claimed_review_job(db, 1) == (0, 1)

        job = db.get(ReviewJob, 1)
        assert job.status == "failed"
        assert job.attempt_count == reviews.MAX_REVIEW_JOB_ATTEMPTS
        assert claim_next_review_job(db, "worker-one", lease_seconds=60) is None


def test_single_run_refresh_reimports_that_run_and_queues_its_review(monkeypatch) -> None:
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
                domain="global",
                project="project-a",
                folder_id=42,
            )
        )
        db.add(
            AlmRun(
                run_id=1,
                workspace_id=workspace.id,
                alm_run_id=7001,
                test_instance_id=91,
                folder_id=42,
                folder_path="Project A",
                run_status="Passed",
                source_hash="stale",
                review_hash="stale",
                raw_json="{}",
            )
        )
        db.commit()

        refresh = queue_sync_job(db, "web", workspace.id, run_id=1)
        assert refresh.created
        assert refresh.job.run_id == 1
        # A single-Run refresh must not occupy the workspace-wide sync slot.
        assert queue_sync_job(db, "web", workspace.id).created

        collected: dict[str, object] = {}

        def stub_collect_run(_config, test_instance_id, folder_id, folder_path):
            collected.update(
                test_instance_id=test_instance_id,
                folder_id=folder_id,
                folder_path=folder_path,
            )
            return {
                "users": [],
                "records": [
                    {
                        "folder": {"id": "42", "path": "Project A"},
                        "testSet": {"id": "5", "name": "Set", "folderPath": "Project A"},
                        "testInstance": {"id": "91", "test-id": "3"},
                        "testOwner": "tester",
                        "run": {
                            "id": "7001",
                            "status": "Passed",
                            "testcycl-id": "91",
                            "last-modified": "2026-08-20 10:00:00",
                            "steps": [{"id": "1", "step-order": "1", "status": "Passed"}],
                        },
                    }
                ],
            }

        monkeypatch.setattr(worker_tasks, "collect_run", stub_collect_run)

        claimed = claim_next_sync_job(db, "worker-one", lease_seconds=60)
        assert claimed is not None and claimed.id == refresh.job.id

        worker_tasks.process_sync_job(db, claimed)

        assert collected == {
            "test_instance_id": 91,
            "folder_id": "42",
            "folder_path": "Project A",
        }
        run = db.get(AlmRun, 1)
        assert run.current_revision_id is not None
        assert claimed.status == "completed"
        assert claimed.active_key is None
        review_job = db.scalar(select(ReviewJob).where(ReviewJob.run_id == 1))
        assert review_job is not None
        assert review_job.status == "queued"
        assert review_job.revision_id == run.current_revision_id


def test_abandoned_sync_job_is_reaped_so_a_new_one_can_be_queued() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Project A", slug="project-a")
        db.add(workspace)
        db.flush()
        abandoned = SyncJob(
            workspace_id=workspace.id,
            active_key=f"alm-sync:{workspace.id}",
            status="running",
            attempt_count=worker_tasks.MAX_JOB_ATTEMPTS,
            claimed_by="worker-one",
            lease_expires_at=utcnow() - timedelta(seconds=1),
        )
        db.add(abandoned)
        db.commit()

        result = queue_sync_job(db, "web", workspace.id)

        assert result.created
        assert result.job.id != abandoned.id
        assert abandoned.status == "failed"
        assert abandoned.active_key is None
        assert abandoned.completed_at is not None


def test_sync_queue_deduplicates_and_recovers_an_expired_lease() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        first_request = queue_sync_job(db, "web")
        duplicate_request = queue_sync_job(db, "scheduler")

        assert first_request.created
        assert not duplicate_request.created
        assert duplicate_request.job.id == first_request.job.id

        first_claim = claim_next_sync_job(db, "worker-one", lease_seconds=60)
        assert first_claim is not None
        assert first_claim.claimed_by == "worker-one"
        assert claim_next_sync_job(db, "worker-two", lease_seconds=60) is None

        first_claim.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
        recovered_claim = claim_next_sync_job(db, "worker-two", lease_seconds=60)

        assert recovered_claim is not None
        assert recovered_claim.id == first_claim.id
        assert recovered_claim.attempt_count == 2
        assert db.query(SyncJob).count() == 1


def test_sync_jobs_are_deduplicated_per_workspace() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        first_workspace = Workspace(name="Project A", slug="project-a")
        second_workspace = Workspace(name="Project B", slug="project-b")
        db.add_all((first_workspace, second_workspace))
        db.flush()

        first = queue_sync_job(db, "web", first_workspace.id)
        first_duplicate = queue_sync_job(db, "scheduler", first_workspace.id)
        second = queue_sync_job(db, "web", second_workspace.id)

        assert first.created
        assert not first_duplicate.created
        assert second.created
        assert first.job.id != second.job.id
        assert {job.workspace_id for job in db.query(SyncJob).all()} == {
            first_workspace.id,
            second_workspace.id,
        }


def test_review_claim_skips_paused_workspace_and_prefers_priority() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        paused = Workspace(
            name="Paused",
            slug="paused",
            review_queue_paused=True,
            queue_priority=100,
        )
        normal = Workspace(name="Normal", slug="normal", queue_priority=0)
        urgent = Workspace(name="Urgent", slug="urgent", queue_priority=50)
        db.add_all((paused, normal, urgent))
        db.flush()
        db.add_all(
            (
                ReviewJob(
                    workspace_id=paused.id,
                    run_id=1,
                    revision_id=1,
                    status="queued",
                ),
                ReviewJob(
                    workspace_id=normal.id,
                    run_id=2,
                    revision_id=2,
                    status="queued",
                ),
                ReviewJob(
                    workspace_id=urgent.id,
                    run_id=3,
                    revision_id=3,
                    status="queued",
                ),
            )
        )
        db.commit()

        claimed = claim_next_review_job(db, "worker-one", lease_seconds=60)

        assert claimed is not None
        assert claimed.workspace_id == urgent.id


def test_sync_claim_skips_paused_workspace_and_prefers_priority() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        paused = Workspace(
            name="Paused",
            slug="paused",
            sync_queue_paused=True,
            queue_priority=100,
        )
        normal = Workspace(name="Normal", slug="normal", queue_priority=0)
        urgent = Workspace(name="Urgent", slug="urgent", queue_priority=50)
        db.add_all((paused, normal, urgent))
        db.flush()
        db.add_all(
            (
                SyncJob(workspace_id=paused.id, status="queued"),
                SyncJob(workspace_id=normal.id, status="queued"),
                SyncJob(workspace_id=urgent.id, status="queued"),
            )
        )
        db.commit()

        claimed = claim_next_sync_job(db, "worker-one", lease_seconds=60)

        assert claimed is not None
        assert claimed.workspace_id == urgent.id


def test_interrupted_sync_resumes_from_the_stored_cursor(monkeypatch) -> None:
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
                domain="global",
                project="project-a",
                folder_id=42,
            )
        )
        db.commit()
        queued = queue_sync_job(db, "web", workspace.id)
        claim_next_sync_job(db, "worker-one", lease_seconds=60)

        def interrupted_batches(config, progress_callback, **_kwargs):
            yield FolderBatch(folder_id="1", folder_path="A")
            raise RuntimeError("Worker stopped")

        monkeypatch.setattr(worker_tasks, "iter_folder_batches", interrupted_batches)
        with pytest.raises(RuntimeError):
            worker_tasks.process_sync_job(db, queued.job)

        assert json.loads(queued.job.cursor_json) == ["1"]

        resumed_with: dict[str, list[str]] = {}

        def resumed_batches(config, progress_callback, completed_folder_ids=(), **_kw):
            resumed_with["skipped"] = list(completed_folder_ids)
            yield FolderBatch(folder_id="2", folder_path="B")

        monkeypatch.setattr(worker_tasks, "iter_folder_batches", resumed_batches)
        queued.job.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
        resumed = claim_next_sync_job(db, "worker-one", lease_seconds=60)
        assert resumed is not None

        worker_tasks.process_sync_job(db, resumed)

        assert resumed_with["skipped"] == ["1"]
        assert queued.job.cursor_json is None
        assert queued.job.status == "completed"


def test_sync_progress_refreshes_worker_heartbeat(monkeypatch) -> None:
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
                domain="global",
                project="project-a",
                folder_id=42,
            )
        )
        db.commit()
        queued = queue_sync_job(db, "web", workspace.id)
        job = claim_next_sync_job(db, "worker-one", lease_seconds=60)
        assert job is not None

        def batches_with_progress(
            config,
            progress_callback,
            completed_folder_ids=(),
            is_unchanged_run=None,
        ):
            progress_callback(
                FolderCollectionProgress(
                    stage="collecting",
                    message="Project A / Folder 1",
                    folders_discovered=2,
                    folders_processed=1,
                ),
                True,
            )
            yield FolderBatch(folder_id="42", folder_path="Project A")

        monkeypatch.setattr(worker_tasks, "iter_folder_batches", batches_with_progress)

        worker_tasks.process_sync_job(db, queued.job)

        heartbeat = db.get(WorkerHeartbeat, "worker-one")
        assert heartbeat is not None
        assert heartbeat.status == "working"
        assert heartbeat.current_job_type == "sync"
        assert heartbeat.current_job_id == queued.job.id
        assert queued.job.cursor_json is None
        assert queued.job.status == "completed"


def test_sync_progress_returns_latest_job_for_selected_workspace() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        first_workspace = Workspace(name="Project A", slug="project-a")
        second_workspace = Workspace(name="Project B", slug="project-b")
        db.add_all((first_workspace, second_workspace))
        db.flush()
        db.add_all(
            (
                SyncJob(
                    workspace_id=first_workspace.id,
                    status="running",
                    progress_stage="collecting",
                    progress_message="Project A / Folder 3",
                    folders_discovered=5,
                    folders_processed=3,
                    test_sets_discovered=8,
                    runs_discovered=13,
                ),
                SyncJob(
                    workspace_id=second_workspace.id,
                    status="running",
                    progress_stage="collecting",
                    progress_message="Project B / Folder 9",
                    folders_discovered=12,
                    folders_processed=9,
                    test_sets_discovered=21,
                    runs_discovered=34,
                ),
            )
        )
        db.commit()

        progress = sync_progress(first_workspace.id, db)

        assert progress["status"] == "running"
        assert progress["message"] == "Project A / Folder 3"
        assert progress["folders_processed"] == 3
        assert progress["runs_discovered"] == 13


def test_sync_progress_identifies_running_job_without_live_counts() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Project A", slug="project-a")
        db.add(workspace)
        db.flush()
        db.add(SyncJob(workspace_id=workspace.id, status="running"))
        db.commit()

        progress = sync_progress(workspace.id, db)

        assert progress["stage"] == "collecting"
        assert "next synchronization" in progress["message"]