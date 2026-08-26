import re
from pathlib import Path

import app.services.image_evidence as image_evidence
from app.models import EvidenceConfig
from app.services.evidence import step_evidence_profile
from app.services.image_evidence import NetworkImageResolver
from app.services.reviews import _IMAGE_EVIDENCE_ISSUES, _prepare_image_evidence

# Every status the resolver or the review pipeline can attach to image evidence.
IMAGE_EVIDENCE_STATUSES = frozenset(
    {
        "ready",
        "missing",
        "denied",
        "unavailable",
        "outside_root",
        "not_unc",
        "root_not_configured",
        "no_images",
        "no_usable_images",
        "no_matching_images",
        "ambiguous_step_mapping",
        "transport_too_large",
    }
)
SILENT_IMAGE_EVIDENCE_STATUSES = frozenset(
    {"ready", "not_unc", "root_not_configured"}
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"test-png"
JPEG_BYTES = b"\xff\xd8\xff" + b"test-jpeg"
WEBP_BYTES = b"RIFF\x04\x00\x00\x00WEBP" + b"test-webp"


def write_image(path: Path, content: bytes = PNG_BYTES) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_image_evidence_statuses_map_to_exactly_one_review_outcome() -> None:
    assert set(_IMAGE_EVIDENCE_ISSUES) == (
        IMAGE_EVIDENCE_STATUSES - SILENT_IMAGE_EVIDENCE_STATUSES
    )
    assert all(
        status in {"fail", "manual"} and issue_type in {"path", "screenshot"}
        for status, issue_type, _summary in _IMAGE_EVIDENCE_ISSUES.values()
    )


def test_resolver_only_reports_known_image_evidence_statuses() -> None:
    source = Path(image_evidence.__file__).read_text(encoding="utf-8")
    literals = set(re.findall(r'status\s*=\s*"([a-z_]+)"', source))

    assert literals
    assert literals <= IMAGE_EVIDENCE_STATUSES


def test_collects_supported_images_with_depth_and_count_limits(tmp_path: Path) -> None:
    write_image(tmp_path / "01.png")
    write_image(tmp_path / "level1" / "02.jpg", JPEG_BYTES)
    write_image(tmp_path / "level1" / "03.jfif", JPEG_BYTES)
    write_image(tmp_path / "level1" / "level2" / "04.webp", WEBP_BYTES)
    write_image(tmp_path / "level1" / "level2" / "level3" / "ignored.png")
    write_image(tmp_path / "05.png")
    write_image(tmp_path / "06.png")

    result = NetworkImageResolver(max_depth=2, max_images=5).collect(tmp_path)

    assert result.status == "ready"
    assert len(result.images) == 5
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
            external_evidence_review_enabled=True,
        ),
    )

    assert [
        image.relative_name
        for image in prepared.results[1][str(tagged)].images
    ] == ["Step1-1.png", "Step1-2.png"]
    assert [
        image.relative_name
        for image in prepared.results[2][str(tagged)].images
    ] == ["Step2-1.png", "Step2-2.png"]


def test_step_letter_suffix_images_match_the_base_step(tmp_path: Path) -> None:
    write_image(tmp_path / "Step1a.png")
    write_image(tmp_path / "Step1b.JPG", JPEG_BYTES)
    write_image(tmp_path / "Step2a.png")

    result = NetworkImageResolver(
        matching_step_numbers={1},
        require_step_marker=True,
    ).collect(tmp_path)

    assert result.status == "ready"
    assert [image.relative_name for image in result.images] == [
        "Step1a.png",
        "Step1b.JPG",
    ]


def test_direct_image_path_is_loaded_without_screenshot_wording(
    tmp_path: Path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "Step1.png"
    write_image(image_path)
    monkeypatch.setattr(
        image_evidence,
        "validate_network_evidence_path",
        lambda value, allowed_root: "allowed",
    )
    profile = step_evidence_profile(
        "Record the result.",
        "The result is available.",
        f"Result: {image_path}",
        "2026-08-14",
        False,
    )

    prepared = _prepare_image_evidence(
        {
            "steps": [
                {
                    "review_step": 1,
                    "order": "1",
                    "evidence_profile": profile,
                }
            ]
        },
        EvidenceConfig(
            allowed_network_root=str(tmp_path),
            external_evidence_review_enabled=True,
        ),
    )

    assert profile["routing"]["actions"] == [
        "validate_path",
        "load_images",
        "send_to_visual_ai",
    ]
    assert prepared.results[1][str(image_path)].status == "ready"
    assert prepared.results[1][str(image_path)].images[0].relative_name == "Step1.png"


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
            external_evidence_review_enabled=True,
        ),
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
            external_evidence_review_enabled=True,
        ),
    )

    assert prepared.results[1][str(untagged)].status == "ambiguous_step_mapping"
    assert prepared.results[2][str(untagged)].status == "ambiguous_step_mapping"


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


def test_extensionless_leaf_resolves_to_the_matching_image_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    write_image(tmp_path / "step4.jpg", JPEG_BYTES)
    write_image(tmp_path / "step5.PNG")
    monkeypatch.setattr(
        image_evidence,
        "validate_network_evidence_path",
        lambda value, allowed_root: "allowed",
    )
    resolver = NetworkImageResolver()

    result = resolver.resolve(str(tmp_path / "step4"), str(tmp_path))

    assert result.status == "ready"
    assert [image.relative_name for image in result.images] == ["step4.jpg"]


def test_extensionless_leaf_stays_missing_when_no_image_shares_the_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    write_image(tmp_path / "step4.png")
    (tmp_path / "step9.txt").write_text("not an image", encoding="utf-8")
    monkeypatch.setattr(
        image_evidence,
        "validate_network_evidence_path",
        lambda value, allowed_root: "allowed",
    )
    resolver = NetworkImageResolver()

    assert resolver.resolve(str(tmp_path / "step9"), str(tmp_path)).status == "missing"


def test_extensionless_folder_segment_is_not_matched_against_an_image(
    tmp_path: Path,
    monkeypatch,
) -> None:
    write_image(tmp_path / "step4.png")
    monkeypatch.setattr(
        image_evidence,
        "validate_network_evidence_path",
        lambda value, allowed_root: "allowed",
    )
    resolver = NetworkImageResolver()

    assert resolver.resolve(str(tmp_path / "step4" / "shot"), str(tmp_path)).status == "missing"
