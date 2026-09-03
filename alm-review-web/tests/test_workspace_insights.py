import json
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AlmRun, ReviewJob, ReviewResult, RunRevision, Workspace
from app.services.workspace_insights import workspace_equipment_insights
from app.web import _workspace_run_id, export_equipment_usage, templates


def _reviewed_run(
    db: Session,
    workspace_id: int,
    run_id: int,
    policy_key: str,
    steps: list[dict],
    verdict: str = "qualified",
    actual_tester: str = "tester1",
) -> None:
    run = AlmRun(
        run_id=run_id,
        workspace_id=workspace_id,
        alm_run_id=run_id + 1000,
        test_id=run_id + 2000,
        test_name=f"Test {run_id}",
        actual_tester=actual_tester,
        execution_at=datetime(2026, 8, run_id, 10, 0),
        source_hash=f"{run_id:064d}",
        review_hash=f"{run_id + 1:064d}",
        raw_json="{}",
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
    job = ReviewJob(
        workspace_id=workspace_id,
        run_id=run_id,
        revision_id=revision.id,
        status="completed",
    )
    db.add(job)
    db.flush()
    db.add(
        ReviewResult(
            workspace_id=workspace_id,
            job_id=job.id,
            run_id=run_id,
            revision_id=revision.id,
            prompt_version_id=1,
            source_hash=run.source_hash,
            review_policy_key=policy_key,
            model_name="test-model",
            verdict=verdict,
            step_results_json=json.dumps(steps),
        )
    )


def _equipment_step(
    step: int,
    matches: list[dict],
    *,
    status: str = "pass",
    code: str = "matched",
    unknown: list[str] | None = None,
    reported: list[str] | None = None,
    pending: list[str] | None = None,
) -> dict:
    return {
        "review_step": step,
        "equipment": {
            "review_step": step,
            "status": status,
            "code": code,
            "summary": "Equipment reviewed.",
            "execution_date": f"2026-08-{step:02d}",
            "matches": matches,
            "unknown_identifiers": unknown or [],
            "reported_identifiers": reported or [],
            "unrecognized_reported_identifiers": [],
            "pending_device_names": pending or [],
        },
    }


def _device(reference: str, description: str) -> dict:
    return {
        "registry_reference": reference,
        "equipment_id": reference,
        "description": description,
        "manufacturer": "Philips",
        "model_number": "M1",
        "serial_number": "S1",
        "calibration_date": "2026-01-01",
        "calibration_due_date": "2026-12-31",
        "equipment_status": "In use",
        "matched_by": ["equipment_id"],
    }


def test_workspace_equipment_insights_deduplicates_devices_and_tracks_coverage() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Earth", slug="earth")
        db.add(workspace)
        db.flush()
        _reviewed_run(
            db,
            workspace.id,
            1,
            "current-policy",
            [
                _equipment_step(1, [_device("EQ-1", "Digital thermometer")]),
                _equipment_step(
                    2,
                    [],
                    status="fail",
                    code="equipment_not_found",
                    unknown=["UNKNOWN-7"],
                ),
            ],
        )
        _reviewed_run(
            db,
            workspace.id,
            2,
            "old-policy",
            [
                _equipment_step(1, [_device("EQ-1", "Digital thermometer")]),
                _equipment_step(3, [_device("EQ-2", "Stopwatch")]),
            ],
            verdict="unqualified",
        )
        db.add(
            AlmRun(
                run_id=3,
                workspace_id=workspace.id,
                source_hash="3" * 64,
                review_hash="4" * 64,
                raw_json="{}",
            )
        )
        db.commit()

        insights = workspace_equipment_insights(db, workspace.id, "current-policy")

    assert insights["metrics"] == {
        "total_runs": 3,
        "reviewed_runs": 2,
        "analyzed_runs": 2,
        "uncovered_runs": 1,
        "stale_review_runs": 1,
        "unique_devices": 2,
        "runs_using_equipment": 2,
        "usage_records": 3,
        "unresolved_references": 1,
    }
    thermometer = next(
        item for item in insights["devices"] if item["equipment_id"] == "EQ-1"
    )
    assert thermometer["run_count"] == 2
    assert thermometer["step_count"] == 2
    assert {item["run_id"] for item in thermometer["references"]} == {1, 2}
    assert insights["unresolved"][0]["identifiers"] == ["UNKNOWN-7"]
    assert insights["unresolved"][0]["actual_tester"] == "tester1"
    assert all(item["equipment_id"] != "UNKNOWN-7" for item in insights["devices"])


def test_workspace_equipment_insights_does_not_flag_confirmed_identifiers() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Earth", slug="earth")
        db.add(workspace)
        db.flush()
        _reviewed_run(
            db,
            workspace.id,
            1,
            "policy",
            [
                _equipment_step(
                    3,
                    [_device("EQ-STOPWATCH", "Stopwatch")],
                    reported=["EQ-STOPWATCH"],
                    pending=["stopwatch"],
                )
            ],
        )
        db.commit()

        insights = workspace_equipment_insights(db, workspace.id, "policy")

    assert insights["metrics"]["unique_devices"] == 1
    assert insights["metrics"]["unresolved_references"] == 0
    assert insights["unresolved"] == []


def test_workspace_run_id_resolves_the_displayed_alm_id_with_legacy_fallback() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                AlmRun(
                    run_id=156201,
                    workspace_id=1,
                    alm_run_id=156098,
                    source_hash="1" * 64,
                    review_hash="2" * 64,
                    raw_json="{}",
                ),
                AlmRun(
                    run_id=42,
                    workspace_id=1,
                    source_hash="3" * 64,
                    review_hash="4" * 64,
                    raw_json="{}",
                ),
            ]
        )
        db.commit()

        assert _workspace_run_id(db, 1, 156098) == 156201
        assert _workspace_run_id(db, 1, 42) == 42
        assert _workspace_run_id(db, 2, 156098) is None


def test_equipment_usage_csv_has_one_row_per_run_step_reference() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Earth Formal", slug="earth-formal")
        db.add(workspace)
        db.flush()
        _reviewed_run(
            db,
            workspace.id,
            1,
            "policy",
            [_equipment_step(1, [_device("EQ-1", "Digital thermometer")])],
        )
        db.commit()

        response = export_equipment_usage(workspace=workspace.id, db=db)

    content = response.body.decode("utf-8-sig")
    assert response.headers["content-disposition"] == (
        'attachment; filename="earth-formal-equipment-usage.csv"'
    )
    assert "registry_reference,equipment_id" in content
    assert "Earth Formal,EQ-1,EQ-1,Digital thermometer" in content
    assert content.count("EQ-1") == 2


def test_workspace_insights_template_and_dashboard_link_are_exposed() -> None:
    dashboard, _, _ = templates.env.loader.get_source(
        templates.env,
        "dashboard.html",
    )
    insights, _, _ = templates.env.loader.get_source(
        templates.env,
        "workspace_insights.html",
    )

    assert "/insights/equipment?workspace={{ current_workspace.id }}" in dashboard
    assert "Workspace Insights" in insights
    assert "Confirmed equipment" in insights
    assert "Unresolved equipment references" in insights
    assert "<th>Actual tester</th>" in insights
    assert (
        'href="/workspaces/{{ current_workspace.id }}/runs/'
        '{{ reference.alm_run_id }}"'
    ) in insights
    assert (
        'href="/workspaces/{{ current_workspace.id }}/runs/{{ item.alm_run_id }}"'
        in insights
    )
    assert "/exports/equipment-usage.csv?workspace=" in insights
