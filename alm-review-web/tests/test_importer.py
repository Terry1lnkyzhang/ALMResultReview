from copy import deepcopy

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AlmRun,
    AlmUser,
    ReviewJob,
    ReviewResult,
    RunRevision,
    RunStep,
    SyncHistory,
    Workspace,
)
from app.services.alm import _latest_run
from app.services.importer import (
    import_data,
    queue_latest_alm_changes,
    unchanged_run_check,
)


def sample_data() -> dict:
    return {
        "users": [
            {
                "code1_id": "actual-user",
                "full_name": "Actual User",
                "email": "actual@example.com",
                "active": True,
            }
        ],
        "records": [
            {
                "folder": {"id": "5174", "path": "Testing / Common"},
                "testSet": {"id": "20129", "name": "Cardiac Scan"},
                "testInstance": {
                    "id": "231016",
                    "test-id": "33962",
                    "owner": "assigned-user",
                    "actual-tester": "actual-user",
                },
                "run": {
                    "id": "152711",
                    "test-id": "33962",
                    "testcycl-id": "231016",
                    "status": "Passed",
                    "test-name": "Cardiac scan",
                    "owner": "actual-user",
                    "execution-date": "2026-07-30",
                    "execution-time": "04:40:00",
                    "last-modified": "2026-07-30 04:40:00",
                    "steps": [
                        {
                            "id": "541021",
                            "step-order": "1",
                            "name": "Step 1",
                            "status": "Passed",
                            "descriptionText": "Start scan",
                            "expectedText": "Scan succeeds",
                            "actualText": "Scan succeeded",
                        }
                    ],
                },
            }
        ]
    }


def test_import_creates_revision_only_when_source_changes() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        first = import_data(sample_data(), db)
        unchanged = import_data(sample_data(), db)
        changed_data = deepcopy(sample_data())
        changed_data["records"][0]["run"]["steps"][0]["actualText"] += ","
        changed = import_data(changed_data, db)

        run = db.get(AlmRun, 152711)
        revisions = db.scalars(
            select(RunRevision).where(RunRevision.run_id == 152711).order_by(RunRevision.id)
        ).all()
        job_count = db.scalar(select(func.count()).select_from(ReviewJob))

        assert (first.new_runs, unchanged.unchanged_runs, changed.changed_runs) == (1, 1, 1)
        assert len(revisions) == 2
        assert job_count == 0
        assert run is not None
        assert run.current_revision_id == revisions[-1].id
        assert revisions[0].review_hash != revisions[1].review_hash
        user = db.get(AlmUser, "actual-user")
        assert user is not None
        assert user.full_name == "Actual User"


def test_import_persists_location_and_creates_revision_when_it_changes() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    first_data = sample_data()
    first_data["records"][0]["run"]["location"] = "Bay 5"
    changed_data = deepcopy(first_data)
    changed_data["records"][0]["run"]["location"] = "Bay 6"

    with Session(engine) as db:
        import_data(first_data, db)
        result = import_data(changed_data, db)

        run = db.get(AlmRun, 152711)
        revisions = db.scalars(
            select(RunRevision).where(RunRevision.run_id == 152711)
        ).all()

        assert result.changed_runs == 1
        assert run is not None
        assert run.execution_location == "Bay 6"
        assert len(revisions) == 2
        assert revisions[0].review_hash != revisions[1].review_hash


def test_unchanged_run_check_only_skips_runs_with_the_stored_last_modified() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        import_data(sample_data(), db)
        run = db.get(AlmRun, 152711)
        assert run is not None
        is_unchanged = unchanged_run_check(db, run.workspace_id)

        assert is_unchanged({"id": "152711", "last-modified": "2026-07-30 04:40:00"})
        assert not is_unchanged(
            {"id": "152711", "last-modified": "2026-08-19 09:00:00"}
        )
        assert not is_unchanged({"id": "999999", "last-modified": "2026-07-30 04:40:00"})
        assert not is_unchanged({"id": "152711"})


def test_import_preserves_step_rich_text_for_display() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    data = sample_data()
    data["records"][0]["run"]["steps"][0]["expectedText"] = (
        "<table><tr><th>Item</th><td>Value</td></tr></table>"
    )

    with Session(engine) as db:
        import_data(data, db)

        step = db.scalar(select(RunStep))

        assert step is not None
        assert "<table>" in step.expected


def test_import_does_not_queue_unchanged_run_when_review_policy_changed() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        import_data(sample_data(), db)
        run = db.get(AlmRun, 152711)
        assert run is not None
        job = ReviewJob(
            run_id=run.run_id,
            revision_id=run.current_revision_id,
            status="completed",
        )
        db.add(job)
        db.flush()
        db.add(
            ReviewResult(
                job_id=job.id,
                run_id=run.run_id,
                revision_id=run.current_revision_id,
                prompt_version_id=1,
                source_hash=run.source_hash,
                review_policy_key="outdated",
                model_name="test-model",
                verdict="qualified",
            )
        )
        db.commit()

        result = import_data(sample_data(), db)
        jobs = db.scalars(
            select(ReviewJob).where(ReviewJob.run_id == 152711).order_by(ReviewJob.id)
        ).all()

        assert result.unchanged_runs == 1
    assert [item.status for item in jobs] == ["completed"]


