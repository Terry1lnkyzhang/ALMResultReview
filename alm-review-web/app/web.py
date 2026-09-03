from __future__ import annotations

import csv
import io
import json
import re
import secrets
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session, load_only

from app.config import PROJECT_DIR, get_settings
from app.database import get_db
from app.hashing import normalize_text
from app.models import (
    AiConfig,
    AlmRun,
    AlmUser,
    EquipmentImportHistory,
    EquipmentRegistry,
    EvidenceConfig,
    PromptVersion,
    ReviewJob,
    ReviewResult,
    RunRevision,
    RunStep,
    SyncConfig,
    SyncJob,
    WorkerHeartbeat,
    Workspace,
)
from app.services.ai_transport import ai_endpoint_available, ai_endpoint_health_status
from app.services.docx_import import MAX_DOCX_BYTES, parse_alm_docx
from app.services.equipment_registry import (
    import_equipment_workbook,
    optional_equipment_identity_error,
)
from app.services.importer import import_data, import_file, queue_latest_alm_changes
from app.services.review_operations import (
    REREVIEW_SCOPES,
    active_run_review_job,
    cancel_queued_reviews,
    delete_local_run,
    queue_failed_reviews,
    queue_rereviews,
    queue_rereviews_for_run_ids,
    queue_run_review,
    workspace_review_progress,
)
from app.services.review_policy import current_review_policy_key
from app.services.review_status import (
    current_reviews,
    is_force_qualified,
    review_update_reasons,
)
from app.services.reviews import (
    current_review,
    manual_decision_locks_run,
    save_manual_decision,
    test_ai_connection,
)
from app.services.rich_text import render_alm_rich_text, snapshot_step_fields
from app.services.scheduler import configure_scheduler
from app.services.skill_runner import (
    discover_skills,
    load_skill,
    skill_manifest_metadata,
)
from app.services.worker_tasks import queue_run_sync_jobs, queue_sync_job
from app.services.workspace_insights import workspace_equipment_insights
from app.services.workspaces import (
    resolve_workspace,
    workspace_evidence_config,
    workspace_slug,
    workspace_sync_config,
)

basic_auth = HTTPBasic(auto_error=False)


def _has_enabled_ai_endpoint(db: Session) -> bool:
    return db.scalar(
        select(AiConfig.id).where(AiConfig.enabled.is_(True)).limit(1)
    ) is not None


def _ai_endpoint_statuses(
    db: Session,
    configs: list[AiConfig],
) -> list[dict[str, Any]]:
    running_by_config = dict(
        db.execute(
            select(ReviewJob.ai_config_id, func.count())
            .where(ReviewJob.status == "running")
            .group_by(ReviewJob.ai_config_id)
        ).all()
    )
    if None in running_by_config:
        running_by_config[1] = running_by_config.get(1, 0) + running_by_config.pop(None)
    return [
        {
            "id": config.id,
            "model_name": config.model_name,
            "enabled": config.enabled,
            "available": ai_endpoint_available(config),
            "health_status": ai_endpoint_health_status(config),
            "running": running_by_config.get(config.id, 0),
            "capacity": max(1, min(4, config.review_concurrency)),
            "last_error": config.last_error,
        }
        for config in configs
    ]


def _skill_catalog() -> list[dict[str, Any]]:
    catalog = []
    for skill_id in discover_skills():
        try:
            metadata = skill_manifest_metadata(skill_id)
            if metadata["status"] == "planned":
                catalog.append(metadata)
                continue
            definition = load_skill(skill_id)
        except Exception as exc:
            catalog.append(
                {"skill_id": skill_id, "status": "unavailable", "error": str(exc)}
            )
            continue
        catalog.append(
            {
                "skill_id": definition.skill_id,
                "name": definition.name,
                "version": definition.version,
                "stage": definition.stage,
                "status": "available",
                "skill_hash": definition.skill_hash,
                "required_capabilities": definition.required_capabilities,
                "optional_capabilities": definition.optional_capabilities,
            }
        )
    return catalog


def _pipeline_skill_traces(pipeline: dict[str, Any]) -> list[dict[str, Any]]:
    stages = pipeline.get("stages", {})
    if not isinstance(stages, dict):
        return []
    traces: list[dict[str, Any]] = []

    def add(stage: str, value: Any, mode: str = "authoritative") -> None:
        if not isinstance(value, dict) or not value.get("skill_id"):
            return
        trace = dict(value)
        trace["pipeline_stage"] = stage
        trace.setdefault("mode", mode)
        traces.append(trace)

    text = stages.get("text_review", {})
    if isinstance(text, dict):
        for trace in text.get("skills", []):
            add("text_review", trace)
    image = stages.get("image_review", {})
    if isinstance(image, dict):
        for trace in image.get("skills", []):
            add("image_review", trace)
    equipment = stages.get("equipment_review", {})
    if isinstance(equipment, dict):
        add("equipment_review", equipment.get("skill"))
    return traces


def require_web_access(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(basic_auth)],
) -> None:
    settings = get_settings()
    auth_required = settings.app_role == "web" or settings.web_auth_enabled
    if not auth_required:
        return
    if not settings.web_auth_username or not settings.web_auth_password:
        raise HTTPException(
            status_code=503,
            detail="WEB_AUTH_USERNAME and WEB_AUTH_PASSWORD must be configured.",
        )
    valid = bool(
        credentials
        and secrets.compare_digest(credentials.username, settings.web_auth_username)
        and secrets.compare_digest(credentials.password, settings.web_auth_password)
    )
    if not valid:
        raise HTTPException(
            status_code=401,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Basic"},
        )


router = APIRouter(dependencies=[Depends(require_web_access)])
templates = Jinja2Templates(directory=PROJECT_DIR / "app" / "templates")
templates.env.filters["alm_rich_text"] = render_alm_rich_text

STATUS_LABELS = {
    "qualified": "合格",
    "force_qualified": "人工判定合格",
    "unqualified": "不合格",
    "needs_manual_review": "需人工复核",
    "pending_review": "待评审",
    "review_failed": "评审失败",
    "warning": "有警告",
}

_SEARCH_WHITESPACE_RE = re.compile(r"\s+")


def _redirect(path: str, message: str, kind: str = "success") -> RedirectResponse:
    separator = "&" if "?" in path else "?"
    return RedirectResponse(
        f"{path}{separator}message={quote(message)}&message_kind={quote(kind)}",
        status_code=303,
    )


def _person_label(code1_id: str, users: dict[str, AlmUser]) -> str:
    if not code1_id:
        return "Unassigned"
    user = users.get(code1_id)
    return f"{user.full_name} ({code1_id})" if user and user.full_name else code1_id


