import json
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AiConfig,
    AlmRun,
    EvidenceConfig,
    PromptVersion,
    ReviewJob,
    RunRevision,
)
from app.services.image_evidence import ImageEvidenceResult, ResolvedImage
from app.services.review_pipeline import (
    STAGE_GATES,
    STAGE_ORDER,
    STAGE_SKILLS,
    build_pipeline_trace,
)
from app.services.reviews import process_job
from app.services.skill_runner import discover_skills, load_skill

EXPECTED_STAGE_ORDER = [
    "routing",
    "text_review",
    "location_review",
    "image_review",
    "report_review",
    "equipment_review",
    "aggregation",
]
ALLOWED_ROOT = r"\\server\approved"
IMAGE_PATH = ALLOWED_ROOT + r"\case\Step1.png"

# Skill titles are read from the packages so renaming a Skill cannot silently pass.
SKILL_TITLES = {
    skill_id: load_skill(skill_id).instructions.splitlines()[0].lstrip("# ").strip()
    for skill_id in (
        "alm-text-review",
        "location-consistency",
        "image-evidence-review",
        "equipment-role",
    )
}


class StubResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "choices": [
                {"message": {"content": json.dumps(self.payload, ensure_ascii=False)}}
            ]
        }


class StubImageResolver:
    def __init__(self, **_kwargs: Any) -> None:
        return None

    def resolve(self, _value: str, _allowed_root: str) -> ImageEvidenceResult:
        return ImageEvidenceResult(
            status="ready",
            images=(
                ResolvedImage(
                    relative_name="Step1.png",
                    media_type="image/png",
                    size_bytes=12,
                    sha256="a" * 64,
                    data_url="data:image/png;base64,AA==",
                ),
            ),
        )


def identify_skill(system_content: str) -> str:
    for skill_id, title in SKILL_TITLES.items():
        if title in system_content:
            return skill_id
    raise AssertionError(f"Unexpected Skill request: {system_content[:80]}")


def reference_role(candidate: dict[str, Any]) -> tuple[str, bool]:
    if candidate["type"] == "equipment":
        return "dut_or_other", False
    return "result_evidence", True


def skill_response(request: dict[str, Any]) -> StubResponse:
    system_content = request["messages"][0]["content"]
    user_content = request["messages"][1]["content"]
    skill_id = identify_skill(system_content)
    raw_input = user_content if isinstance(user_content, str) else user_content[0]["text"]
    payload = json.loads(raw_input)
    if skill_id == "location-consistency":
        if payload["parent_name"] == "0. Common Config":
            return StubResponse(
                {
                    "has_configuration_claim": False,
                    "status": "not_applicable",
                    "comparisons": [],
                    "reason": "父层级没有声明具体配置。",
                }
            )
        return StubResponse(
            {
                "has_configuration_claim": True,
                "status": "fail",
                "comparisons": [
                    {
                        "field": "dms_version",
                        "parent_text": "V2 or V6",
                        "status": "matched",
                        "reason": "数据库中的 V6 属于允许范围。",
                    },
                    {
                        "field": "dms_coverage",
                        "parent_text": "4cm",
                        "status": "mismatched",
                        "reason": "数据库配置为 2cm。",
                    },
                ],
                "reason": "DMS coverage 与测试位置配置不一致。",
            }
        )
    steps = payload["steps"]
    if skill_id == "alm-text-review":
        return StubResponse(
            {
                "assessments": [
                    {
                        "review_step": step["review_step"],
                        "applicability": "applicable",
                        "findings": [],
                        "reference_decisions": [
                            {
                                "candidate_id": candidate["candidate_id"],
                                "role": reference_role(candidate)[0],
                                "requires_check": reference_role(candidate)[1],
                                "reason": "The candidate belongs to this step.",
                            }
                            for candidate in step["reference_candidates"]
                        ],
                        "summary": "Actual supports Expected.",
                    }
                    for step in steps
                ]
            }
        )
    if skill_id == "image-evidence-review":
        return StubResponse(
            {
                "assessments": [
                    {
                        "review_step": step["review_step"],
                        "status": "pass",
                        "reason": "The supplied images support Expected.",
                        "observed_media_ids": [
                            image["media_id"] for image in step["images"]
                        ],
                    }
                    for step in steps
                ]
            }
        )
    return StubResponse(
        {
            "decisions": [
                {
                    "review_step": step["review_step"],
                    "role": "dut_or_other",
                    "required": False,
                    "selected_equipment_ids": [],
                    "reason": "The device is the product under test.",
                }
                for step in steps
            ]
        }
    )


