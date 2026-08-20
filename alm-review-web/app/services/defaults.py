from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AiConfig, EvidenceConfig, PromptVersion, SyncConfig
from app.services.workspaces import default_workspace

# Legacy record only: the review is driven by the packaged Skills under
# `app/review_skills/`, and this template is never sent to the model.
DEFAULT_PROMPT = """You are an ALM test execution record reviewer.

This template is retained for compatibility. The active review instructions,
input/output schemas and examples live in the packaged review Skills under
app/review_skills/, and every semantic call goes through SkillRunner.

{{RUN_CONTENT}}
"""


def ensure_defaults(db: Session) -> None:
    workspace = default_workspace(db)
    if db.get(AiConfig, 1) is None:
        db.add(AiConfig(id=1))
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

    active_prompt = db.scalar(select(PromptVersion).where(PromptVersion.is_active.is_(True)))
    if active_prompt is None:
        db.add(PromptVersion(name="Default review prompt", template=DEFAULT_PROMPT, is_active=True))

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