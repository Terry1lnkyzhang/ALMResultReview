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


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _BREAK_RE.sub("\n", text)
    text = unescape(_TAG_RE.sub("", text))
    text = "\n".join(_WHITESPACE_RE.sub(" ", line).strip() for line in text.splitlines())
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def _digest(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def source_hash(record: dict[str, Any]) -> str:
    return _digest(record)


def review_content(record: dict[str, Any]) -> dict[str, Any]:
    run = record.get("run") or {}
    folder = record.get("folder") or {}
    test_set = record.get("testSet") or {}
    test_instance = record.get("testInstance") or {}
    steps = run.get("steps") or []
    return {
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


def review_payload(record: dict[str, Any]) -> dict[str, Any]:
    content = review_content(record)
    raw_steps = (record.get("run") or {}).get("steps") or []
    execution_date = content["execution_date"]
    enriched_steps = []
    for review_step, (normalized, raw) in enumerate(
        zip(content["steps"], raw_steps, strict=True), start=1
    ):
        step = dict(normalized)
        step["review_step"] = review_step
        attachment_declared = bool(raw.get("attachment") or raw.get("attachmentContents"))
        step["attachment_declared"] = attachment_declared
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
    return _digest(review_content(record))