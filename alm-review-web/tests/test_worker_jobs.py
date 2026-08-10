from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import ReviewJob, SyncJob, utcnow
from app.services.reviews import claim_next_review_job
from app.services.worker_tasks import claim_next_sync_job, queue_sync_job


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