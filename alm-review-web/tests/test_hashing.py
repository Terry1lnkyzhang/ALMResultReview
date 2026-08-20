from copy import deepcopy

from app.hashing import review_hash, review_payload, source_hash


def sample_record() -> dict:
    return {
        "run": {
            "id": "152711",
            "status": "Passed",
            "test-name": "Cardiac scan",
            "test-description": "<html><body><p>Verify scan</p></body></html>",
            "steps": [
                {
                    "step-order": "1",
                    "name": "Step 1",
                    "status": "Passed",
                    "description": "<p>Start scan</p>",
                    "expected": "<p>Scan succeeds</p>",
                    "actual": "<p>Scan succeeded</p>",
                }
            ],
        }
    }


def test_actual_comma_changes_review_hash() -> None:
    original = sample_record()
    modified = deepcopy(original)
    modified["run"]["steps"][0]["actual"] = "<p>Scan succeeded,</p>"

    assert review_hash(original) != review_hash(modified)
    assert source_hash(original) != source_hash(modified)


def test_html_format_only_change_does_not_change_review_hash() -> None:
    original = sample_record()
    modified = deepcopy(original)
    modified["run"]["steps"][0]["actual"] = (
        "<html><body>\n<div><span>Scan succeeded</span></div>\n</body></html>"
    )

    assert review_hash(original) == review_hash(modified)
    assert source_hash(original) != source_hash(modified)


def test_location_is_included_in_review_payload_and_hash_when_present() -> None:
    original = sample_record()
    modified = deepcopy(original)
    modified["run"]["location"] = "KunPeng-TMI-0009"

    assert review_payload(modified)["execution_location"] == "KunPeng-TMI-0009"
    assert review_hash(original) != review_hash(modified)


def test_phase_one_evidence_is_payload_metadata_not_hash_content() -> None:
    record = sample_record()
    record["run"]["steps"][0]["actual"] = r"Saved C:\Evidence\step.png on 2026-07-30"
    record["run"]["execution-date"] = "2026-07-30"

    payload = review_payload(record)

    assert payload["external_evidence_phase"] == "deferred"
    assert payload["review_capabilities"]["path_access"] is False
    assert payload["steps"][0]["evidence_profile"]["actual_paths"][0]["raw"] == (
        r"C:\Evidence\step.png"
    )
    assert review_hash(sample_record()) == (
        "61c224162b3d3650ce1d40661f0465dc07a0e3408d684903559d27f720b53cdf"
    )


def test_actual_format_profile_ignores_minor_whitespace_and_reports_disruptive_runs() -> None:
    record = sample_record()
    record["run"]["steps"][0]["actual"] = (
        "<p>Scan  succeeded</p><div>  Result saved  </div><br><br>Done"
    )

    actual_format = review_payload(record)["steps"][0]["actual_format"]

    assert actual_format["layout_text"] == (
        "Scan  succeeded\n  Result saved  \n\n\nDone"
    )
    assert actual_format["signals"] == [
        {"type": "blank_line_runs", "count": 1, "max_consecutive": 2}
    ]

    record["run"]["steps"][0]["actual"] = "One  two\nThree  four\nFive    six"

    signals = review_payload(record)["steps"][0]["actual_format"]["signals"]

    assert signals == [
        {"type": "repeated_spaces", "count": 3, "max_width": 4}
    ]


def test_actual_format_profile_ignores_single_extra_space_and_normal_blank_line() -> None:
    record = sample_record()
    record["run"]["steps"][0]["actual"] = "Scan  succeeded\n\nResult saved "

    actual_format = review_payload(record)["steps"][0]["actual_format"]

    assert actual_format["signals"] == []


def test_review_payload_aligns_numbered_expected_and_actual_subitems() -> None:
    record = sample_record()
    record["run"]["steps"][0]["expected"] = (
        "1. Trigger Threshold:______ "
        "4. The CT value displayed is_____, which is within 0 to 400. "
        "8.The number of images obtained is____, which is the same as planned."
    )
    record["run"]["steps"][0]["actual"] = (
        "1. Trigger Threshold:___450___ "
        "4. The CT value displayed was__14___, which was within 0 to 400. "
        "8.The number of images obtained was __39__, which was the same as planned."
    )

    comparison = review_payload(record)["steps"][0]["numbered_comparison"]

    assert comparison == [
        {
            "number": 1,
            "expected": "Trigger Threshold:______",
            "actual": "Trigger Threshold:___450___",
        },
        {
            "number": 4,
            "expected": "The CT value displayed is_____, which is within 0 to 400.",
            "actual": "The CT value displayed was__14___, which was within 0 to 400.",
        },
        {
            "number": 8,
            "expected": (
                "The number of images obtained is____, which is the same as planned."
            ),
            "actual": (
                "The number of images obtained was __39__, which was the same as planned."
            ),
        },
    ]


def test_review_payload_does_not_treat_single_marker_or_decimal_as_numbered_items() -> None:
    record = sample_record()
    record["run"]["steps"][0]["expected"] = "1. Record application version 1.2."
    record["run"]["steps"][0]["actual"] = "1. Application version was 1.2."

    assert "numbered_comparison" not in review_payload(record)["steps"][0]


def test_review_payload_does_not_align_zero_or_duplicate_numbering() -> None:
    record = sample_record()
    record["run"]["steps"][0]["expected"] = "0. Setup 1. First 1. Repeated"
    record["run"]["steps"][0]["actual"] = "0. Setup 1. Done 1. Done again"

    assert "numbered_comparison" not in review_payload(record)["steps"][0]