import csv
import json
from io import BytesIO, StringIO
from urllib.parse import unquote
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import PROJECT_DIR
from app.database import Base, get_db
from app.models import AlmRun, ReviewJob, ReviewResult, RunRevision, RunStep
from app.services.docx_import import parse_alm_docx
from app.services.importer import import_data
from app.services.review_operations import queue_unreviewed_runs
from app.services.review_policy import current_review_policy_key
from app.web import export_test_case_results, router

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _paragraph(text: str, style: str = "") -> str:
    properties = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{properties}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _table(rows: list[list[str]]) -> str:
    return "<w:tbl>" + "".join(
        "<w:tr>" + "".join(
            f"<w:tc><w:p><w:r><w:t>{value}</w:t></w:r></w:p></w:tc>"
            for value in row
        ) + "</w:tr>"
        for row in rows
    ) + "</w:tbl>"


def sample_docx(test_case_id: int = 51954, run_id: int = 152837) -> bytes:
    body = "".join(
        (
            _paragraph("1Test Set: 999 - Wrong TOC value12", "TOC1"),
            _paragraph("1.1Test Case: 999 - Wrong TOC case12", "TOC2"),
            _paragraph("1.1.1Test Run: 99912", "TOC3"),
            _paragraph("Test Set: 20373 - Bay5", "1"),
            _paragraph(f"Test Case: {test_case_id} - Environment Check", "2"),
            _table(
                [
                    ["Field Label", "Field Value", "Field Label", "Field Value"],
                    ["Test ID:", str(test_case_id), "Test Name:", "Environment Check"],
                    ["Designer:", "owner1(Test Owner)", "Tester:", "tester1(Tester One)"],
                    ["Execution ID:", "232439", "Status:", "Passed"],
                    ["Exec Date:", "7/31/26", "None", "None"],
                ]
            ),
            _paragraph(f"Test Run: {run_id}", "3"),
            _table(
                [
                    ["Field Label", "Field Value", "Field Label", "Field Value"],
                    ["Location:", "Bay 5", "Status:", "Passed"],
                ]
            ),
            _table(
                [
                    ["Step Name", "Description", "Expected"],
                    ["1", "Open About page", "Version is displayed"],
                ]
            ),
            _table([["Actual"], ["Version 5.0 was displayed"]]),
        )
    )
    document = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{WORD_NAMESPACE}"><w:body>{body}</w:body></w:document>'
    )
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
    return output.getvalue()


def test_parse_alm_docx_ignores_toc_and_maps_run_steps() -> None:
    data = parse_alm_docx(sample_docx(), "verification.docx")

    assert len(data["records"]) == 1
    record = data["records"][0]
    assert record["testSet"] == {
        "id": "20373",
        "name": "Bay5",
        "folderPath": "Word import",
    }
    assert record["testOwner"] == "owner1"
    assert record["run"]["id"] == "152837"
    assert record["run"]["execution-date"] == "2026-07-31"
    assert record["run"]["steps"] == [
        {
            "step-order": 1,
            "name": "1",
            "descriptionText": "Open About page",
            "expectedText": "Version is displayed",
            "actualText": "Version 5.0 was displayed",
            "status": "Passed",
        }
    ]


def test_docx_payload_uses_existing_revision_and_review_queue_flow() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    data = parse_alm_docx(sample_docx(), "verification.docx")

    with Session(engine) as db:
        first = import_data(data, db, source="docx:verification.docx")
        repeated = import_data(data, db, source="docx:verification.docx")

        run = db.scalar(select(AlmRun).where(AlmRun.alm_run_id == 152837))
        step = db.scalar(select(RunStep))
        revision_count = db.scalar(select(func.count()).select_from(RunRevision))
        job_count = db.scalar(select(func.count()).select_from(ReviewJob))

        assert (first.new_runs, repeated.unchanged_runs) == (1, 1)
        assert run is not None
        assert run.test_name == "Environment Check"
        assert run.test_set_name == "Bay5"
        assert run.execution_location == "Bay 5"
        assert run.execution_at is not None
        assert step is not None
        assert step.actual == "Version 5.0 was displayed"
        assert revision_count == 1
        assert job_count == 0


