from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

LongText = Text().with_variant(LONGTEXT(), "mysql")


def utcnow() -> datetime:
    return datetime.utcnow()


class AlmRun(Base):
    __tablename__ = "alm_runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "alm_run_id", name="uq_workspace_alm_run"),
    )

    run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    workspace_id: Mapped[int | None] = mapped_column(Integer, index=True)
    alm_run_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    test_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    test_instance_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    test_set_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    folder_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    test_name: Mapped[str] = mapped_column(String(512), default="")
    test_set_name: Mapped[str] = mapped_column(String(512), default="")
    folder_path: Mapped[str] = mapped_column(String(1500), default="")
    run_status: Mapped[str] = mapped_column(String(64), default="")
    test_owner: Mapped[str] = mapped_column(String(128), default="")
    assigned_tester: Mapped[str] = mapped_column(String(128), default="")
    actual_tester: Mapped[str] = mapped_column(String(128), default="", index=True)
    execution_at: Mapped[datetime | None] = mapped_column(DateTime)
    alm_last_modified: Mapped[datetime | None] = mapped_column(DateTime)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    review_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    current_revision_id: Mapped[int | None] = mapped_column(Integer, index=True)
    raw_json: Mapped[str] = mapped_column(LongText, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class AlmUser(Base):
    __tablename__ = "alm_users"

    code1_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    email: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class RunRevision(Base):
    __tablename__ = "run_revisions"
    __table_args__ = (
        UniqueConstraint("run_id", "revision_number", name="uq_run_revision_number"),
        UniqueConstraint("run_id", "source_hash", name="uq_run_revision_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("alm_runs.run_id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    review_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_last_modified: Mapped[datetime | None] = mapped_column(DateTime)
    snapshot_json: Mapped[str] = mapped_column(LongText, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class RunStep(Base):
    __tablename__ = "run_steps"
    __table_args__ = (UniqueConstraint("revision_id", "step_id", name="uq_revision_step"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("run_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_id: Mapped[int | None] = mapped_column(BigInteger)
    step_order: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str] = mapped_column(LongText, default="")
    expected: Mapped[str] = mapped_column(LongText, default="")
    actual: Mapped[str] = mapped_column(LongText, default="")


class PromptVersion(Base):
    __tablename__ = "prompt_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    template: Mapped[str] = mapped_column(LongText, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class AiConfig(Base):
    __tablename__ = "ai_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    base_url: Mapped[str] = mapped_column(
        String(1000), default="http://161.92.92.153:6000/v1/chat/completions"
    )
    model_name: Mapped[str] = mapped_column(String(255), default="qwen3")
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class EvidenceConfig(Base):
    __tablename__ = "evidence_configs"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_evidence_config_workspace"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int | None] = mapped_column(Integer, index=True)
    allowed_network_root: Mapped[str] = mapped_column(String(1500), default="")
    local_html_fallback_root: Mapped[str] = mapped_column(String(1500), default="")
    network_evidence_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    image_review_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    allow_insecure_image_transport: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class EquipmentImportHistory(Base):
    __tablename__ = "equipment_import_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    sheet_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    inserted_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unchanged_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class EquipmentRegistry(Base):
    __tablename__ = "equipment_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equipment_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(512), default="", nullable=False, index=True)
    manufacturer: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    model_number: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    accuracy_class: Mapped[str] = mapped_column(Text, default="", nullable=False)
    measurement_range: Mapped[str] = mapped_column(Text, default="", nullable=False)
    serial_number: Mapped[str] = mapped_column(String(255), default="", nullable=False, index=True)
    calibration_date: Mapped[date | None] = mapped_column(Date)
    calibration_due_date: Mapped[date | None] = mapped_column(Date, index=True)
    received_date: Mapped[date | None] = mapped_column(Date)
    calibration_interval: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    subordinate_area: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    equipment_user: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    equipment_status: Mapped[str] = mapped_column(
        String(255), default="", nullable=False, index=True
    )
    calibration_location: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    instruction_number: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    source_filename: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    source_sheet: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    source_row: Mapped[int | None] = mapped_column(Integer)
    source_import_id: Mapped[int | None] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class ReviewJob(Base):
    __tablename__ = "review_jobs"
    __table_args__ = (Index("ix_review_jobs_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int | None] = mapped_column(Integer, index=True)
    batch_id: Mapped[str | None] = mapped_column(String(36), index=True)
    run_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("run_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="")
    claimed_by: Mapped[str | None] = mapped_column(String(255), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class SyncJob(Base):
    __tablename__ = "sync_jobs"
    __table_args__ = (
        Index("ix_sync_jobs_status_created", "status", "created_at"),
        UniqueConstraint("active_key", name="uq_sync_jobs_active_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int | None] = mapped_column(Integer, index=True)
    active_key: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), default="web", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="")
    progress_stage: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    progress_message: Mapped[str] = mapped_column(String(1500), default="", nullable=False)
    folders_discovered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    folders_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    test_sets_discovered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    runs_discovered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(255), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="idle", nullable=False)
    current_job_type: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    current_job_id: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ReviewResult(Base):
    __tablename__ = "review_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int | None] = mapped_column(Integer, index=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("review_jobs.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    run_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    revision_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    prompt_version_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    review_policy_key: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    issue_summary: Mapped[str] = mapped_column(LongText, default="")
    criteria_json: Mapped[str | None] = mapped_column(LongText)
    step_results_json: Mapped[str | None] = mapped_column(LongText)
    warnings_json: Mapped[str | None] = mapped_column(LongText)
    raw_response: Mapped[str] = mapped_column(LongText, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ManualDecision(Base):
    __tablename__ = "manual_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int | None] = mapped_column(Integer, index=True)
    run_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    revision_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    review_result_id: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    operator: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    original_ai_verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class SyncConfig(Base):
    __tablename__ = "sync_configs"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_sync_config_workspace"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int | None] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    server_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    project: Mapped[str] = mapped_column(String(255), nullable=False)
    folder_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    folder_path: Mapped[str] = mapped_column(String(1500), default="")
    schedule_hour: Mapped[int] = mapped_column(Integer, default=2)
    schedule_minute: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    equipment_review_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    equipment_area_filter: Mapped[str] = mapped_column(
        String(255), default="", nullable=False
    )
    legacy_policy_adopted: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SyncHistory(Base):
    __tablename__ = "sync_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int | None] = mapped_column(Integer, index=True)
    sync_config_id: Mapped[int | None] = mapped_column(Integer, index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    discovered_runs: Mapped[int] = mapped_column(Integer, default=0)
    new_runs: Mapped[int] = mapped_column(Integer, default=0)
    changed_runs: Mapped[int] = mapped_column(Integer, default=0)
    unchanged_runs: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)