def configure_review(db: Session) -> None:
    db.add(
        AiConfig(
            id=1,
            base_url="http://ai.test/v1/chat/completions",
            model_name="test-model",
            enabled=True,
        )
    )
    db.add(
        PromptVersion(
            name="test",
            template="Review this content: {{RUN_CONTENT}}",
            is_active=True,
        )
    )
    db.commit()


def add_job(db: Session, *, actual: str) -> ReviewJob:
    snapshot = {
        "run": {
            "id": "42",
            "status": "Passed",
            "execution-date": "2026-08-01",
            "execution-time": "10:30:00",
            "steps": [
                {
                    "step-order": "1",
                    "name": "Capture evidence",
                    "status": "Passed",
                    "descriptionText": "Capture the result evidence.",
                    "expectedText": "The result is recorded.",
                    "actualText": actual,
                    "execution-date": "2026-08-01",
                    "execution-time": "10:31:00",
                }
            ],
        }
    }
    run = AlmRun(
        run_id=42,
        run_status="Passed",
        source_hash="a" * 64,
        review_hash="b" * 64,
        raw_json=json.dumps(snapshot),
    )
    db.add(run)
    db.flush()
    revision = RunRevision(
        run_id=run.run_id,
        revision_number=1,
        source_hash=run.source_hash,
        review_hash=run.review_hash,
        snapshot_json=json.dumps(snapshot),
    )
    db.add(revision)
    db.flush()
    run.current_revision_id = revision.id
    job = ReviewJob(run_id=run.run_id, revision_id=revision.id, status="queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def run_pipeline(
    monkeypatch,
    *,
    actual: str,
    external_evidence: bool | None = None,
    location_assessment: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str], str, dict[str, Any]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        configure_review(db)
        if external_evidence is not None:
            db.add(
                EvidenceConfig(
                    allowed_network_root=ALLOWED_ROOT,
                    external_evidence_review_enabled=external_evidence,
                )
            )
            db.commit()
        job = add_job(db, actual=actual)
        called_skills: list[str] = []

        def post(*_args, **kwargs):
            request = kwargs["json"]
            called_skills.append(identify_skill(request["messages"][0]["content"]))
            return skill_response(request)

        monkeypatch.setattr("app.services.reviews.httpx.post", post)
        monkeypatch.setattr(
            "app.services.reviews.NetworkImageResolver", StubImageResolver
        )
        if location_assessment is not None:
            monkeypatch.setattr(
                "app.services.reviews.load_location_assessment",
                lambda *_args: location_assessment,
            )

        result = process_job(db, job)
        return (
            json.loads(result.pipeline_json),
            called_skills,
            result.verdict,
            json.loads(result.criteria_json),
        )


def ready_location_assessment(parent_name: str) -> dict[str, Any]:
    return {
        "status": "ready",
        "failure_code": "",
        "reason": "The location has one effective configuration row.",
        "alm_location": "CHESS-CAST-20006",
        "folder_path": f"Testing / Li Xianjin / {parent_name}",
        "parent_name": parent_name,
        "candidate_count": 1,
        "candidate_items": ["SY Bay10(CHESS-CAST-20006)"],
        "selected_config": {
            "item": "SY Bay10(CHESS-CAST-20006)",
            "product": "Tenara",
            "dms_version": "V6",
            "dms_coverage": "2cm",
            "couch": "Enhance Incisive STD",
            "computer": "CT Tenara/CT 5300 G5 STD",
        },
    }


def test_pipeline_stages_keep_their_declared_order(monkeypatch) -> None:
    pipeline, called_skills, _verdict, _criteria = run_pipeline(
        monkeypatch,
        actual="The result was recorded in the test report.",
    )

    assert list(pipeline["stages"]) == EXPECTED_STAGE_ORDER
    assert called_skills == ["alm-text-review"]


def test_declaration_is_the_source_of_the_stage_order() -> None:
    assert list(STAGE_ORDER) == EXPECTED_STAGE_ORDER
    assert set(STAGE_SKILLS.values()) <= set(discover_skills())
    assert set(STAGE_GATES.values()) == {
        "external_evidence_review_enabled",
        "equipment_review_enabled",
    }


