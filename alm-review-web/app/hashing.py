from __future__ import annotations

import hashlib
import json
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any

from app.services.evidence import CAPABILITIES, step_evidence_profile

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<(?:br\s*/?|/p|/div)>\s*", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_NUMBERED_ITEM_RE = re.compile(r"(?<!\S)(?P<number>[1-9]\d?)\.(?!\d)\s*")


class _TableAwareTextParser(HTMLParser):
    """Render HTML `<table>` grids as Markdown pipe tables while stripping other tags."""

    _BLOCK_END_TAGS = frozenset(
        {"p", "div", "li", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "hr"}
    )
    _IGNORED_TAGS = frozenset({"script", "style", "template", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._ignored_depth = 0
        self._table_stack: list[list[list[str]]] = []
        self._cell_stack: list[tuple[list[str], int]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        normalized_tag = tag.casefold()
        if self._ignored_depth:
            self._ignored_depth += 1
            return
        if normalized_tag in self._IGNORED_TAGS:
            self._ignored_depth = 1
            return
        if normalized_tag == "table":
            self._table_stack.append([])
        elif normalized_tag == "tr" and self._table_stack:
            self._table_stack[-1].append([])
        elif normalized_tag in {"td", "th"} and self._table_stack:
            if not self._table_stack[-1]:
                self._table_stack[-1].append([])
            attr_map = {key.casefold(): (val or "") for key, val in attrs}
            raw_colspan = attr_map.get("colspan", "1")
            colspan = (
                int(raw_colspan)
                if raw_colspan.isdigit() and 1 <= int(raw_colspan) <= 20
                else 1
            )
            self._cell_stack.append(([], colspan))
        elif normalized_tag == "br":
            if self._cell_stack:
                self._cell_stack[-1][0].append(" / ")
            else:
                self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if self._ignored_depth:
            self._ignored_depth -= 1
            return
        if normalized_tag in {"td", "th"} and self._cell_stack and self._table_stack:
            buf, colspan = self._cell_stack.pop()
            cell_text = " ".join("".join(buf).split()).replace("|", "\\|")
            self._table_stack[-1][-1].append(cell_text)
            for _ in range(colspan - 1):
                self._table_stack[-1][-1].append("")
        elif normalized_tag == "table" and self._table_stack:
            raw_rows = self._table_stack.pop()
            rows = [
                row
                for row in raw_rows
                if any(cell != "" for cell in row) or len(row) > 1
            ]
            if rows:
                col_count = max(len(row) for row in rows)
                padded = [row + [""] * (col_count - len(row)) for row in rows]
                lines = [
                    "| " + " | ".join(padded[0]) + " |",
                    "| " + " | ".join(["---"] * col_count) + " |",
                    *("| " + " | ".join(row) + " |" for row in padded[1:]),
                ]
                rendered = "\n" + "\n".join(lines) + "\n"
                if self._cell_stack:
                    self._cell_stack[-1][0].append(" " + " ; ".join(lines) + " ")
                else:
                    self.out.append(rendered)
        elif normalized_tag in self._BLOCK_END_TAGS:
            if self._cell_stack:
                self._cell_stack[-1][0].append(" ")
            else:
                self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._cell_stack:
            self._cell_stack[-1][0].append(data)
        else:
            self.out.append(data)


def normalize_table_text(value: Any) -> str:
    """Normalize ALM HTML text while preserving `<table>` 2D row/column alignment."""
    if value is None:
        return ""
    raw = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if "<table" not in raw.casefold():
        return normalize_text(raw)
    parser = _TableAwareTextParser()
    parser.feed(raw)
    parser.close()
    rendered = "".join(parser.out)
    cleaned_lines = [
        line.strip()
        if line.lstrip().startswith("|")
        else _WHITESPACE_RE.sub(" ", line).strip()
        for line in rendered.splitlines()
    ]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(cleaned_lines)).strip()


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _BREAK_RE.sub("\n", text)
    text = unescape(_TAG_RE.sub("", text))
    text = "\n".join(_WHITESPACE_RE.sub(" ", line).strip() for line in text.splitlines())
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def actual_format_profile(value: Any) -> dict[str, Any]:
    if value is None:
        return {"layout_text": "", "signals": []}
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _BREAK_RE.sub("\n", text)
    text = unescape(_TAG_RE.sub("", text)).strip("\n")
    lines = text.splitlines()
    signals: list[dict[str, Any]] = []
    repeated_space_widths = [
        len(match.group())
        for line in lines
        for match in re.finditer(r"[ \t]{2,}", line.strip(" \t"))
    ]
    leading_whitespace = [
        (index, len(line) - len(line.lstrip()))
        for index, line in enumerate(lines, start=1)
        if line and line[0].isspace()
    ]
    blank_line_runs: list[int] = []
    consecutive_blank_lines = 0
    for line in lines:
        if not line.strip():
            consecutive_blank_lines += 1
        else:
            if consecutive_blank_lines >= 2:
                blank_line_runs.append(consecutive_blank_lines)
            consecutive_blank_lines = 0
    if consecutive_blank_lines >= 2:
        blank_line_runs.append(consecutive_blank_lines)

    if len(repeated_space_widths) >= 3 or any(
        width >= 4 for width in repeated_space_widths
    ):
        signals.append(
            {
                "type": "repeated_spaces",
                "count": len(repeated_space_widths),
                "max_width": max(repeated_space_widths),
            }
        )
    if len(leading_whitespace) >= 3 and len(
        {width for _, width in leading_whitespace}
    ) > 1:
        signals.append(
            {
                "type": "inconsistent_leading_whitespace",
                "lines": [index for index, _ in leading_whitespace[:20]],
            }
        )
    if blank_line_runs:
        signals.append(
            {
                "type": "blank_line_runs",
                "count": len(blank_line_runs),
                "max_consecutive": max(blank_line_runs),
            }
        )
    return {"layout_text": text, "signals": signals}


def _numbered_items(value: str) -> dict[int, str]:
    matches = list(_NUMBERED_ITEM_RE.finditer(value))
    numbers = [int(match.group("number")) for match in matches]
    if len(numbers) < 2 or len(numbers) != len(set(numbers)):
        return {}
    return {
        number: value[
            match.end() : matches[index + 1].start()
            if index + 1 < len(matches)
            else len(value)
        ].strip()
        for index, (number, match) in enumerate(zip(numbers, matches, strict=True))
    }


def _digest(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _content_addressed_attachments(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                value.get("sha256", "")
                if key == "data_url"
                else _content_addressed_attachments(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_content_addressed_attachments(item) for item in value]
    return value


def source_hash(record: dict[str, Any]) -> str:
    return _digest(_content_addressed_attachments(record))


def review_content(record: dict[str, Any]) -> dict[str, Any]:
    run = record.get("run") or {}
    folder = record.get("folder") or {}
    test_set = record.get("testSet") or {}
    test_instance = record.get("testInstance") or {}
    steps = run.get("steps") or []
    content = {
        "run_id": normalize_text(run.get("id")),
        "test_id": normalize_text(run.get("test-id") or test_instance.get("test-id")),
        "test_instance_id": normalize_text(
            run.get("testcycl-id") or test_instance.get("id")
        ),
        "run_status": normalize_text(run.get("status")),
        "run_type": normalize_text(run.get("subtype-id")),
        "test_name": normalize_text(run.get("test-name")),
        "test_description": normalize_text(run.get("test-description")),
        "folder_path": normalize_text(folder.get("path") or test_set.get("folderPath")),
        "test_set_name": normalize_text(test_set.get("name") or run.get("cycle-name")),
        "execution_date": normalize_text(run.get("execution-date")),
        "execution_time": normalize_text(run.get("execution-time")),
        "result_last_modified": normalize_text(run.get("last-modified")),
        "duration_seconds": normalize_text(run.get("duration")),
        "assigned_tester": normalize_text(test_instance.get("owner")),
        "actual_tester": normalize_text(
            test_instance.get("actual-tester") or run.get("owner")
        ),
        "comments": normalize_text(run.get("comments")),
        "steps": [
            {
                "order": normalize_text(step.get("step-order")),
                "name": normalize_text(step.get("name")),
                "status": normalize_text(step.get("status")),
                "description": normalize_text(
                    step.get("descriptionText", step.get("description"))
                ),
                "expected": normalize_text(step.get("expectedText", step.get("expected"))),
                "actual": normalize_text(step.get("actualText", step.get("actual"))),
                "execution_date": normalize_text(step.get("execution-date")),
                "execution_time": normalize_text(step.get("execution-time")),
                "attachment_declared": bool(step.get("attachment")),
                "attachment_content_available": bool(step.get("attachmentContents")),
            }
            for step in steps
        ],
    }
    execution_location = normalize_text(run.get("location"))
    if execution_location:
        content["execution_location"] = execution_location
    return content


def review_payload(
    record: dict[str, Any],
) -> dict[str, Any]:
    content = review_content(record)
    raw_steps = (record.get("run") or {}).get("steps") or []
    execution_date = content["execution_date"]
    enriched_steps = []
    for review_step, (normalized, raw) in enumerate(
        zip(content["steps"], raw_steps, strict=True), start=1
    ):
        step = dict(normalized)
        step["review_step"] = review_step
        step["description"] = normalize_table_text(
            raw.get("descriptionText", raw.get("description"))
        )
        step["expected"] = normalize_table_text(
            raw.get("expectedText", raw.get("expected"))
        )
        step["actual"] = normalize_table_text(
            raw.get("actualText", raw.get("actual"))
        )
        step["actual_format"] = actual_format_profile(
            raw.get("actualText", raw.get("actual"))
        )
        expected_items = _numbered_items(normalized["expected"])
        actual_items = _numbered_items(normalized["actual"])
        if expected_items or actual_items:
            step["numbered_comparison"] = [
                {
                    "number": number,
                    "expected": expected_items.get(number, ""),
                    "actual": actual_items.get(number, ""),
                }
                for number in sorted(expected_items.keys() | actual_items.keys())
            ]
        attachment_declared = bool(raw.get("attachment") or raw.get("attachmentContents"))
        step["attachment_declared"] = attachment_declared
        step["attachment_contents"] = list(raw.get("attachmentContents") or [])
        step["evidence_profile"] = step_evidence_profile(
            step["description"],
            step["expected"],
            step["actual"],
            execution_date,
            attachment_declared,
        )
        enriched_steps.append(step)
    return {
        **content,
        "review_capabilities": CAPABILITIES.as_dict(),
        "external_evidence_phase": "deferred",
        "steps": enriched_steps,
    }


def review_hash(record: dict[str, Any]) -> str:
    content = review_content(record)
    raw_steps = (record.get("run") or {}).get("steps") or []
    for normalized, raw in zip(content["steps"], raw_steps, strict=True):
        attachments = [
            {
                "attachment_id": str(item.get("attachment_id") or ""),
                "name": str(item.get("name") or ""),
                "mime_type": str(item.get("mime_type") or ""),
                "size_bytes": int(item.get("size_bytes") or 0),
                "sha256": str(item.get("sha256") or ""),
            }
            for item in (raw.get("attachmentContents") or [])
        ]
        if attachments:
            normalized["attachments"] = attachments
    return _digest(content)