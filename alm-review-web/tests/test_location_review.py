import pytest

from app.services.location_review import (
    LocationConfig,
    assess_location_config,
    fallback_location_skill_output,
    location_key,
    parent_name,
    validate_location_skill_output,
)


def config(
    *,
    item: str = "SY Bay10(CHESS-CAST-20006)",
    product: str = "Tenara",
    dms_version: str = "V6",
    dms_coverage: str = "2cm",
    couch: str = "Enhance Incisive STD",
    computer: str = "CT Tenara/CT 5300 G5 STD",
) -> LocationConfig:
    return LocationConfig(
        item=item,
        product=product,
        dms_version=dms_version,
        dms_coverage=dms_coverage,
        couch=couch,
        computer=computer,
    )


def test_location_key_uses_the_final_parenthesized_identifier() -> None:
    assert location_key(" SY Bay10(CHESS-CAST-20006) ") == "chess-cast-20006"
    assert location_key("Offline") == "offline"


def test_parent_name_uses_the_last_nonempty_folder_segment() -> None:
    assert parent_name("Testing / Zhao Yize / 4.6 DMS-4cm V2 or V6 / ") == (
        "4.6 DMS-4cm V2 or V6"
    )


def test_location_requires_one_effective_configuration() -> None:
    missing_location = assess_location_config("", "Testing / 0. Common Config", ())
    not_found = assess_location_config(
        "CHESS-CAST-20006", "Testing / 0. Common Config", ()
    )
    ambiguous = assess_location_config(
        "CHESS-CAST-20006",
        "Testing / 0. Common Config",
        (
            config(item="SY Bay10(CHESS-CAST-20006)"),
            config(item="SY Other(CHESS-CAST-20006)"),
        ),
    )
    empty = assess_location_config(
        "CHESS-CAST-20006",
        "Testing / 0. Common Config",
        (config(product="", dms_version="", dms_coverage="", couch="", computer=""),),
    )

    assert missing_location["failure_code"] == "alm_location_missing"
    assert not_found["failure_code"] == "location_config_not_found"
    assert ambiguous["failure_code"] == "location_config_ambiguous"
    assert empty["failure_code"] == "location_config_empty"
    assert all(
        result["status"] == "fail"
        for result in (missing_location, not_found, ambiguous, empty)
    )


def test_valid_location_configuration_is_ready_for_semantic_review() -> None:
    result = assess_location_config(
        "CHESS-CAST-20006",
        "Automation Testing Phase2 / Li Xianjin / 4.6 DMS-4cm V2 or V6",
        (config(),),
    )

    assert result["status"] == "ready"
    assert result["parent_name"] == "4.6 DMS-4cm V2 or V6"
    assert result["candidate_items"] == ["SY Bay10(CHESS-CAST-20006)"]
    assert result["selected_config"]["dms_coverage"] == "2cm"


def test_location_skill_cannot_skip_explicit_configuration_markers() -> None:
    skill_input = {
        "parent_name": "4.6 DMS-4cm V2 or V6",
        "location_config": config().as_dict(),
    }
    output = {
        "has_configuration_claim": False,
        "status": "not_applicable",
        "comparisons": [],
        "reason": "No claim.",
    }

    with pytest.raises(ValueError, match="skipped explicit configuration markers"):
        validate_location_skill_output(skill_input, output)

    fallback = fallback_location_skill_output(skill_input, output, "invalid")
    validate_location_skill_output(skill_input, fallback)
    assert fallback["status"] == "uncertain"
    assert {item["field"] for item in fallback["comparisons"]} == {
        "dms_version",
        "dms_coverage",
    }


def test_product_version_is_not_forced_into_the_dms_version_field() -> None:
    skill_input = {
        "parent_name": "1.2 Product-CT 5300 V7.0",
        "location_config": config(product="CT5300 V7.0").as_dict(),
    }
    output = {
        "has_configuration_claim": True,
        "status": "pass",
        "comparisons": [
            {
                "field": "product",
                "parent_text": "CT 5300 V7.0",
                "status": "matched",
                "reason": "The product matches.",
            }
        ],
        "reason": "The product configuration matches.",
    }

    validate_location_skill_output(skill_input, output)


def test_location_fallback_rejects_a_match_against_an_empty_field() -> None:
    skill_input = {
        "parent_name": "1.2 Product-CT 5300",
        "location_config": config(product="").as_dict(),
    }
    output = {
        "has_configuration_claim": True,
        "status": "pass",
        "comparisons": [
            {
                "field": "product",
                "parent_text": "CT 5300",
                "status": "matched",
                "reason": "The product matches.",
            }
        ],
        "reason": "The product matches.",
    }

    fallback = fallback_location_skill_output(skill_input, output, "invalid")

    validate_location_skill_output(skill_input, fallback)
    assert fallback["status"] == "fail"
    assert fallback["comparisons"][0]["status"] == "mismatched"