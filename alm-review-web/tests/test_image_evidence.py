from pathlib import Path

import app.services.image_evidence as image_evidence
from app.models import AiConfig, EvidenceConfig
from app.services.image_evidence import (
    NetworkImageResolver,
    image_transport_allowed,
)
from app.services.reviews import _prepare_image_evidence

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"test-png"
JPEG_BYTES = b"\xff\xd8\xff" + b"test-jpeg"
WEBP_BYTES = b"RIFF\x04\x00\x00\x00WEBP" + b"test-webp"


def write_image(path: Path, content: bytes = PNG_BYTES) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_collects_supported_images_with_depth_and_count_limits(tmp_path: Path) -> None:
    write_image(tmp_path / "01.png")
    write_image(tmp_path / "level1" / "02.jpg", JPEG_BYTES)
    write_image(tmp_path / "level1" / "level2" / "03.webp", WEBP_BYTES)
    write_image(tmp_path / "level1" / "level2" / "level3" / "ignored.png")
    write_image(tmp_path / "04.png")
    write_image(tmp_path / "05.png")

    result = NetworkImageResolver(max_depth=2, max_images=4).collect(tmp_path)

    assert result.status == "ready"
    assert len(result.images) == 4
    assert {image.media_type for image in result.images} <= {
        "image/png",
        "image/jpeg",
        "image/webp",
    }
    assert all(
        image.data_url.startswith(f"data:{image.media_type};base64,")
        for image in result.images
    )
    assert all(len(image.sha256) == 64 for image in result.images)
    assert not any("level3" in image.relative_name for image in result.images)


def test_rejects_missing_empty_oversized_and_invalid_images(tmp_path: Path) -> None:
    resolver = NetworkImageResolver(max_image_bytes=16)

    assert resolver.collect(tmp_path / "missing").status == "missing"
    assert resolver.collect(tmp_path).status == "no_images"

    write_image(tmp_path / "oversized.png", PNG_BYTES + b"x" * 32)
    write_image(tmp_path / "invalid.png", b"not an image")

    result = resolver.collect(tmp_path)

    assert result.status == "no_usable_images"
    assert result.skipped_oversized == 1
    assert result.skipped_invalid == 1


def test_total_image_budget_limits_request_size(tmp_path: Path) -> None:
    write_image(tmp_path / "01.png")
    write_image(tmp_path / "02.png")
    resolver = NetworkImageResolver(
        max_image_bytes=1024,
        max_total_bytes=len(PNG_BYTES) + 1,
    )

    result = resolver.collect(tmp_path)

    assert result.status == "ready"
    assert [image.relative_name for image in result.images] == ["01.png"]
    assert result.skipped_oversized == 1


def test_shared_directory_images_are_matched_to_their_review_step(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tagged = tmp_path / "tagged"
    write_image(tagged / "Step1-1.png")
    write_image(tagged / "Step1-2.png")
    write_image(tagged / "Step2-1.png")
    write_image(tagged / "Step2-2.png")
    monkeypatch.setattr(
        image_evidence,
        "validate_network_evidence_path",
        lambda value, allowed_root: "allowed",
    )
    content = {
        "steps": [
            {
                "review_step": review_step,
                "order": str(review_step),
                "evidence_profile": {
                    "screenshot_review_required": True,
                    "actual_paths": [{"raw": str(tagged)}],
                },
            }
            for review_step in (1, 2)
        ]
    }

    prepared = _prepare_image_evidence(
        content,
        EvidenceConfig(
            allowed_network_root=str(tmp_path),
            network_evidence_enabled=True,
            image_review_enabled=True,
        ),
        AiConfig(base_url="https://ai.example/v1"),
    )

    assert [
        image.relative_name
        for image in prepared.results[1][str(tagged)].images
    ] == ["Step1-1.png", "Step1-2.png"]
    assert [
        image.relative_name
        for image in prepared.results[2][str(tagged)].images
    ] == ["Step2-1.png", "Step2-2.png"]


def test_alm_step_order_takes_priority_over_internal_review_step(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tagged = tmp_path / "tagged"
    write_image(tagged / "Step1.png")
    write_image(tagged / "Step3.png")
    monkeypatch.setattr(
        image_evidence,
        "validate_network_evidence_path",
        lambda value, allowed_root: "allowed",
    )
    content = {
        "steps": [
            {
                "review_step": 1,
                "order": "3",
                "evidence_profile": {
                    "screenshot_review_required": True,
                    "actual_paths": [{"raw": str(tagged)}],
                },
            }
        ]
    }

    prepared = _prepare_image_evidence(
        content,
        EvidenceConfig(
            allowed_network_root=str(tmp_path),
            network_evidence_enabled=True,
            image_review_enabled=True,
        ),
        AiConfig(base_url="https://ai.example/v1"),
    )

    assert [
        image.relative_name for image in prepared.results[1][str(tagged)].images
    ] == ["Step3.png"]


def test_shared_directory_without_step_markers_is_ambiguous(
    tmp_path: Path,
    monkeypatch,
) -> None:
    untagged = tmp_path / "untagged"
    write_image(untagged / "evidence.png")
    monkeypatch.setattr(
        image_evidence,
        "validate_network_evidence_path",
        lambda value, allowed_root: "allowed",
    )
    content = {
        "steps": [
            {
                "review_step": review_step,
                "order": str(review_step),
                "evidence_profile": {
                    "screenshot_review_required": True,
                    "actual_paths": [{"raw": str(untagged)}],
                },
            }
            for review_step in (1, 2)
        ]
    }

    prepared = _prepare_image_evidence(
        content,
        EvidenceConfig(
            allowed_network_root=str(tmp_path),
            network_evidence_enabled=True,
            image_review_enabled=True,
        ),
        AiConfig(base_url="https://ai.example/v1"),
    )

    assert prepared.results[1][str(untagged)].status == "ambiguous_step_mapping"
    assert prepared.results[2][str(untagged)].status == "ambiguous_step_mapping"


def test_image_transport_requires_https_localhost_or_explicit_approval() -> None:
    assert image_transport_allowed("https://ai.example/v1", allow_insecure=False)
    assert image_transport_allowed("http://127.0.0.1:6000/v1", allow_insecure=False)
    assert not image_transport_allowed("http://161.92.92.153:6000/v1", allow_insecure=False)
    assert image_transport_allowed("http://161.92.92.153:6000/v1", allow_insecure=True)


def test_resolve_does_not_require_pathlib_resolve_for_approved_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    write_image(tmp_path / "evidence.png")
    monkeypatch.setattr(
        image_evidence,
        "validate_network_evidence_path",
        lambda value, allowed_root: "allowed",
    )
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda self, strict=False: (_ for _ in ()).throw(
            AssertionError("Path.resolve must not be used for DFS evidence")
        ),
    )
    resolver = NetworkImageResolver()

    result = resolver.resolve(str(tmp_path), str(tmp_path))

    assert result.status == "ready"
    assert [image.relative_name for image in result.images] == ["evidence.png"]
