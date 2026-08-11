from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import ReviewJob, SyncJob, Workspace, utcnow
from app.services.reviews import claim_next_review_job
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