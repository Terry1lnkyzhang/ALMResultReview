from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AiConfig,
    AlmRun,
    EvidenceConfig,
    ReviewJob,
    RunRevision,
    SyncConfig,
    SyncJob,
    Workspace,
)
from app.web import create_workspace, import_snapshot, review_run_now, sync_alm


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