def _to_app_timezone(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    local_tz = ZoneInfo(get_settings().app_timezone)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(local_tz)


def _public_job_error(error_message: str | None) -> str | None:
    if error_message is None:
        return None
    return re.sub(
        r"(?i)(api[_ -]?key\s*:\s*)[^\s,.'\"}]+",
        r"\1[REDACTED]",
        error_message,
    )


def _run_view(
    db: Session,
    run: AlmRun,
    policy_key: str,
    users: dict[str, AlmUser],
    review=None,
    prompt_version_id: int | None = None,
    model_name: str | None = None,
) -> dict:
    if review is None:
        review = current_review(db, run, policy_key)
    return {
        "run": run,
        "actual_tester_label": _person_label(run.actual_tester, users),
        "test_owner_label": _person_label(run.test_owner, users),
        "review": review,
        "review_summary": review.result.issue_summary if review.result else "",
        "final_status": review.final_status,
        "force_qualified": is_force_qualified(review),
        "has_warning": review.has_warning,
        "status_label": STATUS_LABELS[review.final_status],
        "review_update_reasons": review_update_reasons(
            review.result,
            policy_key,
            prompt_version_id,
            model_name,
        ),
    }


def _matches_status(item: dict, status: str) -> bool:
    # Force qualified and warning are lenses over the final statuses, not statuses.
    if status == "force_qualified":
        return item["force_qualified"]
    if status == "warning":
        return item["has_warning"]
    return status == "all" or item["final_status"] == status


def _flatten_step_text(value: str | None) -> str:
    # ALM step fields are rich text, so tags and &nbsp; have to go before comparing.
    return _SEARCH_WHITESPACE_RE.sub(" ", normalize_text(value)).strip().casefold()


def _step_text_run_ids(
    db: Session,
    workspace_id: int,
    include_legacy: bool,
    query: str,
) -> set[int]:
    """Return Run ids whose current revision mentions the query in a reviewed step."""
    needle = _flatten_step_text(query)
    if not needle:
        return set()
    # Narrow the LongText scan in SQL first; the longest token survives tag and entity noise.
    token = max(needle.split(" "), key=len)
    pattern = "%{}%".format(
        token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    rows = db.execute(
        select(AlmRun.run_id, RunStep.description, RunStep.expected, RunStep.actual)
        .join(RunRevision, RunRevision.id == AlmRun.current_revision_id)
        .join(RunStep, RunStep.revision_id == RunRevision.id)
        .where(
            or_(
                AlmRun.workspace_id == workspace_id,
                include_legacy and AlmRun.workspace_id.is_(None),
            ),
            or_(
                RunStep.description.ilike(pattern, escape="\\"),
                RunStep.expected.ilike(pattern, escape="\\"),
                RunStep.actual.ilike(pattern, escape="\\"),
            ),
        )
    ).all()
    return {
        run_id
        for run_id, description, expected, actual in rows
        if any(
            needle in _flatten_step_text(field)
            for field in (description, expected, actual)
        )
    }


def _matches_dashboard_filters(
    item: dict,
    status: str,
    tester: str,
    owner: str,
    query: str,
    step_text_run_ids: set[int] | None = None,
) -> bool:
    normalized_query = query.strip().casefold()
    return (
        _matches_status(item, status)
        and (tester == "all" or (item["run"].actual_tester or "Unassigned") == tester)
        and (owner == "all" or (item["run"].test_owner or "Unassigned") == owner)
        and (
            not normalized_query
            or normalized_query in str(item["run"].run_id)
            or normalized_query in str(item["run"].test_id or "")
            or normalized_query in item["run"].test_name.casefold()
            or normalized_query in item["run"].test_set_name.casefold()
            or normalized_query in item["run"].folder_path.casefold()
            or normalized_query in item["run"].execution_location.casefold()
            or normalized_query in item["actual_tester_label"].casefold()
            or normalized_query in item["test_owner_label"].casefold()
            or normalized_query in item["review_summary"].casefold()
            or (
                step_text_run_ids is not None
                and item["run"].run_id in step_text_run_ids
            )
        )
    )


def _dashboard_filter_path(
    workspace_id: int,
    status: str,
    tester: str,
    owner: str,
    query: str,
    search_steps: bool = False,
) -> str:
    return (
        f"/?workspace={workspace_id}&status={quote(status)}&tester={quote(tester)}"
        f"&owner={quote(owner)}&query={quote(query)}"
        f"&search_steps={'1' if search_steps else '0'}"
    )


def _filtered_run_ids(
    db: Session,
    workspace_id: int,
    include_legacy: bool,
    status: str,
    tester: str,
    owner: str,
    query: str,
    search_steps: bool = False,
) -> list[int]:
    runs = db.scalars(
        select(AlmRun)
        .where(
            or_(
                AlmRun.workspace_id == workspace_id,
                include_legacy and AlmRun.workspace_id.is_(None),
            )
        )
        .order_by(desc(AlmRun.execution_at), desc(AlmRun.run_id))
    ).all()
    policy_key = current_review_policy_key(db, workspace_id)
    users = {user.code1_id: user for user in db.scalars(select(AlmUser)).all()}
    step_matches = (
        _step_text_run_ids(db, workspace_id, include_legacy, query)
        if search_steps
        else None
    )
    return [
        item["run"].run_id
        for run in runs
        if _matches_dashboard_filters(
            item := _run_view(db, run, policy_key, users),
            status,
            tester,
            owner,
            query,
            step_matches,
        )
    ]


def _run_sync_batch_progress(db: Session, workspace_id: int) -> dict[str, Any]:
    """Aggregate the per-Run ALM refresh batch that is still being processed.

    `queue_run_sync_jobs` writes one row per Run inside a single transaction, so a
    batch is identified by its shared `created_at`.
    """
    batch_created_at = db.scalar(
        select(func.min(SyncJob.created_at)).where(
            SyncJob.workspace_id == workspace_id,
            SyncJob.run_id.is_not(None),
            SyncJob.status.in_(("queued", "running")),
        )
    )
    if batch_created_at is None:
        return {
            "active": False,
            "total": 0,
            "done": 0,
            "queued": 0,
            "running": 0,
            "failed": 0,
            "percent": 0,
        }
    counts = dict(
        db.execute(
            select(SyncJob.status, func.count())
            .where(
                SyncJob.workspace_id == workspace_id,
                SyncJob.run_id.is_not(None),
                SyncJob.created_at == batch_created_at,
            )
            .group_by(SyncJob.status)
        ).all()
    )
    total = sum(counts.values())
    done = counts.get("completed", 0)
    return {
        "active": True,
        "total": total,
        "done": done,
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "failed": counts.get("failed", 0),
        "percent": round(done * 100 / total) if total else 0,
    }


def _current_sync_job(db: Session, workspace_id: int) -> SyncJob | None:
    """Pick the job the worker is actually on.

    Jobs are claimed oldest-first, so the newest row is the last one to run and
    would keep the progress panel stuck on "queued" for a whole batch.
    """
    for condition in (SyncJob.status == "running", SyncJob.status == "queued"):
        job = db.scalar(
            select(SyncJob)
            .where(SyncJob.workspace_id == workspace_id, condition)
            .order_by(SyncJob.created_at, SyncJob.id)
            .limit(1)
        )
        if job is not None:
            return job
    return db.scalar(
        select(SyncJob)
        .where(SyncJob.workspace_id == workspace_id)
        .order_by(desc(SyncJob.created_at), desc(SyncJob.id))
        .limit(1)
    )


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    text = str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text


def _format_steps_for_export(steps: list[RunStep]) -> str:
    """Plain, human-readable step text for CSV -- rich text markup stripped, no JSON."""

    def _plain(value: str) -> str:
        # ALM's &nbsp; survives normalize_text as U+00A0; flatten it for a CSV reader.
        return normalize_text(value).replace("\xa0", " ")

    blocks = [
        f"Step {step.step_order} - {step.name or 'Untitled'} [{step.status or 'n/a'}]\n"
        f"Description: {_plain(step.description) or '-'}\n"
        f"Expected: {_plain(step.expected) or '-'}\n"
        f"Actual: {_plain(step.actual) or '-'}"
        for step in steps
    ]
    return "\n\n".join(blocks)


def _optional_date(value: str) -> date | None:
    return date.fromisoformat(value) if value.strip() else None


def _equipment_display_identifier(equipment: EquipmentRegistry) -> str:
    return equipment.equipment_id or equipment.serial_number or equipment.description


def _set_equipment_values(equipment: EquipmentRegistry, values: dict[str, str]) -> None:
    equipment.equipment_id = values["equipment_id"].strip().upper() or None
    equipment.description = values["description"].strip()
    equipment.manufacturer = values["manufacturer"].strip()
    equipment.model_number = values["model_number"].strip()
    equipment.revision = values["revision"].strip()
    equipment.accuracy_class = values["accuracy_class"].strip()
    equipment.measurement_range = values["measurement_range"].strip()
    equipment.serial_number = values["serial_number"].strip()
    equipment.calibration_date = _optional_date(values["calibration_date"])
    equipment.calibration_due_date = _optional_date(values["calibration_due_date"])
    equipment.received_date = _optional_date(values["received_date"])
    equipment.calibration_interval = values["calibration_interval"].strip()
    equipment.subordinate_area = values["subordinate_area"].strip()
    equipment.equipment_user = values["equipment_user"].strip()
    equipment.equipment_status = values["equipment_status"].strip()
    equipment.calibration_location = values["calibration_location"].strip()
    equipment.instruction_number = values["instruction_number"].strip()


def _equipment_values(
    equipment_id: str,
    description: str,
    manufacturer: str,
    model_number: str,
    revision: str,
    accuracy_class: str,
    measurement_range: str,
    serial_number: str,
    calibration_date: str,
    calibration_due_date: str,
    received_date: str,
    calibration_interval: str,
    subordinate_area: str,
    equipment_user: str,
    equipment_status: str,
    calibration_location: str,
    instruction_number: str,
) -> dict[str, str]:
    return {
        "equipment_id": equipment_id,
        "description": description,
        "manufacturer": manufacturer,
        "model_number": model_number,
        "revision": revision,
        "accuracy_class": accuracy_class,
        "measurement_range": measurement_range,
        "serial_number": serial_number,
        "calibration_date": calibration_date,
        "calibration_due_date": calibration_due_date,
        "received_date": received_date,
        "calibration_interval": calibration_interval,
        "subordinate_area": subordinate_area,
        "equipment_user": equipment_user,
        "equipment_status": equipment_status,
        "calibration_location": calibration_location,
        "instruction_number": instruction_number,
    }


def _equipment_form(
    equipment_id: str = Form(""),
    description: str = Form(...),
    manufacturer: str = Form(""),
    model_number: str = Form(""),
    revision: str = Form(""),
    accuracy_class: str = Form(""),
    measurement_range: str = Form(""),
    serial_number: str = Form(""),
    calibration_date: str = Form(""),
    calibration_due_date: str = Form(""),
    received_date: str = Form(""),
    calibration_interval: str = Form(""),
    subordinate_area: str = Form(""),
    equipment_user: str = Form(""),
    equipment_status: str = Form(""),
    calibration_location: str = Form(""),
    instruction_number: str = Form(""),
) -> dict[str, str]:
    return _equipment_values(
        equipment_id,
        description,
        manufacturer,
        model_number,
        revision,
        accuracy_class,
        measurement_range,
        serial_number,
        calibration_date,
        calibration_due_date,
        received_date,
        calibration_interval,
        subordinate_area,
        equipment_user,
        equipment_status,
        calibration_location,
        instruction_number,
    )


@router.get("/")
def dashboard(
    request: Request,
    status: str = Query(default="all"),
    tester: str = Query(default="all"),
    owner: str = Query(default="all"),
    query: str = Query(default=""),
    search_steps: bool = Query(default=False),
    project: str = Query(default="all"),
    workspace: int | None = None,
    db: Session = Depends(get_db),
):
    include_legacy = workspace is None
    current_workspace = resolve_workspace(db, workspace)
    workspaces = db.scalars(
        select(Workspace)
        .where(Workspace.archived.is_(False))
        .order_by(Workspace.name)
    ).all()
    project_options = sorted(
        {item.project for item in workspaces},
        key=lambda name: (name == "", name.casefold()),
    )
    selected_project = project if project in project_options else "all"
    workspace_options = [
        item
        for item in workspaces
        if selected_project == "all" or item.project == selected_project
    ]
    if workspace_options and all(
        item.id != current_workspace.id for item in workspace_options
    ):
        current_workspace = workspace_options[0]
        include_legacy = False
    runs = db.scalars(
        select(AlmRun)
        .options(
            load_only(
                AlmRun.run_id,
                AlmRun.alm_run_id,
                AlmRun.test_id,
                AlmRun.test_name,
                AlmRun.test_set_name,
                AlmRun.folder_path,
                AlmRun.execution_location,
                AlmRun.run_status,
                AlmRun.test_owner,
                AlmRun.actual_tester,
                AlmRun.execution_at,
                AlmRun.source_hash,
                AlmRun.current_revision_id,
            )
        )
        .where(
            or_(
                AlmRun.workspace_id == current_workspace.id,
                include_legacy and AlmRun.workspace_id.is_(None),
            )
        )
        .order_by(desc(AlmRun.execution_at), desc(AlmRun.run_id))
    ).all()
    policy_key = current_review_policy_key(db, current_workspace.id)
    active_prompt = db.scalar(
        select(PromptVersion)
        .where(PromptVersion.is_active.is_(True))
        .order_by(desc(PromptVersion.id))
        .limit(1)
    )
    ai_configs = db.scalars(select(AiConfig).order_by(AiConfig.id)).all()
    ai_config = next((config for config in ai_configs if config.id == 1), None)
    active_model_names = list(
        dict.fromkeys(config.model_name for config in ai_configs if config.enabled)
    )
    active_model_name = active_model_names[0] if len(active_model_names) == 1 else None
    ai_endpoint_statuses = _ai_endpoint_statuses(db, ai_configs)
    review_concurrency_total = sum(
        max(1, min(4, config.review_concurrency))
        for config in ai_configs
        if config.enabled
    )
    users = {user.code1_id: user for user in db.scalars(select(AlmUser)).all()}
    reviews_by_run = current_reviews(db, runs, policy_key)
    all_views = [
        _run_view(
            db,
            run,
            policy_key,
            users,
            reviews_by_run[run.run_id],
            active_prompt.id if active_prompt else None,
            active_model_name,
        )
        for run in runs
    ]
    review_update_count = sum(bool(item["review_update_reasons"]) for item in all_views)
    review_update_reason_counts = Counter(
        reason
        for item in all_views
        for reason in item["review_update_reasons"]
    )
    status_counts = Counter(item["final_status"] for item in all_views)
    status_counts["force_qualified"] = sum(item["force_qualified"] for item in all_views)
    status_counts["warning"] = sum(item["has_warning"] for item in all_views)
    step_matches = (
        _step_text_run_ids(db, current_workspace.id, include_legacy, query)
        if search_steps
        else None
    )
    tester_scope = [
        item
        for item in all_views
        if _matches_dashboard_filters(item, status, "all", owner, query, step_matches)
    ]
    tester_counts = Counter(
        item["run"].actual_tester or "Unassigned" for item in tester_scope
    )
    owner_scope = [
        item
        for item in all_views
        if _matches_dashboard_filters(item, status, tester, "all", query, step_matches)
    ]
    owner_counts = Counter(
        item["run"].test_owner or "Unassigned" for item in owner_scope
    )

    filtered = [
        item
        for item in all_views
        if _matches_dashboard_filters(item, status, tester, owner, query, step_matches)
    ]
    max_tester_count = max(tester_counts.values(), default=1)
    max_owner_count = max(owner_counts.values(), default=1)
    review_job_counts = dict(
        db.execute(
            select(ReviewJob.status, func.count())
            .where(
                ReviewJob.workspace_id == current_workspace.id,
                ReviewJob.status.in_(("queued", "running")),
            )
            .group_by(ReviewJob.status)
        ).all()
    )
    active_review_counts = {
        (workspace_id, job_status): count
        for workspace_id, job_status, count in db.execute(
            select(ReviewJob.workspace_id, ReviewJob.status, func.count())
            .where(ReviewJob.status.in_(("queued", "running")))
            .group_by(ReviewJob.workspace_id, ReviewJob.status)
        ).all()
    }
    queue_overview = sorted(
        (
            {
                "workspace": item,
                "queued": active_review_counts.get((item.id, "queued"), 0),
                "running": active_review_counts.get((item.id, "running"), 0),
            }
            for item in workspaces
            if active_review_counts.get((item.id, "queued"), 0)
            or active_review_counts.get((item.id, "running"), 0)
            or item.review_queue_paused
        ),
        key=lambda item: (-item["workspace"].queue_priority, item["workspace"].name),
    )
    queued_review_preview = [
        {
            "job": job,
            "run": run,
            "workspace": job_workspace,
            "created_at": job.created_at.replace(tzinfo=UTC).astimezone(
                ZoneInfo(get_settings().app_timezone)
            ),
        }
        for job, run, job_workspace in db.execute(
            select(ReviewJob, AlmRun, Workspace)
            .join(Workspace, Workspace.id == ReviewJob.workspace_id)
            .outerjoin(
                AlmRun,
                (AlmRun.run_id == ReviewJob.run_id)
                & (AlmRun.workspace_id == ReviewJob.workspace_id),
            )
            .where(ReviewJob.status == "queued", Workspace.archived.is_(False))
            .order_by(
                Workspace.review_queue_paused,
                desc(Workspace.queue_priority),
                ReviewJob.created_at,
                ReviewJob.id,
            )
            .limit(10)
        ).all()
    ]
    latest_sync_job = db.scalar(
        select(SyncJob)
        .where(SyncJob.workspace_id == current_workspace.id)
        .order_by(desc(SyncJob.created_at), desc(SyncJob.id))
        .limit(1)
    )
    current_sync_job = _current_sync_job(db, current_workspace.id)
    active_sync_job = (
        current_sync_job
        if current_sync_job is not None
        and current_sync_job.status in ("queued", "running", "failed")
        else None
    )
    run_sync_batch = _run_sync_batch_progress(db, current_workspace.id)
    sync_display_at = None
    if latest_sync_job is not None:
        sync_timestamp = latest_sync_job.completed_at or latest_sync_job.started_at
        if sync_timestamp is not None:
            sync_display_at = sync_timestamp.replace(tzinfo=UTC).astimezone(
                ZoneInfo(get_settings().app_timezone)
            )
    worker_heartbeat = db.scalar(
        select(WorkerHeartbeat).order_by(desc(WorkerHeartbeat.last_seen_at)).limit(1)
    )
    now = datetime.utcnow()
    worker_has_active_review = db.scalar(
        select(ReviewJob.id)
        .where(
            ReviewJob.status == "running",
            ReviewJob.lease_expires_at.is_not(None),
            ReviewJob.lease_expires_at > now,
        )
        .limit(1)
    )
    worker_online = bool(
        (active_sync_job is not None and active_sync_job.status == "running")
        or worker_has_active_review
        or (
            worker_heartbeat
            and worker_heartbeat.status != "offline"
            and worker_heartbeat.last_seen_at
            >= now - timedelta(seconds=max(15, get_settings().worker_poll_seconds * 3))
        )
    )
    review_progress = workspace_review_progress(db, current_workspace.id)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "runs": filtered,
            "workspaces": workspaces,
            "workspace_options": workspace_options,
            "project_options": project_options,
            "selected_project": selected_project,
            "current_project": current_workspace.project,
            "current_workspace": current_workspace,
            "total_runs": len(all_views),
            "status_counts": status_counts,
            "tester_counts": [
                (code1_id, _person_label(code1_id, users), count)
                for code1_id, count in tester_counts.most_common()
            ],
            "owner_counts": [
                (code1_id, _person_label(code1_id, users), count)
                for code1_id, count in owner_counts.most_common()
            ],
            "max_tester_count": max_tester_count,
            "max_owner_count": max_owner_count,
            "status_labels": STATUS_LABELS,
            "selected_status": status,
            "selected_tester": tester,
            "selected_owner": owner,
            "query": query,
            "search_steps": search_steps,
            "review_job_counts": review_job_counts,
            "queue_overview": queue_overview,
            "queued_review_preview": queued_review_preview,
            "review_update_count": review_update_count,
            "review_update_reason_counts": review_update_reason_counts,
            "active_sync_job": active_sync_job,
            "latest_sync_job": latest_sync_job,
            "run_sync_batch": run_sync_batch,
            "sync_display_at": sync_display_at,
            "ai_config": ai_config,
            "ai_endpoint_statuses": ai_endpoint_statuses,
            "review_concurrency_total": review_concurrency_total,
            "worker_heartbeat": worker_heartbeat,
            "worker_online": worker_online,
            "review_progress": review_progress,
            "message": request.query_params.get("message"),
            "message_kind": request.query_params.get("message_kind", "success"),
        },
    )


