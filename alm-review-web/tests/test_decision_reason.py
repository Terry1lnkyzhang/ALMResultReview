import json

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import PROJECT_DIR
from app.database import Base, get_db
from app.models import AlmRun, ManualDecision, ReviewJob, ReviewResult, RunRevision
from app.services.decision_reason import (
    DecisionAnswer,
    DecisionRow,
    answer_problems,
    compose_reason,
    decision_items_json,
    decision_rows,
    validate_answers,
)
from app.services.review_policy import current_review_policy_key
from app.web import router

EQUIPMENT_SUMMARY = "已核验匹配设备，但其他设备数据无法通过台账确认（标识符：CHESS-CAST-20006）。"
IMAGE_SUMMARY = "证据文件夹中没有可评审的图像。"
WARNING_SUMMARY = "Actual 中 'Evdience' 拼写错误，应为 'Evidence'。"


def _result(**fields) -> ReviewResult:
    return ReviewResult(
        job_id=1,
        run_id=42,
        revision_id=1,
        prompt_version_id=1,
        source_hash="a" * 64,
        model_name="test-model",
        **fields,
    )


def _findings_result(verdict: str = "unqualified", with_location: bool = False) -> dict:
    def step(number: int, status: str, issue_type: str, summary: str) -> dict:
        return {
            "review_step": number,
            "issues": [{"status": status, "type": issue_type, "summary": summary}],
        }

    steps = [
        step(1, "manual", "equipment", EQUIPMENT_SUMMARY),
        step(2, "fail", "screenshot", IMAGE_SUMMARY),
        step(3, "manual", "equipment", EQUIPMENT_SUMMARY),
        step(4, "pass", "language", "ok"),
    ]
    criteria = {}
    if with_location:
        criteria["location_consistency"] = {"status": "fail", "summary": "计算机配置不匹配。"}
    return {
        "verdict": verdict,
        "step_results_json": json.dumps(steps, ensure_ascii=False),
        "criteria_json": json.dumps(criteria, ensure_ascii=False),
        "warnings_json": json.dumps(
            [{"step": 2, "type": "minor_language", "summary": WARNING_SUMMARY}],
            ensure_ascii=False,
        ),
    }


def test_identical_findings_share_one_row_and_warnings_follow_blocking_items() -> None:
    rows = decision_rows(_result(**_findings_result(with_location=True)))

    assert [(row.kind, row.type, row.steps, row.required) for row in rows] == [
        ("fail", "location", (), True),
        ("manual", "equipment", (1, 3), True),
        ("fail", "screenshot", (2,), True),
        ("warning", "minor_language", (2,), False),
    ]
    assert rows[0].step_label == "整体"
    assert rows[0].label == "测试位置"
    assert rows[1].step_label == "步骤 1, 3"
    assert rows[1].label == "设备追溯"
    assert "设备编号" in rows[1].evidence_hint


def test_warnings_are_required_when_they_are_the_only_findings() -> None:
    rows = decision_rows(
        _result(
            verdict="qualified",
            warnings_json=json.dumps(
                [{"step": 1, "type": "minor_language", "summary": WARNING_SUMMARY}]
            ),
        )
    )

    assert [(row.kind, row.required) for row in rows] == [("warning", True)]


def test_failed_review_and_unstructured_results_still_get_one_row() -> None:
    failed = decision_rows(None, "AI endpoint timeout")
    assert [(row.kind, row.summary) for row in failed] == [
        ("review_failed", "AI endpoint timeout")
    ]

    legacy = decision_rows(_result(verdict="unqualified", issue_summary="旧版结论"))
    assert [(row.type, row.summary, row.required) for row in legacy] == [
        ("summary", "旧版结论", True)
    ]


