from __future__ import annotations

import base64
import binascii
import hashlib
import io
import math
import os
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath

from PIL import Image, ImageOps, UnidentifiedImageError

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
IMAGE_TRANSPORT_MAX_BYTES = 4 * 1024 * 1024
IMAGE_TRANSPORT_MAX_PIXELS = 12_000_000
IMAGE_TRANSPORT_TARGET_BYTES = 3_500_000
IMAGE_TRANSPORT_TARGET_PIXELS = 10_000_000
IMAGE_TRANSPORT_MAX_EDGE = 2048
_MAX_OPTIMIZATION_SOURCE_PIXELS = 80_000_000


@dataclass(frozen=True)
class ResolvedImage:
    relative_name: str
    media_type: str
    size_bytes: int
    sha256: str
    data_url: str
    width: int = 0
    height: int = 0
    transport_optimized: bool = False
    original_media_type: str = ""
    original_size_bytes: int = 0
    original_sha256: str = ""
    original_width: int = 0
    original_height: int = 0


@dataclass(frozen=True)
class ImageEvidenceResult:
    status: str
    images: tuple[ResolvedImage, ...] = ()
    skipped_oversized: int = 0
    skipped_invalid: int = 0
    detail: str = ""
    source_kind: str = "approved_root"


def _decoded_image(image: ResolvedImage) -> Image.Image:
    _, separator, encoded = image.data_url.partition(",")
    if not separator:
        raise ValueError("Image data URL is missing its payload.")
    content = base64.b64decode(encoded, validate=True)
    with Image.open(io.BytesIO(content)) as source:
        decoded = ImageOps.exif_transpose(source)
        decoded.load()
        return decoded.copy()


def _jpeg_compatible(image: Image.Image) -> Image.Image:
    if image.mode in {"RGB", "L"}:
        return image
    if "A" in image.getbands():
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        return background
    return image.convert("RGB")


def _encode_transport_image(
    source: Image.Image,
    image: ResolvedImage,
    size: tuple[int, int],
    *,
    quality: int,
    force_lossy: bool,
) -> ResolvedImage:
    rendered = source
    if rendered.size != size:
        rendered = rendered.resize(size, Image.Resampling.LANCZOS)
    output = io.BytesIO()
    media_type = image.media_type
    if media_type == "image/png" and not force_lossy:
        rendered.save(output, format="PNG", optimize=True, compress_level=9)
    elif media_type == "image/webp" and not force_lossy:
        rendered.save(output, format="WEBP", quality=quality, method=6)
    else:
        media_type = "image/jpeg"
        _jpeg_compatible(rendered).save(
            output,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
        )
    content = output.getvalue()
    return ResolvedImage(
        relative_name=image.relative_name,
        media_type=media_type,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        data_url=f"data:{media_type};base64,{base64.b64encode(content).decode('ascii')}",
        width=size[0],
        height=size[1],
        transport_optimized=True,
        original_media_type=image.original_media_type or image.media_type,
        original_size_bytes=image.original_size_bytes or image.size_bytes,
        original_sha256=image.original_sha256 or image.sha256,
        original_width=image.original_width or image.width,
        original_height=image.original_height or image.height,
    )


def optimize_images_for_transport(
    images: tuple[ResolvedImage, ...],
) -> tuple[ResolvedImage, ...] | None:
    total_bytes = sum(image.size_bytes for image in images)
    total_pixels = sum(image.width * image.height for image in images)
    if (
        total_bytes <= IMAGE_TRANSPORT_MAX_BYTES
        and total_pixels <= IMAGE_TRANSPORT_MAX_PIXELS
    ):
        return images
    if not images or total_pixels <= 0 or total_pixels > _MAX_OPTIMIZATION_SOURCE_PIXELS:
        return None
    try:
        decoded = tuple(_decoded_image(image) for image in images)
    except (OSError, ValueError, binascii.Error, UnidentifiedImageError):
        return None

    base_sizes = []
    for source in decoded:
        edge_scale = min(1.0, IMAGE_TRANSPORT_MAX_EDGE / max(source.size))
        base_sizes.append(
            (
                max(1, round(source.width * edge_scale)),
                max(1, round(source.height * edge_scale)),
            )
        )
    base_pixels = sum(width * height for width, height in base_sizes)
    if base_pixels > IMAGE_TRANSPORT_TARGET_PIXELS:
        pixel_scale = math.sqrt(IMAGE_TRANSPORT_TARGET_PIXELS / base_pixels)
        base_sizes = [
            (max(1, round(width * pixel_scale)), max(1, round(height * pixel_scale)))
            for width, height in base_sizes
        ]

    attempts = (
        (1.0, 85, False, False),
        (1.0, 85, False, True),
        (0.90, 80, True, True),
        (0.80, 75, True, True),
        (0.70, 70, True, True),
        (0.60, 65, True, True),
        (0.50, 60, True, True),
        (0.40, 55, True, True),
    )
    try:
        for scale, quality, force_lossy, reencode in attempts:
            optimized = tuple(
                image
                if not reencode and scale == 1.0 and source.size == size
                else _encode_transport_image(
                    source,
                    image,
                    (
                        max(1, round(size[0] * scale)),
                        max(1, round(size[1] * scale)),
                    ),
                    quality=quality,
                    force_lossy=force_lossy,
                )
                for source, image, size in zip(decoded, images, base_sizes, strict=True)
            )
            if (
                sum(image.size_bytes for image in optimized)
                <= IMAGE_TRANSPORT_TARGET_BYTES
                and sum(image.width * image.height for image in optimized)
                <= IMAGE_TRANSPORT_TARGET_PIXELS
            ):
                return optimized
    except OSError:
        return None
    return None


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

    def resolve(
        self,
        value: str,
        allowed_root: str,
        fallback_root: str = "",
    ) -> ImageEvidenceResult:
        path_status = validate_network_evidence_path(value, allowed_root)
        if path_status != "allowed":
            return ImageEvidenceResult(status=path_status)
        try:
            source = self._approved_source(value, allowed_root)
        except FileNotFoundError:
            return self._resolve_fallback(value, allowed_root, fallback_root)
        except PermissionError as exc:
            return ImageEvidenceResult(status="denied", detail=str(exc)[:300])
        except OSError as exc:
            return ImageEvidenceResult(status="unavailable", detail=str(exc)[:300])
        if source is None:
            return ImageEvidenceResult(status="outside_root")
        result = self.collect(source)
        if result.status == "missing":
            return self._resolve_fallback(value, allowed_root, fallback_root)
        return result

    def _resolve_fallback(
        self,
        value: str,
        allowed_root: str,
        fallback_root: str,
    ) -> ImageEvidenceResult:
        try:
            source = self._fallback_source(value, allowed_root, fallback_root)
        except FileNotFoundError:
            return ImageEvidenceResult(status="missing")
        except PermissionError as exc:
            return ImageEvidenceResult(status="denied", detail=str(exc)[:300])
        except OSError as exc:
            return ImageEvidenceResult(status="unavailable", detail=str(exc)[:300])
        if source is None:
            return ImageEvidenceResult(status="outside_root")
        return replace(self.collect(source), source_kind="evidence_fallback")

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
        return self._relative_source(Path(str(root)), relative_parts)

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
        return self._relative_source(Path(configured_fallback), relative_parts)

    def _relative_source(
        self,
        root: Path,
        relative_parts: tuple[str, ...],
    ) -> Path | None:
        current = root
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
