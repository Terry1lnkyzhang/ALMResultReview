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
    ReviewResult,
    Workspace,
)
from app.services.automation_release import automation_release_policy_snapshot
from app.services.skill_runner import skill_policy_identity
from app.services.workspaces import resolve_workspace, workspace_evidence_config

# Bump this whenever deterministic review preprocessing or guard behavior changes.
REVIEW_ENGINE_VERSION = "2026.09.02.2"
_APP_DIRECTORY = Path(__file__).resolve().parents[1]
_REVIEW_POLICY_FILES = (
    _APP_DIRECTORY / "review_pipeline.toml",
    _APP_DIRECTORY / "hashing.py",
    _APP_DIRECTORY / "services" / "automation_release.py",
    _APP_DIRECTORY / "services" / "equipment_pipeline.py",
    _APP_DIRECTORY / "services" / "evidence.py",
    _APP_DIRECTORY / "services" / "equipment_review.py",
    _APP_DIRECTORY / "services" / "html_evidence.py",
    _APP_DIRECTORY / "services" / "image_evidence.py",
    _APP_DIRECTORY / "services" / "review_pipeline.py",
    _APP_DIRECTORY / "services" / "skill_runner.py",
    _APP_DIRECTORY / "services" / "reviews.py",
    # Skill instructions, schemas and examples steer the verdict as much as the code.
    *sorted(
        path
        for path in (_APP_DIRECTORY / "review_skills").rglob("*")
        if path.is_file()
    ),
)


def _review_implementation_hash() -> str:
    digest = hashlib.sha256()
    for path in _REVIEW_POLICY_FILES:
        digest.update(path.relative_to(_APP_DIRECTORY).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def current_review_policy_key(
    db: Session,
    workspace_id: int | None = None,
) -> str:
    workspace = resolve_workspace(db, workspace_id)
    ai_pool = db.scalars(
        select(AiConfig)
        .where(AiConfig.enabled.is_(True))
        .order_by(AiConfig.id)
    ).all()
    evidence_config = workspace_evidence_config(db, workspace.id)
    equipment_statement = select(EquipmentRegistry).order_by(
        EquipmentRegistry.equipment_id,
        EquipmentRegistry.id,
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
            "revision": item.revision,
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
    authoritative_skill_ids = ["alm-text-review"]
    if (
        evidence_config
        and evidence_config.external_evidence_review_enabled
    ):
        authoritative_skill_ids.extend(
            ["image-evidence-review", "html-evidence-review"]
        )
    if workspace.equipment_review_enabled:
        authoritative_skill_ids.append("equipment-role")
    release_project_name = (
        evidence_config.automation_release_project_name
        if evidence_config
        else ""
    )
    policy = {
        "workspace_id": workspace.id,
        "project": workspace.project,
        "engine_version": REVIEW_ENGINE_VERSION,
        "implementation_hash": _review_implementation_hash(),
        "ai_pool": [
            {
                "id": config.id,
                "base_url": config.base_url,
                "model_name": config.model_name,
            }
            for config in ai_pool
        ],
        "authoritative_skills": [
            skill_policy_identity(skill_id)
            for skill_id in authoritative_skill_ids
        ],
        "allowed_network_root": (
            evidence_config.allowed_network_root if evidence_config else None
        ),
        "local_html_fallback_root": (
            evidence_config.local_html_fallback_root if evidence_config else None
        ),
        "automation_release_project_name": (
            release_project_name or None
        ),
        "automation_release_snapshot": automation_release_policy_snapshot(
            db,
            release_project_name,
        ),
        "external_evidence_review_enabled": bool(
            evidence_config and evidence_config.external_evidence_review_enabled
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