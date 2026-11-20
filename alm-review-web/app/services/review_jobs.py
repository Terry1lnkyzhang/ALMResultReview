"""Review job claiming, lease renewal, retry fencing, and job execution.

Kept outside `_REVIEW_POLICY_FILES` so queue/worker reliability updates never
invalidate historical `ReviewResult` policy hashes.
"""
from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import desc, func, or_, select, update
from sqlalchemy.orm import Session

from app.hashing import review_payload
from app.models import (
    AiConfig,
    AlmRun,
    EquipmentRegistry,
    ReviewJob,
    ReviewResult,
    RunRevision,
    Workspace,
    utcnow,
)
from app.services.ai_transport import (
    completion_url,
    record_ai_endpoint_failure,
    record_ai_endpoint_success,
)
from app.services.automation_release import (
    assess_automation_release,
    effective_step_execution_at,
    load_automation_releases,
)
from app.services.evidence import is_html_report_path
from app.services.location_review import load_location_assessment
from app.services.review_engine import execute_review
from app.services.review_policy import current_review_policy_key
from app.services.review_types import ReviewContext
from app.services.worker_lease import (
    WorkerLeaseLost,
    fence_worker_lease,
    owns_worker_lease,
)
from app.services.workspaces import resolve_workspace, workspace_evidence_config

DEFAULT_JOB_LEASE_SECONDS = 15 * 60
MAX_REVIEW_JOB_ATTEMPTS = 3
FAILED_JOB_RETRY_BACKOFF_SECONDS = 60


class ReviewJobLeaseLost(WorkerLeaseLost):
    """The review attempt lost its job lease, regardless of Worker ownership."""


