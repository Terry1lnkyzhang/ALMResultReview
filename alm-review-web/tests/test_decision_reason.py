import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import PROJECT_DIR, get_settings
from app.database import Base, get_db
from app.models import AlmRun, ManualDecision, ReviewJob, ReviewResult, RunRevision, Workspace
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
    assert "Bay 编号" in rows[1].explanation_hint


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

    problems = answer_problems(row, DecisionAnswer(explanation))

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

    assert answer_problems(row, DecisionAnswer(explanation)) == []


def test_copied_ai_summary_is_checked_without_extra_fields() -> None:
    row = DecisionRow("manual", "equipment", EQUIPMENT_SUMMARY, (1,))

    problems = answer_problems(row, DecisionAnswer(EQUIPMENT_SUMMARY))

    assert problems == ["“AI 哪里错了 / 为什么不影响”不能直接复制 AI 结论。"]


def test_optional_rows_may_stay_blank_but_not_half_filled() -> None:
    row = DecisionRow("warning", "minor_language", WARNING_SUMMARY, (2,), required=False)

    assert answer_problems(row, DecisionAnswer()) == []
    assert answer_problems(row, DecisionAnswer("没问题")) != []


def test_reason_and_items_list_only_answered_rows() -> None:
    rows = [
        DecisionRow("manual", "equipment", EQUIPMENT_SUMMARY, (1, 3)),
        DecisionRow("warning", "minor_language", WARNING_SUMMARY, (2,), required=False),
    ]
    answers = [
        DecisionAnswer("CHESS-CAST-20006 是 Bay 编号，不是设备"),
        DecisionAnswer(),
    ]

    assert validate_answers(rows, answers) == {}
    assert compose_reason(rows, answers) == (
        "[步骤 1, 3 · 设备追溯] CHESS-CAST-20006 是 Bay 编号，不是设备"
    )
    items = json.loads(decision_items_json(rows, answers))
    assert [(item["steps"], item["ai_summary"]) for item in items] == [
        ([1, 3], EQUIPMENT_SUMMARY)
    ]
    assert items[0]["explanation"] == "CHESS-CAST-20006 是 Bay 编号，不是设备"
    assert "evidence" not in items[0]


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


