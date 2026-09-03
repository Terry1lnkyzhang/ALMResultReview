from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PureWindowsPath

from app.services.evidence import validate_network_evidence_path

_IGNORED_TAGS = {"noscript", "script", "style", "template"}
_BLOCK_CHAR_LIMIT = 2000
_AUTOMATION_RESULT_DIRECTORIES = {
    "systemverificationautomaionresult",
    "systemverificationautomationresult",
}
_WHITESPACE_RUN_RE = re.compile(r"\s+")
_RESULT_DATA_PATTERN = re.compile(r"\bvar\s+resultData\s*=\s*")
_ACTUAL_FIELD_PATTERN = re.compile(
    r"(?ims)^actual:\s*(.*?)(?=^(?:stepName|description|expect|status|log|evidence):|\Z)"
)
_PHANTOM_CODE_PATTERN = re.compile(
    r"(?i)(?P<label>phantom\s*code|模体(?:代码|编号))\s*[:：]\s*"
    r"(?P<identifier>[A-Z0-9]+(?:-[A-Z0-9]+){2,})"
)
_SUMMARY_FIELDS = (
    "testName",
    "testProject",
    "testTester",
    "testPlace",
    "beginTime",
    "totalTime",
    "testSoftwareVersion",
    "testcaseCount",
    "testcasePass",
    "testcaseFailed",
    "testcaseManual",
    "testcaseNotRun",
    "testStepsCount",
    "testStepsPass",
    "testStepsFail",
    "testStepsManual",
    "testStepsAgentPass",
    "testStepsAgentFailed",
    "testError",
)
_RESULT_FIELDS = (
    "stepName",
    "description",
    "expect",
    "actual",
    "status",
    "log",
    "evidence",
)


@dataclass(frozen=True)
class HtmlEvidenceBlock:
    block_id: str
    text: str


@dataclass(frozen=True)
class HtmlEvidenceResult:
    status: str
    size_bytes: int = 0
    sha256: str = ""
    blocks: tuple[HtmlEvidenceBlock, ...] = ()
    detail: str = ""


def actual_phantom_codes(block: HtmlEvidenceBlock) -> tuple[tuple[str, str], ...]:
    """Return labeled phantom identifiers from structured report actual fields."""
    found: list[tuple[str, str]] = []
    for actual_match in _ACTUAL_FIELD_PATTERN.finditer(block.text):
        for code_match in _PHANTOM_CODE_PATTERN.finditer(actual_match.group(1)):
            found.append(
                (
                    " ".join(code_match.group("label").split()),
                    code_match.group("identifier").upper(),
                )
            )
    return tuple(dict.fromkeys(found))


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized_tag = tag.casefold()
        if self._ignored_depth:
            self._ignored_depth += 1
        elif normalized_tag in _IGNORED_TAGS:
            self._ignored_depth = 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        normalized = " ".join(data.split())
        if normalized:
            self.fragments.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        del tag
        if self._ignored_depth:
            self._ignored_depth -= 1


def _blocks(fragments: list[str]) -> tuple[HtmlEvidenceBlock, ...]:
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for fragment in fragments:
        remaining = fragment
        while remaining:
            available = _BLOCK_CHAR_LIMIT - current_length - (1 if current else 0)
            if available <= 0:
                chunks.append("\n".join(current))
                current = []
                current_length = 0
                available = _BLOCK_CHAR_LIMIT
            part = remaining[:available]
            current.append(part)
            current_length += len(part) + (1 if len(current) > 1 else 0)
            remaining = remaining[available:]
    if current:
        chunks.append("\n".join(current))
    return tuple(
        HtmlEvidenceBlock(block_id=f"block-{index}", text=text)
        for index, text in enumerate(chunks, start=1)
    )


def _field_text(name: str, value: object) -> str:
    if isinstance(value, (dict, list)):
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        rendered = "" if value is None else str(value)
    return f"{name}: {rendered.strip()}"


def _named_blocks(block_id: str, text: str) -> list[HtmlEvidenceBlock]:
    chunks = _blocks([text])
    if len(chunks) == 1:
        return [HtmlEvidenceBlock(block_id=block_id, text=chunks[0].text)]
    return [
        HtmlEvidenceBlock(
            block_id=f"{block_id}-part-{index}",
            text=chunk.text,
        )
        for index, chunk in enumerate(chunks, start=1)
    ]


def _result_data_blocks(document: str) -> tuple[HtmlEvidenceBlock, ...] | None:
    match = _RESULT_DATA_PATTERN.search(document)
    if match is None:
        return None
    value, _ = json.JSONDecoder().raw_decode(document, match.end())
    if not isinstance(value, dict) or not isinstance(value.get("testResult"), list):
        raise ValueError("HTML resultData must contain a testResult array.")
    blocks = _named_blocks(
        "report-summary",
        "\n".join(
            _field_text(field_name, value.get(field_name))
            for field_name in _SUMMARY_FIELDS
            if field_name in value
        ),
    )
    for index, row in enumerate(value["testResult"], start=1):
        if not isinstance(row, dict):
            raise ValueError("HTML resultData testResult items must be objects.")
        blocks.extend(
            _named_blocks(
                f"test-result-{index}",
                "\n".join(
                    _field_text(field_name, row.get(field_name))
                    for field_name in _RESULT_FIELDS
                    if field_name in row
                ),
            )
        )
    return tuple(blocks)