@pytest.mark.parametrize(
    "explanation",
    [
        "合格",
        "确认",
        "OK",
        "证据没问题",
        "已检查证据没问题",
        "图片和执行结果没有问题",
        "Step14没有问题",
        "Step 1, 3, 5 没问题",
        "步骤 3 和步骤 5 没问题",
        "The actual result is consistent with the expect result.",
        "Manual validation passed",
    ],
)
def test_generic_explanations_are_rejected(explanation: str) -> None:
    row = DecisionRow("fail", "screenshot", IMAGE_SUMMARY, (2,))

    problems = answer_problems(row, DecisionAnswer("ai_unable", explanation, "Step2.mp4"))

    assert any("过于笼统" in problem for problem in problems)


@pytest.mark.parametrize(
    "explanation",
    [
        "免校准模体",
        "证据为 MP4 视频",
        "Avg Scan Size 即 AWED",
        "0.33s/r 仅 Tenara 有",
        "更换记录 9/25",
    ],
)
def test_specific_explanations_are_accepted(explanation: str) -> None:
    row = DecisionRow("fail", "screenshot", IMAGE_SUMMARY, (2,))

    assert answer_problems(row, DecisionAnswer("ai_unable", explanation, "Step2.mp4")) == []


def test_category_evidence_and_copied_ai_summary_are_checked() -> None:
    row = DecisionRow("manual", "equipment", EQUIPMENT_SUMMARY, (1,))

    problems = answer_problems(row, DecisionAnswer("", EQUIPMENT_SUMMARY, "没问题"))

    assert problems == [
        "请选择判定类别。",
        "“AI 哪里错了 / 为什么不影响”不能直接复制 AI 结论。",
        "“人工核对的证据”过于笼统，请写明核对的文件名、路径、设备编号或原文。",
    ]


def test_optional_rows_may_stay_blank_but_not_half_filled() -> None:
    row = DecisionRow("warning", "minor_language", WARNING_SUMMARY, (2,), required=False)

    assert answer_problems(row, DecisionAnswer()) == []
    assert answer_problems(row, DecisionAnswer("accepted", "", "")) != []


def test_reason_and_items_list_only_answered_rows() -> None:
    rows = [
        DecisionRow("manual", "equipment", EQUIPMENT_SUMMARY, (1, 3)),
        DecisionRow("warning", "minor_language", WARNING_SUMMARY, (2,), required=False),
    ]
    answers = [
        DecisionAnswer("ai_error", "CHESS-CAST-20006 是 Bay 编号，不是设备", "Step1 Actual 中的 Location"),
        DecisionAnswer(),
    ]

    assert validate_answers(rows, answers) == {}
    assert compose_reason(rows, answers) == (
        "[步骤 1, 3 · 设备追溯] AI 判断错误：CHESS-CAST-20006 是 Bay 编号，不是设备；"
        "证据：Step1 Actual 中的 Location"
    )
    items = json.loads(decision_items_json(rows, answers))
    assert [(item["category"], item["steps"], item["ai_summary"]) for item in items] == [
        ("ai_error", [1, 3], EQUIPMENT_SUMMARY)
    ]


@pytest.fixture
def client_and_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def override_get_db():
        with Session(engine) as db:
            yield db

    app = FastAPI()
    app.mount("/static", StaticFiles(directory=PROJECT_DIR / "app" / "static"), name="static")
    app.include_router(router)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, follow_redirects=False) as client:
        yield client, engine


def _prepare_run(engine, verdict: str = "unqualified") -> int:
    with Session(engine) as db:
        run = AlmRun(run_id=42, source_hash="a" * 64, review_hash="b" * 64, raw_json="{}")
        db.add(run)
        db.flush()
        revision = RunRevision(
            run_id=42,
            revision_number=1,
            source_hash=run.source_hash,
            review_hash=run.review_hash,
            snapshot_json="{}",
        )
        db.add(revision)
        db.flush()
        run.current_revision_id = revision.id
        job = ReviewJob(run_id=42, revision_id=revision.id, status="completed")
        db.add(job)
        db.flush()
        result = ReviewResult(
            job_id=job.id,
            run_id=42,
            revision_id=revision.id,
            prompt_version_id=1,
            source_hash=run.source_hash,
            review_policy_key=current_review_policy_key(db),
            model_name="test-model",
            **_findings_result(verdict),
        )
        db.add(result)
        db.commit()
        return result.id


