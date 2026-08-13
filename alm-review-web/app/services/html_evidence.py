from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PureWindowsPath

from app.services.evidence import validate_network_evidence_path

_RESULT_LABEL = "result (passed/failed)"


@dataclass(frozen=True)
class HtmlEvidenceResult:
    status: str
    result_count: int = 0
    non_passed_values: tuple[str, ...] = ()
    detail: str = ""


class _TableRowParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() == "tr":
            self._row = []
        elif tag.casefold() in {"td", "th"} and self._row is not None:
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in {"td", "th"} and self._cell_parts is not None:
            if self._row is not None:
                self._row.append(" ".join("".join(self._cell_parts).split()))
            self._cell_parts = None
        elif normalized_tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
            self._cell_parts = None


class HtmlEvidenceResolver:
    def __init__(self, *, max_file_bytes: int = 5 * 1024 * 1024) -> None:
        self.max_file_bytes = max(1, max_file_bytes)

    def resolve(self, value: str, allowed_root: str) -> HtmlEvidenceResult:
        path_status = validate_network_evidence_path(value, allowed_root)
        if path_status != "allowed":
            return HtmlEvidenceResult(status=path_status)
        try:
            source = self._approved_source(value, allowed_root)
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

        parser = _TableRowParser()
        try:
            parser.feed(content.decode("utf-8", errors="replace"))
        except (UnicodeError, ValueError) as exc:
            return HtmlEvidenceResult(status="invalid", detail=str(exc)[:300])

        result_values: list[str] = []
        for row in parser.rows:
            label_index = next(
                (
                    index
                    for index, value in enumerate(row)
                    if value.strip().casefold() == _RESULT_LABEL
                ),
                None,
            )
            if label_index is not None:
                result_values.extend(row[label_index + 1 :])
        if not result_values:
            return HtmlEvidenceResult(status="result_row_missing")
        non_passed_values = tuple(
            value or "<empty>"
            for value in result_values
            if value.strip().casefold() != "passed"
        )
        return HtmlEvidenceResult(
            status="fail" if non_passed_values else "pass",
            result_count=len(result_values),
            non_passed_values=non_passed_values,
        )

    def _approved_source(self, value: str, allowed_root: str) -> Path | None:
        root = PureWindowsPath(allowed_root.strip().rstrip("\\/"))
        relative_parts = PureWindowsPath(value).relative_to(root).parts
        current = Path(str(root))
        for part in relative_parts:
            with os.scandir(current) as entries:
                entry = next(
                    (item for item in entries if item.name.casefold() == part.casefold()),
                    None,
                )
                if entry is None:
                    raise FileNotFoundError(str(current / part))
                if self._is_reparse_point(entry):
                    return None
                current = Path(entry.path)
        return current

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