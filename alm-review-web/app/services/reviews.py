"""Backward-compatible re-export facade over decomposed review services.

New code should import from the dedicated modules directly:
- `manual_decisions`: `CurrentReview`, `current_review`, `save_manual_decision`, `revoke_manual_decision`
- `review_jobs`: `process_job`, `claim_next_review_job`, `renew_review_job_lease`, `reap_abandoned_review_jobs`
- `review_engine` / `stage_*`: `execute_review` and stage helpers
- `review_types`: `ReviewContext`, `ReviewPlan`, `PreparedImageEvidence`, constants

Kept outside `_REVIEW_POLICY_FILES` so non-verdict changes never alter the review policy hash.
"""
from __future__ import annotations

import httpx

from app.services.ai_transport import (
    ai_headers as _ai_headers,
    completion_url as _completion_url,
    test_ai_connection,
)
from app.services.automation_release import load_automation_releases
from app.services.equipment_pipeline import (
    _apply_disabled_equipment_guards,
    _apply_equipment_guards,
    run_equipment_review_stage as _equipment_review_stage,
)
from app.services.html_evidence import HtmlEvidenceResolver
from app.services.image_evidence import NetworkImageResolver
from app.services.location_review import load_location_assessment
from app.services.manual_decisions import (
    FORCE_QUALIFIED_DECISIONS,
    VALID_MANUAL_DECISIONS,
    WARNING_QUALIFIED_DECISION,
    CurrentReview,
    allowed_manual_decisions,
    current_review,
    has_review_warning,
    has_unresolved_review_warning,
    revoke_manual_decision,
    save_manual_decision,
)
from app.services.review_engine import (
    _build_review_plan,
    _plan_specialist_passes,
    _prepare_stage,
    execute_review,
)
from app.services.review_jobs import (
    DEFAULT_JOB_LEASE_SECONDS,
    FAILED_JOB_RETRY_BACKOFF_SECONDS,
    MAX_REVIEW_JOB_ATTEMPTS,
    ReviewJobLeaseLost,
    claim_next_review_job,
    process_claimed_review_job,
    process_job,
    process_queued_jobs,
    reap_abandoned_review_jobs,
    renew_review_job_lease,
)
from app.services.review_types import (
    CRITERIA_NAMES,
    HTML_SKILL_CHAR_BUDGET,
    STEP_FIELD_CHAR_LIMIT,
    TEXT_BATCH_CHAR_BUDGET,
    TEXT_BATCH_MAX_STEPS,
    VALID_CRITERION_STATUSES,
    VALID_VERDICTS,
    PipelineOutcome,
    PreparedImageEvidence,
    ReportReviewRequest,
    ReviewContext,
    ReviewPlan,
    _append_step_issue,
    _clip_step_text,
    _criterion,
    _evidence_actions,
    _manual_criterion,
    _recalculate_result,
    _routed_paths,
)
from app.services.skill_runner import skill_runner
from app.services.stage_aggregation import (
    _apply_capability_guards,
    aggregate_review_pipeline as _aggregate_review_pipeline,
)
from app.services.stage_image_review import (
    _IMAGE_EVIDENCE_ISSUES,
    _image_audit_metadata,
    _image_review_batches,
    _merge_image_skill_trace,
    _prepare_image_evidence,
    _run_image_review_skill,
    run_image_review_stage as _image_review_stage,
)
from app.services.stage_location_review import (
    run_location_review_stage as _location_review_stage,
)
from app.services.stage_report_review import (
    _compact_html_quote,
    _guard_selected_html_fail,
    _html_block_stats,
    _html_final_input,
    _html_skill_batch_inputs,
    _html_skill_input,
    _manual_html_assessment,
    _reuse_verified_html_citations,
    _run_html_review_batches,
    _run_html_review_skill,
    _run_report_pipeline,
    _select_html_blocks,
    _validate_html_assessment,
    _validate_html_batch_assessment,
    _validate_html_citations,
    run_report_review_stage as _report_review_stage,
)
from app.services.stage_routing import (
    _apply_reference_routing,
    _attach_reference_candidates,
    _equipment_source_field,
    _fallback_missing_routing_references,
    _routes_result_evidence,
    _run_evidence_routing_skill,
    _validate_reference_decision,
    _validate_routing_references,
    finalize_routing_after_applicability,
    routed_evidence_references,
    run_routing_stage as _routing_stage,
)
from app.services.stage_text_review import (
    _run_text_semantic_skills,
    _text_skill_batches,
    _text_skill_steps,
    run_text_review_stage as _text_review_stage,
)
from app.services.worker_lease import fence_worker_lease

