from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import EvidenceConfig, SyncConfig, Workspace


def workspace_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().casefold()).strip("-")
    return slug or "workspace"


def default_workspace(db: Session) -> Workspace:
    workspace = db.scalar(
        select(Workspace)
        .where(Workspace.archived.is_(False))
        .order_by(Workspace.id)
        .limit(1)
    )
    if workspace is None:
        workspace = Workspace(
            name="Testing",
            slug="testing",
            equipment_review_enabled=True,
            equipment_area_filter="",
            archived=False,
        )
        db.add(workspace)
        db.flush()
    return workspace


def resolve_workspace(db: Session, workspace_id: int | None = None) -> Workspace:
    if not isinstance(workspace_id, int):
        workspace_id = None
    workspace = db.get(Workspace, workspace_id) if workspace_id is not None else None
    if workspace is None:
        workspace = default_workspace(db)
    return workspace


def workspace_sync_config(db: Session, workspace_id: int) -> SyncConfig | None:
    config = db.scalar(
        select(SyncConfig)
        .where(SyncConfig.workspace_id == workspace_id)
        .order_by(SyncConfig.id)
        .limit(1)
    )
    if config is None:
        config = db.scalar(
            select(SyncConfig)
            .where(SyncConfig.workspace_id.is_(None))
            .order_by(SyncConfig.id)
            .limit(1)
        )
        if config is not None:
            config.workspace_id = workspace_id
    return config


def workspace_evidence_config(db: Session, workspace_id: int) -> EvidenceConfig | None:
    config = db.scalar(
        select(EvidenceConfig)
        .where(EvidenceConfig.workspace_id == workspace_id)
        .order_by(EvidenceConfig.id)
        .limit(1)
    )
    if config is None:
        config = db.scalar(
            select(EvidenceConfig)
            .where(EvidenceConfig.workspace_id.is_(None))
            .order_by(EvidenceConfig.id)
            .limit(1)
        )
    return config


def next_internal_run_id(db: Session, preferred_id: int) -> int:
    if db.get_bind().dialect.name != "sqlite":
        db.flush()
    if db.get_bind().dialect.name == "sqlite" or preferred_id > 0:
        from app.models import AlmRun

        if db.get(AlmRun, preferred_id) is None:
            return preferred_id
        largest = db.scalar(select(func.max(AlmRun.run_id))) or 0
        return max(largest + 1, preferred_id + 1)
    return preferred_id