def test_word_folder_import_queues_each_new_run_once() -> None:
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
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=PROJECT_DIR / "app" / "static"), name="static")
    app.dependency_overrides[get_db] = override_get_db
    files = [
        ("documents", ("a.docx", sample_docx(51954, 152837), DOCX_MIME)),
        ("documents", ("b.docx", sample_docx(51955, 152838), DOCX_MIME)),
        ("documents", ("notes.txt", b"not a record", "text/plain")),
        ("documents", ("~$a.docx", b"lock", DOCX_MIME)),
        ("documents", ("broken.docx", b"not a zip", DOCX_MIME)),
    ]
    # Browsers may post base names only; the page sends webkitRelativePath separately.
    form = {
        "relative_paths": json.dumps(
            [
                "records/a.docx",
                "records/sub/b.docx",
                "records/notes.txt",
                "records/~$a.docx",
                "records/broken.docx",
            ]
        )
    }
    with TestClient(app, follow_redirects=False) as client:
        first = client.post("/actions/import-docx-folder", data=form, files=files)
        second = client.post("/actions/import-docx-folder", data=form, files=files)
        dashboard = client.get("/?workspace=1")

    first_message = unquote(first.headers["location"])
    second_message = unquote(second.headers["location"])
    assert first.status_code == 303
    assert "2 documents read, 1 failed, 2 other files skipped" in first_message
    assert "AI review queued for 2 Runs" in first_message
    assert "test location on" in first_message
    assert "records/broken.docx: The file is not a valid DOCX document." in first_message
    assert "AI review queued for 0 Runs (0 already reviewed, 2 already queued)" in (
        second_message
    )
    assert dashboard.status_code == 200
    assert "records/sub/b.docx" in dashboard.text
    assert 'id="documents-list"' in dashboard.text
    with Session(engine) as db:
        runs = db.scalars(select(AlmRun).order_by(AlmRun.test_id)).all()
        jobs = db.scalars(select(ReviewJob)).all()
        assert [(run.test_id, run.source_document) for run in runs] == [
            (51954, "records/a.docx"),
            (51955, "records/sub/b.docx"),
        ]
        assert sorted(job.revision_id for job in jobs) == sorted(
            run.current_revision_id for run in runs
        )
        assert {job.status for job in jobs} == {"queued"}


def test_word_folder_import_skips_runs_that_already_have_a_result() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    data = parse_alm_docx(sample_docx(), "verification.docx")
    with Session(engine) as db:
        import_data(data, db, source="docx:verification.docx")
        run = db.scalar(select(AlmRun))
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
                review_policy_key=current_review_policy_key(db),
                model_name="test-model",
                verdict="qualified",
                issue_summary="No issues",
            )
        )
        db.commit()

        result = queue_unreviewed_runs(db, run.workspace_id, {152837})
        queued_jobs = db.scalar(
            select(func.count()).select_from(ReviewJob).where(ReviewJob.status == "queued")
        )

    assert (result.queued, result.already_reviewed, result.already_active) == (0, 1, 0)
    assert queued_jobs == 0


def test_test_case_results_export_groups_by_document_then_test_case() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        import_data(
            {
                "users": [],
                "records": [
                    *parse_alm_docx(sample_docx(51953, 152836))["records"],
                    *parse_alm_docx(sample_docx(51955, 152838))["records"],
                    *parse_alm_docx(sample_docx(51954, 152837))["records"],
                ],
            },
            db,
            source="docx-folder:2 files",
            source_documents={
                152836: "records/z.docx",
                152837: "records/a.docx",
                152838: "records/a.docx",
            },
        )
        reviewed = db.scalar(select(AlmRun).where(AlmRun.test_id == 51955))
        assert reviewed is not None
        job = ReviewJob(
            workspace_id=reviewed.workspace_id,
            run_id=reviewed.run_id,
            revision_id=reviewed.current_revision_id,
            status="completed",
        )
        db.add(job)
        db.flush()
        db.add(
            ReviewResult(
                job_id=job.id,
                workspace_id=reviewed.workspace_id,
                run_id=reviewed.run_id,
                revision_id=reviewed.current_revision_id,
                prompt_version_id=1,
                source_hash=reviewed.source_hash,
                review_policy_key=current_review_policy_key(db, reviewed.workspace_id),
                model_name="test-model",
                verdict="unqualified",
                issue_summary="步骤 1：Actual does not state the version",
            )
        )
        db.commit()

        response = export_test_case_results(
            status="all",
            tester="all",
            owner="all",
            query="",
            search_steps=False,
            workspace=reviewed.workspace_id,
            db=db,
        )
        one_document = export_test_case_results(
            status="all",
            tester="all",
            owner="all",
            query="records/z.docx",
            search_steps=False,
            workspace=reviewed.workspace_id,
            db=db,
        )

    assert response.body.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(StringIO(response.body.decode("utf-8-sig"))))
    assert [
        (
            row["Source Document"],
            row["Test Case ID"],
            row["Run ID"],
            row["Final Result"],
            row["Summary"],
        )
        for row in rows
    ] == [
        ("records/a.docx", "51954", "152837", "待评审", ""),
        (
            "records/a.docx",
            "51955",
            "152838",
            "不合格",
            "步骤 1：Actual does not state the version",
        ),
        ("records/z.docx", "51953", "152836", "待评审", ""),
    ]
    assert rows[1]["AI Verdict"] == "不合格"
    assert rows[1]["ALM Result"] == "Passed"
    document_rows = list(
        csv.DictReader(StringIO(one_document.body.decode("utf-8-sig")))
    )
    assert [row["Test Case ID"] for row in document_rows] == ["51953"]