@router.get("/api/review-progress")
def review_progress(workspace: int | None = None, db: Session = Depends(get_db)):
    current_workspace = resolve_workspace(db, workspace)
    progress = workspace_review_progress(db, current_workspace.id)
    return {
        "available": True,
        "total": progress.total,
        "reviewed": progress.reviewed,
        "qualified": progress.qualified,
        "force_qualified": progress.force_qualified,
        "unqualified": progress.unqualified,
        "manual": progress.manual,
        "pending": progress.pending,
        "review_failed": progress.review_failed,
        "warning": progress.warning,
        "queued": progress.queued,
        "running": progress.running,
        "percent": progress.percent,
    }


@router.get("/api/queue-status")
def queue_status(workspace: int | None = None, db: Session = Depends(get_db)):
    current_workspace = resolve_workspace(db, workspace)
    counts = dict(
        db.execute(
            select(ReviewJob.status, func.count())
            .where(
                ReviewJob.workspace_id == current_workspace.id,
                ReviewJob.status.in_(("queued", "running")),
            )
            .group_by(ReviewJob.status)
        ).all()
    )
    ai_configs = db.scalars(select(AiConfig).order_by(AiConfig.id)).all()
    enabled_ai_configs = [config for config in ai_configs if config.enabled]
    endpoint_statuses = _ai_endpoint_statuses(db, ai_configs)
    worker_heartbeat = db.scalar(
        select(WorkerHeartbeat).order_by(desc(WorkerHeartbeat.last_seen_at)).limit(1)
    )
    now = datetime.utcnow()
    active_review = db.scalar(
        select(ReviewJob.id)
        .where(
            ReviewJob.status == "running",
            ReviewJob.lease_expires_at.is_not(None),
            ReviewJob.lease_expires_at > now,
        )
        .limit(1)
    )
    active_sync = db.scalar(
        select(SyncJob.id)
        .where(
            SyncJob.status == "running",
            SyncJob.lease_expires_at.is_not(None),
            SyncJob.lease_expires_at > now,
        )
        .limit(1)
    )
    worker_has_active_job = bool(active_review or active_sync)
    sync_batch = _run_sync_batch_progress(db, current_workspace.id)
    worker_online = bool(
        worker_has_active_job
        or (
            worker_heartbeat
            and worker_heartbeat.status != "offline"
            and worker_heartbeat.last_seen_at
            >= now - timedelta(seconds=max(15, get_settings().worker_poll_seconds * 3))
        )
    )
    return {
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "review_paused": current_workspace.review_queue_paused,
        "pending_run_sync": sync_batch["queued"] + sync_batch["running"],
        "review_concurrency": sum(
            max(1, min(4, config.review_concurrency)) for config in enabled_ai_configs
        ),
        "endpoints": endpoint_statuses,
        "worker_online": worker_online,
        "worker_status": (
            "working"
            if worker_has_active_job
            else worker_heartbeat.status
            if worker_online and worker_heartbeat is not None
            else "offline"
        ),
        "worker_id": worker_heartbeat.worker_id if worker_heartbeat else None,
    }


