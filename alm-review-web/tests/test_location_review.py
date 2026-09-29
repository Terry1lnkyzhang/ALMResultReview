from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import TestLocationIdentity as LocationIdentity
from app.models import TestLocationVersion as LocationVersion
from app.services.location_review import (
    LocationConfig,
    assess_location_config,
    assess_location_history,
    fallback_location_skill_output,
    location_key,
    parent_name,
    validate_location_skill_output,
)
from app.services.test_locations import TEST_LOCATION_TABLE, resolve_test_location_at


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


@pytest.mark.parametrize("location", ["Offline", "offline", " Laptop "])
def test_non_site_location_does_not_require_a_physical_configuration(
    location: str,
) -> None:
    result = assess_location_config(
        location,
        "Testing / 0. Common Config",
        (),
        frozenset({"offline", "laptop"}),
    )

    assert result["status"] == "not_applicable"
    assert result["failure_code"] == ""
    assert result["candidate_count"] == 0
    assert result["selected_config"] is None


def test_non_site_location_with_configuration_claim_requires_manual_review() -> None:
    result = assess_location_config(
        "Offline",
        "Testing / 1.1 Product-CT Tenara",
        (),
        frozenset({"offline", "laptop"}),
    )

    assert result["status"] == "manual"
    assert result["failure_code"] == "non_site_location_configuration_claim"
    assert "product" in result["reason"]


def test_non_site_location_still_requires_a_parent_folder() -> None:
    result = assess_location_config(
        "Laptop",
        "",
        (),
        frozenset({"offline", "laptop"}),
    )

    assert result["status"] == "fail"
    assert result["failure_code"] == "parent_name_missing"


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


def test_run_157322_uses_bay17_version_effective_at_execution_not_current() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS atframeworkdb")
        Base.metadata.create_all(connection)
        TEST_LOCATION_TABLE.create(connection)
        with Session(bind=connection) as db:
            item = "SY Bay17(CHESS-SCIM-0009)"
            db.execute(
                TEST_LOCATION_TABLE.insert().values(
                    Item=item,
                    Product="Tenara",
                    Version="V6",
                    Collimation="4cm",
                    Platform="Noah",
                    SystemConfig="CT 5300/Incisive CT G4 Prem",
                )
            )
            identity = LocationIdentity(current_item=item)
            db.add(identity)
            db.flush()
            db.add_all(
                [
                    LocationVersion(
                        location_id=identity.id,
                        item=item,
                        product="Tenara",
                        version="V6",
                        collimation="4cm",
                        platform="Noah",
                        system_config=computer,
                        valid_from=start,
                        valid_to=end,
                        operation="update",
                    )
                    for computer, start, end in (
                        (
                            "CT Tenara G5 Prem",
                            datetime(2026, 9, 16, 15, 39, 30),
                            datetime(2026, 9, 25, 0, 2, 22),
                        ),
                        (
                            "CT 5300/Incisive CT G4 Prem",
                            datetime(2026, 9, 25, 0, 2, 22),
                            None,
                        ),
                    )
                ]
            )
            db.commit()
            folder = "Testing / Zhao Yize / 5.1 PC-CT Tenara G5 Prem+ V6 4cm DMS"

            result = assess_location_history(
                db, "CHESS-SCIM-0009", folder, datetime(2026, 9, 20, 5, 12, 8)
            )
            assert result["status"] == "ready"
            assert result["execution_at"] == "2026-09-20 10:12:08"
            assert result["selected_config"]["computer"] == "CT Tenara G5 Prem"
            assert result["selected_version"]["valid_to"] == "2026-09-25 00:02:22"
            assert resolve_test_location_at(
                db, "CHESS-SCIM-0009", datetime(2026, 9, 20, 10, 12, 8)
            ).version.id == result["selected_version"]["id"]

            after_change = assess_location_history(
                db, "CHESS-SCIM-0009", folder, datetime(2026, 9, 25, 0, 0)
            )
            assert after_change["selected_config"]["computer"] == (
                "CT 5300/Incisive CT G4 Prem"
            )
            unknown = assess_location_history(
                db, "CHESS-SCIM-0009", folder, datetime(2026, 9, 14)
            )
            assert unknown["status"] == "manual"
            assert unknown["failure_code"] == "location_history_unknown"
            assert unknown["selected_config"] is None

            missing_time = assess_location_history(db, "CHESS-SCIM-0009", folder, None)
            assert missing_time["status"] == "manual"
            assert missing_time["failure_code"] == "location_execution_time_missing"
            assert missing_time["selected_config"] is None

            missing_location = assess_location_history(
                db, "CHESS-UNKNOWN", folder, datetime(2026, 9, 20, 5, 12, 8)
            )
            assert missing_location["status"] == "fail"
            assert missing_location["failure_code"] == "location_config_not_found"
            unknown_without_time = assess_location_history(
                db, "CHESS-UNKNOWN", folder, None
            )
            assert unknown_without_time["status"] == "fail"

            new_item = "SY Bay18(CHESS-SCIM-0010)"
            db.execute(TEST_LOCATION_TABLE.insert().values(Item=new_item, Product="Tenara"))
            db.commit()
            no_history = assess_location_history(
                db, "CHESS-SCIM-0010", folder, datetime(2026, 9, 20, 5, 12, 8)
            )
            assert no_history["status"] == "manual"
            assert no_history["failure_code"] == "location_history_unknown"
            assert no_history["selected_config"] is None

            # Historical executions remain reviewable after retirement, even
            # when the current AT Framework row has been removed.
            db.execute(TEST_LOCATION_TABLE.delete().where(TEST_LOCATION_TABLE.c.Item == item))
            db.commit()
            retired_location = assess_location_history(
                db, "CHESS-SCIM-0009", folder, datetime(2026, 9, 20, 5, 12, 8)
            )
            assert retired_location["selected_config"]["computer"] == (
                "CT Tenara G5 Prem"
            )
            assert assess_location_history(db, "CHESS-SCIM-0009", folder, None)[
                "status"
            ] == "manual"


def test_location_history_rejects_overlapping_aliases() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        for item in ("SY Bay17(CHESS-SCIM-0009)", "Other Bay(CHESS-SCIM-0009)"):
            identity = LocationIdentity(current_item=item)
            db.add(identity)
            db.flush()
            db.add(
                LocationVersion(
                    location_id=identity.id,
                    item=item,
                    valid_from=datetime(2026, 9, 1),
                    operation="create",
                )
            )
        db.commit()
        resolution = resolve_test_location_at(
            db, "CHESS-SCIM-0009", datetime(2026, 9, 20)
        )
        assert resolution.status == "ambiguous"
        assert resolution.version is None
        assert len(resolution.candidates) == 2
        assessment = assess_location_history(
            db, "CHESS-SCIM-0009", "Testing / 0. Common Config", datetime(2026, 9, 20)
        )
        assert assessment["status"] == "fail"
        assert assessment["candidate_count"] == 2