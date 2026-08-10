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
from sqlalchemy.orm import Session

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
)
from app.services.equipment_registry import import_equipment_workbook
from app.services.importer import import_file, queue_stale_reviews
from app.services.review_operations import (
    REREVIEW_SCOPES,
    latest_rereview_progress,
    queue_rereviews,
)
from app.services.review_policy import current_review_policy_key
from app.services.reviews import (
    current_review,
    save_manual_decision,
    test_ai_connection,
)
from app.services.scheduler import configure_scheduler
from app.services.worker_tasks import queue_sync_job

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
    return RedirectResponse(
        f"{path}?message={quote(message)}&message_kind={quote(kind)}", status_code=303
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
) -> dict:
    review = current_review(db, run, policy_key)
    return {
        "run": run,
        "actual_tester_label": _person_label(run.actual_tester, users),
        "review": review,
        "review_summary": review.result.issue_summary if review.result else "",
        "final_status": review.final_status,
        "status_label": STATUS_LABELS[review.final_status],
    }


def _matches_dashboard_filters(item: dict, status: str, tester: str, query: str) -> bool:
    normalized_query = query.strip().casefold()
    return (
        (status == "all" or item["final_status"] == status)
        and (tester == "all" or (item["run"].actual_tester or "Unassigned") == tester)
        and (
            not normalized_query
            or normalized_query in str(item["run"].run_id)
            or normalized_query in str(item["run"].test_id or "")
            or normalized_query in item["run"].test_name.casefold()
            or normalized_query in item["run"].test_set_name.casefold()
            or normalized_query in item["run"].folder_path.casefold()
            or normalized_query in item["actual_tester_label"].casefold()
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
    query: str = Query(default=""),
    db: Session = Depends(get_db),
):
    runs = db.scalars(select(AlmRun).order_by(desc(AlmRun.execution_at), desc(AlmRun.run_id))).all()
    policy_key = current_review_policy_key(db)
    users = {user.code1_id: user for user in db.scalars(select(AlmUser)).all()}
    all_views = [_run_view(db, run, policy_key, users) for run in runs]
    status_counts = Counter(item["final_status"] for item in all_views)
    tester_counts = Counter((item["run"].actual_tester or "Unassigned") for item in all_views)

    filtered = [
        item
        for item in all_views
        if _matches_dashboard_filters(item, status, tester, query)
    ]
    recent_sync = db.scalar(select(SyncHistory).order_by(desc(SyncHistory.started_at)).limit(1))
    max_tester_count = max(tester_counts.values(), default=1)
    review_job_counts = dict(
        db.execute(
            select(ReviewJob.status, func.count())
            .where(ReviewJob.status.in_(("queued", "running")))
            .group_by(ReviewJob.status)
        ).all()
    )
    review_job_counts["failed"] = status_counts.get("review_failed", 0)
    active_sync_job = db.scalar(
        select(SyncJob)
        .where(SyncJob.status.in_(("queued", "running", "failed")))
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
    rereview_progress = latest_rereview_progress(db)
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
            "total_runs": len(all_views),
            "status_counts": status_counts,
            "tester_counts": [
                (code1_id, _person_label(code1_id, users), count)
                for code1_id, count in tester_counts.most_common()
            ],
            "max_tester_count": max_tester_count,
            "status_labels": STATUS_LABELS,
            "selected_status": status,
            "selected_tester": tester,
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
def review_progress(db: Session = Depends(get_db)):
    progress = latest_rereview_progress(db)
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


@router.get("/exports/reviews.csv")
def export_reviews(
    status: str = Query(default="all"),
    tester: str = Query(default="all"),
    query: str = Query(default=""),
    db: Session = Depends(get_db),
) -> Response:
    runs = db.scalars(select(AlmRun).order_by(desc(AlmRun.execution_at), desc(AlmRun.run_id))).all()
    policy_key = current_review_policy_key(db)
    users = {user.code1_id: user for user in db.scalars(select(AlmUser)).all()}
    views = [
        item
        for run in runs
        if _matches_dashboard_filters(
            item := _run_view(db, run, policy_key, users), status, tester, query
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
def import_snapshot(db: Session = Depends(get_db)):
    path = get_settings().import_path
    if path is None:
        return _redirect("/", "Snapshot import path is not configured.", "error")
    if not path.exists():
        return _redirect("/", f"Import file not found: {path}", "error")
    try:
        result = import_file(path, db)
    except Exception as exc:
        return _redirect("/", f"Import failed: {exc}", "error")
    return _redirect(
        "/",
        f"Sync complete: {result.new_runs} new, {result.changed_runs} changed, "
        f"{result.unchanged_runs} unchanged.",
    )


@router.post("/actions/sync-alm")
def sync_alm(db: Session = Depends(get_db)):
    config = db.scalar(select(SyncConfig).order_by(SyncConfig.id).limit(1))
    if config is None:
        return _redirect("/", "No ALM synchronization scope is configured.", "error")
    result = queue_sync_job(db, requested_by="web")
    message = (
        f"ALM synchronization queued as job {result.job.id}."
        if result.created
        else f"ALM synchronization job {result.job.id} is already {result.job.status}."
    )
    return _redirect("/", message)


@router.post("/actions/process-reviews")
def process_reviews(db: Session = Depends(get_db)):
    ai_config = db.get(AiConfig, 1)
    if ai_config is None or not ai_config.enabled:
        return _redirect(
            "/", "AI review is disabled. Configure the local model before processing.", "error"
        )
    queued = queue_stale_reviews(db)
    return _redirect(
        "/",
        f"Review queue refreshed: {queued} stale Runs queued. The laptop worker will process them.",
    )


@router.post("/actions/rereview")
def rereview_runs(
    scope: str = Form(...),
    db: Session = Depends(get_db),
):
    ai_config = db.get(AiConfig, 1)
    if ai_config is None or not ai_config.enabled:
        return _redirect(
            "/", "AI review is disabled. Configure the model before re-reviewing.", "error"
        )
    if scope not in REREVIEW_SCOPES:
        return _redirect("/", "Invalid re-review scope.", "error")
    result = queue_rereviews(db, scope)
    return _redirect(
        "/",
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
def configuration(request: Request, db: Session = Depends(get_db)):
    sync_config = db.scalar(select(SyncConfig).order_by(SyncConfig.id).limit(1))
    ai_config = db.get(AiConfig, 1)
    evidence_config = db.get(EvidenceConfig, 1)
    prompt = db.scalar(
        select(PromptVersion)
        .where(PromptVersion.is_active.is_(True))
        .order_by(desc(PromptVersion.id))
        .limit(1)
    )
    return templates.TemplateResponse(
        request=request,
        name="configuration.html",
        context={
            "sync_config": sync_config,
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
):
    if get_settings().app_role == "web":
        return _redirect(
            "/ops/configuration",
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
        return _redirect("/ops/configuration", f"AI connection failed: {exc}", "error")
    return _redirect(
        "/ops/configuration",
        f"AI connection succeeded: {ai_config.model_name} at {ai_config.base_url}",
    )


@router.post("/ops/configuration")
def save_configuration(
    request: Request,
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
    prompt_name: str = Form(...),
    prompt_template: str = Form(...),
    db: Session = Depends(get_db),
):
    if not 0 <= schedule_hour <= 23 or not 0 <= schedule_minute <= 59:
        return _redirect("/ops/configuration", "Invalid schedule time.", "error")
    sync_config = db.scalar(select(SyncConfig).order_by(SyncConfig.id).limit(1))
    if sync_config is None:
        sync_config = SyncConfig(
            name=folder_path or str(folder_id),
            server_url=server_url,
            domain=domain,
            project=project,
            folder_id=folder_id,
        )
        db.add(sync_config)
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

    evidence_config = db.get(EvidenceConfig, 1)
    if evidence_config is None:
        evidence_config = EvidenceConfig(id=1)
        db.add(evidence_config)
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
    queued_reviews = queue_stale_reviews(db)
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        configure_scheduler(scheduler)
    schedule_note = (
        " Restart the laptop Worker to load schedule-time changes."
        if get_settings().app_role == "web"
        else ""
    )
    return _redirect(
        "/ops/configuration",
        f"Configuration and schedule saved. {queued_reviews} stale reviews queued."
        f"{schedule_note}",
    )


@router.get("/api/health")
def health(db: Session = Depends(get_db)) -> dict:
    db.scalar(select(AlmRun.run_id).limit(1))
    return {"status": "ok"}