__all__ = [
    "CRITERIA_NAMES",
    "CurrentReview",
    "DEFAULT_JOB_LEASE_SECONDS",
    "FAILED_JOB_RETRY_BACKOFF_SECONDS",
    "FORCE_QUALIFIED_DECISIONS",
    "HTML_SKILL_CHAR_BUDGET",
    "HtmlEvidenceResolver",
    "MAX_REVIEW_JOB_ATTEMPTS",
    "NetworkImageResolver",
    "PipelineOutcome",
    "PreparedImageEvidence",
    "ReportReviewRequest",
    "ReviewContext",
    "ReviewJobLeaseLost",
    "ReviewPlan",
    "STEP_FIELD_CHAR_LIMIT",
    "TEXT_BATCH_CHAR_BUDGET",
    "TEXT_BATCH_MAX_STEPS",
    "VALID_CRITERION_STATUSES",
    "VALID_MANUAL_DECISIONS",
    "VALID_VERDICTS",
    "WARNING_QUALIFIED_DECISION",
    "_IMAGE_EVIDENCE_ISSUES",
    "_aggregate_review_pipeline",
    "_ai_headers",
    "_append_step_issue",
    "_apply_capability_guards",
    "_apply_disabled_equipment_guards",
    "_apply_equipment_guards",
    "_apply_reference_routing",
    "_attach_reference_candidates",
    "_build_review_plan",
    "_clip_step_text",
    "_compact_html_quote",
    "_completion_url",
    "_criterion",
    "_equipment_review_stage",
    "_equipment_source_field",
    "_evidence_actions",
    "_fallback_missing_routing_references",
    "_guard_selected_html_fail",
    "_html_block_stats",
    "_html_final_input",
    "_html_skill_batch_inputs",
    "_html_skill_input",
    "_image_audit_metadata",
    "_image_review_batches",
    "_image_review_stage",
    "_location_review_stage",
    "_manual_criterion",
    "_manual_html_assessment",
    "_merge_image_skill_trace",
    "_plan_specialist_passes",
    "_prepare_image_evidence",
    "_prepare_stage",
    "_recalculate_result",
    "_report_review_stage",
    "_reuse_verified_html_citations",
    "_routed_paths",
    "_routes_result_evidence",
    "_routing_stage",
    "_run_evidence_routing_skill",
    "_run_html_review_batches",
    "_run_html_review_skill",
    "_run_image_review_skill",
    "_run_report_pipeline",
    "_run_text_semantic_skills",
    "_select_html_blocks",
    "_text_review_stage",
    "_text_skill_batches",
    "_text_skill_steps",
    "_validate_html_assessment",
    "_validate_html_batch_assessment",
    "_validate_html_citations",
    "_validate_reference_decision",
    "_validate_routing_references",
    "allowed_manual_decisions",
    "claim_next_review_job",
    "current_review",
    "execute_review",
    "fence_worker_lease",
    "finalize_routing_after_applicability",
    "has_review_warning",
    "has_unresolved_review_warning",
    "httpx",
    "load_automation_releases",
    "load_location_assessment",
    "process_claimed_review_job",
    "process_job",
    "process_queued_jobs",
    "reap_abandoned_review_jobs",
    "renew_review_job_lease",
    "revoke_manual_decision",
    "routed_evidence_references",
    "save_manual_decision",
    "skill_runner",
    "test_ai_connection",
]
