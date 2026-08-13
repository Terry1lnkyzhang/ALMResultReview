from __future__ import annotations

import re
from datetime import datetime
from io import BytesIO
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WORD = f"{{{WORD_NAMESPACE}}}"
MAX_DOCX_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 150 * 1024 * 1024
MAX_DOCUMENT_XML_BYTES = 50 * 1024 * 1024

TEST_SET_PATTERN = re.compile(r"^Test Set:\s*(\d+)\s*-\s*(.+)$", re.IGNORECASE)
TEST_CASE_PATTERN = re.compile(r"^Test Case:\s*(\d+)\s*-\s*(.+)$", re.IGNORECASE)
TEST_RUN_PATTERN = re.compile(r"^Test Run:\s*(\d+)\s*$", re.IGNORECASE)


def _text(node: ElementTree.Element) -> str:
    value = "".join(part.text or "" for part in node.iter(f"{WORD}t"))
    return value.replace("\xa0", " ").strip()


def _cell_text(cell: ElementTree.Element) -> str:
    parts: list[str] = []
    for child in cell:
        if child.tag == f"{WORD}p":
            value = _text(child)
            if value:
                parts.append(value)
        elif child.tag == f"{WORD}tbl":
            for row in _table_rows(child):
                value = " | ".join(item for item in row if item)
                if value:
                    parts.append(value)
    return "\n".join(parts).strip()


def _table_rows(table: ElementTree.Element) -> list[list[str]]:
    return [
        [_cell_text(cell) for cell in row.findall(f"{WORD}tc")]
        for row in table.findall(f"{WORD}tr")
    ]


def _paragraph_style(paragraph: ElementTree.Element) -> str:
    style = paragraph.find(f"{WORD}pPr/{WORD}pStyle")
    return style.get(f"{WORD}val", "") if style is not None else ""


def _normalized_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _metadata(rows: list[list[str]]) -> dict[str, str]:
    if not rows or "field label" not in {_normalized_label(cell) for cell in rows[0]}:
        return {}
    values: dict[str, str] = {}
    for row in rows[1:]:
        for index in range(0, len(row) - 1, 2):
            label = _normalized_label(row[index])
            value = row[index + 1].strip()
            if label and value and value.casefold() != "none":
                values[label] = value
    return values


def _parse_date(value: str) -> str:
    for pattern in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(value.strip(), pattern).date().isoformat()
        except ValueError:
            continue
    return ""


def _person(value: str) -> tuple[str, str]:
    cleaned = value.strip()
    if not cleaned or cleaned.casefold() == "none":
        return "", ""
    match = re.fullmatch(r"([^()\s]+)\s*\((.*)\)", cleaned)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return cleaned, ""


def _step_order(value: str, fallback: int) -> int:
    match = re.search(r"\d+", value)
    return int(match.group()) if match else fallback


def _document_root(content: bytes) -> ElementTree.Element:
    if not content:
        raise ValueError("The Word document is empty.")
    if len(content) > MAX_DOCX_BYTES:
        raise ValueError("The Word document exceeds the 50 MB upload limit.")
    try:
        with ZipFile(BytesIO(content)) as archive:
            if sum(item.file_size for item in archive.infolist()) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("The expanded Word document is too large.")
            try:
                document_info = archive.getinfo("word/document.xml")
            except KeyError as exc:
                raise ValueError("The file is not a supported DOCX document.") from exc
            if document_info.file_size > MAX_DOCUMENT_XML_BYTES:
                raise ValueError("The Word document content is too large.")
            document_xml = archive.read(document_info)
    except BadZipFile as exc:
        raise ValueError("The file is not a valid DOCX document.") from exc
    try:
        return ElementTree.fromstring(document_xml)
    except ElementTree.ParseError as exc:
        raise ValueError("The Word document contains invalid XML.") from exc


