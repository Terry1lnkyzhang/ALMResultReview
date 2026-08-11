from copy import deepcopy

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AlmRun, AlmUser, ReviewJob, ReviewResult, RunRevision, Workspace
from app.services.alm import _latest_run
from app.services.importer import import_data, queue_stale_reviews


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
        assert job_count == 2
        assert run is not None
        assert run.current_revision_id == revisions[-1].id
        assert revisions[0].review_hash != revisions[1].review_hash
        user = db.get(AlmUser, "actual-user")
        assert user is not None
        assert user.full_name == "Actual User"


def test_import_queues_unchanged_run_when_review_policy_changed() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        import_data(sample_data(), db)
        run = db.get(AlmRun, 152711)
        job = db.scalar(select(ReviewJob).where(ReviewJob.run_id == 152711))
        assert run is not None and job is not None
        job.status = "completed"
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
        assert [item.status for item in jobs] == ["completed", "queued"]


def test_stale_review_scan_does_not_requeue_superseded_revision() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        import_data(sample_data(), db)
        job = db.scalar(select(ReviewJob).where(ReviewJob.run_id == 152711))
        assert job is not None
        job.status = "superseded"
        db.commit()

        queued = queue_stale_reviews(db)

        assert queued == 0
        jobs = db.scalars(select(ReviewJob).where(ReviewJob.run_id == 152711)).all()
        assert [item.status for item in jobs] == ["superseded"]


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
        assert db.scalar(select(func.count()).select_from(ReviewJob)) == 1


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