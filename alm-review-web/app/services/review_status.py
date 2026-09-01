from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import case, desc, select, tuple_
from sqlalchemy.orm import Session, load_only

from app.models import AlmRun, ManualDecision, ReviewJob, ReviewResult
from app.services.reviews import CurrentReview

# Manual decisions that pin a Run to qualified against, or without, an AI verdict.
FORCE_QUALIFIED_DECISIONS = frozenset({"override_qualified", "confirmed_qualified"})


def is_force_qualified(review: CurrentReview) -> bool:
    manual = review.manual_decision
    return manual is not None and manual.decision in FORCE_QUALIFIED_DECISIONS


def _review_from_result(
    result: ReviewResult,
    manual: ManualDecision | None,
) -> CurrentReview:
    if result.verdict == "qualified":
        final_status = "qualified"
    elif result.verdict == "unqualified":
        final_status = (
            "qualified"
            if manual and manual.decision == "override_qualified"
            else "unqualified"
        )
    elif manual and manual.decision == "confirmed_qualified":
        final_status = "qualified"
    elif manual and manual.decision == "confirmed_unqualified":
        final_status = "unqualified"
    else:
        final_status = "needs_manual_review"
    return CurrentReview(result, manual, final_status)


def review_update_reasons(
    result: ReviewResult | None,
    policy_key: str,
    prompt_version_id: int | None,
    model_name: str | None,
) -> tuple[str, ...]:
    if result is None or result.review_policy_key == policy_key:
        return ()
    reasons = []
    if prompt_version_id is not None and result.prompt_version_id != prompt_version_id:
        reasons.append(
            f"提示词版本已从 {result.prompt_version_id} 更新为 {prompt_version_id}"
        )
    if model_name is not None and result.model_name != model_name:
        reasons.append(f"AI 模型已从 {result.model_name} 更新为 {model_name}")
    reasons.append("评审规则、证据设置或设备台账已更新")
    return tuple(reasons)


def current_reviews(
    db: Session,
    runs: Sequence[AlmRun],
    policy_key: str,
) -> dict[int, CurrentReview]:
    reviews = {
        run.run_id: CurrentReview(None, None, "pending_review") for run in runs
    }
    active_runs = [run for run in runs if run.current_revision_id is not None]
    if not active_runs:
        return reviews

    runs_by_id = {run.run_id: run for run in active_runs}
    review_keys = [
        (run.run_id, run.current_revision_id, run.source_hash) for run in active_runs
    ]
    results = db.scalars(
        select(ReviewResult)
        .options(
            load_only(
                ReviewResult.id,
                ReviewResult.run_id,
                ReviewResult.revision_id,
                ReviewResult.source_hash,
                ReviewResult.review_policy_key,
                ReviewResult.prompt_version_id,
                ReviewResult.model_name,
                ReviewResult.verdict,
                ReviewResult.issue_summary,
                ReviewResult.completed_at,
            )
        )
        .where(
            tuple_(
                ReviewResult.run_id,
                ReviewResult.revision_id,
                ReviewResult.source_hash,
            ).in_(review_keys),
        )
        .order_by(
            ReviewResult.run_id,
            desc(
                case(
                    (ReviewResult.review_policy_key == policy_key, 1),
                    else_=0,
                )
            ),
            desc(ReviewResult.completed_at),
            desc(ReviewResult.id),
        )
    ).all()
    results_by_run: dict[int, ReviewResult] = {}
    for result in results:
        results_by_run.setdefault(result.run_id, result)

    manuals_by_result: dict[int, ManualDecision] = {}
    if results_by_run:
        manuals = db.scalars(
            select(ManualDecision)
            .options(
                load_only(
                    ManualDecision.id,
                    ManualDecision.run_id,
                    ManualDecision.revision_id,
                    ManualDecision.review_result_id,
                    ManualDecision.decision,
                    ManualDecision.source_hash,
                    ManualDecision.created_at,
                )
            )
            .where(
                ManualDecision.review_result_id.in_(
                    result.id for result in results_by_run.values()
                )
            )
            .order_by(
                ManualDecision.review_result_id,
                desc(ManualDecision.created_at),
                desc(ManualDecision.id),
            )
        ).all()
        for manual in manuals:
            result = results_by_run.get(manual.run_id)
            run = runs_by_id.get(manual.run_id)
            if (
                result is not None
                and run is not None
                and manual.review_result_id == result.id
                and manual.revision_id == run.current_revision_id
                and manual.source_hash == run.source_hash
            ):
                manuals_by_result.setdefault(manual.review_result_id, manual)

    unresolved_revision_ids = {
        run.current_revision_id
        for run in active_runs
        if run.run_id not in results_by_run
    }
    failed_revision_ids = set()
    if unresolved_revision_ids:
        failed_revision_ids = set(
            db.scalars(
                select(ReviewJob.revision_id)
                .where(
                    ReviewJob.revision_id.in_(unresolved_revision_ids),
                    ReviewJob.status == "failed",
                )
                .distinct()
            ).all()
        )

    for run in active_runs:
        result = results_by_run.get(run.run_id)
        if result is not None:
            reviews[run.run_id] = _review_from_result(
                result,
                manuals_by_result.get(result.id),
            )
        elif run.current_revision_id in failed_revision_ids:
            reviews[run.run_id] = CurrentReview(None, None, "review_failed")
    return reviews