def _prepare_run(
    engine, verdict: str = "unqualified", workspace_id: int | None = None, run_id: int = 42,
) -> int:
    with Session(engine) as db:
        run = AlmRun(
            run_id=run_id, workspace_id=workspace_id,
            source_hash="a" * 64, review_hash="b" * 64, raw_json="{}",
        )
        db.add(run)
        db.flush()
        revision = RunRevision(
            run_id=run_id,
            revision_number=1,
            source_hash=run.source_hash,
            review_hash=run.review_hash,
            snapshot_json="{}",
        )
        db.add(revision)
        db.flush()
        run.current_revision_id = revision.id
        job = ReviewJob(run_id=run_id, revision_id=revision.id, status="completed")
        db.add(job)
        db.flush()
        result = ReviewResult(
            job_id=job.id,
            workspace_id=workspace_id,
            run_id=run_id,
            revision_id=revision.id,
            prompt_version_id=1,
            source_hash=run.source_hash,
            review_policy_key=current_review_policy_key(db, workspace_id),
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
    assert response.text.count('name="explanation"') == 3
    assert 'name="category"' not in response.text
    assert 'name="evidence"' not in response.text
    assert f"AI 结论：{EQUIPMENT_SUMMARY}" in response.text
    assert "步骤 1, 3" in response.text
    assert "选填" in response.text
    assert 'data-manual-decision-confirm' in response.text
    assert '<dialog class="sync-confirm-dialog" data-manual-decision-feedback' not in response.text


def test_copilot_finding_is_shown_and_can_be_overridden(client_and_engine) -> None:
    client, engine = client_and_engine
    result_id = _prepare_run(engine, "qualified")
    with Session(engine) as db:
        primary = db.get(ReviewResult, result_id)
        assert primary is not None
        primary.criteria_json = primary.step_results_json = primary.warnings_json = None
        primary.copilot_reviews_json = json.dumps({
            "skill": {"verdict": "unqualified", "findings": [{
                "location": "Locator", "summary": "Finding", "evidence": "Evidence", "rule": "Rule",
            }]},
        })
        db.commit()

    detail = client.get("/runs/42")
    assert detail.status_code == 200
    assert "AI 原始结论（非最终裁决）：合格" in detail.text
    assert "Copilot Skill: 不合格" in detail.text
    assert 'value="override_qualified"' in detail.text
    assert detail.text.count('name="explanation"') == 1
    assert "Locator: Finding (Evidence)" in detail.text

    response = client.post("/runs/42/manual-decision", data={
        "decision": "override_qualified",
        "review_result_id": str(result_id),
        "explanation": "Verified against source",
    })
    assert response.status_code == 303
    with Session(engine) as db:
        manual = db.scalar(select(ManualDecision))
        assert manual is not None
        assert manual.original_ai_verdict == "qualified"
        assert "Copilot Skill" in manual.reason


def test_copilot_web_import_moves_only_current_qualified_runs(client_and_engine, monkeypatch) -> None:
    client, engine = client_and_engine
    settings = get_settings()
    monkeypatch.setattr(settings, "app_role", "combined")
    monkeypatch.setattr(settings, "web_auth_enabled", False)
    with Session(engine) as db:
        workspace = Workspace(name="Workspace", slug="workspace")
        db.add(workspace)
        db.commit()
        workspace_id = workspace.id
    qualified_id = _prepare_run(engine, "qualified", workspace_id=workspace_id)
    _prepare_run(engine, "unqualified", workspace_id=workspace_id, run_id=43)
    task_url = f"/workspaces/{workspace_id}/copilot-review/task"
    import_url = f"/workspaces/{workspace_id}/copilot-review/results"
    assert client.get(task_url).status_code == 403
    remote_dash = client.get(f"/?workspace={workspace_id}&status=qualified")
    assert remote_dash.status_code == 200
    assert "Download Copilot task" not in remote_dash.text

    with TestClient(client.app, follow_redirects=False, client=("127.0.0.1", 50000)) as local_client:
        local_dash = local_client.get(f"/?workspace={workspace_id}&status=qualified")
        assert local_dash.status_code == 200
        assert "Download Copilot task" in local_dash.text
        assert local_client.get(task_url).status_code == 200

    monkeypatch.setattr(settings, "web_auth_enabled", True)
    monkeypatch.setattr(settings, "web_auth_username", "test-user")
    monkeypatch.setattr(settings, "web_auth_password", "test-password")
    auth = ("test-user", "test-password")
    assert client.get(task_url).status_code == 401
    task = client.get(task_url, auth=auth)
    assert task.status_code == 200
    payload = task.json()
    assert [(item["review_result_id"], item["modes"]) for item in payload["runs"]] == [
        (qualified_id, ["skill", "open"]),
    ]
    assert payload["skills"] and all(skill["hash"] for skill in payload["skills"])
    assert payload["external_evidence_review_enabled"] is False

    outcome_files = [("reports", ("open.json", json.dumps({
        "mode": "open",
        "results": [{
            "review_result_id": qualified_id,
            "source_hash": payload["runs"][0]["source_hash"],
            "outcome": {"verdict": "unqualified", "findings": [{
                "location": "Locator", "summary": "Finding", "evidence": "Evidence",
            }]},
        }],
    }), "application/json"))]
    assert client.post(import_url, files=outcome_files).status_code == 401
    assert client.post(
        import_url, files=outcome_files, auth=auth, headers={"origin": "https://invalid.example"},
    ).status_code == 403
    imported = client.post(import_url, files=outcome_files, auth=auth)
    assert imported.status_code == 303
    assert "status=unqualified" in imported.headers["location"]
    dashboard = client.get(imported.headers["location"], auth=auth)
    assert dashboard.status_code == 200
    assert "Copilot Open: Finding" in dashboard.text
    assert '<a href="/runs/42">Copilot Open: Finding</a>' in dashboard.text


def test_copilot_one_click_launch_and_auto_import(
    client_and_engine, monkeypatch, tmp_path
) -> None:
    from pathlib import Path

    from app.services import copilot_review

    client, engine = client_and_engine
    settings = get_settings()
    monkeypatch.setattr(settings, "app_role", "combined")
    monkeypatch.setattr(settings, "web_auth_enabled", False)
    monkeypatch.setattr(copilot_review, "COPILOT_RUNS_DIR", tmp_path / "copilot_runs")
    monkeypatch.setattr(copilot_review, "find_vscode_cli", lambda: "code.cmd")
    monkeypatch.setattr("app.web.find_vscode_cli", lambda: "code.cmd")

    launched: list[tuple[str, Path, Path, str]] = []

    def fake_launch(
        db, workspace, limit, *, modes=("skill", "open"), **kwargs
    ):
        return copilot_review.launch_local_copilot_batch(
            db,
            workspace,
            limit,
            modes=modes,
            launcher=lambda code_bin, instr, task, mode: launched.append(
                (code_bin, instr, task, mode)
            ),
            run_async=False,
            delay_seconds=0.0,
        )

    monkeypatch.setattr("app.web.launch_local_copilot_batch", fake_launch)
    monkeypatch.setenv("NO_PROXY", "*")
    assert "NO_PROXY" not in copilot_review._clean_vscode_env()

    with Session(engine) as db:
        workspace = Workspace(name="Workspace", slug="workspace")
        db.add(workspace)
        db.commit()
        workspace_id = workspace.id
    qualified_id = _prepare_run(engine, "qualified", workspace_id=workspace_id)
    launch_url = f"/workspaces/{workspace_id}/copilot-review/launch"

    assert client.post(launch_url, data={"limit": "5", "mode": "both"}).status_code == 403

    with TestClient(
        client.app, follow_redirects=False, client=("127.0.0.1", 50000)
    ) as local_client:
        dash = local_client.get(f"/?workspace={workspace_id}")
        assert dash.status_code == 200
        assert "一键启动 Copilot 复核" in dash.text

        started = local_client.post(
            launch_url, data={"limit": "5", "mode": "both", "return_status": "all"}
        )
        assert started.status_code == 303
        assert len(launched) == 2
        assert [mode for _, _, _, mode in launched] == ["skill", "open"]

        task_data = json.loads(launched[0][2].read_text(encoding="utf-8"))
        assert "location_assessment" in task_data["runs"][0]
        assert "equipment_context" in task_data["runs"][0]
        source_hash = task_data["runs"][0]["source_hash"]

        status_before = local_client.get(
            f"/api/copilot-review-status?workspace={workspace_id}"
        ).json()
        assert status_before["batch"]["active"] is True
        batch_dir = launched[0][2].parent

        # Simulate Copilot writing UTF-8 BOM open rejection and skill pass files
        (batch_dir / "open-results.json").write_bytes(
            ("\ufeff" + json.dumps({
                "mode": "open",
                "results": [{
                    "review_result_id": qualified_id,
                    "source_hash": source_hash,
                    "outcome": {
                        "verdict": "unqualified",
                        "findings": [{
                            "location": "Step 1",
                            "summary": "Auto imported finding",
                            "evidence": "Cited evidence",
                        }],
                    },
                }],
            })).encode("utf-8")
        )
        (batch_dir / "skill-results.json").write_text(
            json.dumps({
                "mode": "skill",
                "results": [{
                    "review_result_id": qualified_id,
                    "source_hash": source_hash,
                    "outcome": {"verdict": "qualified", "findings": []},
                }],
            }),
            encoding="utf-8",
        )

        status_after = local_client.get(
            f"/api/copilot-review-status?workspace={workspace_id}"
        ).json()
        assert status_after["sync"]["imported_rounds"] == 2
        assert status_after["sync"]["rejected_runs"] == 1
        assert status_after["batch"]["active"] is False
        assert status_after["batch"]["modes"]["open"]["status"] == "imported"
        assert status_after["batch"]["modes"]["skill"]["status"] == "imported"

        unq = local_client.get(f"/?workspace={workspace_id}&status=unqualified")
        assert unq.status_code == 200
        assert "Copilot Open: Auto imported finding" in unq.text

        batch_id = status_after["batch"]["batch_id"]
        cancelled = local_client.post(
            f"/workspaces/{workspace_id}/copilot-review/batches/{batch_id}/cancel"
        )
        assert cancelled.status_code == 303
        assert copilot_review.latest_copilot_batch(workspace_id) is None


def test_copilot_auto_continue_reviews_all_qualified_runs_in_batches(
    client_and_engine, monkeypatch, tmp_path
) -> None:
    from pathlib import Path

    from app.services import copilot_review

    client, engine = client_and_engine
    settings = get_settings()
    monkeypatch.setattr(settings, "app_role", "combined")
    monkeypatch.setattr(settings, "web_auth_enabled", False)
    monkeypatch.setattr(copilot_review, "COPILOT_RUNS_DIR", tmp_path / "copilot_runs")
    monkeypatch.setattr(copilot_review, "find_vscode_cli", lambda: "code.cmd")
    monkeypatch.setattr("app.web.find_vscode_cli", lambda: "code.cmd")

    launched: list[tuple[str, Path, Path, str]] = []
    real_launch = copilot_review.launch_local_copilot_batch
    real_sync = copilot_review.sync_pending_copilot_batches

    def sync_launcher(code_bin, instr, task, mode):
        launched.append((code_bin, instr, task, mode))

    def fake_launch(db, workspace, limit, *, modes=("skill", "open"), **kwargs):
        return real_launch(
            db,
            workspace,
            limit,
            modes=modes,
            auto_continue=kwargs.get("auto_continue", False),
            launcher=sync_launcher,
            run_async=False,
            delay_seconds=0.0,
        )

    def fake_sync(db, workspace_id=None, **kwargs):
        return real_sync(
            db,
            workspace_id,
            launcher=sync_launcher,
            run_async=False,
            delay_seconds=0.0,
            **kwargs,
        )

    monkeypatch.setattr("app.web.launch_local_copilot_batch", fake_launch)
    monkeypatch.setattr("app.web.sync_pending_copilot_batches", fake_sync)

    with Session(engine) as db:
        workspace = Workspace(name="Workspace", slug="workspace-all")
        db.add(workspace)
        db.commit()
        workspace_id = workspace.id

    q1 = _prepare_run(engine, "qualified", workspace_id=workspace_id, run_id=101)
    q2 = _prepare_run(engine, "qualified", workspace_id=workspace_id, run_id=102)
    q3 = _prepare_run(engine, "qualified", workspace_id=workspace_id, run_id=103)
    _prepare_run(engine, "unqualified", workspace_id=workspace_id, run_id=104)

    launch_url = f"/workspaces/{workspace_id}/copilot-review/launch"
    with TestClient(
        client.app, follow_redirects=False, client=("127.0.0.1", 50000)
    ) as local_client:
        dash = local_client.get(f"/?workspace={workspace_id}")
        assert dash.status_code == 200
        assert "全部待审（自动分批）" in dash.text
        assert "当前待审：3 条" in dash.text

        started = local_client.post(
            launch_url,
            data={"limit": "2", "mode": "both", "scope": "all", "return_status": "all"},
        )
        assert started.status_code == 303
        assert len(launched) == 2

        # Attempting to start another batch while active should be blocked.
        dup = local_client.post(
            launch_url,
            data={"limit": "2", "mode": "both", "scope": "all", "return_status": "all"},
        )
        assert dup.status_code == 303
        assert "message_kind=error" in dup.headers["location"]
        assert len(launched) == 2

        batch1_task = json.loads(launched[0][2].read_text(encoding="utf-8"))
        batch1_dir = launched[0][2].parent
        assert [item["review_result_id"] for item in batch1_task["runs"]] == [q1, q2]
        source_hash = batch1_task["runs"][0]["source_hash"]

        # Only writing Open results must NOT launch batch 2 yet (Skill is still pending).
        (batch1_dir / "open-results.json").write_text(
            json.dumps({
                "mode": "open",
                "results": [
                    {
                        "review_result_id": q1,
                        "source_hash": source_hash,
                        "outcome": {
                            "verdict": "unqualified",
                            "findings": [{
                                "location": "Step 1",
                                "summary": "Batch 1 rejection",
                                "evidence": "Mismatch",
                            }],
                        },
                    },
                    {
                        "review_result_id": q2,
                        "source_hash": source_hash,
                        "outcome": {"verdict": "qualified", "findings": []},
                    },
                ],
            }),
            encoding="utf-8",
        )
        mid = local_client.get(f"/api/copilot-review-status?workspace={workspace_id}").json()
        assert mid["sync"]["imported_rounds"] == 1
        assert mid["sync"]["continued_batches"] == 0
        assert len(launched) == 2
        assert mid["batch"]["batch_index"] == 1
        assert mid["batch"]["active"] is True

        # Once Skill finishes too, batch 2 for the remaining run q3 launches automatically.
        (batch1_dir / "skill-results.json").write_text(
            json.dumps({
                "mode": "skill",
                "results": [
                    {
                        "review_result_id": q1,
                        "source_hash": source_hash,
                        "outcome": {"verdict": "qualified", "findings": []},
                    },
                    {
                        "review_result_id": q2,
                        "source_hash": source_hash,
                        "outcome": {"verdict": "qualified", "findings": []},
                    },
                ],
            }),
            encoding="utf-8",
        )
        step2 = local_client.get(f"/api/copilot-review-status?workspace={workspace_id}").json()
        assert step2["sync"]["imported_rounds"] == 1
        assert step2["sync"]["continued_batches"] == 1
        assert len(launched) == 4
        assert step2["batch"]["batch_index"] == 2
        assert step2["batch"]["run_count"] == 1
        assert step2["batch"]["remaining_runs"] == 0
        assert step2["batch"]["completed_runs_total"] == 2
        assert step2["batch"]["rejected_runs_total"] == 1
        assert step2["batch"]["active"] is True

        batch2_task = json.loads(launched[2][2].read_text(encoding="utf-8"))
        batch2_dir = launched[2][2].parent
        assert [item["review_result_id"] for item in batch2_task["runs"]] == [q3]

        for mode_name in ("open", "skill"):
            (batch2_dir / f"{mode_name}-results.json").write_text(
                json.dumps({
                    "mode": mode_name,
                    "results": [{
                        "review_result_id": q3,
                        "source_hash": source_hash,
                        "outcome": {"verdict": "qualified", "findings": []},
                    }],
                }),
                encoding="utf-8",
            )
        done = local_client.get(f"/api/copilot-review-status?workspace={workspace_id}").json()
        assert done["sync"]["imported_rounds"] == 2
        assert done["sync"]["continued_batches"] == 0
        assert len(launched) == 4
        assert done["batch"]["active"] is False
        assert done["batch"]["continuation_state"] == "completed"
        assert done["batch"]["completed_runs_total"] == 3
        assert done["batch"]["rejected_runs_total"] == 1
        assert copilot_review.has_pending_copilot_batches() is False

        final_dash = local_client.get(f"/?workspace={workspace_id}")
        assert final_dash.status_code == 200
        assert "自动连续复核已完成（共 2 批，累计 3 条 Run，1 条转不合格）" in final_dash.text
        assert "当前待审：0 条" in final_dash.text


def test_copilot_auto_continue_stops_on_launch_failure_or_cancel(
    client_and_engine, monkeypatch, tmp_path
) -> None:
    from app.services import copilot_review

    _, engine = client_and_engine
    monkeypatch.setattr(copilot_review, "COPILOT_RUNS_DIR", tmp_path / "copilot_runs")
    monkeypatch.setattr(copilot_review, "find_vscode_cli", lambda: "code.cmd")

    with Session(engine) as db:
        workspace = Workspace(name="Workspace", slug="workspace-stop")
        db.add(workspace)
        db.commit()
        workspace_id = workspace.id

    q1 = _prepare_run(engine, "qualified", workspace_id=workspace_id, run_id=201)
    _prepare_run(engine, "qualified", workspace_id=workspace_id, run_id=202)

    launched_modes: list[str] = []

    def flaky_launcher(code_bin, instr, task, mode):
        launched_modes.append(mode)
        if mode == "skill":
            raise RuntimeError("VS Code window closed")

    with Session(engine) as db:
        workspace = db.get(Workspace, workspace_id)
        assert workspace is not None
        manifest = copilot_review.launch_local_copilot_batch(
            db,
            workspace,
            1,
            modes=("skill", "open"),
            auto_continue=True,
            launcher=flaky_launcher,
            run_async=False,
            delay_seconds=0.0,
        )
        batch_dir = Path(manifest["task_path"]).parent
        task_data = json.loads(Path(manifest["task_path"]).read_text(encoding="utf-8"))
        source_hash = task_data["runs"][0]["source_hash"]
        assert task_data["runs"][0]["review_result_id"] == q1

        # Open finishes, but Skill failed to launch -> must stop auto-continuation.
        (batch_dir / "open-results.json").write_text(
            json.dumps({
                "mode": "open",
                "results": [{
                    "review_result_id": q1,
                    "source_hash": source_hash,
                    "outcome": {"verdict": "qualified", "findings": []},
                }],
            }),
            encoding="utf-8",
        )
        summary = copilot_review.sync_pending_copilot_batches(
            db,
            workspace_id,
            launcher=flaky_launcher,
            run_async=False,
            delay_seconds=0.0,
        )
        assert summary["imported_rounds"] == 1
        assert summary["continued_batches"] == 0
        assert launched_modes == ["skill", "open"]

        batch_state = copilot_review.latest_copilot_batch(workspace_id)
        assert batch_state is not None
        assert batch_state["active"] is False
        assert batch_state["continuation_state"] == "stopped"
        assert "未成功导入" in batch_state["continuation_error"]
        assert copilot_review.has_pending_copilot_batches() is False

        # Start a new auto-continue batch for Open only, then cancel before writing results.
        launched_modes.clear()
        manifest2 = copilot_review.launch_local_copilot_batch(
            db,
            workspace,
            1,
            modes=("open",),
            auto_continue=True,
            launcher=lambda c, i, t, m: launched_modes.append(m),
            run_async=False,
            delay_seconds=0.0,
        )
        assert copilot_review.cancel_copilot_batch(workspace_id, manifest2["batch_id"]) is True
        batch2_dir = Path(manifest2["task_path"]).parent
        task2_data = json.loads(Path(manifest2["task_path"]).read_text(encoding="utf-8"))
        (batch2_dir / "open-results.json").write_text(
            json.dumps({
                "mode": "open",
                "results": [{
                    "review_result_id": task2_data["runs"][0]["review_result_id"],
                    "source_hash": task2_data["runs"][0]["source_hash"],
                    "outcome": {"verdict": "qualified", "findings": []},
                }],
            }),
            encoding="utf-8",
        )
        summary2 = copilot_review.sync_pending_copilot_batches(
            db,
            workspace_id,
            launcher=lambda c, i, t, m: launched_modes.append(m),
            run_async=False,
            delay_seconds=0.0,
        )
        assert summary2["imported_rounds"] == 0
        assert summary2["continued_batches"] == 0
        assert launched_modes == ["open"]


def test_copilot_includes_all_manually_confirmed_qualified_and_supports_reoverride(
    client_and_engine, monkeypatch, tmp_path
) -> None:
    from app.services import copilot_review
    from app.services.manual_decisions import current_review, save_manual_decision
    from app.services.review_status import is_force_qualified

    client, engine = client_and_engine
    settings = get_settings()
    monkeypatch.setattr(settings, "app_role", "combined")
    monkeypatch.setattr(settings, "web_auth_enabled", False)
    monkeypatch.setattr(copilot_review, "COPILOT_RUNS_DIR", tmp_path / "copilot_runs")
    monkeypatch.setattr(copilot_review, "find_vscode_cli", lambda: "code.cmd")
    monkeypatch.setattr("app.web.find_vscode_cli", lambda: "code.cmd")

    launched: list[tuple[str, Path, Path, str]] = []
    real_launch = copilot_review.launch_local_copilot_batch
    real_sync = copilot_review.sync_pending_copilot_batches

    def sync_launcher(code_bin, instr, task, mode):
        launched.append((code_bin, instr, task, mode))

    def fake_launch(db, workspace, limit, *, modes=("skill", "open"), **kwargs):
        return real_launch(
            db,
            workspace,
            limit,
            modes=modes,
            auto_continue=kwargs.get("auto_continue", False),
            launcher=sync_launcher,
            run_async=False,
            delay_seconds=0.0,
        )

    def fake_sync(db, workspace_id=None, **kwargs):
        return real_sync(
            db,
            workspace_id,
            launcher=sync_launcher,
            run_async=False,
            delay_seconds=0.0,
            **kwargs,
        )

    monkeypatch.setattr("app.web.launch_local_copilot_batch", fake_launch)
    monkeypatch.setattr("app.web.sync_pending_copilot_batches", fake_sync)

    with Session(engine) as db:
        workspace = Workspace(name="Workspace", slug="workspace-manual-copilot")
        db.add(workspace)
        db.commit()
        workspace_id = workspace.id

    # 1. override_qualified (from unqualified)
    r_override = _prepare_run(engine, "unqualified", workspace_id=workspace_id, run_id=301)
    # 2. confirmed_qualified (from needs_manual_review)
    r_confirmed = _prepare_run(
        engine, "needs_manual_review", workspace_id=workspace_id, run_id=302
    )
    # 3. confirmed_warning_qualified (from qualified with warning)
    r_warning = _prepare_run(engine, "qualified", workspace_id=workspace_id, run_id=303)
    # 4. confirmed_unqualified (must stay excluded)
    _prepare_run(engine, "needs_manual_review", workspace_id=workspace_id, run_id=304)

    with Session(engine) as db:
        m1 = save_manual_decision(
            db,
            db.get(AlmRun, 301),
            "override_qualified",
            "op1",
            "[步骤 1, 3 · 设备追溯] CHESS-CAST-20006 是 Bay 编号，不是设备",
        )
        m1.items_json = json.dumps(
            [{
                "kind": "manual",
                "type": "equipment",
                "label": "设备追溯",
                "steps": [1, 3],
                "step_label": "步骤 1, 3",
                "ai_summary": EQUIPMENT_SUMMARY,
                "explanation": "CHESS-CAST-20006 是 Bay 编号，不是设备",
            }],
            ensure_ascii=False,
        )
        save_manual_decision(
            db,
            db.get(AlmRun, 302),
            "confirmed_qualified",
            "op2",
            "图片证据已核对与步骤一致",
        )
        save_manual_decision(
            db,
            db.get(AlmRun, 303),
            "confirmed_warning_qualified",
            "op3",
            "已在 ALM 修正拼写错误",
        )
        save_manual_decision(
            db,
            db.get(AlmRun, 304),
            "confirmed_unqualified",
            "op4",
            "确认存在真实缺陷",
        )
        db.commit()

    with TestClient(
        client.app, follow_redirects=False, client=("127.0.0.1", 50000)
    ) as local_client:
        dash = local_client.get(f"/?workspace={workspace_id}")
        assert dash.status_code == 200
        assert "当前待审：3 条" in dash.text

        started = local_client.post(
            f"/workspaces/{workspace_id}/copilot-review/launch",
            data={"limit": "10", "mode": "both", "scope": "all", "return_status": "all"},
        )
        assert started.status_code == 303
        assert len(launched) == 2

        task_data = json.loads(launched[0][2].read_text(encoding="utf-8"))
        assert [item["review_result_id"] for item in task_data["runs"]] == [
            r_override,
            r_confirmed,
            r_warning,
        ]
        # Verify manual_qualification payload and instructions are attached.
        mq_override = task_data["runs"][0]["manual_qualification"]
        assert mq_override is not None
        assert mq_override["decision"] == "override_qualified"
        assert mq_override["original_ai_verdict"] == "unqualified"
        assert "Bay 编号" in mq_override["reason"]
        assert len(mq_override["resolved_items"]) == 1
        assert task_data["runs"][1]["manual_qualification"]["decision"] == "confirmed_qualified"
        assert (
            task_data["runs"][2]["manual_qualification"]["decision"]
            == "confirmed_warning_qualified"
        )
        for _, instr_path, _, _ in launched:
            assert "manual_qualification" in instr_path.read_text(encoding="utf-8")

        batch_dir = launched[0][2].parent
        hashes = {item["review_result_id"]: item["source_hash"] for item in task_data["runs"]}

        # Open rejects run 301 (override_qualified) and 302 (confirmed_qualified),
        # and passes 303 (confirmed_warning_qualified).
        (batch_dir / "open-results.json").write_text(
            json.dumps({
                "mode": "open",
                "results": [
                    {
                        "review_result_id": r_override,
                        "source_hash": hashes[r_override],
                        "outcome": {
                            "verdict": "unqualified",
                            "findings": [{
                                "location": "Step 2",
                                "summary": "算术差值错误",
                                "evidence": "3.4 - 3.3 != 0.5",
                            }],
                        },
                    },
                    {
                        "review_result_id": r_confirmed,
                        "source_hash": hashes[r_confirmed],
                        "outcome": {
                            "verdict": "unqualified",
                            "findings": [{
                                "location": "Step 1",
                                "summary": "缺少关键读数记录",
                                "evidence": "Actual omitted temperature reading",
                            }],
                        },
                    },
                    {
                        "review_result_id": r_warning,
                        "source_hash": hashes[r_warning],
                        "outcome": {"verdict": "qualified", "findings": []},
                    },
                ],
            }),
            encoding="utf-8",
        )
        # Skill also imports cleanly on the same batch even though Open disputed 301/302.
        (batch_dir / "skill-results.json").write_text(
            json.dumps({
                "mode": "skill",
                "results": [
                    {
                        "review_result_id": r_override,
                        "source_hash": hashes[r_override],
                        "outcome": {"verdict": "qualified", "findings": []},
                    },
                    {
                        "review_result_id": r_confirmed,
                        "source_hash": hashes[r_confirmed],
                        "outcome": {"verdict": "qualified", "findings": []},
                    },
                    {
                        "review_result_id": r_warning,
                        "source_hash": hashes[r_warning],
                        "outcome": {"verdict": "qualified", "findings": []},
                    },
                ],
            }),
            encoding="utf-8",
        )
        sync_resp = local_client.get(
            f"/api/copilot-review-status?workspace={workspace_id}"
        ).json()
        assert sync_resp["sync"]["imported_rounds"] == 2
        assert sync_resp["sync"]["rejected_runs"] == 0
        assert sync_resp["sync"]["manual_review_runs"] == 2
        assert sync_resp["batch"]["continuation_state"] == "completed"
        assert sync_resp["batch"]["manual_review_runs_total"] == 2

        with Session(engine) as db:
            rev301 = current_review(db, db.get(AlmRun, 301))
            rev302 = current_review(db, db.get(AlmRun, 302))
            rev303 = current_review(db, db.get(AlmRun, 303))
            # Manually confirmed qualified runs disputed by Copilot move to Manual review first.
            assert rev301.final_status == "needs_manual_review"
            assert rev301.copilot_rejected is True
            assert rev301.copilot_disputed_manual is True
            assert is_force_qualified(rev301) is False
            assert rev302.final_status == "needs_manual_review"
            assert rev302.copilot_disputed_manual is True
            assert is_force_qualified(rev302) is False
            assert rev303.final_status == "qualified" and is_force_qualified(rev303)

        # Dashboard Manual review view shows disputed runs with the Copilot badge;
        # Unqualified view does NOT include them yet.
        mr_dash = local_client.get(
            f"/?workspace={workspace_id}&status=needs_manual_review"
        )
        assert mr_dash.status_code == 200
        assert "#301" in mr_dash.text
        assert "#302" in mr_dash.text
        assert "Copilot 质疑" in mr_dash.text
        assert "查看需人工复核" in mr_dash.text
        assert "2 条转人工复核" in mr_dash.text

        unq_before = local_client.get(
            f"/?workspace={workspace_id}&status=unqualified"
        )
        assert unq_before.status_code == 200
        assert "#301" not in unq_before.text
        assert "#302" not in unq_before.text

        # Detail page for disputed 301 offers both confirmed_qualified and confirmed_unqualified,
        # and asks only for the new Copilot finding (not the old first-pass AI findings).
        detail_disputed = local_client.get("/runs/301")
        assert detail_disputed.status_code == 200
        assert "原人工确认合格记录（待复核）" in detail_disputed.text
        assert "复核 Copilot 异议" in detail_disputed.text
        assert 'value="confirmed_qualified"' in detail_disputed.text
        assert 'value="confirmed_unqualified"' in detail_disputed.text
        assert detail_disputed.text.count('name="explanation"') == 1
        assert "Step 2: 算术差值错误" in detail_disputed.text

        # Branch A (run 301): operator rejects Copilot finding -> restores to
        # Qualified AND Manually confirmed qualified (force_qualified).
        reconfirm = local_client.post(
            "/runs/301/manual-decision",
            data={
                "decision": "confirmed_qualified",
                "review_result_id": str(r_override),
                "explanation": ["经复核 Step 2 差值为四舍五入单位换算导致，实测值满足容差"],
            },
        )
        assert reconfirm.status_code == 303
        with Session(engine) as db:
            rev301_after = current_review(db, db.get(AlmRun, 301))
            assert rev301_after.final_status == "qualified"
            assert rev301_after.copilot_rejected is False
            assert rev301_after.copilot_disputed_manual is False
            assert is_force_qualified(rev301_after) is True

        # Branch B (run 302): operator agrees with Copilot finding -> moves into Unqualified.
        agree_unq = local_client.post(
            "/runs/302/manual-decision",
            data={
                "decision": "confirmed_unqualified",
                "review_result_id": str(r_confirmed),
                "reason": "同意 Copilot 复核意见，Step 1 确实漏记温度读数",
            },
        )
        assert agree_unq.status_code == 303
        with Session(engine) as db:
            rev302_after = current_review(db, db.get(AlmRun, 302))
            assert rev302_after.final_status == "unqualified"
            assert rev302_after.copilot_rejected is False
            assert rev302_after.copilot_disputed_manual is False
            assert is_force_qualified(rev302_after) is False

        # Dashboard Manually confirmed qualified includes 301 and 303, not 302.
        fq_dash = local_client.get(
            f"/?workspace={workspace_id}&status=force_qualified"
        )
        assert fq_dash.status_code == 200
        assert "#301" in fq_dash.text
        assert "#302" not in fq_dash.text
        assert "#303" in fq_dash.text

        # Dashboard Unqualified now includes 302 (agreed by operator).
        unq_after = local_client.get(
            f"/?workspace={workspace_id}&status=unqualified"
        )
        assert unq_after.status_code == 200
        assert "#302" in unq_after.text

        # Detail page for 301 shows latest manual decision and preserves the earlier one.
        detail_restored = local_client.get("/runs/301")
        assert detail_restored.status_code == 200
        assert "更早的人工裁决（1 条）" in detail_restored.text
        assert "四舍五入单位换算" in detail_restored.text
        assert "Bay 编号" in detail_restored.text


def test_generic_answers_are_sent_back_with_the_draft(client_and_engine) -> None:
    client, engine = client_and_engine
    result_id = _prepare_run(engine)

    response = client.post(
        "/runs/42/manual-decision",
        data={
            "decision": "override_qualified",
            "review_result_id": str(result_id),
            "explanation": ["合格", "证据为 MP4 视频", ""],
        },
    )

    assert response.status_code == 422
    assert "过于笼统" in response.text
    assert "证据为 MP4 视频" in response.text
    assert 'data-manual-decision-feedback' in response.text
    assert '裁决未记录' in response.text
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
            "explanation": ["CHESS-CAST-20006 是 Bay 编号，不是设备", "证据为 MP4 视频", ""],
        },
    )

    assert response.status_code == 303
    assert "decision_feedback=1" in response.headers["location"]
    with Session(engine) as db:
        manual = db.scalar(select(ManualDecision))
        assert manual is not None
        assert manual.reason.splitlines() == [
            "[步骤 1, 3 · 设备追溯] CHESS-CAST-20006 是 Bay 编号，不是设备",
            "[步骤 2 · 截图证据] 证据为 MP4 视频",
        ]
        assert [item["type"] for item in json.loads(manual.items_json)] == [
            "equipment",
            "screenshot",
        ]

    detail = client.get(response.headers["location"])
    assert 'data-manual-decision-feedback' in detail.text
    assert '裁决已记录' in detail.text
    assert "人工裁决已记录。" in detail.text
    assert "CHESS-CAST-20006 是 Bay 编号，不是设备" in detail.text
    assert "人工核对的证据" not in detail.text
    assert "AI 原始结论（非最终裁决）：不合格" in detail.text