def test_latest_run_only_considers_passed_results() -> None:
    runs = [
        {
            "id": "10",
            "status": "Passed",
            "execution-date": "2026-07-29",
            "execution-time": "10:00:00",
        },
        {
            "id": "11",
            "status": "Not Completed",
            "execution-date": "2026-07-30",
            "execution-time": "10:00:00",
        },
    ]

    assert _latest_run(runs) == runs[0]
    assert _latest_run(runs[1:]) is None


def test_import_ignores_results_that_are_not_passed() -> None:
    data = sample_data()
    not_completed = deepcopy(data["records"][0])
    not_completed["run"]["id"] = "152712"
    not_completed["run"]["status"] = "Not Completed"
    data["records"].append(not_completed)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        result = import_data(data, db)

        assert result.discovered_runs == 1
        assert db.get(AlmRun, 152711) is not None
        assert db.get(AlmRun, 152712) is None
        assert db.scalar(select(func.count()).select_from(ReviewJob)) == 0


def test_same_alm_run_id_is_isolated_between_workspaces() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        first_workspace = Workspace(name="Project A", slug="project-a")
        second_workspace = Workspace(name="Project B", slug="project-b")
        db.add_all((first_workspace, second_workspace))
        db.flush()

        import_data(sample_data(), db, workspace_id=first_workspace.id)
        import_data(sample_data(), db, workspace_id=second_workspace.id)
        runs = db.scalars(select(AlmRun).order_by(AlmRun.workspace_id)).all()
        for run in runs:
            db.add(
                ReviewJob(
                    workspace_id=run.workspace_id,
                    run_id=run.run_id,
                    revision_id=run.current_revision_id,
                    status="queued",
                )
            )
        db.commit()

        jobs = db.scalars(select(ReviewJob).order_by(ReviewJob.workspace_id)).all()

        assert len(runs) == 2
        assert {run.workspace_id for run in runs} == {
            first_workspace.id,
            second_workspace.id,
        }
        assert {run.alm_run_id for run in runs} == {152711}
        assert len({run.run_id for run in runs}) == 2
        assert [job.workspace_id for job in jobs] == [
            first_workspace.id,
            second_workspace.id,
        ]


def test_queue_latest_alm_changes_only_queues_unreviewed_latest_sync_revisions() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Project A", slug="project-a")
        db.add(workspace)
        db.flush()
        import_data(
            sample_data(),
            db,
            source="alm:folder:42",
            workspace_id=workspace.id,
        )
        first = queue_latest_alm_changes(db, workspace.id)
        first_job = db.scalar(select(ReviewJob))
        assert first_job is not None
        first_job.status = "completed"
        db.add(
            ReviewResult(
                workspace_id=workspace.id,
                job_id=first_job.id,
                run_id=first_job.run_id,
                revision_id=first_job.revision_id,
                prompt_version_id=1,
                source_hash="a" * 64,
                review_policy_key="old-policy",
                model_name="test-model",
                verdict="qualified",
            )
        )
        db.commit()

        changed_data = deepcopy(sample_data())
        changed_data["records"][0]["run"]["steps"][0]["actualText"] += " updated"
        import_data(
            changed_data,
            db,
            source="alm:folder:42",
            workspace_id=workspace.id,
        )
        second = queue_latest_alm_changes(db, workspace.id)
        duplicate = queue_latest_alm_changes(db, workspace.id)

        assert (first.changed, first.queued) == (1, 1)
        assert (second.changed, second.queued, second.already_reviewed) == (1, 1, 0)
        assert (duplicate.changed, duplicate.queued, duplicate.already_active) == (1, 0, 1)
        assert db.scalar(select(func.count()).select_from(ReviewJob)) == 2


def test_queue_latest_alm_changes_does_not_queue_unchanged_current_revisions() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        workspace = Workspace(name="Project A", slug="project-a")
        db.add(workspace)
        db.flush()
        import_data(
            sample_data(),
            db,
            source="alm:folder:42",
            workspace_id=workspace.id,
        )
        queue_latest_alm_changes(db, workspace.id)
        job = db.scalar(select(ReviewJob))
        assert job is not None
        job.status = "completed"
        db.add(
            ReviewResult(
                workspace_id=workspace.id,
                job_id=job.id,
                run_id=job.run_id,
                revision_id=job.revision_id,
                prompt_version_id=1,
                source_hash="a" * 64,
                review_policy_key="old-policy",
                model_name="test-model",
                verdict="qualified",
            )
        )
        db.commit()
        import_data(
            sample_data(),
            db,
            source="alm:folder:42",
            workspace_id=workspace.id,
        )
        # Second-resolution timestamps let an earlier revision fall into this window.
        latest_sync = db.scalars(
            select(SyncHistory).order_by(SyncHistory.id.desc()).limit(1)
        ).one()
        latest_sync.started_at = db.scalar(select(func.min(RunRevision.created_at)))
        db.commit()

        result = queue_latest_alm_changes(db, workspace.id)

        assert (result.changed, result.queued) == (0, 0)
        assert db.scalar(select(func.count()).select_from(ReviewJob)) == 1