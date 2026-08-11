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


def test_actual_format_profile_preserves_layout_and_reports_whitespace_signals() -> None:
    record = sample_record()
    record["run"]["steps"][0]["actual"] = (
        "<p>Scan  succeeded</p><div>  Result saved  </div><br><br>Done"
    )

    actual_format = review_payload(record)["steps"][0]["actual_format"]

    assert actual_format["layout_text"] == (
        "Scan  succeeded\n  Result saved  \n\n\nDone"
    )
    assert actual_format["signals"] == [
        {"type": "repeated_spaces", "count": 3},
        {"type": "leading_whitespace", "lines": [2]},
        {"type": "trailing_whitespace", "lines": [2]},
        {"type": "blank_line_runs", "count": 1},
    ]