@router.get("/api/sync-progress")
def sync_progress(workspace: int | None = None, db: Session = Depends(get_db)):
    current_workspace = resolve_workspace(db, workspace)
    batch = _run_sync_batch_progress(db, current_workspace.id)
    job = _current_sync_job(db, current_workspace.id)
    if job is None:
        return {"available": False, "batch": batch}
    legacy_running_job = (
        job.status == "running"
        and job.progress_stage == "queued"
        and not job.progress_message
    )
    return {
        "available": True,
        "job_id": job.id,
        "status": job.status,
        "stage": "collecting" if legacy_running_job else job.progress_stage,
        "message": (
            "This synchronization started before live progress tracking; "
            "detailed counts will be available from the next synchronization."
            if legacy_running_job
            else job.progress_message
        ),
        "folders_discovered": job.folders_discovered,
        "folders_processed": job.folders_processed,
        "test_sets_discovered": job.test_sets_discovered,
        "runs_discovered": job.runs_discovered,
        "run_id": job.run_id,
        "batch": batch,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error": job.error_message,
    }


@router.get("/insights/equipment")
def equipment_usage_insights(
    request: Request,
    workspace: int | None = None,
    query: str = Query(default=""),
    db: Session = Depends(get_db),
):
    current_workspace = resolve_workspace(db, workspace)
    workspaces = db.scalars(
        select(Workspace)
        .where(Workspace.archived.is_(False))
        .order_by(Workspace.name)
    ).all()
    policy_key = current_review_policy_key(db, current_workspace.id)
    insights = workspace_equipment_insights(db, current_workspace.id, policy_key)
    normalized_query = query.strip().casefold()
    devices = insights["devices"]
    unresolved = insights["unresolved"]
    if normalized_query:
        devices = [
            item
            for item in devices
            if normalized_query
            in json.dumps(item, ensure_ascii=False, default=str).casefold()
        ]
        unresolved = [
            item
            for item in unresolved
            if normalized_query
            in json.dumps(item, ensure_ascii=False, default=str).casefold()
        ]
    return templates.TemplateResponse(
        request=request,
        name="workspace_insights.html",
        context={
            "current_workspace": current_workspace,
            "workspaces": workspaces,
            "metrics": insights["metrics"],
            "devices": devices,
            "unresolved": unresolved,
            "query": query,
            "message": request.query_params.get("message"),
            "message_kind": request.query_params.get("message_kind", "success"),
        },
    )


@router.get("/exports/equipment-usage.csv")
def export_equipment_usage(
    workspace: int | None = None,
    db: Session = Depends(get_db),
) -> Response:
    current_workspace = resolve_workspace(db, workspace)
    policy_key = current_review_policy_key(db, current_workspace.id)
    insights = workspace_equipment_insights(db, current_workspace.id, policy_key)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        (
            "workspace",
            "registry_reference",
            "equipment_id",
            "description",
            "manufacturer",
            "model_number",
            "serial_number",
            "calibration_date",
            "calibration_due_date",
            "equipment_status",
            "run_id",
            "alm_run_id",
            "testcase_id",
            "test_name",
            "step",
            "execution_date",
            "review_status",
            "review_code",
            "matched_by",
        )
    )
    for device in insights["devices"]:
        for reference in device["references"]:
            writer.writerow(
                _csv_value(value)
                for value in (
                    current_workspace.name,
                    device["registry_reference"],
                    device["equipment_id"],
                    device["description"],
                    device["manufacturer"],
                    device["model_number"],
                    device["serial_number"],
                    device["calibration_date"],
                    device["calibration_due_date"],
                    device["equipment_status"],
                    reference["run_id"],
                    reference["alm_run_id"],
                    reference["test_id"],
                    reference["test_name"],
                    reference["step"],
                    reference["execution_date"],
                    reference["status"],
                    reference["code"],
                    ", ".join(reference["matched_by"]),
                )
            )
    filename = f"{workspace_slug(current_workspace.name)}-equipment-usage.csv"
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/exports/reviews.csv")
def export_reviews(
    status: str = Query(default="all"),
    tester: str = Query(default="all"),
    owner: str = Query(default="all"),
    query: str = Query(default=""),
    search_steps: bool = Query(default=False),
    workspace: int | None = None,
    db: Session = Depends(get_db),
) -> Response:
    include_legacy = workspace is None
    current_workspace = resolve_workspace(db, workspace)
    runs = db.scalars(
        select(AlmRun)
        .where(
            or_(
                AlmRun.workspace_id == current_workspace.id,
                include_legacy and AlmRun.workspace_id.is_(None),
            )
        )
        .order_by(desc(AlmRun.execution_at), desc(AlmRun.run_id))
    ).all()
    policy_key = current_review_policy_key(db, current_workspace.id)
    users = {user.code1_id: user for user in db.scalars(select(AlmUser)).all()}
    step_matches = (
        _step_text_run_ids(db, current_workspace.id, include_legacy, query)
        if search_steps
        else None
    )
    views = [
        item
        for run in runs
        if _matches_dashboard_filters(
            item := _run_view(db, run, policy_key, users),
            status,
            tester,
            owner,
            query,
            step_matches,
        )
    ]

    revision_ids = {
        item["run"].current_revision_id
        for item in views
        if item["run"].current_revision_id is not None
    }
    steps_by_revision: dict[int, list[RunStep]] = {}
    if revision_ids:
        for step in db.scalars(
            select(RunStep)
            .where(RunStep.revision_id.in_(revision_ids))
            .order_by(RunStep.revision_id, RunStep.step_order, RunStep.id)
        ).all():
            steps_by_revision.setdefault(step.revision_id, []).append(step)

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        (
            "run_id",
            "revision_id",
            "test_id",
            "test_name",
            "test_set_name",
            "folder_path",
            "execution_location",
            "actual_tester_id",
            "actual_tester",
            "test_owner_id",
            "test_owner",
            "alm_result",
            "execution_at",
            "source_hash",
            "ai_verdict",
            "final_status",
            "issue_summary",
            "steps",
            "criteria_json",
            "step_results_json",
            "warnings_json",
            "model_name",
            "review_policy_key",
            "review_completed_at",
            "manual_decision",
            "manual_operator",
            "manual_reason",
            "manual_decision_at",
        )
    )
    for item in views:
        run = item["run"]
        review = item["review"]
        result = review.result
        manual = review.manual_decision
        writer.writerow(
            _csv_value(value)
            for value in (
                run.run_id,
                run.current_revision_id,
                run.test_id,
                run.test_name,
                run.test_set_name,
                run.folder_path,
                run.execution_location,
                run.actual_tester,
                item["actual_tester_label"],
                run.test_owner,
                item["test_owner_label"],
                run.run_status,
                run.execution_at,
                run.source_hash,
                result.verdict if result else None,
                review.final_status,
                result.issue_summary if result else None,
                _format_steps_for_export(
                    steps_by_revision.get(run.current_revision_id, [])
                ),
                result.criteria_json if result else None,
                result.step_results_json if result else None,
                result.warnings_json if result else None,
                result.model_name if result else None,
                result.review_policy_key if result else None,
                result.completed_at if result else None,
                manual.decision if manual else None,
                manual.operator if manual else None,
                manual.reason if manual else None,
                manual.created_at if manual else None,
            )
        )
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="alm-review-results.csv"'},
    )


@router.get("/runs", include_in_schema=False)
def runs_index() -> RedirectResponse:
    return RedirectResponse("/", status_code=307)


@router.get("/run", include_in_schema=False)
def run_index() -> RedirectResponse:
    return RedirectResponse("/", status_code=307)