def test_run_detail_lists_each_finding_for_the_operator(client_and_engine) -> None:
    client, engine = client_and_engine
    _prepare_run(engine)

    response = client.get("/runs/42")

    assert response.status_code == 200
    assert response.text.count('name="category"') == 3
    assert f"AI 结论：{EQUIPMENT_SUMMARY}" in response.text
    assert "步骤 1, 3" in response.text
    assert "选填" in response.text


def test_generic_answers_are_sent_back_with_the_draft(client_and_engine) -> None:
    client, engine = client_and_engine
    result_id = _prepare_run(engine)

    response = client.post(
        "/runs/42/manual-decision",
        data={
            "decision": "override_qualified",
            "review_result_id": str(result_id),
            "category": ["ai_error", "ai_unable", ""],
            "explanation": ["合格", "证据为 MP4 视频", ""],
            "evidence": ["没问题", "Step2.mp4", ""],
        },
    )

    assert response.status_code == 422
    assert "过于笼统" in response.text
    assert "证据为 MP4 视频" in response.text
    with Session(engine) as db:
        assert db.scalar(select(ManualDecision)) is None


def test_structured_answers_are_saved_with_items(client_and_engine) -> None:
    client, engine = client_and_engine
    result_id = _prepare_run(engine)

    response = client.post(
        "/runs/42/manual-decision",
        data={
            "decision": "override_qualified",
            "review_result_id": str(result_id),
            "category": ["reference_data", "ai_unable", ""],
            "explanation": ["CHESS-CAST-20006 是 Bay 编号，不是设备", "证据为 MP4 视频", ""],
            "evidence": ["Step1 Actual 中的 Location", "Step2.mp4", ""],
        },
    )

    assert response.status_code == 303
    with Session(engine) as db:
        manual = db.scalar(select(ManualDecision))
        assert manual is not None
        assert manual.reason.splitlines() == [
            "[步骤 1, 3 · 设备追溯] 参考数据问题：CHESS-CAST-20006 是 Bay 编号，不是设备；"
            "证据：Step1 Actual 中的 Location",
            "[步骤 2 · 截图证据] AI 无法检查：证据为 MP4 视频；证据：Step2.mp4",
        ]
        assert [item["type"] for item in json.loads(manual.items_json)] == [
            "equipment",
            "screenshot",
        ]

    detail = client.get("/runs/42")
    assert "证据：Step2.mp4" in detail.text


def test_stale_review_result_requires_refilling(client_and_engine) -> None:
    client, engine = client_and_engine
    result_id = _prepare_run(engine)

    response = client.post(
        "/runs/42/manual-decision",
        data={
            "decision": "override_qualified",
            "review_result_id": str(result_id + 1),
            "category": ["ai_error", "ai_unable", ""],
            "explanation": ["CHESS-CAST-20006 是 Bay 编号", "证据为 MP4 视频", ""],
            "evidence": ["Step1 Location", "Step2.mp4", ""],
        },
    )

    assert response.status_code == 303
    assert "message_kind=error" in response.headers["location"]
    with Session(engine) as db:
        assert db.scalar(select(ManualDecision)) is None


def test_confirmed_unqualified_keeps_free_text_reason(client_and_engine) -> None:
    client, engine = client_and_engine
    _prepare_run(engine, verdict="needs_manual_review")

    response = client.post(
        "/runs/42/manual-decision",
        data={"decision": "confirmed_unqualified", "reason": "Evidence missing"},
    )

    assert response.status_code == 303
    with Session(engine) as db:
        manual = db.scalar(select(ManualDecision))
        assert manual is not None
        assert manual.reason == "Evidence missing"
        assert manual.items_json is None
