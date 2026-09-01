from __future__ import annotations

import hashlib
import json
import re
from html import unescape
from typing import Any

from app.services.evidence import CAPABILITIES, step_evidence_profile

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<(?:br\s*/?|/p|/div)>\s*", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_NUMBERED_ITEM_RE = re.compile(r"(?<!\S)(?P<number>[1-9]\d?)\.(?!\d)\s*")


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
            run.get("owner") or test_instance.get("actual-tester")
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
        step["actual_format"] = actual_format_profile(
            raw.get("actualText", raw.get("actual"))
        )
        expected_items = _numbered_items(step["expected"])
        actual_items = _numbered_items(step["actual"])
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