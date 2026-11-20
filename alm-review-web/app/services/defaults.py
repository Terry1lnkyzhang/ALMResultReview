from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AiConfig, EvidenceConfig, SyncConfig, WorkerLease
from app.services.workspaces import default_workspace


def ensure_defaults(db: Session) -> None:
    workspace = default_workspace(db)
    if db.get(AiConfig, 1) is None:
        db.add(AiConfig(id=1))
    if db.get(AiConfig, 2) is None:
        db.add(AiConfig(id=2, enabled=False))
    if db.get(WorkerLease, 1) is None:
        db.add(WorkerLease(id=1))
    evidence_config = db.scalar(
        select(EvidenceConfig)
        .where(EvidenceConfig.workspace_id == workspace.id)
        .order_by(EvidenceConfig.id)
        .limit(1)
    )
    if evidence_config is None:
        evidence_config = db.get(EvidenceConfig, 1)
        if evidence_config is None:
            evidence_config = EvidenceConfig(workspace_id=workspace.id)
            db.add(evidence_config)
        else:
            evidence_config.workspace_id = workspace.id

    sync_config = db.scalar(select(SyncConfig).limit(1))
    if sync_config is None:
        db.add(
            SyncConfig(
                workspace_id=workspace.id,
                name="Testing",
                server_url="http://ilqhfaatc1msalm.code1.emi.philips.com",
                domain="global",
                project="sy_vnv",
                folder_id=5172,
                folder_path="Testing",
                schedule_hour=2,
                schedule_minute=0,
                enabled=False,
            )
        )
    elif sync_config.workspace_id is None:
        sync_config.workspace_id = workspace.id
    db.commit()