from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import PureWindowsPath
from typing import Any, Protocol
from urllib.parse import urlparse

_PATH_PATTERNS = (
    re.compile(r'["\'](?P<path>\\\\[^"\']+)["\']'),
    re.compile(r'["\'](?P<path>[A-Za-z]:\\[^"\']+)["\']'),
    re.compile(r'(?m)^[ \t]*(?P<path>\\\\[^\r\n<>"|?*]+)[ \t]*$'),
    re.compile(r'''(?m):[ \t]*(?P<path>\\\\[^\r\n<>"'|?*]+)[ \t]*$'''),
    re.compile(
        r'''(?im)\b(?:refers?|referred|refered)\s+to[ \t]+'''
        r'''(?P<path>\\\\[^\r\n<>"'|?*]+)[ \t]*$'''
    ),
    re.compile(r"file:///[^\s<>\"']+", re.IGNORECASE),
    re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE),
    re.compile(r"\\\\[^\s<>\"|?*]+"),
    re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:\\)[^\s<>\"|?*]+"),
)
_DATE_PATTERN = re.compile(
    r"\b(?P<year>20\d{2})[-/.](?P<month>0?[1-9]|1[0-2])"
    r"[-/.](?P<day>0?[1-9]|[12]\d|3[01])\b"
)
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_HTML_SUFFIXES = {".html", ".htm"}
_SCREENSHOT_TERMS = ("screenshot", "screen shot", "screen capture", "截图", "图片")
_PHANTOM_TERMS = ("phantom", "模体")
_PHANTOM_PART_NUMBER_PATTERN = re.compile(
    r"\bphantom\b.{0,80}\bpart\s*(?:number|no\.?)\s*(?:is|:)?\s*"
    r"[A-Z0-9]+(?:-[A-Z0-9]+){2,}\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReviewCapabilities:
    actual_language_review: bool = True
    expected_actual_review: bool = True
    actual_path_detection: bool = True
    actual_date_comparison: bool = True
    path_access: bool = False
    folder_scan: bool = False
    html_review: bool = False
    image_review: bool = False
    reference_lookup: bool = False

    def as_dict(self) -> dict[str, bool]:
        return asdict(self)


CAPABILITIES = ReviewCapabilities()


class ExternalEvidenceResolver(Protocol):
    def resolve(self, path: str) -> dict[str, Any]: ...


class DeferredExternalEvidenceResolver:
    def resolve(self, path: str) -> dict[str, Any]:
        return {
            "path": path,
            "status": "deferred",
            "reason": "外部文件、文件夹、HTML 和图片审核将在后续阶段实现。",
        }


def validate_network_evidence_path(value: str, allowed_network_root: str) -> str:
    if not value.startswith("\\\\"):
        return "not_unc"
    candidate = PureWindowsPath(value)
    if ".." in candidate.parts:
        return "outside_root"
    root_value = allowed_network_root.strip().rstrip("\\/")
    if not root_value or not root_value.startswith("\\\\"):
        return "root_not_configured"
    root = PureWindowsPath(root_value)
    try:
        candidate.relative_to(root)
    except ValueError:
        return "outside_root"
    return "allowed"


def _clean_path(value: str) -> str:
    return value.rstrip(".,;:)]}，。；：")


def _path_kind(value: str) -> str:
    parsed = urlparse(value)
    path_value = parsed.path if parsed.scheme else value
    suffix = PureWindowsPath(path_value).suffix.casefold()
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _HTML_SUFFIXES:
        return "html"
    if value.endswith(("\\", "/")) or not suffix:
        return "folder_or_unknown"
    return "file"


def extract_paths(value: str) -> list[dict[str, str]]:
    candidates: list[tuple[int, int, str, dict[str, str]]] = []
    for pattern in _PATH_PATTERNS:
        for match in pattern.finditer(value):
            path = _clean_path((match.groupdict().get("path") or match.group(0)).strip())
            if path:
                candidates.append(
                    (
                        match.start(),
                        match.end(),
                        path.casefold(),
                        {
                            "raw": path,
                            "kind": _path_kind(path),
                            "access_status": "deferred",
                        },
                    )
                )
    selected: list[tuple[int, int, str, dict[str, str]]] = []
    seen: set[str] = set()
    for candidate in sorted(candidates, key=lambda item: (item[1] - item[0]), reverse=True):
        start, end, key, _ = candidate
        if key in seen or any(start >= item[0] and end <= item[1] for item in selected):
            continue
        selected.append(candidate)
        seen.add(key)
    return [item[3] for item in sorted(selected, key=lambda match: match[0])]


def extract_dates(value: str, execution_date: str) -> list[dict[str, Any]]:
    dates: list[dict[str, Any]] = []
    for match in _DATE_PATTERN.finditer(value):
        try:
            normalized = date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            ).isoformat()
        except ValueError:
            continue
        dates.append(
            {
                "raw": match.group(0),
                "normalized": normalized,
                "same_day_as_execution": (
                    normalized == execution_date if execution_date else None
                ),
            }
        )
    return dates


def step_evidence_profile(
    description: str,
    expected: str,
    actual: str,
    execution_date: str,
    attachment_declared: bool,
) -> dict[str, Any]:
    combined = "\n".join((description, expected, actual)).casefold()
    reference_lookup_required = any(term in combined for term in _PHANTOM_TERMS)
    if _PHANTOM_PART_NUMBER_PATTERN.search(combined):
        reference_lookup_required = False
    return {
        "actual_paths": extract_paths(actual),
        "actual_dates": extract_dates(actual, execution_date),
        "attachment_declared": attachment_declared,
        "screenshot_review_required": attachment_declared
        or any(term in combined for term in _SCREENSHOT_TERMS),
        "reference_lookup_required": reference_lookup_required,
    }