def test_existing_structured_decision_still_displays_legacy_fields(client_and_engine) -> None:
    client, engine = client_and_engine
    result_id = _prepare_run(engine)
    response = client.post(
        "/runs/42/manual-decision",
        data={
            "decision": "override_qualified",
            "review_result_id": str(result_id),
            "explanation": ["CHESS-CAST-20006 是 Bay 编号", "证据为 MP4 视频", ""],
        },
    )
    assert response.status_code == 303
    with Session(engine) as db:
        manual = db.scalar(select(ManualDecision))
        assert manual is not None
        items = json.loads(manual.items_json)
        items[0]["category_label"] = "参考数据问题"
        items[0]["evidence"] = "Step1 Actual 中的 Location"
        manual.items_json = json.dumps(items, ensure_ascii=False)
        db.commit()

    detail = client.get("/runs/42")
    assert "参考数据问题" in detail.text
    assert "证据：Step1 Actual 中的 Location" in detail.text


def test_stale_review_result_requires_refilling(client_and_engine) -> None:
    client, engine = client_and_engine
    result_id = _prepare_run(engine)

    response = client.post(
        "/runs/42/manual-decision",
        data={
            "decision": "override_qualified",
            "review_result_id": str(result_id + 1),
            "explanation": ["CHESS-CAST-20006 是 Bay 编号", "证据为 MP4 视频", ""],
        },
    )

    assert response.status_code == 303
    assert "decision_feedback=1" in response.headers["location"]
    assert "message_kind=error" in response.headers["location"]
    detail = client.get(response.headers["location"])
    assert 'data-manual-decision-feedback' in detail.text
    assert '裁决未记录' in detail.text
    assert "AI 评审结果已更新" in detail.text
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
    assert "decision_feedback=1" in response.headers["location"]
    with Session(engine) as db:
        manual = db.scalar(select(ManualDecision))
        assert manual is not None
        assert manual.reason == "Evidence missing"
        assert manual.items_json is None

    detail = client.get(response.headers["location"])
    assert '裁决已记录' in detail.text


def test_missing_reason_gets_failure_dialog(client_and_engine) -> None:
    client, engine = client_and_engine
    _prepare_run(engine, verdict="needs_manual_review")

    response = client.post(
        "/runs/42/manual-decision", data={"decision": "confirmed_unqualified"}
    )

    assert response.status_code == 303
    detail = client.get(response.headers["location"])
    assert '裁决未记录' in detail.text
    assert 'A reason is required.' in detail.text
    with Session(engine) as db:
        assert db.scalar(select(ManualDecision)) is None
