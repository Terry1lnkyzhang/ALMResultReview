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
            "reason": "External file, folder, HTML and image review runs in a later stage.",
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
    covered: list[tuple[int, int]] = []
    for candidate in sorted(candidates, key=lambda item: (item[1] - item[0]), reverse=True):
        start, end, key, _ = candidate
        if any(start >= span[0] and end <= span[1] for span in covered):
            continue
        # Cover the span even when the text repeats, so a shorter regex cannot
        # leak a truncated prefix out of a later occurrence of the same path.
        covered.append((start, end))
        if key in seen:
            continue
        selected.append(candidate)
        seen.add(key)
    return [item[3] for item in sorted(selected, key=lambda match: match[0])]


def analyze_html_path_sequences(paths: list[dict[str, str]]) -> list[dict[str, Any]]:
    html_paths = [
        PureWindowsPath(item["raw"])
        for item in paths
        if item.get("kind") == "html"
    ]
    paths_by_location = {
        (str(path.parent).casefold(), path.stem.casefold()): path
        for path in html_paths
    }
    candidate_members: dict[tuple[str, str], dict[int, PureWindowsPath]] = {}
    for path in html_paths:
        match = re.fullmatch(r"(?P<base>.+)_(?P<number>[2-9]\d*)", path.stem)
        if match is None:
            continue
        key = (str(path.parent).casefold(), match.group("base").casefold())
        candidate_members.setdefault(key, {})[int(match.group("number"))] = path

    sequences: list[dict[str, Any]] = []
    for key, numbered_paths in candidate_members.items():
        base_path = paths_by_location.get(key)
        if base_path is None and len(numbered_paths) < 2:
            continue
        numbers = sorted(({1} if base_path is not None else set()) | numbered_paths.keys())
        expected_numbers = set(range(1, max(numbers) + 1))
        missing_numbers = sorted(expected_numbers - set(numbers))
        display_path = base_path or next(iter(numbered_paths.values()))
        base_name = (
            display_path.stem
            if base_path is not None
            else re.sub(r"_[2-9]\d*$", "", display_path.stem)
        )
        sequences.append(
            {
                "base_name": base_name,
                "numbers": numbers,
                "missing_numbers": missing_numbers,
                "status": "fail" if missing_numbers else "pass",
            }
        )
    return sorted(sequences, key=lambda item: item["base_name"].casefold())


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
    actual_paths = extract_paths(actual)
    image_paths = [path for path in actual_paths if path["kind"] == "image"]
    html_paths = [path for path in actual_paths if path["kind"] == "html"]
    candidate_paths = [
        path
        for path in actual_paths
        if path["kind"] not in {"image", "html"}
    ]
    screenshot_review_required = bool(
        attachment_declared or image_paths
    )

    triggers: list[str] = []
    actions: list[str] = []
    intents: list[str] = []
    if attachment_declared:
        triggers.append("alm_attachment_declared")
        actions.append("review_attachment")
        intents.append("image_evidence")
    if image_paths:
        triggers.extend(
            f"direct_image_path:{PureWindowsPath(path['raw']).suffix.casefold()}"
            for path in image_paths
        )
        intents.append("image_evidence")
    if html_paths:
        triggers.extend(
            f"direct_html_path:{PureWindowsPath(path['raw']).suffix.casefold()}"
            for path in html_paths
        )
        intents.append("html_report")

    if "image_evidence" in intents and candidate_paths + image_paths:
        actions.extend(("validate_path", "load_images", "send_to_visual_ai"))
    if "html_report" in intents:
        actions.extend(("validate_path", "parse_html_report"))
    if len(set(intents)) > 1:
        intent = "mixed_evidence"
        reason = "The Step carries both image evidence and HTML report signals."
    elif intents:
        intent = intents[0]
        reason = (
            "A direct image path was detected."
            if intent == "image_evidence"
            else "A direct HTML report path was detected."
        )
    else:
        intent = "none"
        reason = "No external evidence signal requiring a read was detected."

    reference_lookup_required = any(term in combined for term in _PHANTOM_TERMS)
    if _PHANTOM_PART_NUMBER_PATTERN.search(combined):
        reference_lookup_required = False
    return {
        "actual_paths": actual_paths,
        "actual_dates": extract_dates(actual, execution_date),
        "attachment_declared": attachment_declared,
        "screenshot_review_required": screenshot_review_required,
        "path_validation_required": "validate_path" in actions,
        "reference_lookup_required": reference_lookup_required,
        "routing": {
            "intent": intent,
            "triggers": list(dict.fromkeys(triggers)),
            "actions": list(dict.fromkeys(actions)),
            "decision_source": "deterministic",
            "confidence": 1.0 if intent != "pending_classification" else None,
            "reason": reason,
            "manual_required": False,
        },
    }