from app.services.evidence import (
    DeferredExternalEvidenceResolver,
    analyze_html_path_sequences,
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


def test_repeated_path_with_spaces_does_not_leak_a_truncated_prefix() -> None:
    path = (
        r"\\code1\dfscle\BUSINESS\VandV\CT-SysVer\Earth"
        r"\System Verification Cycle01\SZbay08\58805_231421"
    )
    actual = (
        "1.Layout tab was unavailable\n\nsaves screenshots as follow:\n\n"
        + path
        + "\n\n5.The layout tab was available.\n\nsaves screenshots as follow:\n\n"
        + path
    )

    paths = extract_paths(actual)

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


def test_narrative_local_directory_is_not_declared_as_external_evidence() -> None:
    profile = step_evidence_profile(
        "Delete the report.",
        r"The corresponding folder in D:\PerformanceData\Report is removed.",
        r"The report and folder in D:\PerformanceData\Report were removed.",
        "2026-08-06",
        False,
    )

    assert [path["raw"] for path in profile["actual_paths"]] == [
        r"D:\PerformanceData\Report"
    ]
    assert not profile["path_validation_required"]


def test_screenshot_path_is_declared_as_external_evidence() -> None:
    profile = step_evidence_profile(
        "Take a screenshot.",
        "The screenshot proves the result.",
        r"Saved screenshots as follow path: C:\Evidence\step1.png",
        "2026-08-06",
        False,
    )

    assert profile["path_validation_required"]


def test_direct_image_path_routes_to_visual_review_without_screenshot_wording() -> None:
    profile = step_evidence_profile(
        "Record the result.",
        "The result is available.",
        r"Result: \\server\approved\Step1.png",
        "2026-08-06",
        False,
    )

    assert profile["screenshot_review_required"]
    assert profile["routing"] == {
        "intent": "image_evidence",
        "triggers": ["direct_image_path:.png"],
        "actions": ["validate_path", "load_images", "send_to_visual_ai"],
        "decision_source": "deterministic",
        "confidence": 1.0,
        "reason": "A direct image path was detected.",
        "manual_required": False,
    }


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


def test_automation_html_suffixes_are_continuous_from_unsuffixed_report() -> None:
    parent = r"\\code1\dfscle\automation\34834"
    base = "CT-NMP.SRS.PhyChar.8-Axial-slice thickness-0.625_34834"
    paths = [
        {"raw": parent + "\\" + base + ".html", "kind": "html"},
        *[
            {"raw": parent + "\\" + base + f"_{number}.html", "kind": "html"}
            for number in range(2, 9)
        ],
    ]

    sequences = analyze_html_path_sequences(paths)

    assert sequences == [
        {
            "base_name": base,
            "numbers": list(range(1, 9)),
            "missing_numbers": [],
            "status": "pass",
        }
    ]


def test_extracts_saved_screenshot_automation_report_sequence() -> None:
    parent = (
        r"\\code1\dfscle\BUSINESS\VandV\CT-SysVer\Earth"
        r"\System Verification Cycle01\SystemVerificationAutomaionResult"
        r"\34834\6.4 Product-CT Tenara+V6 4cm"
    )
    base = "CT-NMP.SRS.PhyChar.8-Axial-slice thickness-0.625_34834"
    path_lines = [parent + "\\" + base + ".html"] + [
        parent + "\\" + base + f"_{number}.html" for number in range(2, 9)
    ]
    actual = "Saved screenshot: refer to automation test report\n" + "\n".join(
        path_lines
    )

    paths = extract_paths(actual)

    assert [path["raw"] for path in paths] == path_lines
    assert analyze_html_path_sequences(paths)[0]["numbers"] == list(range(1, 9))


def test_automation_html_suffix_gap_is_reported() -> None:
    parent = r"\\server\approved\automation"
    paths = [
        {"raw": parent + r"\report_34834.html", "kind": "html"},
        {"raw": parent + r"\report_34834_2.html", "kind": "html"},
        {"raw": parent + r"\report_34834_4.html", "kind": "html"},
    ]

    assert analyze_html_path_sequences(paths)[0]["missing_numbers"] == [3]
    assert analyze_html_path_sequences(paths)[0]["status"] == "fail"