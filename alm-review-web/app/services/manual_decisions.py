"""Manual review decisions and active review status resolution.

Kept separate from verdict-producing modules so changes to operator decision
persistence or lookup queries never alter `_review_implementation_hash()`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import case, desc, select
from sqlalchemy.orm import Session

from app.models import AlmRun, ManualDecision, ReviewJob, ReviewResult
from app.services.copilot_reports import CopilotMode, CopilotOutcome, load_copilot_reports
from app.services.review_policy import current_review_policy_key

VALID_MANUAL_DECISIONS = {
    "needs_manual_review": {"confirmed_qualified", "confirmed_unqualified"},
    "unqualified": {"override_qualified"},
}
WARNING_QUALIFIED_DECISION = "confirmed_warning_qualified"
FORCE_QUALIFIED_DECISIONS = frozenset(
    {"override_qualified", "confirmed_qualified", WARNING_QUALIFIED_DECISION}
)


@dataclass
class CurrentReview:
    result: ReviewResult | None
    manual_decision: ManualDecision | None
    final_status: str
    has_warning: bool = False
    temporary_evidence_used: bool = False
    copilot_reports: dict[CopilotMode, CopilotOutcome] = field(default_factory=dict)


def allowed_manual_decisions(review: CurrentReview) -> frozenset[str]:
    if review.manual_decision is not None:
        return frozenset()
    if review.result is None:
        if review.final_status == "review_failed":
            return frozenset({"override_qualified"})
        return frozenset()
    if review.final_status == "unqualified":
        return frozenset(VALID_MANUAL_DECISIONS["unqualified"])
    allowed = set(VALID_MANUAL_DECISIONS.get(review.result.verdict, set()))
    if review.result.verdict == "qualified" and review.has_warning:
        allowed.add(WARNING_QUALIFIED_DECISION)
    return frozenset(allowed)


def has_review_warning(warnings_json: str | None) -> bool:
    """An empty warning list is stored as '[]', so a non-empty string is not enough."""
    return bool(warnings_json) and warnings_json.strip() not in ("", "[]")


def has_unresolved_review_warning(
    has_warning: bool,
    manual_decision: ManualDecision | None,
) -> bool:
    return has_warning and not (
        manual_decision
        and manual_decision.decision in FORCE_QUALIFIED_DECISIONS
    )


def review_from_result(
    result: ReviewResult,
    manual: ManualDecision | None,
    has_warning: bool,
) -> CurrentReview:
    reports = load_copilot_reports(result.copilot_reviews_json)
    copilot_rejected = result.verdict == "qualified" and any(
        report.verdict == "unqualified" for report in reports.values()
    )
    if manual and manual.decision in FORCE_QUALIFIED_DECISIONS:
        final_status = "qualified"
    elif copilot_rejected or (manual and manual.decision == "confirmed_unqualified"):
        final_status = "unqualified"
    elif result.verdict in {"qualified", "unqualified"}:
        final_status = result.verdict
    else:
        final_status = "needs_manual_review"
    return CurrentReview(
        result,
        manual,
        final_status,
        has_unresolved_review_warning(has_warning, manual),
        bool(result.temporary_evidence_used),
        reports,
    )


def current_review(
    db: Session,
    run: AlmRun,
    policy_key: str | None = None,
) -> CurrentReview:
    if run.current_revision_id is None:
        return CurrentReview(None, None, "pending_review")
    active_policy_key = policy_key or current_review_policy_key(db, run.workspace_id)

    result = db.scalar(
        select(ReviewResult)
        .where(
            ReviewResult.run_id == run.run_id,
            ReviewResult.revision_id == run.current_revision_id,
            ReviewResult.source_hash == run.source_hash,
        )
        .order_by(
            desc(
                case(
                    (ReviewResult.review_policy_key == active_policy_key, 1),
                    else_=0,
                )
            ),
            desc(ReviewResult.completed_at),
            desc(ReviewResult.id),
        )
        .limit(1)
    )
    manual = db.scalar(
        select(ManualDecision)
        .where(
            ManualDecision.run_id == run.run_id,
            ManualDecision.revision_id == run.current_revision_id,
            ManualDecision.source_hash == run.source_hash,
        )
        .order_by(desc(ManualDecision.created_at), desc(ManualDecision.id))
        .limit(1)
    )
    if result is None:
        if manual and manual.decision in FORCE_QUALIFIED_DECISIONS:
            return CurrentReview(None, manual, "qualified")
        failed_job = db.scalar(
            select(ReviewJob.id)
            .where(
                ReviewJob.revision_id == run.current_revision_id,
                ReviewJob.status == "failed",
            )
            .limit(1)
        )
        return CurrentReview(
            None,
            manual,
            "review_failed" if failed_job else "pending_review",
        )
    return review_from_result(result, manual, has_review_warning(result.warnings_json))


def revoke_manual_decision(db: Session, run: AlmRun) -> int:
    if run.current_revision_id is None:
        return 0
    decisions = db.scalars(
        select(ManualDecision).where(
            ManualDecision.run_id == run.run_id,
            ManualDecision.revision_id == run.current_revision_id,
            ManualDecision.source_hash == run.source_hash,
        )
    ).all()
    for decision in decisions:
        db.delete(decision)
    if decisions:
        db.commit()
    return len(decisions)


def save_manual_decision(
    db: Session,
    run: AlmRun,
    decision: str,
    operator: str,
    reason: str,
) -> ManualDecision:
    review = current_review(db, run)
    allowed = allowed_manual_decisions(review)
    if decision not in allowed:
        basis = (
            f"AI verdict {review.result.verdict!r}"
            if review.result is not None
            else f"review status {review.final_status!r}"
        )
        raise ValueError(
            f"Decision {decision!r} is not allowed for {basis}."
        )
    if not reason.strip():
        raise ValueError("A reason is required.")

    manual = ManualDecision(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        revision_id=run.current_revision_id,
        review_result_id=review.result.id if review.result else None,
        decision=decision,
        operator=operator.strip() or "unknown",
        reason=reason.strip(),
        source_hash=run.source_hash,
        original_ai_verdict=(
            review.result.verdict if review.result else review.final_status
        ),
    )
    db.add(manual)
    db.commit()
    db.refresh(manual)
    return manual