def process_job(
    db: Session,
    job: ReviewJob,
    allow_disabled: bool = False,
    worker_owner_token: str | None = None,
    expected_attempt: int | None = None,
    expected_claimed_by: str | None = None,
) -> ReviewResult:
    job_id = job.id
    if expected_attempt is not None and (
        job.status != "running"
        or job.attempt_count != expected_attempt
        or job.claimed_by != expected_claimed_by
    ):
        raise ReviewJobLeaseLost("Review job was reclaimed before execution began.")
    run = db.get(AlmRun, job.run_id)
    revision = db.get(RunRevision, job.revision_id)
    if run is None or revision is None:
        raise ValueError("Review job references missing run data.")
    workspace = resolve_workspace(db, run.workspace_id or job.workspace_id)
    policy_key = current_review_policy_key(db, workspace.id)
    if job.status == "completed":
        existing = db.scalar(select(ReviewResult).where(ReviewResult.job_id == job.id))
        if existing is None:
            raise ValueError("Completed review job has no result.")
        if existing.review_policy_key != policy_key:
            raise ValueError("Completed review job belongs to an outdated review policy.")
        return existing
    if run.current_revision_id != revision.id or run.source_hash != revision.source_hash:
        if expected_attempt is not None:
            db.rollback()
            job = _owned_review_job(db, job_id, expected_attempt, expected_claimed_by)
            if job is None:
                db.rollback()
                raise ReviewJobLeaseLost("Review job was reclaimed before becoming outdated.")
        job.status = "outdated"
        job.completed_at = utcnow()
        db.commit()
        raise ValueError("Review job is outdated because the ALM run changed.")

    ai_config = db.get(AiConfig, job.ai_config_id or 1)
    if ai_config is None or (not ai_config.enabled and not allow_disabled):
        raise ValueError("AI review is not configured or is disabled.")

    if job.status != "running":
        job.status = "running"
        job.started_at = utcnow()
        job.attempt_count += 1
        job.error_message = ""
        db.commit()
    attempt = job.attempt_count
    claimant = job.claimed_by

    snapshot = json.loads(revision.snapshot_json)
    equipment_registry: list[EquipmentRegistry] = []
    if workspace.equipment_review_enabled:
        equipment_statement = select(EquipmentRegistry).order_by(
            EquipmentRegistry.equipment_id,
            EquipmentRegistry.id,
        )
        if workspace.equipment_area_filter:
            equipment_statement = equipment_statement.where(
                EquipmentRegistry.subordinate_area == workspace.equipment_area_filter
            )
        equipment_registry = list(db.scalars(equipment_statement).all())
    content = review_payload(snapshot)
    if workspace.location_review_enabled:
        location_assessment = load_location_assessment(
            db,
            str(content.get("execution_location") or ""),
            str(content.get("folder_path") or ""),
            run.execution_at,
        )
    else:
        location_assessment = {
            "status": "disabled",
            "failure_code": "workspace_disabled",
            "reason": "当前工作区已关闭测试位置一致性核对。",
            "alm_location": str(content.get("execution_location") or "").strip(),
            "folder_path": str(content.get("folder_path") or "").strip(),
            "parent_name": "",
            "candidate_count": 0,
            "candidate_items": [],
            "selected_config": None,
        }
    evidence_config = workspace_evidence_config(db, workspace.id)
    release_assessments: dict[int, dict[str, Any]] = {}
    release_project = (
        evidence_config.automation_release_project_name
        if evidence_config
        else ""
    )
    release_testcase_id = str(content.get("test_id") or run.test_id or "")
    html_steps = [
        step
        for step in content.get("steps", [])
        if any(
            is_html_report_path(path)
            for path in step["evidence_profile"]["actual_paths"]
        )
    ]
    if html_steps and release_project:
        lookup_error = ""
        try:
            releases = load_automation_releases(
                db,
                release_project,
                release_testcase_id,
            )
        except Exception as exc:
            releases = ()
            lookup_error = str(exc)[:300]
        for step in html_steps:
            html_paths = tuple(
                path["raw"]
                for path in step["evidence_profile"]["actual_paths"]
                if is_html_report_path(path)
            )
            release_assessments[step["review_step"]] = assess_automation_release(
                actual=step["actual"],
                html_paths=html_paths,
                project_name=release_project,
                testcase_id=release_testcase_id,
                releases=releases,
                lookup_error=lookup_error,
                execution_at=effective_step_execution_at(step, run.execution_at),
            )
    ctx = ReviewContext(
        ai_config=ai_config,
        content=content,
        project=workspace.project,
        workspace_name=workspace.name,
        evidence_config=evidence_config,
        equipment_enabled=workspace.equipment_review_enabled,
        location_enabled=workspace.location_review_enabled,
        equipment_registry=equipment_registry,
        automation_release_assessments=release_assessments,
        location_assessment=location_assessment,
    )
    started = time.perf_counter()
    try:
        if worker_owner_token and not owns_worker_lease(db, worker_owner_token):
            raise WorkerLeaseLost("Worker singleton lease was lost before review execution.")
        outcome = execute_review(ctx)
        parsed = outcome.parsed
        duration_ms = round((time.perf_counter() - started) * 1000)
        result = ReviewResult(
            workspace_id=workspace.id,
            job_id=job_id,
            run_id=run.run_id,
            revision_id=revision.id,
            prompt_version_id=0,
            source_hash=revision.source_hash,
            review_policy_key=policy_key,
            model_name=ai_config.model_name,
            ai_config_id=ai_config.id,
            ai_endpoint=completion_url(ai_config.base_url),
            verdict=parsed["verdict"],
            issue_summary=parsed["issue_summary"],
            criteria_json=json.dumps(parsed["criteria"], ensure_ascii=False),
            step_results_json=json.dumps(parsed["step_results"], ensure_ascii=False),
            warnings_json=json.dumps(parsed["warnings"], ensure_ascii=False),
            pipeline_json=json.dumps(outcome.pipeline, ensure_ascii=False),
            temporary_evidence_used=bool(parsed.get("temporary_evidence_used")),
            raw_response=outcome.raw_response,
            duration_ms=duration_ms,
        )
        # Claimed jobs use their own Session; drop its long-lived read transaction
        # before taking final locks. Direct callers may bind a Session to a shared
        # test/outer connection, where rollback would discard their fixture data.
        if expected_attempt is not None:
            db.rollback()
        if worker_owner_token and not fence_worker_lease(db, worker_owner_token):
            raise WorkerLeaseLost("Worker singleton lease was lost during review execution.")
        current_job = _owned_review_job(db, job_id, attempt, claimant)
        if current_job is None:
            raise ReviewJobLeaseLost("Review job was reclaimed during execution.")
        existing = db.scalar(select(ReviewResult).where(ReviewResult.job_id == job_id))
        if existing is not None:
            result = existing
        else:
            db.add(result)
        current_job.status = "completed"
        current_job.claimed_by = None
        current_job.lease_expires_at = None
        current_job.completed_at = utcnow()
        db.commit()
        db.refresh(result)
        return result
    except ReviewJobLeaseLost:
        db.rollback()
        raise
    except WorkerLeaseLost:
        db.rollback()
        interrupted_job = _owned_review_job(db, job_id, attempt, claimant)
        if interrupted_job is not None:
            if db.scalar(select(ReviewResult.id).where(ReviewResult.job_id == job_id)):
                db.rollback()
                raise
            # The attempt count is a fencing generation: never decrement/reuse it.
            retryable = attempt < MAX_REVIEW_JOB_ATTEMPTS
            interrupted_job.status = "queued" if retryable else "failed"
            interrupted_job.ai_config_id = None
            interrupted_job.claimed_by = None
            interrupted_job.lease_expires_at = None
            interrupted_job.started_at = None
            interrupted_job.completed_at = None if retryable else utcnow()
            interrupted_job.error_message = (
                "" if retryable else "Worker lease lost during review."
            )
            db.commit()
        raise
    except Exception as exc:
        db.rollback()
        failed_job = _owned_review_job(db, job_id, attempt, claimant)
        if failed_job is None:
            db.rollback()
            raise ReviewJobLeaseLost("Review job was reclaimed after an error.") from exc
        _mark_job_failed(failed_job, exc)
        db.commit()
        raise


