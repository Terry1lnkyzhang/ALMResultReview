from __future__ import annotations

import csv
import io
import json
import secrets
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
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
    SyncHistory,
    SyncJob,
    WorkerHeartbeat,
    Workspace,
)
from app.services.equipment_registry import import_equipment_workbook
from app.services.importer import import_file, queue_stale_reviews
from app.services.review_operations import (
    REREVIEW_SCOPES,
    latest_rereview_progress,
    queue_rereviews,
)
from app.services.review_policy import current_review_policy_key
from app.services.review_status import current_reviews
from app.services.reviews import (
    current_review,
    save_manual_decision,
    test_ai_connection,
)
from app.services.scheduler import configure_scheduler
from app.services.worker_tasks import queue_sync_job
from app.services.workspaces import (
    resolve_workspace,
    workspace_evidence_config,
    workspace_slug,
    workspace_sync_config,
)

basic_auth = HTTPBasic(auto_error=False)


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

STATUS_LABELS = {
    "qualified": "Qualified",
    "unqualified": "Unqualified",
    "needs_manual_review": "Manual review",
    "pending_review": "Pending",
    "review_failed": "Review failed",
}


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


def _run_view(
    db: Session,
    run: AlmRun,
    policy_key: str,
    users: dict[str, AlmUser],
    review=None,
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
        "status_label": STATUS_LABELS[review.final_status],
    }


def _matches_dashboard_filters(
    item: dict,
    status: str,
    tester: str,
    owner: str,
    query: str,
) -> bool:
    normalized_query = query.strip().casefold()
    return (
        (status == "all" or item["final_status"] == status)
        and (tester == "all" or (item["run"].actual_tester or "Unassigned") == tester)
        and (owner == "all" or (item["run"].test_owner or "Unassigned") == owner)
        and (
            not normalized_query
            or normalized_query in str(item["run"].run_id)
            or normalized_query in str(item["run"].test_id or "")
            or normalized_query in item["run"].test_name.casefold()
            or normalized_query in item["run"].test_set_name.casefold()
            or normalized_query in item["run"].folder_path.casefold()
            or normalized_query in item["actual_tester_label"].casefold()
            or normalized_query in item["test_owner_label"].casefold()
            or normalized_query in item["review_summary"].casefold()
        )
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


def _optional_date(value: str) -> date | None:
    return date.fromisoformat(value) if value.strip() else None


def _set_equipment_values(equipment: EquipmentRegistry, values: dict[str, str]) -> None:
    equipment.equipment_id = values["equipment_id"].strip().upper()
    equipment.description = values["description"].strip()
    equipment.manufacturer = values["manufacturer"].strip()
    equipment.model_number = values["model_number"].strip()
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
    equipment_id: str = Form(...),
    description: str = Form(...),
    manufacturer: str = Form(""),
    model_number: str = Form(""),
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
    users = {user.code1_id: user for user in db.scalars(select(AlmUser)).all()}
    reviews_by_run = current_reviews(db, runs, policy_key)
    all_views = [
        _run_view(db, run, policy_key, users, reviews_by_run[run.run_id]) for run in runs
    ]
    status_counts = Counter(item["final_status"] for item in all_views)
    tester_scope = [
        item
        for item in all_views
        if _matches_dashboard_filters(item, status, "all", owner, query)
    ]
    tester_counts = Counter(
        item["run"].actual_tester or "Unassigned" for item in tester_scope
    )
    owner_scope = [
        item
        for item in all_views
        if _matches_dashboard_filters(item, status, tester, "all", query)
    ]
    owner_counts = Counter(
        item["run"].test_owner or "Unassigned" for item in owner_scope
    )

    filtered = [
        item
        for item in all_views
        if _matches_dashboard_filters(item, status, tester, owner, query)
    ]
    recent_sync = db.scalar(
        select(SyncHistory)
        .where(SyncHistory.workspace_id == current_workspace.id)
        .order_by(desc(SyncHistory.started_at))
        .limit(1)
    )
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
    review_job_counts["failed"] = status_counts.get("review_failed", 0)
    active_sync_job = db.scalar(
        select(SyncJob)
        .where(
            SyncJob.workspace_id == current_workspace.id,
            SyncJob.status.in_(("queued", "running", "failed")),
        )
        .order_by(desc(SyncJob.created_at))
        .limit(1)
    )
    worker_heartbeat = db.scalar(
        select(WorkerHeartbeat).order_by(desc(WorkerHeartbeat.last_seen_at)).limit(1)
    )
    worker_online = bool(
        worker_heartbeat
        and worker_heartbeat.status != "offline"
        and worker_heartbeat.last_seen_at
        >= datetime.utcnow()
        - timedelta(seconds=max(15, get_settings().worker_poll_seconds * 3))
    )
    rereview_progress = latest_rereview_progress(db, current_workspace.id)
    rereview_started_at = (
        rereview_progress.created_at.replace(tzinfo=UTC).astimezone(
            ZoneInfo(get_settings().app_timezone)
        )
        if rereview_progress is not None
        else None
    )
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "runs": filtered,
            "workspaces": workspaces,
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
            "recent_sync": recent_sync,
            "review_job_counts": review_job_counts,
            "active_sync_job": active_sync_job,
            "worker_heartbeat": worker_heartbeat,
            "worker_online": worker_online,
            "rereview_progress": rereview_progress,
            "rereview_started_at": rereview_started_at,
            "message": request.query_params.get("message"),
            "message_kind": request.query_params.get("message_kind", "success"),
        },
    )