@router.get("/runs/{run_id}")
def run_detail(request: Request, run_id: int, db: Session = Depends(get_db)):
    run = db.get(AlmRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    policy_key = current_review_policy_key(db, run.workspace_id)
    review = current_review(db, run, policy_key)
    active_prompt = db.scalar(
        select(PromptVersion)
        .where(PromptVersion.is_active.is_(True))
        .order_by(desc(PromptVersion.id))
        .limit(1)
    )
    ai_config = db.get(AiConfig, 1)
    update_reasons = review_update_reasons(
        review.result,
        policy_key,
        active_prompt.id if active_prompt else None,
        ai_config.model_name if ai_config else None,
    )
    users = {user.code1_id: user for user in db.scalars(select(AlmUser)).all()}
    steps = []
    if run.current_revision_id:
        current_revision = db.get(RunRevision, run.current_revision_id)
        source_fields = snapshot_step_fields(
            current_revision.snapshot_json if current_revision else ""
        )
        stored_steps = db.scalars(
            select(RunStep)
            .where(RunStep.revision_id == run.current_revision_id)
            .order_by(RunStep.step_order, RunStep.id)
        ).all()
        steps = [
            SimpleNamespace(
                step_order=step.step_order,
                name=step.name,
                status=step.status,
                description=render_alm_rich_text(
                    source_fields.get(step.step_id, {}).get(
                        "description", step.description
                    )
                    or "-"
                ),
                expected=render_alm_rich_text(
                    source_fields.get(step.step_id, {}).get("expected", step.expected)
                    or "-"
                ),
                actual=render_alm_rich_text(
                    source_fields.get(step.step_id, {}).get("actual", step.actual)
                    or "-"
                ),
            )
            for step in stored_steps
        ]
    revisions = db.scalars(
        select(RunRevision)
        .where(RunRevision.run_id == run_id)
        .order_by(desc(RunRevision.revision_number))
    ).all()
    results = db.scalars(
        select(ReviewResult)
        .where(ReviewResult.run_id == run_id)
        .order_by(desc(ReviewResult.completed_at))
    ).all()
    review_completed_at = _to_app_timezone(
        review.result.completed_at if review.result else None
    )
    manual_decision_at = _to_app_timezone(
        review.manual_decision.created_at if review.manual_decision else None
    )
    revision_created_at = {
        revision.id: _to_app_timezone(revision.created_at)
        for revision in revisions
    }
    allowed_decisions = {
        "needs_manual_review": [
            ("confirmed_qualified", "确认合格"),
            ("confirmed_unqualified", "确认不合格"),
        ],
        "unqualified": [("override_qualified", "人工判定合格")],
    }.get(review.result.verdict if review.result else "", [])
    review_criteria = {}
    review_step_results = []
    review_warnings = []
    review_pipeline = {}
    if review.result and review.result.criteria_json:
        try:
            review_criteria = json.loads(review.result.criteria_json)
        except json.JSONDecodeError:
            review_criteria = {}
    if review.result and review.result.step_results_json:
        try:
            review_step_results = json.loads(review.result.step_results_json)
        except json.JSONDecodeError:
            review_step_results = []
    if review.result and review.result.warnings_json:
        try:
            review_warnings = json.loads(review.result.warnings_json)
        except json.JSONDecodeError:
            review_warnings = []
    if review.result and review.result.pipeline_json:
        try:
            review_pipeline = json.loads(review.result.pipeline_json)
        except json.JSONDecodeError:
            review_pipeline = {}
    step_review_map = {
        item.get("review_step"): item
        for item in review_step_results
        if isinstance(item, dict)
    }
    review_skill_traces = _pipeline_skill_traces(review_pipeline)
    active_sync_job = db.scalar(
        select(SyncJob)
        .where(
            SyncJob.run_id == run_id,
            SyncJob.status.in_(("queued", "running")),
        )
        .order_by(desc(SyncJob.id))
        .limit(1)
    )
    active_review_job = active_run_review_job(db, run)
    latest_review_job = db.scalar(
        select(ReviewJob)
        .where(
            ReviewJob.run_id == run.run_id,
            ReviewJob.revision_id == run.current_revision_id,
        )
        .order_by(desc(ReviewJob.id))
        .limit(1)
    )
    return templates.TemplateResponse(
        request=request,
        name="run_detail.html",
        context={
            "run": run,
            "test_owner_label": _person_label(run.test_owner, users),
            "assigned_tester_label": _person_label(run.assigned_tester, users),
            "actual_tester_label": _person_label(run.actual_tester, users),
            "review": review,
            "review_update_reasons": update_reasons,
            "active_review_job": active_review_job,
            "latest_review_job": latest_review_job,
            "latest_review_job_error": _public_job_error(
                latest_review_job.error_message if latest_review_job else None
            ),
            "active_sync_job": active_sync_job,
            "steps": steps,
            "revisions": revisions,
            "results": results,
            "allowed_decisions": allowed_decisions,
            "review_criteria": review_criteria,
            "review_pipeline": review_pipeline,
            "review_skill_traces": review_skill_traces,
            "review_step_results": review_step_results,
            "step_review_map": step_review_map,
            "review_warnings": review_warnings,
            "review_completed_at": review_completed_at,
            "manual_decision_at": manual_decision_at,
            "revision_created_at": revision_created_at,
            "status_label": STATUS_LABELS[review.final_status],
            "message": request.query_params.get("message"),
            "message_kind": request.query_params.get("message_kind", "success"),
        },
    )


@router.post("/runs/{run_id}/manual-decision")
def decide_run(
    request: Request,
    run_id: int,
    decision: str = Form(...),
    reason: str = Form(...),
    db: Session = Depends(get_db),
):
    run = db.get(AlmRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    # No login exists, so the caller address is the only attributable identity.
    operator = request.client.host if request.client else "web"
    try:
        save_manual_decision(db, run, decision, operator, reason)
    except ValueError as exc:
        return _redirect(f"/runs/{run_id}", str(exc), "error")
    return _redirect(f"/runs/{run_id}", "人工裁决已记录。")


@router.post("/runs/{run_id}/review-now")
def review_run_now(
    run_id: int,
    force_new: bool = Form(False),
    db: Session = Depends(get_db),
):
    run = db.get(AlmRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if not _has_enabled_ai_endpoint(db):
        return _redirect(
            f"/runs/{run_id}",
            "尚未配置 AI 评审。",
            "error",
        )
    if run.current_revision_id is None:
        return _redirect(f"/runs/{run_id}", "该运行没有当前版本。", "error")
    manual = manual_decision_locks_run(db, run)
    if manual is not None:
        return _redirect(
            f"/runs/{run_id}",
            f"{manual.operator} 已人工处理此版本。"
            "如果内容已变化，请先从 ALM 刷新。",
            "error",
        )
    queue_run_review(db, run)
    return _redirect(
        f"/runs/{run_id}",
        "AI 评审已加入 Worker 队列。"
        + ("此前的评审历史将保留。" if force_new else ""),
    )


@router.post("/runs/{run_id}/refresh")
def refresh_run_from_alm(run_id: int, db: Session = Depends(get_db)):
    run = db.get(AlmRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.test_instance_id is None:
        return _redirect(
            f"/runs/{run_id}",
            "This Run has no ALM test instance reference to refresh from.",
            "error",
        )
    queued = queue_sync_job(db, "web", run.workspace_id, run_id=run.run_id)
    if not queued.created:
        return _redirect(
            f"/runs/{run_id}",
            "A refresh is already queued for this Run.",
            "error",
        )
    return _redirect(
        f"/runs/{run_id}",
        "ALM refresh queued. The Worker will re-import this Run and then review it.",
    )


@router.post("/runs/{run_id}/delete")
def delete_run(
    run_id: int,
    confirmation: str = Form(...),
    db: Session = Depends(get_db),
):
    run = db.get(AlmRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    display_id = str(run.alm_run_id or run.run_id)
    if confirmation.strip() != display_id:
        return _redirect(
            f"/runs/{run_id}",
            f"Run ID confirmation did not match {display_id}. Nothing was deleted.",
            "error",
        )
    workspace_id = run.workspace_id
    try:
        delete_local_run(db, run)
    except ValueError as exc:
        return _redirect(f"/runs/{run_id}", str(exc), "error")
    dashboard = f"/?workspace={workspace_id}" if workspace_id is not None else "/"
    return _redirect(
        dashboard,
        f"Run {display_id} and its local review history were deleted. If the Run "
        "still exists in ALM, a future sync may import it again.",
    )


@router.post("/actions/import-snapshot")
def import_snapshot(
    db: Session = Depends(get_db),
    workspace_id: int | None = Form(None),
):
    explicit_workspace = isinstance(workspace_id, int)
    workspace = resolve_workspace(db, workspace_id)
    redirect_path = f"/?workspace={workspace.id}" if explicit_workspace else "/"
    path = get_settings().import_path
    if path is None:
        return _redirect(redirect_path, "Snapshot import path is not configured.", "error")
    if not path.exists():
        return _redirect(redirect_path, f"Import file not found: {path}", "error")
    try:
        result = import_file(path, db, workspace.id)
    except Exception as exc:
        return _redirect(redirect_path, f"Import failed: {exc}", "error")
    return _redirect(
        redirect_path,
        f"Sync complete: {result.new_runs} new, {result.changed_runs} changed, "
        f"{result.unchanged_runs} unchanged.",
    )


@router.post("/actions/import-docx")
async def import_docx(
    document: UploadFile = File(...),
    workspace_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    workspace = resolve_workspace(db, workspace_id)
    redirect_path = f"/?workspace={workspace.id}"
    filename = Path(document.filename or "ALM export.docx").name
    if Path(filename).suffix.casefold() != ".docx":
        await document.close()
        return _redirect(redirect_path, "Select an ALM Word export (.docx).", "error")
    content = await document.read(MAX_DOCX_BYTES + 1)
    await document.close()
    try:
        data = parse_alm_docx(content, filename)
        result = import_data(
            data,
            db,
            source=f"docx:{filename}",
            workspace_id=workspace.id,
        )
    except Exception as exc:
        db.rollback()
        return _redirect(redirect_path, f"Word import failed: {exc}", "error")
    return _redirect(
        redirect_path,
        f"Word import completed: {result.discovered_runs} Passed Runs; "
        f"{result.new_runs} added, {result.changed_runs} changed, "
        f"{result.unchanged_runs} unchanged. Reviews were not queued.",
    )


@router.post("/actions/sync-alm")
def sync_alm(
    db: Session = Depends(get_db),
    workspace_id: int | None = Form(None),
):
    explicit_workspace = isinstance(workspace_id, int)
    workspace = resolve_workspace(db, workspace_id)
    redirect_path = f"/?workspace={workspace.id}" if explicit_workspace else "/"
    config = workspace_sync_config(db, workspace.id)
    if config is None:
        return _redirect(redirect_path, "No ALM synchronization scope is configured.", "error")
    result = queue_sync_job(db, requested_by="web", workspace_id=workspace.id)
    message = (
        f"ALM synchronization queued as job {result.job.id}."
        if result.created
        else f"ALM synchronization job {result.job.id} is already {result.job.status}."
    )
    return _redirect(redirect_path, message)


@router.post("/actions/process-reviews")
def process_reviews(
    db: Session = Depends(get_db),
    workspace_id: int | None = Form(None),
):
    workspace = resolve_workspace(db, workspace_id)
    redirect_path = f"/?workspace={workspace.id}"
    if not _has_enabled_ai_endpoint(db):
        return _redirect(
            redirect_path,
            "AI review is disabled. Configure the local model before processing.",
            "error",
        )
    result = queue_latest_alm_changes(db, workspace.id)
    if result.sync_id is None:
        return _redirect(
            redirect_path,
            "No completed ALM synchronization is available.",
            "error",
        )
    return _redirect(
        redirect_path,
        f"Latest ALM changes: {result.changed} review-content changes found; "
        f"{result.queued} queued, {result.already_reviewed} already reviewed, "
        f"{result.already_active} already waiting or running.",
    )


@router.post("/actions/cancel-review-jobs")
def cancel_review_jobs(
    workspace_id: int = Form(...),
    job_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    workspace = db.get(Workspace, workspace_id)
    if workspace is None or workspace.archived:
        return _redirect("/", "Workspace not found.", "error")
    removed = cancel_queued_reviews(db, workspace.id, job_id)
    if job_id is not None:
        message = (
            f"Queued Review job {job_id} removed."
            if removed
            else f"Review job {job_id} is no longer waiting and was not removed."
        )
    else:
        message = f"Removed {removed} queued Review jobs from {workspace.name}."
    return _redirect(f"/?workspace={workspace.id}", message)


@router.post("/actions/retry-failed-reviews")
def retry_failed_reviews(
    workspace_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    workspace = resolve_workspace(db, workspace_id)
    redirect_path = f"/?workspace={workspace.id}"
    if not _has_enabled_ai_endpoint(db):
        return _redirect(
            redirect_path,
            "AI review is disabled. Configure the model before retrying.",
            "error",
        )
    queued = queue_failed_reviews(db, workspace.id)
    return _redirect(
        redirect_path,
        f"Failed Review retry queued: {queued} Runs. Previous failures were retained.",
    )


@router.post("/actions/rereview")
def rereview_runs(
    scope: str = Form(...),
    db: Session = Depends(get_db),
    workspace_id: int | None = Form(None),
):
    workspace = resolve_workspace(db, workspace_id)
    redirect_path = f"/?workspace={workspace.id}"
    if not _has_enabled_ai_endpoint(db):
        return _redirect(
            redirect_path,
            "AI review is disabled. Configure the model before re-reviewing.",
            "error",
        )
    if scope not in REREVIEW_SCOPES:
        return _redirect(redirect_path, "Invalid re-review scope.", "error")
    result = queue_rereviews(db, scope, workspace.id)
    message = (
        f"Re-review queued: {result.queued} of {result.matched} matching Runs. "
        f"{result.already_active} already active, "
        f"{result.manually_resolved} skipped as manually resolved. "
        "Previous results were retained."
    )
    if result.queued and workspace.review_queue_paused:
        message += " The Review queue is paused, so these jobs wait until you resume it."
    return _redirect(redirect_path, message)


@router.post("/actions/rereview-filtered")
def rereview_filtered_runs(
    status: str = Form(default="all"),
    tester: str = Form(default="all"),
    owner: str = Form(default="all"),
    query: str = Form(default=""),
    search_steps: bool = Form(default=False),
    db: Session = Depends(get_db),
    workspace_id: int | None = Form(None),
):
    include_legacy = workspace_id is None
    workspace = resolve_workspace(db, workspace_id)
    redirect_path = _dashboard_filter_path(
        workspace.id, status, tester, owner, query, search_steps
    )
    if not _has_enabled_ai_endpoint(db):
        return _redirect(
            redirect_path,
            "AI review is disabled. Configure the model before re-reviewing.",
            "error",
        )
    run_ids = _filtered_run_ids(
        db, workspace.id, include_legacy, status, tester, owner, query, search_steps
    )
    result = queue_rereviews_for_run_ids(db, run_ids, workspace.id)
    message = (
        f"Filtered re-review queued: {result.queued} of {result.matched} matching Runs. "
        f"{result.already_active} already active, "
        f"{result.manually_resolved} skipped as manually resolved. "
        "Previous results were retained."
    )
    if result.queued and workspace.review_queue_paused:
        message += " The Review queue is paused, so these jobs wait until you resume it."
    return _redirect(redirect_path, message)


@router.post("/actions/sync-review-filtered")
def sync_and_review_filtered_runs(
    status: str = Form(default="all"),
    tester: str = Form(default="all"),
    owner: str = Form(default="all"),
    query: str = Form(default=""),
    search_steps: bool = Form(default=False),
    db: Session = Depends(get_db),
    workspace_id: int | None = Form(None),
):
    include_legacy = workspace_id is None
    workspace = resolve_workspace(db, workspace_id)
    redirect_path = _dashboard_filter_path(
        workspace.id, status, tester, owner, query, search_steps
    )
    if workspace_sync_config(db, workspace.id) is None:
        return _redirect(
            redirect_path,
            "No ALM synchronization scope is configured.",
            "error",
        )
    if not _has_enabled_ai_endpoint(db):
        return _redirect(
            redirect_path,
            "AI review is disabled. Configure the model before re-reviewing.",
            "error",
        )
    run_ids = _filtered_run_ids(
        db, workspace.id, include_legacy, status, tester, owner, query, search_steps
    )
    result = queue_run_sync_jobs(db, run_ids, workspace.id)
    message = (
        f"ALM refresh queued for {result.queued} of {result.matched} matching Runs; "
        "each one is reviewed automatically once its import lands. "
        f"{result.already_active} already queued, "
        f"{result.skipped} skipped without an ALM test instance."
    )
    if result.queued and workspace.sync_queue_paused:
        message += " The Sync queue is paused, so these jobs wait until you resume it."
    return _redirect(redirect_path, message)


@router.get("/ops/equipment")
def equipment_registry(
    request: Request,
    query: str = Query(default=""),
    status: str = Query(default="all"),
    calibration: str = Query(default="all"),
    db: Session = Depends(get_db),
):
    statement = select(EquipmentRegistry)
    normalized_query = query.strip().casefold()
    if normalized_query:
        search_value = f"%{normalized_query}%"
        statement = statement.where(
            or_(
                func.lower(EquipmentRegistry.equipment_id).like(search_value),
                func.lower(EquipmentRegistry.description).like(search_value),
                func.lower(EquipmentRegistry.serial_number).like(search_value),
                func.lower(EquipmentRegistry.model_number).like(search_value),
                func.lower(EquipmentRegistry.manufacturer).like(search_value),
                func.lower(EquipmentRegistry.equipment_user).like(search_value),
            )
        )
    if status != "all":
        statement = statement.where(EquipmentRegistry.equipment_status == status)

    today = datetime.now(ZoneInfo(get_settings().app_timezone)).date()
    due_soon = today + timedelta(days=30)
    if calibration == "expired":
        statement = statement.where(EquipmentRegistry.calibration_due_date < today)
    elif calibration == "due_soon":
        statement = statement.where(
            EquipmentRegistry.calibration_due_date >= today,
            EquipmentRegistry.calibration_due_date <= due_soon,
        )
    elif calibration == "current":
        statement = statement.where(EquipmentRegistry.calibration_due_date > due_soon)
    elif calibration == "missing":
        statement = statement.where(EquipmentRegistry.calibration_due_date.is_(None))

    equipment = db.scalars(
        statement.order_by(
            EquipmentRegistry.calibration_due_date.is_(None),
            EquipmentRegistry.calibration_due_date,
            EquipmentRegistry.equipment_id,
            EquipmentRegistry.id,
        )
    ).all()
    all_items = db.scalars(select(EquipmentRegistry)).all()
    statuses = db.scalars(
        select(EquipmentRegistry.equipment_status)
        .where(EquipmentRegistry.equipment_status != "")
        .distinct()
        .order_by(EquipmentRegistry.equipment_status)
    ).all()
    imports = db.scalars(
        select(EquipmentImportHistory)
        .order_by(desc(EquipmentImportHistory.imported_at), desc(EquipmentImportHistory.id))
        .limit(10)
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="equipment_registry.html",
        context={
            "equipment": equipment,
            "total_equipment": len(all_items),
            "expired_count": sum(
                item.calibration_due_date is not None
                and item.calibration_due_date < today
                for item in all_items
            ),
            "due_soon_count": sum(
                item.calibration_due_date is not None
                and today <= item.calibration_due_date <= due_soon
                for item in all_items
            ),
            "missing_due_date_count": sum(
                item.calibration_due_date is None for item in all_items
            ),
            "statuses": statuses,
            "imports": imports,
            "query": query,
            "selected_status": status,
            "selected_calibration": calibration,
            "today": today,
            "due_soon": due_soon,
            "message": request.query_params.get("message"),
            "message_kind": request.query_params.get("message_kind", "success"),
        },
    )


@router.post("/ops/equipment/import")
async def import_equipment(
    workbook: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    filename = Path(workbook.filename or "equipment.xlsx").name
    content = await workbook.read(10 * 1024 * 1024 + 1)
    await workbook.close()
    try:
        result = import_equipment_workbook(db, filename, content)
    except Exception as exc:
        db.rollback()
        return _redirect("/ops/equipment", f"Equipment import failed: {exc}", "error")
    return _redirect(
        "/ops/equipment",
        f"Imported {result.total} rows: {result.inserted} added, "
        f"{result.updated} updated, {result.unchanged} unchanged.",
    )


@router.get("/ops/equipment/new")
def new_equipment(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="equipment_form.html",
        context={
            "equipment": None,
            "form_title": "Add equipment",
            "message": request.query_params.get("message"),
            "message_kind": request.query_params.get("message_kind", "success"),
        },
    )


@router.post("/ops/equipment/new")
def create_equipment(
    values: dict[str, str] = Depends(_equipment_form),
    db: Session = Depends(get_db),
):
    normalized_id = values["equipment_id"].strip().upper()
    existing = (
        db.scalar(
            select(EquipmentRegistry.id).where(
                func.upper(EquipmentRegistry.equipment_id) == normalized_id
            )
        )
        if normalized_id
        else None
    )
    if not values["description"].strip():
        return _redirect("/ops/equipment/new", "Equipment name is required.", "error")
    identity_error = optional_equipment_identity_error(
        db,
        values["equipment_id"],
        values["serial_number"],
    )
    if identity_error:
        return _redirect("/ops/equipment/new", identity_error, "error")
    if existing is not None:
        return _redirect("/ops/equipment/new", "Equipment ID already exists.", "error")
    equipment = EquipmentRegistry(equipment_id=normalized_id or None, description="")
    try:
        _set_equipment_values(equipment, values)
    except ValueError as exc:
        return _redirect("/ops/equipment/new", f"Invalid date: {exc}", "error")
    db.add(equipment)
    db.commit()
    return _redirect(
        "/ops/equipment",
        f"Equipment {_equipment_display_identifier(equipment)} added.",
    )


@router.get("/ops/equipment/{equipment_pk}/edit")
def edit_equipment(equipment_pk: int, request: Request, db: Session = Depends(get_db)):
    equipment = db.get(EquipmentRegistry, equipment_pk)
    if equipment is None:
        raise HTTPException(status_code=404, detail="Equipment not found")
    return templates.TemplateResponse(
        request=request,
        name="equipment_form.html",
        context={
            "equipment": equipment,
            "form_title": "Edit equipment",
            "message": request.query_params.get("message"),
            "message_kind": request.query_params.get("message_kind", "success"),
        },
    )


@router.post("/ops/equipment/{equipment_pk}/edit")
def update_equipment(
    equipment_pk: int,
    values: dict[str, str] = Depends(_equipment_form),
    db: Session = Depends(get_db),
):
    equipment = db.get(EquipmentRegistry, equipment_pk)
    if equipment is None:
        raise HTTPException(status_code=404, detail="Equipment not found")
    normalized_id = values["equipment_id"].strip().upper()
    duplicate = (
        db.scalar(
            select(EquipmentRegistry.id).where(
                func.upper(EquipmentRegistry.equipment_id) == normalized_id,
                EquipmentRegistry.id != equipment_pk,
            )
        )
        if normalized_id
        else None
    )
    if not values["description"].strip():
        return _redirect(
            f"/ops/equipment/{equipment_pk}/edit",
            "Equipment name is required.",
            "error",
        )
    identity_error = optional_equipment_identity_error(
        db,
        values["equipment_id"],
        values["serial_number"],
        exclude_pk=equipment_pk,
    )
    if identity_error:
        return _redirect(
            f"/ops/equipment/{equipment_pk}/edit",
            identity_error,
            "error",
        )
    if duplicate is not None:
        return _redirect(
            f"/ops/equipment/{equipment_pk}/edit",
            "Equipment ID already exists.",
            "error",
        )
    try:
        _set_equipment_values(equipment, values)
    except ValueError as exc:
        return _redirect(
            f"/ops/equipment/{equipment_pk}/edit", f"Invalid date: {exc}", "error"
        )
    db.commit()
    return _redirect(
        "/ops/equipment",
        f"Equipment {_equipment_display_identifier(equipment)} updated.",
    )


@router.post("/ops/equipment/{equipment_pk}/delete")
def delete_equipment(equipment_pk: int, db: Session = Depends(get_db)):
    equipment = db.get(EquipmentRegistry, equipment_pk)
    if equipment is None:
        raise HTTPException(status_code=404, detail="Equipment not found")
    equipment_identifier = _equipment_display_identifier(equipment)
    db.delete(equipment)
    db.commit()
    return _redirect("/ops/equipment", f"Equipment {equipment_identifier} deleted.")


@router.get("/ops/configuration")
def configuration(
    request: Request,
    workspace: int | None = None,
    db: Session = Depends(get_db),
):
    current_workspace = resolve_workspace(db, workspace)
    workspaces = db.scalars(
        select(Workspace)
        .where(Workspace.archived.is_(False))
        .order_by(Workspace.name)
    ).all()
    sync_config = workspace_sync_config(db, current_workspace.id)
    ai_config = db.get(AiConfig, 1)
    ai_config_secondary = db.get(AiConfig, 2) or AiConfig(id=2, enabled=False)
    evidence_config = workspace_evidence_config(db, current_workspace.id)
    prompt = db.scalar(
        select(PromptVersion)
        .where(PromptVersion.is_active.is_(True))
        .order_by(desc(PromptVersion.id))
        .limit(1)
    )
    source_locked = bool(
        db.scalar(
            select(AlmRun.run_id)
            .where(AlmRun.workspace_id == current_workspace.id)
            .limit(1)
        )
    )
    # A source field only locks once it holds a value, so blanks stay fillable.
    locked_source_fields = {
        "server_url": source_locked and bool(sync_config and sync_config.server_url.strip()),
        "domain": source_locked and bool(sync_config and sync_config.domain.strip()),
        "project": source_locked and bool(sync_config and sync_config.project.strip()),
        "folder_id": source_locked and bool(sync_config and sync_config.folder_id),
    }
    equipment_areas = db.scalars(
        select(EquipmentRegistry.subordinate_area)
        .where(EquipmentRegistry.subordinate_area != "")
        .distinct()
        .order_by(EquipmentRegistry.subordinate_area)
    ).all()
    project_options = db.scalars(
        select(Workspace.project)
        .where(Workspace.project != "", Workspace.archived.is_(False))
        .distinct()
        .order_by(Workspace.project)
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="configuration.html",
        context={
            "sync_config": sync_config,
            "workspaces": workspaces,
            "current_workspace": current_workspace,
            "source_locked": source_locked,
            "locked_source_fields": locked_source_fields,
            "equipment_areas": equipment_areas,
            "project_options": project_options,
            "ai_config": ai_config,
            "ai_config_secondary": ai_config_secondary,
            "ai_config_health_status": ai_endpoint_health_status(ai_config),
            "ai_config_secondary_health_status": ai_endpoint_health_status(
                ai_config_secondary
            ),
            "evidence_config": evidence_config,
            "prompt": prompt,
            "skill_catalog": _skill_catalog(),
            "app_timezone": get_settings().app_timezone,
            "message": request.query_params.get("message"),
            "message_kind": request.query_params.get("message_kind", "success"),
        },
    )


@router.post("/ops/test-ai")
def test_ai(
    ai_base_url: str = Form(...),
    model_name: str = Form(...),
    timeout_seconds: int = Form(...),
    ai_api_key: str = Form(""),
    clear_ai_api_key: bool = Form(False),
    ai_config_id: int = Form(1),
    ai_base_url_2: str = Form(""),
    model_name_2: str = Form(""),
    timeout_seconds_2: int = Form(120),
    ai_api_key_2: str = Form(""),
    clear_ai_api_key_2: bool = Form(False),
    workspace_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    redirect_path = (
        f"/ops/configuration?workspace={workspace_id}"
        if workspace_id is not None
        else "/ops/configuration"
    )
    if get_settings().app_role == "web":
        return _redirect(
            redirect_path,
            "AI connectivity must be tested on the laptop worker.",
            "error",
        )
    selected_id = 2 if ai_config_id == 2 else 1
    selected_base_url = ai_base_url_2 if selected_id == 2 else ai_base_url
    selected_model_name = model_name_2 if selected_id == 2 else model_name
    selected_timeout = timeout_seconds_2 if selected_id == 2 else timeout_seconds
    selected_api_key = ai_api_key_2 if selected_id == 2 else ai_api_key
    clear_selected_key = (
        clear_ai_api_key_2 if selected_id == 2 else clear_ai_api_key
    )
    saved_config = db.get(AiConfig, selected_id)
    submitted_api_key = selected_api_key.strip()
    ai_config = AiConfig(
        id=selected_id,
        base_url=selected_base_url.strip(),
        model_name=selected_model_name.strip(),
        api_key=(
            ""
            if clear_selected_key
            else submitted_api_key or (saved_config.api_key if saved_config else "")
        ),
        timeout_seconds=max(1, selected_timeout),
        enabled=False,
    )
    try:
        test_ai_connection(ai_config)
    except Exception as exc:
        return _redirect(redirect_path, f"AI connection failed: {exc}", "error")
    return _redirect(
        redirect_path,
        f"AI connection succeeded: {ai_config.model_name} at {ai_config.base_url}",
    )


@router.post("/ops/configuration")
def save_configuration(
    request: Request,
    workspace_id: int = Form(...),
    workspace_name: str = Form(...),
    workspace_project: str = Form(""),
    server_url: str = Form(...),
    domain: str = Form(...),
    project: str = Form(...),
    folder_id: int = Form(...),
    folder_path: str = Form(""),
    schedule_time: str = Form(...),
    sync_enabled: bool = Form(False),
    auto_review_after_sync: bool = Form(False),
    ai_base_url: str = Form(...),
    model_name: str = Form(...),
    timeout_seconds: int = Form(...),
    ai_api_key: str = Form(""),
    clear_ai_api_key: bool = Form(False),
    review_concurrency: int = Form(1),
    ai_enabled: bool = Form(False),
    ai_base_url_2: str = Form(""),
    model_name_2: str = Form(""),
    timeout_seconds_2: int = Form(120),
    ai_api_key_2: str = Form(""),
    clear_ai_api_key_2: bool = Form(False),
    review_concurrency_2: int = Form(1),
    ai_enabled_2: bool = Form(False),
    allowed_network_root: str = Form(""),
    local_html_fallback_root: str = Form(""),
    automation_release_project_name: str = Form(""),
    external_evidence_review_enabled: bool = Form(False),
    equipment_review_enabled: bool = Form(False),
    equipment_area_filter: str = Form(""),
    review_queue_paused: bool = Form(False),
    sync_queue_paused: bool = Form(False),
    queue_priority: int = Form(0),
    prompt_name: str = Form(...),
    prompt_template: str = Form(...),
    db: Session = Depends(get_db),
):
    workspace = db.get(Workspace, workspace_id)
    if workspace is None or workspace.archived:
        return _redirect("/ops/configuration", "Workspace not found.", "error")
    redirect_path = f"/ops/configuration?workspace={workspace.id}"
    secondary_base_url = ai_base_url_2 if isinstance(ai_base_url_2, str) else ""
    secondary_model_name = model_name_2 if isinstance(model_name_2, str) else ""
    secondary_timeout = timeout_seconds_2 if isinstance(timeout_seconds_2, int) else 120
    secondary_api_key = ai_api_key_2 if isinstance(ai_api_key_2, str) else ""
    secondary_clear_key = clear_ai_api_key_2 is True
    secondary_concurrency = (
        review_concurrency_2 if isinstance(review_concurrency_2, int) else 1
    )
    secondary_enabled = ai_enabled_2 is True
    try:
        schedule_hour_text, schedule_minute_text = schedule_time.split(":", 1)
        schedule_hour = int(schedule_hour_text)
        schedule_minute = int(schedule_minute_text)
    except (TypeError, ValueError):
        return _redirect(redirect_path, "Invalid schedule time.", "error")
    if (
        len(schedule_hour_text) != 2
        or len(schedule_minute_text) != 2
        or not schedule_hour_text.isdigit()
        or not schedule_minute_text.isdigit()
        or not 0 <= schedule_hour <= 23
        or not 0 <= schedule_minute <= 59
    ):
        return _redirect(redirect_path, "Invalid schedule time.", "error")
    normalized_workspace_name = workspace_name.strip()
    if not normalized_workspace_name:
        return _redirect(redirect_path, "Workspace name is required.", "error")
    duplicate_name = db.scalar(
        select(Workspace.id).where(
            Workspace.id != workspace.id,
            func.lower(Workspace.name) == normalized_workspace_name.casefold(),
        )
    )
    if duplicate_name is not None:
        return _redirect(
            redirect_path,
            f"Another Workspace is already named {normalized_workspace_name}.",
            "error",
        )
    workspace.name = normalized_workspace_name
    workspace.project = workspace_project.strip()
    workspace.equipment_review_enabled = equipment_review_enabled
    workspace.equipment_area_filter = equipment_area_filter.strip()
    workspace.review_queue_paused = review_queue_paused
    workspace.sync_queue_paused = sync_queue_paused
    workspace.queue_priority = max(-1000, min(1000, queue_priority))
    sync_config = workspace_sync_config(db, workspace.id)
    if sync_config is None:
        sync_config = SyncConfig(
            workspace_id=workspace.id,
            name=folder_path or str(folder_id),
            server_url=server_url,
            domain=domain,
            project=project,
            folder_id=folder_id,
        )
        db.add(sync_config)
    source_locked = bool(
        db.scalar(
            select(AlmRun.run_id)
            .where(AlmRun.workspace_id == workspace.id)
            .limit(1)
        )
    )
    proposed_source = (
        server_url.strip().rstrip("/").casefold(),
        domain.strip().casefold(),
        project.strip().casefold(),
        folder_id,
    )
    current_source = (
        sync_config.server_url.rstrip("/").casefold(),
        sync_config.domain.casefold(),
        sync_config.project.casefold(),
        sync_config.folder_id,
    )
    # Only a source value that was already set is protected; blanks stay fillable.
    locked_change = source_locked and any(
        current and proposed != current
        for proposed, current in zip(proposed_source, current_source, strict=True)
    )
    if locked_change:
        db.rollback()
        return _redirect(
            redirect_path,
            "ALM source is locked after the first import. Create a new Workspace "
            "for a different project or folder.",
            "error",
        )
    duplicate_source = db.scalar(
        select(SyncConfig.id).where(
            SyncConfig.workspace_id != workspace.id,
            func.lower(SyncConfig.server_url) == proposed_source[0],
            func.lower(SyncConfig.domain) == proposed_source[1],
            func.lower(SyncConfig.project) == proposed_source[2],
            SyncConfig.folder_id == folder_id,
        )
    )
    if duplicate_source is not None:
        db.rollback()
        return _redirect(
            redirect_path,
            "This ALM project and folder are already assigned to another Workspace.",
            "error",
        )
    sync_config.server_url = server_url.strip().rstrip("/")
    sync_config.domain = domain.strip()
    sync_config.project = project.strip()
    sync_config.folder_id = folder_id
    sync_config.folder_path = folder_path.strip()
    sync_config.schedule_hour = schedule_hour
    sync_config.schedule_minute = schedule_minute
    sync_config.enabled = sync_enabled
    sync_config.auto_review_after_sync = auto_review_after_sync

    ai_config = db.get(AiConfig, 1)
    if ai_config is None:
        ai_config = AiConfig(id=1)
        db.add(ai_config)
    normalized_primary_url = ai_base_url.strip().rstrip("/")
    normalized_primary_model = model_name.strip()
    submitted_api_key = ai_api_key.strip()
    primary_api_key = (
        ""
        if clear_ai_api_key
        else submitted_api_key or ai_config.api_key
    )
    primary_connection_changed = (
        ai_config.base_url != normalized_primary_url
        or ai_config.model_name != normalized_primary_model
        or ai_config.api_key != primary_api_key
        or ai_config.timeout_seconds != max(1, timeout_seconds)
        or ai_config.enabled != ai_enabled
    )
    ai_config.base_url = normalized_primary_url
    ai_config.model_name = normalized_primary_model
    if clear_ai_api_key:
        ai_config.api_key = ""
    elif submitted_api_key:
        ai_config.api_key = submitted_api_key
    ai_config.timeout_seconds = max(1, timeout_seconds)
    ai_config.review_concurrency = max(1, min(4, review_concurrency))
    ai_config.enabled = ai_enabled
    if primary_connection_changed:
        ai_config.health_status = "healthy"
        ai_config.consecutive_failures = 0
        ai_config.cooldown_until = None
        ai_config.last_error = ""

    ai_config_secondary = db.get(AiConfig, 2)
    if ai_config_secondary is None:
        ai_config_secondary = AiConfig(id=2)
        db.add(ai_config_secondary)
    normalized_secondary_url = secondary_base_url.strip().rstrip("/")
    normalized_secondary_model = secondary_model_name.strip()
    submitted_secondary_api_key = secondary_api_key.strip()
    secondary_effective_api_key = (
        ""
        if secondary_clear_key
        else submitted_secondary_api_key or ai_config_secondary.api_key
    )
    secondary_connection_changed = (
        ai_config_secondary.base_url != normalized_secondary_url
        or ai_config_secondary.model_name != normalized_secondary_model
        or ai_config_secondary.api_key != secondary_effective_api_key
        or ai_config_secondary.timeout_seconds != max(1, secondary_timeout)
        or ai_config_secondary.enabled != secondary_enabled
    )
    ai_config_secondary.base_url = normalized_secondary_url
    ai_config_secondary.model_name = normalized_secondary_model
    if secondary_clear_key:
        ai_config_secondary.api_key = ""
    elif submitted_secondary_api_key:
        ai_config_secondary.api_key = submitted_secondary_api_key
    ai_config_secondary.timeout_seconds = max(1, secondary_timeout)
    ai_config_secondary.review_concurrency = max(
        1, min(4, secondary_concurrency)
    )
    ai_config_secondary.enabled = secondary_enabled
    if secondary_connection_changed:
        ai_config_secondary.health_status = "healthy"
        ai_config_secondary.consecutive_failures = 0
        ai_config_secondary.cooldown_until = None
        ai_config_secondary.last_error = ""

    evidence_config = workspace_evidence_config(db, workspace.id)
    if evidence_config is None:
        evidence_config = EvidenceConfig(workspace_id=workspace.id)
        db.add(evidence_config)
    evidence_config.workspace_id = workspace.id
    evidence_config.allowed_network_root = allowed_network_root.strip().rstrip("\\/")
    evidence_config.local_html_fallback_root = local_html_fallback_root.strip().rstrip("\\/")
    evidence_config.automation_release_project_name = (
        automation_release_project_name.strip()
    )
    evidence_config.external_evidence_review_enabled = (
        external_evidence_review_enabled
    )

    active_prompt = db.scalar(select(PromptVersion).where(PromptVersion.is_active.is_(True)))
    if active_prompt is None or (
        active_prompt.name != prompt_name.strip() or active_prompt.template != prompt_template
    ):
        if active_prompt is not None:
            active_prompt.is_active = False
        db.add(
            PromptVersion(
                name=prompt_name.strip(), template=prompt_template, is_active=True
            )
        )
    db.commit()
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        configure_scheduler(
            scheduler,
            request.app.state.worker_owner_token,
        )
    schedule_note = (
        " Restart the laptop Worker to load schedule-time changes."
        if get_settings().app_role == "web"
        else ""
    )
    return _redirect(
        redirect_path,
        "Configuration and schedule saved."
        + (
            " Automatic review will run after the next scheduled ALM sync."
            if sync_enabled and auto_review_after_sync
            else ""
        )
        + schedule_note,
    )


@router.post("/actions/workspace-queue")
def update_workspace_queue(
    workspace_id: int = Form(...),
    action: str = Form(...),
    db: Session = Depends(get_db),
):
    workspace = db.get(Workspace, workspace_id)
    if workspace is None or workspace.archived:
        return _redirect("/", "Workspace not found.", "error")
    redirect_path = f"/?workspace={workspace.id}"
    if action == "pause-review":
        workspace.review_queue_paused = True
        message = (
            f"Review queue paused for {workspace.name}. The current running job, if any, "
            "will finish; no new jobs from this Workspace will start."
        )
    elif action == "resume-review":
        workspace.review_queue_paused = False
        message = f"Review queue resumed for {workspace.name}."
    elif action == "pause-all":
        workspace.review_queue_paused = True
        workspace.sync_queue_paused = True
        message = (
            f"All queues paused for {workspace.name}. Running work will finish safely."
        )
    elif action == "resume-all":
        workspace.review_queue_paused = False
        workspace.sync_queue_paused = False
        message = f"All queues resumed for {workspace.name}."
    elif action == "prioritize":
        highest_priority = db.scalar(select(func.max(Workspace.queue_priority))) or 0
        workspace.queue_priority = min(1000, highest_priority + 10)
        workspace.review_queue_paused = False
        message = (
            f"{workspace.name} is now the next priority at level "
            f"{workspace.queue_priority}."
        )
    elif action == "normal-priority":
        workspace.queue_priority = 0
        message = f"{workspace.name} priority reset to normal."
    else:
        return _redirect(redirect_path, "Unknown queue action.", "error")
    db.commit()
    return _redirect(redirect_path, message)


@router.post("/ops/workspaces")
def create_workspace(
    name: str = Form(...),
    db: Session = Depends(get_db),
    mode: str = Form("empty"),
    source_workspace_id: int | None = Form(None),
):
    normalized_name = name.strip()
    if not normalized_name:
        return _redirect("/ops/configuration", "Workspace name is required.", "error")
    if db.scalar(
        select(Workspace.id).where(
            func.lower(Workspace.name) == normalized_name.casefold()
        )
    ) is not None:
        return _redirect(
            "/ops/configuration",
            f"A Workspace named {normalized_name} already exists.",
            "error",
        )
    source: Workspace | None = None
    if mode == "copy":
        source = (
            db.get(Workspace, source_workspace_id)
            if source_workspace_id is not None
            else None
        )
        if source is None or source.archived:
            return _redirect(
                "/ops/configuration",
                "The Workspace to copy was not found.",
                "error",
            )
    source_sync = workspace_sync_config(db, source.id) if source else None
    source_evidence = workspace_evidence_config(db, source.id) if source else None
    base_slug = workspace_slug(normalized_name)
    slug = base_slug
    suffix = 2
    while db.scalar(select(Workspace.id).where(Workspace.slug == slug)) is not None:
        slug = f"{base_slug}-{suffix}"
        suffix += 1
    workspace = Workspace(
        name=normalized_name,
        slug=slug,
        project=source.project if source else "",
        equipment_review_enabled=source.equipment_review_enabled if source else True,
        equipment_area_filter=source.equipment_area_filter if source else "",
        queue_priority=source.queue_priority if source else 0,
        archived=False,
    )
    db.add(workspace)
    db.flush()
    db.add(
        SyncConfig(
            workspace_id=workspace.id,
            name=normalized_name,
            server_url=source_sync.server_url if source_sync else "",
            domain=source_sync.domain if source_sync else "",
            project=source_sync.project if source_sync else "",
            # The Test Lab root stays empty so the copy points at a different folder.
            folder_id=0,
            folder_path="",
            schedule_hour=source_sync.schedule_hour if source_sync else 2,
            schedule_minute=source_sync.schedule_minute if source_sync else 0,
            enabled=False,
            auto_review_after_sync=(
                source_sync.auto_review_after_sync if source_sync else False
            ),
        )
    )
    db.add(
        EvidenceConfig(
            workspace_id=workspace.id,
            allowed_network_root=(
                source_evidence.allowed_network_root if source_evidence else ""
            ),
            local_html_fallback_root=(
                source_evidence.local_html_fallback_root if source_evidence else ""
            ),
            external_evidence_review_enabled=(
                source_evidence.external_evidence_review_enabled
                if source_evidence
                else False
            ),
        )
    )
    db.commit()
    message = (
        f"Workspace {workspace.name} created from {source.name}. "
        "Set its Test Lab root folder before syncing."
        if source
        else f"Workspace {workspace.name} created. "
        "Configure its ALM source before syncing."
    )
    return _redirect(f"/ops/configuration?workspace={workspace.id}", message)


@router.get("/api/health")
def health(db: Session = Depends(get_db)) -> dict:
    db.scalar(select(AlmRun.run_id).limit(1))
    return {"status": "ok"}