def test_undeclared_or_missing_stages_are_rejected() -> None:
    gates = {
        "external_evidence_review_enabled": False,
        "equipment_review_enabled": False,
    }
    complete = {stage_id: {"ai_calls": 0} for stage_id in STAGE_ORDER}

    with pytest.raises(ValueError, match="did not report stages"):
        build_pipeline_trace(
            gates=gates,
            plan={},
            stages={
                stage_id: {"ai_calls": 0}
                for stage_id in STAGE_ORDER
                if stage_id != "aggregation"
            },
        )

    with pytest.raises(ValueError, match="undeclared stages"):
        build_pipeline_trace(
            gates=gates,
            plan={},
            stages={**complete, "shadow_review": {"ai_calls": 0}},
        )


def test_pipeline_ai_call_counts_match_the_stage_totals(monkeypatch) -> None:
    pipeline, called_skills, _verdict, _criteria = run_pipeline(
        monkeypatch,
        actual="The result was recorded in the test report.",
    )

    stage_calls = sum(stage["ai_calls"] for stage in pipeline["stages"].values())
    assert stage_calls == pipeline["total_ai_calls"] == len(called_skills)


def test_image_stage_runs_after_the_text_stage(monkeypatch) -> None:
    pipeline, called_skills, _verdict, _criteria = run_pipeline(
        monkeypatch,
        actual=f"The screenshot was archived at {IMAGE_PATH}",
        external_evidence=True,
    )

    assert called_skills == ["alm-text-review", "image-evidence-review"]
    assert pipeline["stages"]["text_review"]["ai_calls"] == 1
    assert pipeline["stages"]["image_review"]["status"] == "completed"
    assert pipeline["stages"]["image_review"]["ai_calls"] == 1
    assert pipeline["total_ai_calls"] == 2


def test_external_stages_are_disabled_without_evidence_review(monkeypatch) -> None:
    pipeline, called_skills, _verdict, _criteria = run_pipeline(
        monkeypatch,
        actual=f"The screenshot was archived at {IMAGE_PATH}",
        external_evidence=False,
    )

    assert called_skills == ["alm-text-review"]
    assert pipeline["stages"]["image_review"]["status"] == "disabled"
    assert pipeline["stages"]["report_review"]["status"] == "disabled"
    assert pipeline["stages"]["image_review"]["ai_calls"] == 0


def test_missing_location_configuration_fails_without_location_ai(monkeypatch) -> None:
    assessment = {
        **ready_location_assessment("0. Common Config"),
        "status": "fail",
        "failure_code": "location_config_not_found",
        "reason": "No test-location configuration matches the ALM Location.",
        "candidate_count": 0,
        "candidate_items": [],
        "selected_config": None,
    }

    pipeline, called_skills, verdict, criteria = run_pipeline(
        monkeypatch,
        actual="The result was recorded in the test report.",
        location_assessment=assessment,
    )

    assert called_skills == ["alm-text-review"]
    assert pipeline["stages"]["location_review"]["ai_calls"] == 0
    assert pipeline["stages"]["location_review"]["assessment"]["status"] == "fail"
    assert verdict == "unqualified"
    assert criteria["location_consistency"]["status"] == "fail"


def test_generic_parent_name_is_not_a_configuration_claim(monkeypatch) -> None:
    pipeline, called_skills, verdict, criteria = run_pipeline(
        monkeypatch,
        actual="The result was recorded in the test report.",
        location_assessment=ready_location_assessment("0. Common Config"),
    )

    assert called_skills == ["alm-text-review", "location-consistency"]
    assessment = pipeline["stages"]["location_review"]["assessment"]
    assert assessment["status"] == "not_applicable"
    assert assessment["semantic_review"]["has_configuration_claim"] is False
    assert verdict == "qualified"
    assert criteria["location_consistency"]["status"] == "not_applicable"


def test_parent_configuration_mismatch_fails_the_run(monkeypatch) -> None:
    pipeline, called_skills, verdict, criteria = run_pipeline(
        monkeypatch,
        actual="The result was recorded in the test report.",
        location_assessment=ready_location_assessment("4.6 DMS-4cm V2 or V6"),
    )

    assert called_skills == ["alm-text-review", "location-consistency"]
    assessment = pipeline["stages"]["location_review"]["assessment"]
    assert assessment["status"] == "fail"
    assert assessment["semantic_review"]["comparisons"][1]["field"] == (
        "dms_coverage"
    )
    assert verdict == "unqualified"
    assert criteria["location_consistency"]["status"] == "fail"