@router.get("/api/review-progress")
def review_progress(workspace: int | None = None, db: Session = Depends(get_db)):
    current_workspace = resolve_workspace(db, workspace)
    progress = latest_rereview_progress(db, current_workspace.id)
    if progress is None:
        return {"available": False}
    return {
        "available": True,
        "total": progress.total,
        "processed": progress.processed,
        "completed": progress.completed,
        "remaining": progress.remaining,
        "queued": progress.queued,
        "running": progress.running,
        "retrying": progress.retrying,
        "failed": progress.failed,
        "skipped": progress.skipped,
        "percent": progress.percent,
    }


@router.get("/api/sync-progress")
def sync_progress(workspace: int | None = None, db: Session = Depends(get_db)):
    current_workspace = resolve_workspace(db, workspace)
    job = db.scalar(
        select(SyncJob)
        .where(SyncJob.workspace_id == current_workspace.id)
        .order_by(desc(SyncJob.created_at), desc(SyncJob.id))
        .limit(1)
    )
    if job is None:
        return {"available": False}
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
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error": job.error_message,
    }


@router.get("/exports/reviews.csv")
def export_reviews(
    status: str = Query(default="all"),
    tester: str = Query(default="all"),
    owner: str = Query(default="all"),
    query: str = Query(default=""),
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
    views = [
        item
        for run in runs
        if _matches_dashboard_filters(
            item := _run_view(db, run, policy_key, users),
            status,
            tester,
            owner,
            query,
        )
    ]

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
    review = current_review(db, run)
    users = {user.code1_id: user for user in db.scalars(select(AlmUser)).all()}
    steps = []
    if run.current_revision_id:
        steps = db.scalars(
            select(RunStep)
            .where(RunStep.revision_id == run.current_revision_id)
            .order_by(RunStep.step_order, RunStep.id)
        ).all()
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
    allowed_decisions = {
        "needs_manual_review": [
            ("confirmed_qualified", "Confirm qualified"),
            ("confirmed_unqualified", "Confirm unqualified"),
        ],
        "unqualified": [("override_qualified", "Force qualified")],
    }.get(review.result.verdict if review.result else "", [])
    review_criteria = {}
    review_step_results = []
    review_warnings = []
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
    step_review_map = {
        item.get("review_step"): item
        for item in review_step_results
        if isinstance(item, dict)
    }
    return templates.TemplateResponse(
        request=request,
        name="run_detail.html",
        context={
            "run": run,
            "test_owner_label": _person_label(run.test_owner, users),
            "assigned_tester_label": _person_label(run.assigned_tester, users),
            "actual_tester_label": _person_label(run.actual_tester, users),
            "review": review,
            "steps": steps,
            "revisions": revisions,
            "results": results,
            "allowed_decisions": allowed_decisions,
            "review_criteria": review_criteria,
            "step_review_map": step_review_map,
            "review_warnings": review_warnings,
            "status_label": STATUS_LABELS[review.final_status],
            "message": request.query_params.get("message"),
            "message_kind": request.query_params.get("message_kind", "success"),
        },
    )


@router.post("/runs/{run_id}/manual-decision")
def decide_run(
    run_id: int,
    decision: str = Form(...),
    operator: str = Form(...),
    reason: str = Form(...),
    db: Session = Depends(get_db),
):
    run = db.get(AlmRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        save_manual_decision(db, run, decision, operator, reason)
    except ValueError as exc:
        return _redirect(f"/runs/{run_id}", str(exc), "error")
    return _redirect(f"/runs/{run_id}", "Manual decision recorded.")


@router.post("/runs/{run_id}/review-now")
def review_run_now(
    run_id: int,
    force_new: bool = Form(False),
    db: Session = Depends(get_db),
):
    run = db.get(AlmRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    ai_config = db.get(AiConfig, 1)
    if ai_config is None:
        return _redirect(
            f"/runs/{run_id}",
            "AI review is not configured.",
            "error",
        )
    if run.current_revision_id is None:
        return _redirect(f"/runs/{run_id}", "The Run has no current revision.", "error")
    job = db.scalar(
        select(ReviewJob)
        .where(
            ReviewJob.run_id == run_id,
            ReviewJob.revision_id == run.current_revision_id,
            ReviewJob.status.in_(("queued", "running")),
        )
        .order_by(desc(ReviewJob.id))
        .limit(1)
    )
    if job is None:
        job = db.scalar(
            select(ReviewJob)
            .where(
                ReviewJob.run_id == run_id,
                ReviewJob.revision_id == run.current_revision_id,
                ReviewJob.status == "failed",
                ReviewJob.attempt_count < 3,
            )
            .order_by(desc(ReviewJob.id))
            .limit(1)
        )
    if job is None:
        job = ReviewJob(
            workspace_id=run.workspace_id,
            run_id=run_id,
            revision_id=run.current_revision_id,
            status="queued",
        )
        db.add(job)
    elif job.status == "failed":
        job.status = "queued"
        job.error_message = ""
        job.completed_at = None
    db.commit()
    return _redirect(
        f"/runs/{run_id}",
        "AI review queued for the laptop worker."
        + (" Previous review history will be retained." if force_new else ""),
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
    ai_config = db.get(AiConfig, 1)
    if ai_config is None or not ai_config.enabled:
        return _redirect(
            redirect_path,
            "AI review is disabled. Configure the local model before processing.",
            "error",
        )
    queued = queue_stale_reviews(db, workspace.id)
    return _redirect(
        redirect_path,
        f"Review queue refreshed: {queued} stale Runs queued. The laptop worker will process them.",
    )


@router.post("/actions/rereview")
def rereview_runs(
    scope: str = Form(...),
    db: Session = Depends(get_db),
    workspace_id: int | None = Form(None),
):
    workspace = resolve_workspace(db, workspace_id)
    redirect_path = f"/?workspace={workspace.id}"
    ai_config = db.get(AiConfig, 1)
    if ai_config is None or not ai_config.enabled:
        return _redirect(
            redirect_path,
            "AI review is disabled. Configure the model before re-reviewing.",
            "error",
        )
    if scope not in REREVIEW_SCOPES:
        return _redirect(redirect_path, "Invalid re-review scope.", "error")
    result = queue_rereviews(db, scope, workspace.id)
    return _redirect(
        redirect_path,
        f"Re-review queued: {result.queued} of {result.matched} matching Runs. "
        f"{result.already_active} already active. Previous results were retained.",
    )


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
    existing = db.scalar(
        select(EquipmentRegistry.id).where(
            func.upper(EquipmentRegistry.equipment_id) == normalized_id
        )
    )
    if not normalized_id or not values["description"].strip():
        return _redirect("/ops/equipment/new", "Equipment ID and name are required.", "error")
    if existing is not None:
        return _redirect("/ops/equipment/new", "Equipment ID already exists.", "error")
    equipment = EquipmentRegistry(equipment_id=normalized_id, description="")
    try:
        _set_equipment_values(equipment, values)
    except ValueError as exc:
        return _redirect("/ops/equipment/new", f"Invalid date: {exc}", "error")
    db.add(equipment)
    db.commit()
    return _redirect("/ops/equipment", f"Equipment {equipment.equipment_id} added.")


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
    duplicate = db.scalar(
        select(EquipmentRegistry.id).where(
            func.upper(EquipmentRegistry.equipment_id) == normalized_id,
            EquipmentRegistry.id != equipment_pk,
        )
    )
    if not normalized_id or not values["description"].strip():
        return _redirect(
            f"/ops/equipment/{equipment_pk}/edit",
            "Equipment ID and name are required.",
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
    return _redirect("/ops/equipment", f"Equipment {equipment.equipment_id} updated.")


@router.post("/ops/equipment/{equipment_pk}/delete")
def delete_equipment(equipment_pk: int, db: Session = Depends(get_db)):
    equipment = db.get(EquipmentRegistry, equipment_pk)
    if equipment is None:
        raise HTTPException(status_code=404, detail="Equipment not found")
    equipment_id = equipment.equipment_id
    db.delete(equipment)
    db.commit()
    return _redirect("/ops/equipment", f"Equipment {equipment_id} deleted.")


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
    equipment_areas = db.scalars(
        select(EquipmentRegistry.subordinate_area)
        .where(EquipmentRegistry.subordinate_area != "")
        .distinct()
        .order_by(EquipmentRegistry.subordinate_area)
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="configuration.html",
        context={
            "sync_config": sync_config,
            "workspaces": workspaces,
            "current_workspace": current_workspace,
            "source_locked": source_locked,
            "equipment_areas": equipment_areas,
            "ai_config": ai_config,
            "evidence_config": evidence_config,
            "prompt": prompt,
            "message": request.query_params.get("message"),
            "message_kind": request.query_params.get("message_kind", "success"),
        },
    )


@router.post("/ops/test-ai")
def test_ai(
    ai_base_url: str = Form(...),
    model_name: str = Form(...),
    timeout_seconds: int = Form(...),
    workspace_id: int | None = Form(None),
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
    ai_config = AiConfig(
        id=1,
        base_url=ai_base_url.strip(),
        model_name=model_name.strip(),
        timeout_seconds=max(1, timeout_seconds),
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
    server_url: str = Form(...),
    domain: str = Form(...),
    project: str = Form(...),
    folder_id: int = Form(...),
    folder_path: str = Form(""),
    schedule_hour: int = Form(...),
    schedule_minute: int = Form(...),
    sync_enabled: bool = Form(False),
    ai_base_url: str = Form(...),
    model_name: str = Form(...),
    timeout_seconds: int = Form(...),
    ai_enabled: bool = Form(False),
    allowed_network_root: str = Form(""),
    local_html_fallback_root: str = Form(""),
    network_evidence_enabled: bool = Form(False),
    image_review_enabled: bool = Form(False),
    allow_insecure_image_transport: bool = Form(False),
    equipment_review_enabled: bool = Form(False),
    equipment_area_filter: str = Form(""),
    prompt_name: str = Form(...),
    prompt_template: str = Form(...),
    db: Session = Depends(get_db),
):
    workspace = db.get(Workspace, workspace_id)
    if workspace is None or workspace.archived:
        return _redirect("/ops/configuration", "Workspace not found.", "error")
    redirect_path = f"/ops/configuration?workspace={workspace.id}"
    if not 0 <= schedule_hour <= 23 or not 0 <= schedule_minute <= 59:
        return _redirect(redirect_path, "Invalid schedule time.", "error")
    workspace.name = workspace_name.strip()
    workspace.equipment_review_enabled = equipment_review_enabled
    workspace.equipment_area_filter = equipment_area_filter.strip()
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
    if source_locked and proposed_source != current_source:
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

    ai_config = db.get(AiConfig, 1)
    if ai_config is None:
        ai_config = AiConfig(id=1)
        db.add(ai_config)
    ai_config.base_url = ai_base_url.strip().rstrip("/")
    ai_config.model_name = model_name.strip()
    ai_config.timeout_seconds = max(1, timeout_seconds)
    ai_config.enabled = ai_enabled

    evidence_config = workspace_evidence_config(db, workspace.id)
    if evidence_config is None:
        evidence_config = EvidenceConfig(workspace_id=workspace.id)
        db.add(evidence_config)
    evidence_config.workspace_id = workspace.id
    evidence_config.allowed_network_root = allowed_network_root.strip().rstrip("\\/")
    evidence_config.local_html_fallback_root = local_html_fallback_root.strip().rstrip("\\/")
    evidence_config.network_evidence_enabled = network_evidence_enabled
    evidence_config.image_review_enabled = image_review_enabled
    evidence_config.allow_insecure_image_transport = allow_insecure_image_transport

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
    queued_reviews = queue_stale_reviews(db, workspace.id)
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        configure_scheduler(scheduler)
    schedule_note = (
        " Restart the laptop Worker to load schedule-time changes."
        if get_settings().app_role == "web"
        else ""
    )
    return _redirect(
        redirect_path,
        f"Configuration and schedule saved. {queued_reviews} stale reviews queued."
        f"{schedule_note}",
    )


@router.post("/ops/workspaces")
def create_workspace(
    name: str = Form(...),
    db: Session = Depends(get_db),
):
    normalized_name = name.strip()
    if not normalized_name:
        return _redirect("/ops/configuration", "Workspace name is required.", "error")
    base_slug = workspace_slug(normalized_name)
    slug = base_slug
    suffix = 2
    while db.scalar(select(Workspace.id).where(Workspace.slug == slug)) is not None:
        slug = f"{base_slug}-{suffix}"
        suffix += 1
    workspace = Workspace(
        name=normalized_name,
        slug=slug,
        equipment_review_enabled=True,
        equipment_area_filter="",
        archived=False,
    )
    db.add(workspace)
    db.flush()
    db.add(
        SyncConfig(
            workspace_id=workspace.id,
            name=normalized_name,
            server_url="",
            domain="",
            project="",
            folder_id=0,
            folder_path="",
            enabled=False,
        )
    )
    db.add(EvidenceConfig(workspace_id=workspace.id))
    db.commit()
    return _redirect(
        f"/ops/configuration?workspace={workspace.id}",
        f"Workspace {workspace.name} created. Configure its ALM source before syncing.",
    )


@router.get("/api/health")
def health(db: Session = Depends(get_db)) -> dict:
    db.scalar(select(AlmRun.run_id).limit(1))
    return {"status": "ok"}