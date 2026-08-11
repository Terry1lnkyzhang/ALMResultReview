from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import desc, select, update
from sqlalchemy.orm import Session

from app.models import (
    AiConfig,
    AlmRun,
    EquipmentRegistry,
    PromptVersion,
    ReviewResult,
    Workspace,
)
from app.services.workspaces import resolve_workspace, workspace_evidence_config

# Bump this whenever deterministic review preprocessing or guard behavior changes.
REVIEW_ENGINE_VERSION = "2026.08.07.1"
_APP_DIRECTORY = Path(__file__).resolve().parents[1]
_REVIEW_POLICY_FILES = (
    _APP_DIRECTORY / "hashing.py",
    _APP_DIRECTORY / "services" / "evidence.py",
    _APP_DIRECTORY / "services" / "equipment_review.py",
    _APP_DIRECTORY / "services" / "image_evidence.py",
    _APP_DIRECTORY / "services" / "reviews.py",
)


def _review_implementation_hash() -> str:
    digest = hashlib.sha256()
    for path in _REVIEW_POLICY_FILES:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def current_review_policy_key(
    db: Session,
    workspace_id: int | None = None,
) -> str:
    workspace = resolve_workspace(db, workspace_id)
    prompt = db.scalar(
        select(PromptVersion)
        .where(PromptVersion.is_active.is_(True))
        .order_by(desc(PromptVersion.id))
        .limit(1)
    )
    ai_config = db.get(AiConfig, 1)
    evidence_config = workspace_evidence_config(db, workspace.id)
    equipment_statement = select(EquipmentRegistry).order_by(
        EquipmentRegistry.equipment_id
    )
    if workspace.equipment_area_filter:
        equipment_statement = equipment_statement.where(
            EquipmentRegistry.subordinate_area == workspace.equipment_area_filter
        )
    equipment_registry = (
        db.scalars(equipment_statement).all()
        if workspace.equipment_review_enabled
        else []
    )
    equipment_snapshot = [
        {
            "equipment_id": item.equipment_id,
            "description": item.description,
            "manufacturer": item.manufacturer,
            "model_number": item.model_number,
            "serial_number": item.serial_number,
            "calibration_date": (
                item.calibration_date.isoformat() if item.calibration_date else None
            ),
            "calibration_due_date": (
                item.calibration_due_date.isoformat()
                if item.calibration_due_date
                else None
            ),
            "received_date": item.received_date.isoformat() if item.received_date else None,
            "equipment_status": item.equipment_status,
        }
        for item in equipment_registry
    ]
    policy = {
        "workspace_id": workspace.id,
        "engine_version": REVIEW_ENGINE_VERSION,
        "implementation_hash": _review_implementation_hash(),
        "prompt_id": prompt.id if prompt else None,
        "prompt_template": prompt.template if prompt else None,
        "ai_base_url": ai_config.base_url if ai_config else None,
        "model_name": ai_config.model_name if ai_config else None,
        "allowed_network_root": (
            evidence_config.allowed_network_root if evidence_config else None
        ),
        "network_evidence_enabled": bool(
            evidence_config and evidence_config.network_evidence_enabled
        ),
        "image_review_enabled": bool(
            evidence_config and evidence_config.image_review_enabled
        ),
        "allow_insecure_image_transport": bool(
            evidence_config and evidence_config.allow_insecure_image_transport
        ),
        "equipment_review_enabled": workspace.equipment_review_enabled,
        "equipment_area_filter": workspace.equipment_area_filter,
        "equipment_registry": equipment_snapshot,
    }
    serialized = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def backfill_legacy_review_policy(db: Session) -> int:
    result = db.execute(
        update(ReviewResult)
        .where(ReviewResult.review_policy_key == "")
        .values(review_policy_key=current_review_policy_key(db))
    )
    db.commit()
    return result.rowcount


def adopt_legacy_workspace_policies(db: Session) -> int:
    workspaces = db.scalars(
        select(Workspace)
        .where(Workspace.legacy_policy_adopted.is_(False))
        .order_by(Workspace.id)
    ).all()
    adopted_results = 0
    for workspace in workspaces:
        policy_key = current_review_policy_key(db, workspace.id)
        runs = db.scalars(
            select(AlmRun).where(
                AlmRun.workspace_id == workspace.id,
                AlmRun.current_revision_id.is_not(None),
            )
        ).all()
        for run in runs:
            result = db.scalar(
                select(ReviewResult)
                .where(
                    ReviewResult.workspace_id == workspace.id,
                    ReviewResult.revision_id == run.current_revision_id,
                    ReviewResult.source_hash == run.source_hash,
                )
                .order_by(desc(ReviewResult.completed_at), desc(ReviewResult.id))
                .limit(1)
            )
            if result is not None:
                result.review_policy_key = policy_key
                adopted_results += 1
        workspace.legacy_policy_adopted = True
    db.commit()
    return adopted_results