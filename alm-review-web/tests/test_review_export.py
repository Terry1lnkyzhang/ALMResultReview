import csv
import io

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AlmRun, AlmUser, ReviewJob, ReviewResult, RunRevision, RunStep
from app.services.review_policy import current_review_policy_key
from app.services.reviews import save_manual_decision
from app.web import export_reviews, run_index


def test_singular_run_path_redirects_to_dashboard() -> None:
    response = run_index()

    assert response.status_code == 307
    assert response.headers["location"] == "/"


def test_export_includes_ai_result_and_manual_override() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = AlmRun(
            run_id=42,
            test_id=7,
            test_name="Exported test",
            test_set_name="Regression",
            folder_path="Root / Export",
            execution_location="Bay 5",
            run_status="Passed",
            test_owner="owner1",
            actual_tester="tester1",
            source_hash="a" * 64,
            review_hash="b" * 64,
            raw_json="{}",
        )
        db.add_all(
            (
                run,
                AlmUser(code1_id="tester1", full_name="Test User"),
                AlmUser(code1_id="owner1", full_name="Test Owner"),
            )
        )
        db.flush()
        revision = RunRevision(
            run_id=run.run_id,
            revision_number=1,
            source_hash=run.source_hash,
            review_hash=run.review_hash,
            snapshot_json="{}",
        )
        db.add(revision)
        db.flush()
        run.current_revision_id = revision.id
        job = ReviewJob(run_id=run.run_id, revision_id=revision.id, status="completed")
        db.add(job)
        db.flush()
        db.add(
            ReviewResult(
                job_id=job.id,
                run_id=run.run_id,
                revision_id=revision.id,
                prompt_version_id=1,
                source_hash=run.source_hash,
                review_policy_key=current_review_policy_key(db),
                model_name="test-model",
                verdict="unqualified",
                issue_summary="AI found a mismatch",
                criteria_json='{"expected_vs_actual":{"status":"fail"}}',
            )
        )
        db.commit()
        save_manual_decision(
            db,
            run,
            "override_qualified",
            "reviewer1",
            "Checked against the source evidence",
        )

        response = export_reviews(
            status="qualified",
            tester="all",
            owner="owner1",
            query="",
            db=db,
        )
        other_owner_response = export_reviews(
            status="qualified",
            tester="all",
            owner="owner2",
            query="",
            db=db,
        )

    assert response.body.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(response.body.decode("utf-8-sig"))))
    assert len(rows) == 1
    assert rows[0]["actual_tester"] == "Test User (tester1)"
    assert rows[0]["execution_location"] == "Bay 5"
    assert rows[0]["test_owner_id"] == "owner1"
    assert rows[0]["test_owner"] == "Test Owner (owner1)"
    assert rows[0]["ai_verdict"] == "unqualified"
    assert rows[0]["final_status"] == "qualified"
    assert rows[0]["manual_decision"] == "override_qualified"
    assert rows[0]["manual_operator"] == "reviewer1"
    assert rows[0]["manual_reason"] == "Checked against the source evidence"
    other_owner_rows = list(
        csv.DictReader(
            io.StringIO(other_owner_response.body.decode("utf-8-sig"))
        )
    )
    assert other_owner_rows == []


def test_export_writes_step_text_as_plain_readable_blocks() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        run = AlmRun(
            run_id=43,
            test_id=8,
            test_name="Exported test with steps",
            test_set_name="Regression",
            folder_path="Root / Export",
            execution_location="Bay 5",
            run_status="Passed",
            source_hash="a" * 64,
            review_hash="b" * 64,
            raw_json="{}",
        )
        db.add(run)
        db.flush()
        revision = RunRevision(
            run_id=run.run_id,
            revision_number=1,
            source_hash=run.source_hash,
            review_hash=run.review_hash,
            snapshot_json="{}",
        )
        db.add(revision)
        db.flush()
        run.current_revision_id = revision.id
        db.add_all(
            (
                RunStep(
                    revision_id=revision.id,
                    step_id=1,
                    step_order=1,
                    name="Hide curve",
                    status="Passed",
                    description="<p>Hide Artery&nbsp; curve.</p>",
                    expected="<div>The curve is hidden.</div>",
                    actual="<div>Result passed.</div>",
                ),
                RunStep(
                    revision_id=revision.id,
                    step_id=2,
                    step_order=2,
                    name="Show curve",
                    status="Passed",
                    description="",
                    expected="",
                    actual="",
                ),
            )
        )
        db.commit()

        response = export_reviews(status="all", tester="all", owner="all", query="", db=db)

    rows = list(csv.DictReader(io.StringIO(response.body.decode("utf-8-sig"))))
    assert len(rows) == 1
    steps_text = rows[0]["steps"]

    assert "<p>" not in steps_text
    assert "&nbsp;" not in steps_text
    assert "\xa0" not in steps_text
    assert "Step 1 - Hide curve [Passed]" in steps_text
    assert "Description: Hide Artery  curve." in steps_text
    assert "Expected: The curve is hidden." in steps_text
    assert "Actual: Result passed." in steps_text
    assert "Step 2 - Show curve [Passed]" in steps_text
    assert "Description: -" in steps_text
