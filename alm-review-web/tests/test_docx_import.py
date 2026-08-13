from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AlmRun, ReviewJob, RunRevision, RunStep
from app.services.docx_import import parse_alm_docx
from app.services.importer import import_data

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


def sample_docx() -> bytes:
    body = "".join(
        (
            _paragraph("1Test Set: 999 - Wrong TOC value12", "TOC1"),
            _paragraph("1.1Test Case: 999 - Wrong TOC case12", "TOC2"),
            _paragraph("1.1.1Test Run: 99912", "TOC3"),
            _paragraph("Test Set: 20373 - Bay5", "1"),
            _paragraph("Test Case: 51954 - Environment Check", "2"),
            _table(
                [
                    ["Field Label", "Field Value", "Field Label", "Field Value"],
                    ["Test ID:", "51954", "Test Name:", "Environment Check"],
                    ["Designer:", "owner1(Test Owner)", "Tester:", "tester1(Tester One)"],
                    ["Execution ID:", "232439", "Status:", "Passed"],
                    ["Exec Date:", "7/31/26", "None", "None"],
                ]
            ),
            _paragraph("Test Run: 152837", "3"),
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