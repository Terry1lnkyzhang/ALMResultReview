from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.hashing import normalize_text, review_hash, source_hash
from app.models import (
    AlmRun,
    AlmUser,
    ReviewJob,
    ReviewResult,
    RunRevision,
    RunStep,
    SyncHistory,
    utcnow,
)
from app.services.rich_text import source_rich_text
from app.services.run_status import (
    REVIEWABLE_RUN_STATUSES,
    is_reviewable_run_status,
    normalize_reviewable_run_status,
)
from app.services.workspaces import next_internal_run_id, resolve_workspace


@dataclass
class ImportResult:
    discovered_runs: int = 0
    new_runs: int = 0
    changed_runs: int = 0
    unchanged_runs: int = 0


@dataclass(frozen=True)
class LatestAlmReviewQueueResult:
    sync_id: int | None
    changed: int
    queued: int
    already_reviewed: int
    already_active: int


def _integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _date_time(date_value: Any, time_value: Any = None) -> datetime | None:
    if not date_value:
        return None
    value = f"{date_value} {time_value}" if time_value else str(date_value)
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    return None


def _raw(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _next_revision_number(db: Session, run_id: int) -> int:
    latest = db.scalar(
        select(func.max(RunRevision.revision_number)).where(RunRevision.run_id == run_id)
    )
    return (latest or 0) + 1


def _add_revision(
    db: Session,
    run_row: AlmRun,
    record: dict[str, Any],
    raw_json: str,
    current_source_hash: str,
    current_review_hash: str,
) -> RunRevision:
    run = record.get("run") or {}
    revision = RunRevision(
        run_id=run_row.run_id,
        revision_number=_next_revision_number(db, run_row.run_id),
        source_hash=current_source_hash,
        review_hash=current_review_hash,
        source_last_modified=_date_time(run.get("last-modified")),
        snapshot_json=raw_json,
    )
    db.add(revision)
    db.flush()

    for fallback_order, step in enumerate(run.get("steps") or [], start=1):
        db.add(
            RunStep(
                revision_id=revision.id,
                step_id=_integer(step.get("id")),
                step_order=_integer(step.get("step-order")) or fallback_order,
                name=normalize_text(step.get("name")),
                status=normalize_text(step.get("status")),
                description=source_rich_text(
                    step.get("descriptionText", step.get("description"))
                ),
                expected=source_rich_text(
                    step.get("expectedText", step.get("expected"))
                ),
                actual=source_rich_text(step.get("actualText", step.get("actual"))),
            )
        )

    run_row.current_revision_id = revision.id
    return revision


def queue_latest_alm_changes(
    db: Session,
    workspace_id: int | None = None,
) -> LatestAlmReviewQueueResult:
    workspace = resolve_workspace(db, workspace_id)
    latest_sync = db.scalar(
        select(SyncHistory)
        .where(
            SyncHistory.workspace_id == workspace.id,
            SyncHistory.status == "completed",
            SyncHistory.source.like("alm:folder:%"),
            SyncHistory.completed_at.is_not(None),
        )
        .order_by(SyncHistory.completed_at.desc(), SyncHistory.id.desc())
        .limit(1)
    )
    if latest_sync is None or latest_sync.completed_at is None:
        return LatestAlmReviewQueueResult(None, 0, 0, 0, 0)

    revisions = db.scalars(
        select(RunRevision)
        .join(AlmRun, AlmRun.run_id == RunRevision.run_id)
        .where(
            AlmRun.workspace_id == workspace.id,
            AlmRun.current_revision_id == RunRevision.id,
            AlmRun.run_status.in_(REVIEWABLE_RUN_STATUSES),
            RunRevision.created_at >= latest_sync.started_at,
            RunRevision.created_at <= latest_sync.completed_at,
        )
        .order_by(RunRevision.id)
    ).all()
    changed_revisions = []
    for revision in revisions:
        previous_review_hash = db.scalar(
            select(RunRevision.review_hash)
            .where(
                RunRevision.run_id == revision.run_id,
                RunRevision.revision_number < revision.revision_number,
            )
            .order_by(RunRevision.revision_number.desc())
            .limit(1)
        )
        if previous_review_hash is None or previous_review_hash != revision.review_hash:
            changed_revisions.append(revision)

    revision_ids = [revision.id for revision in changed_revisions]
    active_revision_ids = set(
        db.scalars(
            select(ReviewJob.revision_id).where(
                ReviewJob.revision_id.in_(revision_ids),
                ReviewJob.status.in_(("queued", "running", "superseded")),
            )
        ).all()
    )
    reviewed_revision_ids = set(
        db.scalars(
            select(ReviewResult.revision_id).where(
                ReviewResult.revision_id.in_(revision_ids)
            )
        ).all()
    )
    # Timestamp granularity can pull an earlier sync's revision into this window;
    # anything already reviewed belongs to that earlier window, not to this one.
    outstanding_revisions = [
        revision
        for revision in changed_revisions
        if revision.id not in reviewed_revision_ids
    ]
    queued = 0
    for revision in outstanding_revisions:
        if revision.id in active_revision_ids:
            continue
        db.add(
            ReviewJob(
                workspace_id=workspace.id,
                run_id=revision.run_id,
                revision_id=revision.id,
                status="queued",
            )
        )
        queued += 1
    db.commit()
    return LatestAlmReviewQueueResult(
        sync_id=latest_sync.id,
        changed=len(outstanding_revisions),
        queued=queued,
        already_reviewed=len(reviewed_revision_ids),
        already_active=len(active_revision_ids - reviewed_revision_ids),
    )


def unchanged_run_check(
    db: Session,
    workspace_id: int,
) -> Callable[[dict[str, Any]], bool]:
    """Skip ALM runs whose stored last-modified stamp already matches the server."""

    def is_unchanged(run: dict[str, Any]) -> bool:
        alm_run_id = _integer(run.get("id"))
        last_modified = _date_time(run.get("last-modified"))
        if alm_run_id is None or last_modified is None:
            return False
        row = db.execute(
            select(AlmRun.alm_last_modified, AlmRun.current_revision_id).where(
                AlmRun.workspace_id == workspace_id,
                AlmRun.alm_run_id == alm_run_id,
            )
        ).first()
        return bool(row and row[1] is not None and row[0] == last_modified)

    return is_unchanged


def import_batch(
    db: Session,
    data: dict[str, Any],
    workspace_id: int,
    result: ImportResult,
) -> ImportResult:
    for item in data.get("users") or []:
        code1_id = normalize_text(item.get("code1_id"))
        if not code1_id:
            continue
        user = db.get(AlmUser, code1_id)
        if user is None:
            user = AlmUser(code1_id=code1_id)
            db.add(user)
        user.full_name = normalize_text(item.get("full_name"))
        user.email = normalize_text(item.get("email"))
        user.active = bool(item.get("active", True))
        user.synced_at = utcnow()

    records = [
        record
        for record in data.get("records") or []
        if is_reviewable_run_status((record.get("run") or {}).get("status"))
    ]
    result.discovered_runs += len(records)
    for record in records:
        run = record.get("run") or {}
        run_id = _integer(run.get("id"))
        if run_id is None:
            continue

        test_instance = record.get("testInstance") or {}
        test_set = record.get("testSet") or {}
        folder = record.get("folder") or {}
        raw_json = _raw(record)
        current_source_hash = source_hash(record)
        current_review_hash = review_hash(record)
        run_row = db.scalar(
            select(AlmRun).where(
                AlmRun.workspace_id == workspace_id,
                AlmRun.alm_run_id == run_id,
            )
        )
        is_new = run_row is None

        if is_new:
            run_row = AlmRun(
                run_id=next_internal_run_id(db, run_id),
                workspace_id=workspace_id,
                alm_run_id=run_id,
                source_hash=current_source_hash,
                review_hash=current_review_hash,
                raw_json=raw_json,
            )
            db.add(run_row)
            result.new_runs += 1
        elif run_row.source_hash != current_source_hash:
            result.changed_runs += 1
        else:
            result.unchanged_runs += 1

        run_row.test_id = _integer(run.get("test-id") or test_instance.get("test-id"))
        run_row.workspace_id = workspace_id
        run_row.alm_run_id = run_id
        run_row.test_instance_id = _integer(
            run.get("testcycl-id") or test_instance.get("id")
        )
        run_row.test_set_id = _integer(test_set.get("id") or run.get("cycle-id"))
        run_row.folder_id = _integer(folder.get("id"))
        run_row.test_name = normalize_text(
            run.get("test-name") or test_instance.get("name")
        )
        run_row.test_set_name = normalize_text(
            test_set.get("name") or run.get("cycle-name")
        )
        run_row.folder_path = normalize_text(
            folder.get("path") or test_set.get("folderPath")
        )
        run_row.execution_location = normalize_text(run.get("location"))
        run_row.run_status = normalize_reviewable_run_status(run.get("status"))
        run_row.test_owner = normalize_text(record.get("testOwner"))
        run_row.assigned_tester = normalize_text(test_instance.get("owner"))
        run_row.actual_tester = normalize_text(
            test_instance.get("actual-tester") or run.get("owner")
        )
        run_row.execution_at = _date_time(
            run.get("execution-date"), run.get("execution-time")
        )
        run_row.alm_last_modified = _date_time(run.get("last-modified"))
        run_row.synced_at = utcnow()

        if is_new or run_row.source_hash != current_source_hash:
            run_row.source_hash = current_source_hash
            run_row.review_hash = current_review_hash
            run_row.raw_json = raw_json
            _add_revision(
                db,
                run_row,
                record,
                raw_json,
                current_source_hash,
                current_review_hash,
            )
    return result


def import_data(
    data: dict[str, Any],
    db: Session,
    source: str = "json",
    workspace_id: int | None = None,
    sync_config_id: int | None = None,
) -> ImportResult:
    workspace = resolve_workspace(db, workspace_id)
    result = ImportResult()
    history = SyncHistory(
        workspace_id=workspace.id,
        sync_config_id=sync_config_id,
        source=source,
        status="running",
    )
    db.add(history)
    db.flush()

    try:
        import_batch(db, data, workspace.id, result)
        history.status = "completed"
        history.discovered_runs = result.discovered_runs
        history.new_runs = result.new_runs
        history.changed_runs = result.changed_runs
        history.unchanged_runs = result.unchanged_runs
        history.completed_at = utcnow()
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        failed_history = SyncHistory(
            workspace_id=workspace.id,
            sync_config_id=sync_config_id,
            source=source,
            status="failed",
            error_message=str(exc),
            started_at=history.started_at,
            completed_at=utcnow(),
        )
        db.add(failed_history)
        db.commit()
        raise


def import_file(
    path: Path,
    db: Session,
    workspace_id: int | None = None,
) -> ImportResult:
    with path.open("r", encoding="utf-8") as stream:
        return import_data(
            json.load(stream),
            db,
            source=f"json:{path.name}",
            workspace_id=workspace_id,
        )