from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, load_only

from app.hashing import review_payload
from app.models import AlmRun, ReviewResult, RunRevision, Workspace
from app.services.copilot_reports import (
    COPILOT_LABELS,
    CopilotRoundReport,
    dump_copilot_reports,
)
from app.services.review_policy import current_review_policy_key
from app.services.review_status import current_reviews
from app.services.skill_runner import discover_skills, load_skill
from app.services.workspaces import workspace_evidence_config


def build_copilot_task(db: Session, workspace: Workspace, limit: int) -> dict[str, Any]:
    policy_key = current_review_policy_key(db, workspace.id)
    runs = list(db.scalars(
        select(AlmRun)
        .options(load_only(
            AlmRun.run_id, AlmRun.current_revision_id, AlmRun.source_hash,
        ))
        .where(AlmRun.workspace_id == workspace.id)
        .order_by(AlmRun.run_id)
    ))
    reviews = current_reviews(db, runs, policy_key)
    items = []
    for run in runs:
        review = reviews[run.run_id]
        primary = review.result
        if (
            primary is None
            or primary.verdict != "qualified"
            or primary.review_policy_key != policy_key
            or review.final_status != "qualified"
            or review.manual_decision is not None
        ):
            continue
        modes = [mode for mode in COPILOT_LABELS if mode not in review.copilot_reports]
        if not modes:
            continue
        revision = db.get(RunRevision, primary.revision_id)
        if revision is None:
            continue
        content = review_payload(json.loads(revision.snapshot_json))
        for step in content["steps"]:
            step.pop("attachment_contents", None)
        items.append({
            "review_result_id": primary.id,
            "source_hash": primary.source_hash,
            "modes": modes,
            "content": content,
        })
        if len(items) >= limit:
            break

    if not items:
        raise ValueError("No eligible first-pass Qualified runs for Copilot review.")
    evidence = workspace_evidence_config(db, workspace.id)
    skills = [load_skill(skill_id) for skill_id in discover_skills()]
    return {
        "workspace_id": workspace.id,
        "policy_key": policy_key,
        "external_evidence_review_enabled": bool(evidence and evidence.external_evidence_review_enabled),
        "equipment_review_enabled": workspace.equipment_review_enabled,
        "location_review_enabled": workspace.location_review_enabled,
        "skills": [
            {
                "id": skill.skill_id,
                "version": skill.version,
                "hash": skill.skill_hash,
                "instructions": skill.instructions,
            }
            for skill in skills
        ],
        "report_schema": CopilotRoundReport.model_json_schema(),
        "runs": items,
    }


def apply_copilot_report(
    db: Session,
    workspace_id: int | None,
    report: CopilotRoundReport,
) -> int:
    result_ids = [item.review_result_id for item in report.results]
    run_ids = select(ReviewResult.run_id).where(ReviewResult.id.in_(result_ids))
    runs = list(db.scalars(
        select(AlmRun).where(AlmRun.workspace_id == workspace_id, AlmRun.run_id.in_(run_ids))
    ))
    policy_key = current_review_policy_key(db, workspace_id)
    active = {
        review.result.id: review
        for review in current_reviews(db, runs, policy_key).values()
        if review.result is not None
    }
    applied = 0
    for item in report.results:
        review = active.get(item.review_result_id)
        primary = review.result if review else None
        if (
            review is None
            or primary is None
            or primary.verdict != "qualified"
            or primary.review_policy_key != policy_key
            or primary.source_hash != item.source_hash
        ):
            raise ValueError(
                f"Review result {item.review_result_id} is stale or not an eligible Qualified result."
            )
        previous = review.copilot_reports.get(report.mode)
        if previous == item.outcome:
            continue
        if previous is not None and previous.verdict is not None:
            raise ValueError(f"Review result {primary.id} already has a {report.mode} verdict.")
        new_reports = {**review.copilot_reports, report.mode: item.outcome}
        updated = db.execute(
            update(ReviewResult)
            .where(
                ReviewResult.id == primary.id,
                ReviewResult.copilot_reviews_json == primary.copilot_reviews_json,
            )
            .values(copilot_reviews_json=dump_copilot_reports(new_reports))
        )
        if updated.rowcount != 1:
            raise ValueError(f"Review result {primary.id} changed during import; submit again.")
        applied += 1
    return applied