def _owned_review_job(
    db: Session, job_id: int, attempt: int, claimant: str | None
) -> ReviewJob | None:
    statement = select(ReviewJob).where(
        ReviewJob.id == job_id,
        ReviewJob.status == "running",
        ReviewJob.attempt_count == attempt,
        ReviewJob.claimed_by == claimant,
    )
    if claimant is not None:
        statement = statement.where(ReviewJob.lease_expires_at > utcnow())
    return db.scalar(statement.with_for_update().execution_options(populate_existing=True))


def renew_review_job_lease(
    db: Session,
    job_id: int,
    worker_id: str,
    attempt: int,
    lease_seconds: int,
    worker_owner_token: str | None = None,
) -> bool:
    """Renew only an unexpired claim from this exact review attempt."""
    if worker_owner_token and not fence_worker_lease(db, worker_owner_token):
        db.rollback()
        return False
    now = utcnow()
    renewed = db.execute(
        update(ReviewJob)
        .where(
            ReviewJob.id == job_id,
            ReviewJob.status == "running",
            ReviewJob.claimed_by == worker_id,
            ReviewJob.attempt_count == attempt,
            ReviewJob.lease_expires_at > now,
        )
        .values(lease_expires_at=now + timedelta(seconds=max(1, lease_seconds)))
        .execution_options(synchronize_session=False)
    ).rowcount == 1
    if renewed:
        db.commit()
    else:
        db.rollback()
    return renewed


def _mark_job_failed(job: ReviewJob, exc: Exception) -> None:
    job.status = "failed"
    job.error_message = str(exc)[:2000]
    job.claimed_by = None
    job.lease_expires_at = None
    job.completed_at = utcnow()
    if getattr(exc, "retryable", True) is False:
        # An identical retry would fail identically; stop burning attempts on it.
        job.attempt_count = MAX_REVIEW_JOB_ATTEMPTS