def parse_alm_docx(content: bytes, filename: str = "ALM export.docx") -> dict[str, Any]:
    root = _document_root(content)
    body = root.find(f"{WORD}body")
    if body is None:
        raise ValueError("The Word document has no readable body.")

    users: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    current_test_set: dict[str, str] | None = None
    current_test_case: dict[str, Any] | None = None
    current_run: dict[str, Any] | None = None
    pending_step: dict[str, Any] | None = None

    def register_user(value: str) -> str:
        code1_id, full_name = _person(value)
        if code1_id:
            users[code1_id] = {
                "code1_id": code1_id,
                "full_name": full_name,
                "email": "",
                "active": True,
            }
        return code1_id

    def finish_run() -> None:
        nonlocal current_run, pending_step
        if current_run is None:
            return
        if current_test_set is None or current_test_case is None:
            raise ValueError(f"Test Run {current_run['id']} has no Test Set or Test Case.")
        metadata = {**current_test_case["metadata"], **current_run["metadata"]}
        status = metadata.get("status", "")
        execution_date = _parse_date(metadata.get("exec date", ""))
        designer = register_user(metadata.get("designer", ""))
        tester = register_user(metadata.get("tester", ""))
        test_id = current_test_case["id"]
        test_name = metadata.get("test name") or current_test_case["name"]
        steps = current_run["steps"]
        for step in steps:
            step["status"] = status
        records.append(
            {
                "folder": {"path": "Word import"},
                "testSet": {
                    "id": current_test_set["id"],
                    "name": current_test_set["name"],
                    "folderPath": "Word import",
                },
                "testInstance": {
                    "id": metadata.get("execution id", ""),
                    "test-id": test_id,
                    "name": test_name,
                    "owner": tester,
                    "actual-tester": tester,
                },
                "testOwner": designer,
                "run": {
                    "id": current_run["id"],
                    "test-id": test_id,
                    "testcycl-id": metadata.get("execution id", ""),
                    "cycle-id": current_test_set["id"],
                    "cycle-name": current_test_set["name"],
                    "status": status,
                    "test-name": test_name,
                    "owner": tester,
                    "execution-date": execution_date,
                    "last-modified": execution_date,
                    "location": metadata.get("location", ""),
                    "steps": steps,
                },
            }
        )
        current_run = None
        pending_step = None

    for node in body:
        if node.tag == f"{WORD}p":
            text = _text(node)
            if not text or _paragraph_style(node).casefold().startswith("toc"):
                continue
            test_set_match = TEST_SET_PATTERN.fullmatch(text)
            test_case_match = TEST_CASE_PATTERN.fullmatch(text)
            test_run_match = TEST_RUN_PATTERN.fullmatch(text)
            if test_set_match:
                finish_run()
                current_test_set = {
                    "id": test_set_match.group(1),
                    "name": test_set_match.group(2).strip(),
                }
                current_test_case = None
            elif test_case_match:
                finish_run()
                current_test_case = {
                    "id": test_case_match.group(1),
                    "name": test_case_match.group(2).strip(),
                    "metadata": {},
                }
            elif test_run_match:
                finish_run()
                current_run = {
                    "id": test_run_match.group(1),
                    "metadata": {},
                    "steps": [],
                }
                pending_step = None
            continue

        if node.tag != f"{WORD}tbl" or current_test_case is None:
            continue
        rows = _table_rows(node)
        table_metadata = _metadata(rows)
        if table_metadata:
            target = (
                current_run["metadata"]
                if current_run is not None
                else current_test_case["metadata"]
            )
            target.update(table_metadata)
            continue
        if current_run is None or not rows:
            continue

        header = [_normalized_label(cell) for cell in rows[0]]
        if header[:3] == ["step name", "description", "expected"]:
            for row in rows[1:]:
                if not any(row):
                    continue
                order = _step_order(row[0] if row else "", len(current_run["steps"]) + 1)
                pending_step = {
                    "step-order": order,
                    "name": row[0].strip() or f"Step {order}",
                    "descriptionText": row[1].strip() if len(row) > 1 else "",
                    "expectedText": row[2].strip() if len(row) > 2 else "",
                    "actualText": "",
                }
                current_run["steps"].append(pending_step)
        elif header and header[0] == "actual" and pending_step is not None:
            pending_step["actualText"] = "\n".join(
                cell
                for row in rows[1:]
                for cell in row
                if cell
            ).strip()

    finish_run()
    if not records:
        raise ValueError(
            "No ALM Test Runs were found. Export a Design Verification Record as DOCX."
        )
    return {"users": list(users.values()), "records": records}