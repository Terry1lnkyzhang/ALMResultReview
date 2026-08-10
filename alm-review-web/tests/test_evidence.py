from app.services.evidence import (
    DeferredExternalEvidenceResolver,
    extract_dates,
    extract_paths,
    step_evidence_profile,
    validate_network_evidence_path,
)


def test_extracts_windows_unc_and_html_paths_without_accessing_them() -> None:
    actual = (
        r"Image C:\Evidence\step2.png; folder \\server\share\case; "
        "report https://example.test/report.html"
    )

    paths = extract_paths(actual)

    assert [(path["raw"], path["kind"]) for path in paths] == [
        (r"C:\Evidence\step2.png", "image"),
        (r"\\server\share\case", "folder_or_unknown"),
        ("https://example.test/report.html", "html"),
    ]
    assert all(path["access_status"] == "deferred" for path in paths)


def test_extracts_quoted_paths_containing_spaces() -> None:
    paths = extract_paths(
        r'''Use "C:\Test Evidence\step 2.png" and '\\server\review share\case 1'.'''
    )

    assert [path["raw"] for path in paths] == [
        r"C:\Test Evidence\step 2.png",
        r"\\server\review share\case 1",
    ]


def test_extracts_unquoted_unc_path_with_spaces_on_its_own_line() -> None:
    root = r"\\code1\dfscle\BUSINESS\VandV\CT-SysVer\Earth\System Verification Cycle01"
    actual = (
        "Screenshots are stored in the following path:\n\n"
        r"\\code1\dfscle\BUSINESS\VandV\CT-SysVer\Earth\System Verification Cycle01"
        r"\SYBay02\Cardiac\32582-231020"
    )

    paths = extract_paths(actual)

    assert [path["raw"] for path in paths] == [
        root + r"\SYBay02\Cardiac\32582-231020"
    ]
    assert validate_network_evidence_path(paths[0]["raw"], root) == "allowed"

def test_extracts_unquoted_unc_path_with_spaces_after_line_prefix() -> None:
    path = (
        r"\\code1\dfscle\BUSINESS\VandV\CT-SysVer\Earth"
        r"\System Verification Cycle01\SYBay09\69529-231833"
    )
    actual = (
        "Saved screenshots as follow: "
        + path
    )

    paths = extract_paths(actual)

    assert paths == [
        {
            "raw": path,
            "kind": "folder_or_unknown",
            "access_status": "deferred",
        }
    ]


def test_extracts_unc_path_after_referred_to_prefix() -> None:
    path = (
        r"\\code1\dfscle\BUSINESS\VandV\CT-SysVer\Earth"
        r"\System Verification Cycle01\SYBay09\Bay09_34578"
    )

    paths = extract_paths("Screenshot refered to " + path)

    assert [item["raw"] for item in paths] == [path]


def test_explicit_phantom_part_number_does_not_require_reference_lookup() -> None:
    profile = step_evidence_profile(
        "Used phantom part number is PCCSY-RD-CT-0-20151202.",
        "Complete the scan.",
        "The scan was completed.",
        "2026-08-06",
        False,
    )

    assert not profile["reference_lookup_required"]


def test_unspecified_phantom_still_requires_reference_lookup() -> None:
    profile = step_evidence_profile(
        "Use the required phantom.",
        "Complete the scan.",
        "The scan was completed.",
        "2026-08-06",
        False,
    )

    assert profile["reference_lookup_required"]


def test_date_comparison_uses_alm_execution_date() -> None:
    dates = extract_dates("Executed 2026/07/30, checked 2026-07-29", "2026-07-30")

    assert [value["normalized"] for value in dates] == ["2026-07-30", "2026-07-29"]
    assert [value["same_day_as_execution"] for value in dates] == [True, False]


def test_deferred_resolver_returns_metadata_without_file_access() -> None:
    result = DeferredExternalEvidenceResolver().resolve(r"C:\restricted\evidence.html")

    assert result["status"] == "deferred"
    assert result["path"] == r"C:\restricted\evidence.html"


def test_network_evidence_path_must_be_below_configured_unc_root() -> None:
    root = r"\\server\approved"

    assert validate_network_evidence_path(r"\\SERVER\APPROVED\case\result.html", root) == (
        "allowed"
    )
    assert validate_network_evidence_path(r"\\server\approved-other\result.html", root) == (
        "outside_root"
    )
    assert validate_network_evidence_path(r"C:\approved\result.html", root) == "not_unc"