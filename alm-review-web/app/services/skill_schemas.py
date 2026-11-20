"""Single source of truth for review Skill Pydantic contracts and JSON Schemas."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Kept generous on purpose: the application already clips these to 200 characters,
# so a verbose model must not fail the whole review job.
REASON_CHAR_LIMIT = 1000

# The vLLM structured-output grammar rejects these JSON Schema keywords.
_GRAMMAR_UNSUPPORTED_KEYS = frozenset({"uniqueItems"})


class ReviewTextStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_step: int = Field(ge=1)
    description: str
    expected: str
    actual: str


class NumberedComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1)
    expected: str
    actual: str


class ReferenceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    type: Literal["equipment", "image", "html_report", "file", "folder_or_unknown"]
    value: str = Field(min_length=1)
    source_field: Literal["description", "expected", "actual", "attachment"]
    detection_source: Literal[
        "path_extension",
        "path_without_extension",
        "equipment_registry_match",
        "equipment_identifier",
        "alm_attachment",
    ]


class ReferenceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    role: Literal[
        "test_equipment",
        "dut_or_other",
        "result_evidence",
        "reference_document",
        "result_evidence_location",
        "unrelated",
        "uncertain",
    ]
    requires_check: bool
    reason: str = Field(min_length=1, max_length=REASON_CHAR_LIMIT)


class ExtractedEquipment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_name: str = Field(default="", max_length=512)
    equipment_id: str = Field(default="", max_length=128)
    serial_number: str = Field(default="", max_length=255)
    reported_calibration_due_date: str = Field(default="", max_length=32)
    source_text: str = Field(min_length=1, max_length=512)


class EvidenceRoutingStep(ReviewTextStep):
    reference_candidates: list[ReferenceCandidate] = Field(default_factory=list)


class EvidenceRoutingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[EvidenceRoutingStep] = Field(min_length=1)


class EvidenceRoutingStepDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_step: int = Field(ge=1)
    reference_decisions: list[ReferenceDecision]
    extracted_equipment: list[ExtractedEquipment] = Field(
        default_factory=list, max_length=12
    )


class EvidenceRoutingOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[EvidenceRoutingStepDecision]


class TestLocationConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: str = Field(min_length=1)
    product: str
    dms_version: str
    dms_coverage: str
    couch: str
    computer: str


class ActualFormatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signals: list[dict[str, Any]] = Field(default_factory=list)


class AlmTextReviewStep(ReviewTextStep):
    alm_step_status: str = Field(min_length=1)
    actual_format: ActualFormatInput = Field(default_factory=ActualFormatInput)
    numbered_comparison: list[NumberedComparison] = Field(default_factory=list)
    routed_evidence_references: list[str] = Field(default_factory=list)


class AlmTextReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = ""
    alm_run_status: Literal["Passed", "Failed"]
    execution_location_config: TestLocationConfigInput | None = None
    steps: list[AlmTextReviewStep] = Field(min_length=1)


class AlmTextFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "actual_missing",
        "actual_insufficient",
        "expected_actual_mismatch",
        "language_quality",
        "evidence_reference_missing",
        "record_documentation_gap",
    ]
    severity: Literal["warning", "fail", "manual"]
    basis: Literal["direct_step_text", "reference_metadata_inference"]
    reason: str = Field(min_length=1, max_length=REASON_CHAR_LIMIT)


class BoundCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str = Field(min_length=1, max_length=120)
    bound: str = Field(min_length=1, max_length=100)
    actual: str = Field(min_length=1, max_length=100)
    within_bound: bool


class ProseConclusionCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_excerpt: str = Field(min_length=1, max_length=200)
    matches_expected_subject_and_unit: bool
    matches_recorded_data: bool


class AlmTextAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_step: int = Field(ge=1)
    applicability: Literal["applicable", "not_applicable", "manual"]
    bound_checks: list[BoundCheck] = Field(default_factory=list, max_length=6)
    conclusion_checks: list[ProseConclusionCheck] = Field(
        default_factory=list, max_length=5
    )
    findings: list[AlmTextFinding]
    summary: str = Field(min_length=1, max_length=REASON_CHAR_LIMIT)


class AlmTextReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessments: list[AlmTextAssessment]


class ImageMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_id: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    relative_name: str = Field(min_length=1)
    mime_type: str = Field(pattern=r"^image/")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)


class ImageReviewStep(ReviewTextStep):
    alm_step_status: str = Field(min_length=1)
    images: list[ImageMetadata] = Field(min_length=1)
    referenced_steps: list[ReviewTextStep] = Field(default_factory=list)


class ImageReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alm_run_status: Literal["Passed", "Failed"]
    steps: list[ImageReviewStep] = Field(min_length=1)


class ImageAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_step: int = Field(ge=1)
    status: Literal["pass", "fail", "manual"]
    reason: str = Field(min_length=1, max_length=REASON_CHAR_LIMIT)
    observed_media_ids: list[str] = Field(min_length=1)


class ImageReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessments: list[ImageAssessment]


class HtmlBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class HtmlReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_truncated: bool
    omitted_block_count: int = Field(default=0, ge=0)
    blocks: list[HtmlBlock]


class AutomationReleaseRecordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: int
    testcase_id: str
    project_name: str
    script_name: str
    file_path: str
    baseline_name: str
    release_time: str
    release_version: str
    version_number: str
    release_revision: int | None
    release_version_format: str
    document_number: str
    document_revision: str
    report_link: str


class AutomationReleaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "disabled",
        "matched",
        "needs_ai",
        "mismatch",
        "not_found",
        "incomplete",
        "unavailable",
    ]
    project_name: str
    testcase_id: str
    candidate_count: int = Field(ge=0)
    claimed_script_name: str
    claimed_script_testcase_id: str
    html_script_names: list[str]
    html_path_testcase_ids: list[str]
    html_path_testcase_match: Literal["not_checked", "exact", "mismatch"]
    actual_name_match: Literal[
        "not_checked", "exact", "mismatch", "missing"
    ]
    failure_code: Literal[
        "",
        "actual_testcase_id_missing",
        "actual_testcase_id_mismatch",
        "html_script_missing",
        "html_path_testcase_id_mismatch",
        "actual_name_missing",
        "actual_name_html_mismatch",
        "release_not_found",
        "release_not_effective_yet",
        "release_testcase_id_mismatch",
        "document_mismatch",
        "document_incomplete",
    ]
    claimed_document_number: str
    claimed_document_revision: str
    selected_release: AutomationReleaseRecordInput | None
    script_name_match: Literal[
        "not_checked", "exact", "compatible", "mismatch", "uncertain", "not_found"
    ]
    document_match: Literal[
        "not_checked", "exact", "mismatch", "uncertain", "not_found"
    ]
    reason: str = Field(min_length=1, max_length=REASON_CHAR_LIMIT)


class HtmlEvidenceCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    quote: str = Field(min_length=1, max_length=2000)
    supports: list[Literal["description", "expected", "actual", "result"]] = Field(
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )


class HtmlAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_step: int = Field(ge=1)
    status: Literal["pass", "fail", "manual"]
    description_coverage: Literal[
        "supported", "contradicted", "not_found", "uncertain"
    ]
    expected_coverage: Literal[
        "supported", "contradicted", "not_found", "uncertain"
    ]
    actual_coverage: Literal[
        "supported", "contradicted", "not_found", "uncertain"
    ]
    result_consistency: Literal["consistent", "inconsistent", "uncertain"]
    release_consistency: Literal[
        "matched", "mismatched", "uncertain", "not_checked"
    ]
    reviewed_report_ids: list[str] = Field(
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )
    evidence: list[HtmlEvidenceCitation]
    reason: str = Field(min_length=1, max_length=REASON_CHAR_LIMIT)


class HtmlReviewStep(ReviewTextStep):
    alm_step_status: str = Field(min_length=1)
    automation_release: AutomationReleaseInput
    reports: list[HtmlReport] = Field(min_length=1)
    batch_observations: list[HtmlAssessment] = Field(default_factory=list)


class HtmlReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alm_run_status: Literal["Passed", "Failed"]
    review_mode: Literal["evidence_batch", "final"]
    batch_index: int = Field(ge=1)
    batch_count: int = Field(ge=1)
    steps: list[HtmlReviewStep] = Field(min_length=1)


class HtmlReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessments: list[HtmlAssessment]


class EquipmentCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    equipment_id: str = Field(min_length=1)
    description: str
    model_number: str
    serial_number: str


class EquipmentRoleStep(ReviewTextStep):
    reported_identifiers: list[str]
    previously_matched_equipment_ids: list[str]
    candidate_equipment: list[EquipmentCandidate]
    extracted_device_names: list[str] = Field(default_factory=list)


class EquipmentRoleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[EquipmentRoleStep] = Field(min_length=1)
    registry_equipment_names: list[str] = Field(default_factory=list)


class EquipmentRoleDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_step: int = Field(ge=1)
    role: Literal["controlled_equipment", "dut_or_other", "uncertain"]
    required: bool
    selected_equipment_ids: list[str]
    selected_equipment_names: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=REASON_CHAR_LIMIT)


class EquipmentRoleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[EquipmentRoleDecision]


class LocationComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: Literal["product", "dms_version", "dms_coverage", "couch", "computer"]
    parent_text: str = Field(min_length=1, max_length=512)
    status: Literal["matched", "mismatched", "uncertain"]
    reason: str = Field(min_length=1, max_length=REASON_CHAR_LIMIT)


class LocationConsistencyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alm_location: str = Field(min_length=1)
    folder_path: str = Field(min_length=1)
    parent_name: str = Field(min_length=1)
    location_config: TestLocationConfigInput


class LocationConsistencyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_configuration_claim: bool
    status: Literal["pass", "not_applicable", "fail", "uncertain"]
    comparisons: list[LocationComparison] = Field(max_length=5)
    reason: str = Field(min_length=1, max_length=REASON_CHAR_LIMIT)


SKILL_MODELS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    "evidence-routing": (EvidenceRoutingInput, EvidenceRoutingOutput),
    "alm-text-review": (AlmTextReviewInput, AlmTextReviewOutput),
    "image-evidence-review": (ImageReviewInput, ImageReviewOutput),
    "html-evidence-review": (HtmlReviewInput, HtmlReviewOutput),
    "equipment-role": (EquipmentRoleInput, EquipmentRoleOutput),
    "location-consistency": (LocationConsistencyInput, LocationConsistencyOutput),
}


def build_strict_json_schema(
    model: type[BaseModel],
    *,
    require_all_properties: bool = False,
) -> dict[str, Any]:
    """Export an inlined, `$ref`-free JSON Schema suitable for vLLM strict grammar."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def transform(node: Any) -> Any:
        if isinstance(node, list):
            return [transform(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            ref_name = str(node["$ref"]).rsplit("/", 1)[-1]
            resolved = transform(deepcopy(defs[ref_name]))
            siblings = {
                key: transform(value)
                for key, value in node.items()
                if key not in {"$ref", "title", "default"}
            }
            return {**resolved, **siblings}
        cleaned = {
            key: transform(value)
            for key, value in node.items()
            if key != "title" and not (require_all_properties and key == "default")
        }
        if "properties" in cleaned and isinstance(cleaned["properties"], dict):
            cleaned.setdefault("additionalProperties", False)
            if require_all_properties:
                cleaned["required"] = list(cleaned["properties"].keys())
        return cleaned

    inlined = transform(raw)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **inlined,
    }


def grammar_schema(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: grammar_schema(item)
            for key, item in value.items()
            if key not in _GRAMMAR_UNSUPPORTED_KEYS
        }
    if isinstance(value, list):
        return [grammar_schema(item) for item in value]
    return value


def _with_properties(schema: dict[str, Any], **properties: Any) -> dict[str, Any]:
    return {**schema, "properties": {**schema["properties"], **properties}}


def _one_of(*values: Any) -> dict[str, Any]:
    return {"enum": list(dict.fromkeys(values))}


def _exact_array(
    schema: dict[str, Any], items: list[dict[str, Any]]
) -> dict[str, Any]:
    """One item per entry, in order: nothing omitted, repeated or invented."""
    base = {
        key: value
        for key, value in schema.items()
        if key not in {"items", "minItems", "maxItems"}
    }
    return {
        **base,
        "prefixItems": items,
        "items": False,
        "minItems": len(items),
        "maxItems": len(items),
    }


def _choice_array(schema: dict[str, Any], values: list[str]) -> dict[str, Any]:
    if not values:
        return {**schema, "maxItems": 0}
    return {**schema, "items": {**schema["items"], **_one_of(*values)}}


def _routing_ids(
    schema: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    decisions = schema["properties"]["decisions"]
    item = decisions["items"]
    ref_decisions = item["properties"]["reference_decisions"]
    return _with_properties(
        schema,
        decisions=_exact_array(
            decisions,
            [
                _with_properties(
                    item,
                    review_step=_one_of(step["review_step"]),
                    reference_decisions=_exact_array(
                        ref_decisions,
                        [
                            _with_properties(
                                ref_decisions["items"],
                                candidate_id=_one_of(candidate["candidate_id"]),
                            )
                            for candidate in step.get("reference_candidates", [])
                        ],
                    ),
                )
                for step in data["steps"]
            ],
        ),
    )


def _text_review_ids(
    schema: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    assessments = schema["properties"]["assessments"]
    item = assessments["items"]
    return _with_properties(
        schema,
        assessments=_exact_array(
            assessments,
            [
                _with_properties(
                    item,
                    review_step=_one_of(step["review_step"]),
                )
                for step in data["steps"]
            ],
        ),
    )


def _image_review_ids(
    schema: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    assessments = schema["properties"]["assessments"]
    item = assessments["items"]
    observed = item["properties"]["observed_media_ids"]
    return _with_properties(
        schema,
        assessments=_exact_array(
            assessments,
            [
                _with_properties(
                    item,
                    review_step=_one_of(step["review_step"]),
                    observed_media_ids=_exact_array(
                        observed,
                        [_one_of(image["media_id"]) for image in step["images"]],
                    ),
                )
                for step in data["steps"]
            ],
        ),
    )


def _allowed_html_release_consistencies(
    automation_release: dict[str, Any] | None,
) -> tuple[str, ...] | None:
    if not isinstance(automation_release, dict):
        return None
    release_status = automation_release.get("status")
    required = {
        "disabled": "not_checked",
        "matched": "matched",
        "mismatch": "mismatched",
        "not_found": "mismatched",
        "incomplete": "uncertain",
        "unavailable": "uncertain",
    }.get(release_status)
    if automation_release.get("failure_code") in {
        "html_path_testcase_id_mismatch",
        "actual_name_missing",
        "actual_name_html_mismatch",
    }:
        return {
            "exact": ("matched",),
            "compatible": ("matched",),
            "not_checked": (
                ("uncertain", "mismatched")
                if automation_release.get("selected_release") is None
                else ("uncertain",)
            ),
        }.get(automation_release.get("script_name_match"))
    return (required,) if required is not None else None


def _html_review_ids(
    schema: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    assessments = schema["properties"]["assessments"]
    item = assessments["items"]
    evidence = item["properties"]["evidence"]
    narrowed_steps = []
    for step in data["steps"]:
        # Final synthesis may only reuse citations already verified in a batch.
        pairs = (
            [
                (citation["report_id"], citation["block_id"])
                for observation in step["batch_observations"]
                for citation in observation["evidence"]
            ]
            if step["batch_observations"]
            else [
                (report["report_id"], block["block_id"])
                for report in step["reports"]
                for block in report["blocks"]
            ]
        )
        blocks_by_report: dict[str, list[str]] = {}
        for report_id, block_id in pairs:
            blocks_by_report.setdefault(report_id, []).append(block_id)
        citations = [
            _with_properties(
                evidence["items"],
                report_id=_one_of(report_id),
                block_id=_one_of(*block_ids),
            )
            for report_id, block_ids in blocks_by_report.items()
        ]
        allowed_release = _allowed_html_release_consistencies(
            step.get("automation_release")
        )
        extra_props: dict[str, Any] = {}
        if allowed_release is not None:
            extra_props["release_consistency"] = _one_of(*allowed_release)
            if allowed_release != ("matched",) and (
                step.get("automation_release") or {}
            ).get("status") != "disabled":
                extra_props["status"] = _one_of("fail", "manual")
        narrowed_steps.append(
            _with_properties(
                item,
                review_step=_one_of(step["review_step"]),
                reviewed_report_ids=_exact_array(
                    item["properties"]["reviewed_report_ids"],
                    [_one_of(report["report_id"]) for report in step["reports"]],
                ),
                evidence=(
                    {**evidence, "items": {"anyOf": citations}}
                    if citations
                    else {**evidence, "maxItems": 0}
                ),
                **extra_props,
            )
        )
    return _with_properties(
        schema, assessments=_exact_array(assessments, narrowed_steps)
    )


def _equipment_role_ids(
    schema: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    decisions = schema["properties"]["decisions"]
    item = decisions["items"]
    return _with_properties(
        schema,
        decisions=_exact_array(
            decisions,
            [
                _with_properties(
                    item,
                    review_step=_one_of(step["review_step"]),
                    selected_equipment_ids=_choice_array(
                        item["properties"]["selected_equipment_ids"],
                        [
                            candidate["equipment_id"]
                            for candidate in step["candidate_equipment"]
                        ],
                    ),
                    selected_equipment_names=_choice_array(
                        item["properties"]["selected_equipment_names"],
                        data["registry_equipment_names"],
                    ),
                )
                for step in data["steps"]
            ],
        ),
    )


OUTPUT_ID_CONSTRAINTS: dict[
    str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
] = {
    "evidence-routing": _routing_ids,
    "alm-text-review": _text_review_ids,
    "image-evidence-review": _image_review_ids,
    "html-evidence-review": _html_review_ids,
    "equipment-role": _equipment_role_ids,
}