def reap_abandoned_review_jobs(db: Session) -> int:
    """Fail jobs left running by a stopped Worker so they stop blocking the queue."""
    now = utcnow()
    jobs = db.scalars(
        select(ReviewJob).where(
            ReviewJob.status == "running",
            ReviewJob.lease_expires_at.is_not(None),
            ReviewJob.lease_expires_at <= now,
            ReviewJob.attempt_count >= MAX_REVIEW_JOB_ATTEMPTS,
            ~select(ReviewResult.id).where(ReviewResult.job_id == ReviewJob.id).exists(),
        )
    ).all()
    reaped = 0
    for job in jobs:
        reaped += db.execute(
            update(ReviewJob)
            .where(
                ReviewJob.id == job.id,
                ReviewJob.status == "running",
                ReviewJob.attempt_count == job.attempt_count,
                ReviewJob.lease_expires_at <= now,
                ~select(ReviewResult.id).where(ReviewResult.job_id == ReviewJob.id).exists(),
            )
            .values(
                status="failed",
                error_message=(
                    f"Abandoned after {MAX_REVIEW_JOB_ATTEMPTS} attempts; the Worker "
                    "lease expired while the job was running."
                ),
                claimed_by=None,
                lease_expires_at=None,
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
    if reaped:
        db.commit()
    else:
        db.rollback()
    return reaped


def claim_next_review_job(
    db: Session,
    worker_id: str,
    lease_seconds: int = DEFAULT_JOB_LEASE_SECONDS,
    ai_config_id: int | None = None,
) -> ReviewJob | None:
    now = utcnow()
    retry_after = now - timedelta(seconds=FAILED_JOB_RETRY_BACKOFF_SECONDS)
    job = db.scalar(
        select(ReviewJob)
        .outerjoin(Workspace, Workspace.id == ReviewJob.workspace_id)
        .outerjoin(ReviewResult, ReviewResult.job_id == ReviewJob.id)
        .where(
            ReviewResult.id.is_(None),
            or_(
                ReviewJob.status == "queued",
                (
                    (ReviewJob.status == "failed")
                    & or_(
                        ReviewJob.completed_at.is_(None),
                        ReviewJob.completed_at <= retry_after,
                    )
                ),
                (
                    (ReviewJob.status == "running")
                    & (ReviewJob.lease_expires_at.is_not(None))
                    & (ReviewJob.lease_expires_at <= now)
                ),
            ),
            ReviewJob.attempt_count < MAX_REVIEW_JOB_ATTEMPTS,
            or_(
                Workspace.id.is_(None),
                Workspace.review_queue_paused.is_(False),
            ),
        )
        .order_by(
            desc(func.coalesce(Workspace.queue_priority, 0)),
            ReviewJob.created_at,
            ReviewJob.id,
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        db.rollback()
        return None
    job.status = "running"
    if ai_config_id is not None:
        job.ai_config_id = ai_config_id
    job.claimed_by = worker_id
    job.lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))
    job.started_at = now
    job.completed_at = None
    job.attempt_count += 1
    job.error_message = ""
    db.commit()
    db.refresh(job)
    return job


def process_queued_jobs(
    db: Session,
    limit: int = 10,
    worker_id: str = "local-worker",
    lease_seconds: int = DEFAULT_JOB_LEASE_SECONDS,
) -> tuple[int, int]:
    ai_config = db.scalar(
        select(AiConfig)
        .where(AiConfig.enabled.is_(True))
        .order_by(AiConfig.id)
        .limit(1)
    )
    if ai_config is None:
        return 0, 0
    completed = 0
    failed = 0
    for _ in range(limit):
        job = claim_next_review_job(
            db,
            worker_id,
            lease_seconds,
            ai_config_id=ai_config.id,
        )
        if job is None:
            break
        job_completed, job_failed = process_claimed_review_job(
            db,
            job.id,
            expected_attempt=job.attempt_count,
            expected_claimed_by=worker_id,
        )
        completed += job_completed
        failed += job_failed
    return completed, failed


def process_claimed_review_job(
    db: Session,
    job_id: int,
    worker_owner_token: str | None = None,
    expected_attempt: int | None = None,
    expected_claimed_by: str | None = None,
) -> tuple[int, int]:
    job = db.get(ReviewJob, job_id)
    if job is None or job.status != "running":
        return 0, 0
    if expected_attempt is not None and (
        job.attempt_count != expected_attempt
        or job.claimed_by != expected_claimed_by
    ):
        return 0, 0
    attempt = job.attempt_count
    claimant = job.claimed_by
    ai_config_id = job.ai_config_id
    try:
        if worker_owner_token is None and expected_attempt is None:
            process_job(db, job)
        else:
            process_job(
                db,
                job,
                worker_owner_token=worker_owner_token,
                expected_attempt=attempt,
                expected_claimed_by=claimant,
            )
        record_ai_endpoint_success(db, ai_config_id)
        return 1, 0
    except WorkerLeaseLost:
        return 0, 0
    except Exception as exc:
        db.rollback()
        failed_job = _owned_review_job(db, job_id, attempt, claimant)
        if failed_job is not None:
            _mark_job_failed(failed_job, exc)
            db.commit()
        db.rollback()
        latest_job = db.get(ReviewJob, job_id)
        if latest_job is None or (
            latest_job.status != "failed"
            or latest_job.claimed_by is not None
            or latest_job.attempt_count not in (attempt, MAX_REVIEW_JOB_ATTEMPTS)
        ):
            db.rollback()
            return 0, 0
        db.rollback()
        record_ai_endpoint_failure(db, ai_config_id, exc)
        return 0, 1
