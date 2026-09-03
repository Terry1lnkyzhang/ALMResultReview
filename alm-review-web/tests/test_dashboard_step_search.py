from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AlmRun, RunRevision, RunStep, Workspace
from app.web import _filtered_run_ids, _step_text_run_ids, templates


def _seed(db: Session) -> None:
    db.add(Workspace(id=1, name="Testing", slug="testing"))
    db.flush()
    fixtures = (
        (
            101,
            "Curve visibility",
            "<html><body><div>Hide Artery&nbsp; and Vein curves.</div></body></html>",
            "TDC of reference artery is hidden.",
            "Script running completed, result passed.",
        ),
        (
            102,
            "Unrelated scan",
            "<p>Change the tilt angle.</p>",
            "The gantry tilts.",
            "The gantry tilted.",
        ),
    )
    for run_id, test_name, description, expected, actual in fixtures:
        run = AlmRun(
            run_id=run_id,
            workspace_id=1,
            test_id=run_id,
            test_name=test_name,
            test_set_name="Common Tools",
            folder_path="Root / Common",
            execution_location="Bay 1",
            run_status="Passed",
            test_owner="owner1",
            actual_tester="tester1",
            source_hash=f"{run_id:064d}",
            review_hash=f"{run_id:064d}",
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
        db.add(
            RunStep(
                revision_id=revision.id,
                step_id=run_id * 10,
                step_order=1,
                name="Step 1",
                status="Passed",
                description=description,
                expected=expected,
                actual=actual,
            )
        )
    db.commit()


def test_step_text_search_flattens_markup_and_entities() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _seed(db)

        # The stored markup carries tags and a &nbsp;, so a plain phrase must still match.
        assert _step_text_run_ids(db, 1, False, "Hide Artery and Vein") == {101}
        assert _step_text_run_ids(db, 1, False, "reference artery is hidden") == {101}
        assert _step_text_run_ids(db, 1, False, "result passed") == {101}
        assert _step_text_run_ids(db, 1, False, "gantry tilts") == {102}
        assert _step_text_run_ids(db, 1, False, "nothing matches this") == set()
        assert _step_text_run_ids(db, 1, False, "   ") == set()


def test_step_text_search_escapes_like_wildcards() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _seed(db)

        assert _step_text_run_ids(db, 1, False, "%artery%") == set()
        assert _step_text_run_ids(db, 1, False, "_______") == set()


def test_filtered_run_ids_only_reads_step_text_when_requested() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _seed(db)

        without_steps = _filtered_run_ids(
            db, 1, False, "all", "all", "all", "gantry tilts"
        )
        with_steps = _filtered_run_ids(
            db, 1, False, "all", "all", "all", "gantry tilts", True
        )

        assert without_steps == []
        assert with_steps == [102]


def test_filtered_run_ids_searches_the_displayed_alm_run_id() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _seed(db)
        run = db.get(AlmRun, 101)
        assert run is not None
        run.alm_run_id = 154042
        db.commit()

        assert _filtered_run_ids(
            db,
            1,
            False,
            "all",
            "all",
            "all",
            "154042",
        ) == [101]


def test_dashboard_exposes_the_step_text_search_toggle() -> None:
    source, _, _ = templates.env.loader.get_source(templates.env, "dashboard.html")

    assert 'name="search_steps"' in source
    assert "search-steps-toggle" in source
    assert "search_steps={{ 1 if search_steps else 0 }}" in source
