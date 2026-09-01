from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from app.services.evidence import validate_network_evidence_path

_IMAGE_TYPES = {
    ".jfif": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_STEP_IMAGE_PATTERN = re.compile(
    r"(?:^|[\\/_.\s-])step[\s_-]*0*(\d+)[a-z]?(?=$|[\\/_.\s-])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ResolvedImage:
    relative_name: str
    media_type: str
    size_bytes: int
    sha256: str
    data_url: str
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class ImageEvidenceResult:
    status: str
    images: tuple[ResolvedImage, ...] = ()
    skipped_oversized: int = 0
    skipped_invalid: int = 0
    detail: str = ""


def embedded_image(
    *,
    name: str,
    media_type: str,
    data_url: str,
    expected_sha256: str = "",
    max_image_bytes: int = 5 * 1024 * 1024,
) -> ResolvedImage | None:
    prefix = f"data:{media_type};base64,"
    if not data_url.startswith(prefix):
        return None
    try:
        content = base64.b64decode(data_url[len(prefix) :], validate=True)
    except (ValueError, binascii.Error):
        return None
    if not content or len(content) > max_image_bytes:
        return None
    sha256 = hashlib.sha256(content).hexdigest()
    if expected_sha256 and sha256 != expected_sha256.casefold():
        return None
    if not _has_valid_signature(content, media_type):
        return None
    width, height = _image_dimensions(content, media_type)
    return ResolvedImage(
        relative_name=name,
        media_type=media_type,
        size_bytes=len(content),
        sha256=sha256,
        data_url=data_url,
        width=width,
        height=height,
    )


class NetworkImageResolver:
    def __init__(
        self,
        *,
        max_depth: int = 2,
        max_images: int = 4,
        max_image_bytes: int = 5 * 1024 * 1024,
        max_total_bytes: int = 10 * 1024 * 1024,
        max_scanned_entries: int = 500,
        matching_step_numbers: set[int] | None = None,
        require_step_marker: bool = False,
    ) -> None:
        self.max_depth = max(0, max_depth)
        self.max_images = max(1, max_images)
        self.max_image_bytes = max(1, max_image_bytes)
        self.max_total_bytes = max(1, max_total_bytes)
        self.max_scanned_entries = max(1, max_scanned_entries)
        self.matching_step_numbers = frozenset(matching_step_numbers or ())
        self.require_step_marker = require_step_marker

    def resolve(self, value: str, allowed_root: str) -> ImageEvidenceResult:
        path_status = validate_network_evidence_path(value, allowed_root)
        if path_status != "allowed":
            return ImageEvidenceResult(status=path_status)
        try:
            source = self._approved_source(value, allowed_root)
        except FileNotFoundError:
            return ImageEvidenceResult(status="missing")
        except PermissionError as exc:
            return ImageEvidenceResult(status="denied", detail=str(exc)[:300])
        except OSError as exc:
            return ImageEvidenceResult(status="unavailable", detail=str(exc)[:300])
        if source is None:
            return ImageEvidenceResult(status="outside_root")
        return self.collect(source)

    def collect(self, source: Path) -> ImageEvidenceResult:
        try:
            try:
                with os.scandir(source):
                    pass
            except NotADirectoryError:
                candidates = iter(((source, source.name),))
            else:
                candidates = self._candidate_files(source)
        except FileNotFoundError:
            return ImageEvidenceResult(status="missing")
        except PermissionError as exc:
            return ImageEvidenceResult(status="denied", detail=str(exc)[:300])
        except OSError as exc:
            return ImageEvidenceResult(status="unavailable", detail=str(exc)[:300])

        try:
            supported_candidates = [
                (path, relative_name)
                for path, relative_name in candidates
                if path.suffix.casefold() in _IMAGE_TYPES
            ]
        except PermissionError as exc:
            return ImageEvidenceResult(status="denied", detail=str(exc)[:300])
        except OSError as exc:
            return ImageEvidenceResult(status="unavailable", detail=str(exc)[:300])

        if not supported_candidates:
            return ImageEvidenceResult(status="no_images")
        if self.matching_step_numbers:
            tagged_candidates = [
                (candidate, _image_step_numbers(candidate[1]))
                for candidate in supported_candidates
            ]
            matching_candidates = [
                candidate
                for candidate, step_numbers in tagged_candidates
                if step_numbers & self.matching_step_numbers
            ]
            if matching_candidates:
                supported_candidates = matching_candidates
            elif any(step_numbers for _, step_numbers in tagged_candidates):
                return ImageEvidenceResult(status="no_matching_images")
            elif self.require_step_marker:
                return ImageEvidenceResult(status="ambiguous_step_mapping")

        images: list[ResolvedImage] = []
        skipped_oversized = 0
        skipped_invalid = 0
        total_bytes = 0
        try:
            for path, relative_name in supported_candidates:
                if len(images) >= self.max_images:
                    break
                remaining_bytes = min(
                    self.max_image_bytes,
                    self.max_total_bytes - total_bytes,
                )
                if remaining_bytes <= 0:
                    skipped_oversized += 1
                    continue
                with path.open("rb") as image_file:
                    content = image_file.read(remaining_bytes + 1)
                if len(content) > remaining_bytes:
                    skipped_oversized += 1
                    continue
                media_type = _IMAGE_TYPES[path.suffix.casefold()]
                if not _has_valid_signature(content, media_type):
                    skipped_invalid += 1
                    continue
                encoded = base64.b64encode(content).decode("ascii")
                width, height = _image_dimensions(content, media_type)
                images.append(
                    ResolvedImage(
                        relative_name=relative_name,
                        media_type=media_type,
                        size_bytes=len(content),
                        sha256=hashlib.sha256(content).hexdigest(),
                        data_url=f"data:{media_type};base64,{encoded}",
                        width=width,
                        height=height,
                    )
                )
                total_bytes += len(content)
        except PermissionError as exc:
            return ImageEvidenceResult(status="denied", detail=str(exc)[:300])
        except OSError as exc:
            return ImageEvidenceResult(status="unavailable", detail=str(exc)[:300])

        status = "ready" if images else "no_usable_images"
        return ImageEvidenceResult(
            status=status,
            images=tuple(images),
            skipped_oversized=skipped_oversized,
            skipped_invalid=skipped_invalid,
        )

    def _approved_source(self, value: str, allowed_root: str) -> Path | None:
        root = PureWindowsPath(allowed_root.strip().rstrip("\\/"))
        relative_parts = PureWindowsPath(value).relative_to(root).parts
        current = Path(str(root))
        last_index = len(relative_parts) - 1
        for index, part in enumerate(relative_parts):
            with os.scandir(current) as entries:
                entry = next(
                    (item for item in entries if item.name.casefold() == part.casefold()),
                    None,
                )
            if entry is None and index == last_index:
                # Testers routinely paste an evidence path without the file extension,
                # so accept "...\step4" when only "...\step4.jpg" sits on the share.
                entry = self._image_by_stem(current, part)
            if entry is None:
                raise FileNotFoundError(str(current / part))
            if self._is_reparse_point(entry):
                return None
            current = Path(entry.path)
        return current

    @staticmethod
    def _image_by_stem(directory: Path, stem: str) -> os.DirEntry | None:
        target = stem.casefold()
        matches = []
        with os.scandir(directory) as entries:
            for item in entries:
                name, extension = os.path.splitext(item.name)
                if name.casefold() == target and extension.casefold() in _IMAGE_TYPES:
                    matches.append(item)
        matches.sort(key=lambda item: item.name.casefold())
        return matches[0] if matches else None

    def _candidate_files(self, root: Path):
        pending = [(root, 0)]
        scanned_entries = 0
        while pending:
            directory, depth = pending.pop(0)
            with os.scandir(directory) as directory_entries:
                entries = sorted(
                    directory_entries,
                    key=lambda entry: entry.name.casefold(),
                )
            child_directories: list[Path] = []
            for entry in entries:
                scanned_entries += 1
                if scanned_entries > self.max_scanned_entries:
                    return
                try:
                    if self._is_reparse_point(entry):
                        continue
                    entry_path = Path(entry.path)
                    if entry.is_file(follow_symlinks=False):
                        yield entry_path, str(entry_path.relative_to(root))
                    elif entry.is_dir(follow_symlinks=False) and depth < self.max_depth:
                        child_directories.append(entry_path)
                except OSError:
                    continue
            pending.extend((directory, depth + 1) for directory in child_directories)

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


def _has_valid_signature(content: bytes, media_type: str) -> bool:
    if media_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if media_type == "image/webp":
        return (
            len(content) >= 12
            and content.startswith(b"RIFF")
            and content[8:12] == b"WEBP"
        )
    return False


def _image_dimensions(content: bytes, media_type: str) -> tuple[int, int]:
    if media_type == "image/png" and len(content) >= 24:
        return (
            int.from_bytes(content[16:20], "big"),
            int.from_bytes(content[20:24], "big"),
        )
    if media_type != "image/jpeg":
        return 0, 0
    index = 2
    start_of_frame = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while index + 9 < len(content):
        if content[index] != 0xFF:
            index += 1
            continue
        marker = content[index + 1]
        index += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if index + 2 > len(content):
            break
        segment_length = int.from_bytes(content[index : index + 2], "big")
        if segment_length < 2:
            break
        if marker in start_of_frame and index + 7 <= len(content):
            return (
                int.from_bytes(content[index + 5 : index + 7], "big"),
                int.from_bytes(content[index + 3 : index + 5], "big"),
            )
        index += segment_length
    return 0, 0


def _image_step_numbers(relative_name: str) -> frozenset[int]:
    return frozenset(
        int(match.group(1))
        for match in _STEP_IMAGE_PATTERN.finditer(relative_name)
    )