def _whitespace_key(value: str) -> str:
    return _WHITESPACE_RUN_RE.sub(" ", value).strip().casefold()


class HtmlEvidenceResolver:
    def __init__(self, *, max_file_bytes: int = 5 * 1024 * 1024) -> None:
        self.max_file_bytes = max(1, max_file_bytes)

    def resolve(
        self,
        value: str,
        allowed_root: str,
        fallback_root: str = "",
    ) -> HtmlEvidenceResult:
        path_status = validate_network_evidence_path(value, allowed_root)
        if path_status != "allowed":
            return HtmlEvidenceResult(status=path_status)
        try:
            source = self._approved_source(value, allowed_root)
        except FileNotFoundError:
            try:
                source = self._fallback_source(value, allowed_root, fallback_root)
            except FileNotFoundError:
                return HtmlEvidenceResult(status="missing")
        except PermissionError as exc:
            return HtmlEvidenceResult(status="denied", detail=str(exc)[:300])
        except OSError as exc:
            return HtmlEvidenceResult(status="unavailable", detail=str(exc)[:300])
        if source is None:
            return HtmlEvidenceResult(status="outside_root")
        return self.collect(source)

    def collect(self, source: Path) -> HtmlEvidenceResult:
        try:
            with source.open("rb") as report_file:
                content = report_file.read(self.max_file_bytes + 1)
        except FileNotFoundError:
            return HtmlEvidenceResult(status="missing")
        except PermissionError as exc:
            return HtmlEvidenceResult(status="denied", detail=str(exc)[:300])
        except OSError as exc:
            return HtmlEvidenceResult(status="unavailable", detail=str(exc)[:300])
        if len(content) > self.max_file_bytes:
            return HtmlEvidenceResult(status="too_large")

        document = content.decode("utf-8", errors="replace")
        parser = _VisibleTextParser()
        try:
            result_data_blocks = _result_data_blocks(document)
            parser.feed(document)
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            return HtmlEvidenceResult(status="invalid", detail=str(exc)[:300])
        blocks = result_data_blocks or _blocks(parser.fragments)
        if not blocks:
            return HtmlEvidenceResult(
                status="no_visible_text",
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        return HtmlEvidenceResult(
            status="ready",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            blocks=blocks,
        )

    def _approved_source(self, value: str, allowed_root: str) -> Path | None:
        root = PureWindowsPath(allowed_root.strip().rstrip("\\/"))
        relative_parts = PureWindowsPath(value).relative_to(root).parts
        return self._approved_relative_source(Path(str(root)), relative_parts)

    def _fallback_source(
        self,
        value: str,
        allowed_root: str,
        fallback_root: str,
    ) -> Path | None:
        configured_fallback = fallback_root.strip().rstrip("\\/")
        if not configured_fallback:
            raise FileNotFoundError(value)
        approved_root = PureWindowsPath(allowed_root.strip().rstrip("\\/"))
        relative_parts = PureWindowsPath(value).relative_to(approved_root).parts
        candidates = [relative_parts]
        if (
            relative_parts
            and relative_parts[0].casefold() in _AUTOMATION_RESULT_DIRECTORIES
        ):
            candidates.insert(0, relative_parts[1:])
        for parts in candidates:
            try:
                return self._approved_relative_source(Path(configured_fallback), parts)
            except FileNotFoundError:
                continue
        raise FileNotFoundError(value)

    def _approved_relative_source(
        self,
        root: Path,
        relative_parts: tuple[str, ...],
    ) -> Path | None:
        current = root
        for part in relative_parts:
            entry = self._match_entry(current, part)
            if entry is None:
                raise FileNotFoundError(str(current / part))
            if self._is_reparse_point(entry):
                return None
            current = Path(entry.path)
        return current

    @staticmethod
    def _match_entry(current: Path, part: str) -> os.DirEntry[str] | None:
        folded = part.casefold()
        relaxed_key = _whitespace_key(part)
        relaxed: list[os.DirEntry[str]] = []
        with os.scandir(current) as entries:
            for item in entries:
                if item.name.casefold() == folded:
                    return item
                if _whitespace_key(item.name) == relaxed_key:
                    relaxed.append(item)
        # ALM rich text stores spaces as &nbsp; and collapses repeated spaces, so the
        # quoted path can differ from the share only in whitespace. Accept that single
        # deterministic case; stay missing when more than one entry could match.
        return relaxed[0] if len(relaxed) == 1 else None

    @staticmethod
    def _is_reparse_point(entry: os.DirEntry) -> bool:
        if entry.is_symlink():
            return True
        attributes = getattr(
            entry.stat(follow_symlinks=False),
            "st_file_attributes",
            0,
        )
        return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)