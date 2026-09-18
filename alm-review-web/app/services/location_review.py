from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

_LOCATION_ALIAS_RE = re.compile(r"\((?P<identifier>[^()]*)\)\s*$")
_LOCATION_CONFIG_QUERY = text(
    "SELECT Item AS item, Product AS product, "
    "`DMS Version` AS dms_version, `DMS Coverage` AS dms_coverage, "
    "Couch AS couch, Computer AS computer "
    "FROM atframeworkdb.loadtestcasetestlocation ORDER BY Item"
)
_CONFIG_FIELDS = ("product", "dms_version", "dms_coverage", "couch", "computer")
_PARENT_FIELD_MARKERS = {
    "product": re.compile(r"\bproduct\b", re.IGNORECASE),
    "dms_version": re.compile(r"\bv\s*(?:2|6)\b", re.IGNORECASE),
    "dms_coverage": re.compile(r"\b\d+\s*cm\b", re.IGNORECASE),
    "couch": re.compile(r"\bcouch\b|\bnoah\b|\benhanc(?:e|ed)\b", re.IGNORECASE),
    "computer": re.compile(r"\bpc\s*[-+]", re.IGNORECASE),
}


@dataclass(frozen=True)
class LocationConfig:
    item: str
    product: str
    dms_version: str
    dms_coverage: str
    couch: str
    computer: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _string(value: Any) -> str:
    return str(value or "").strip()


def location_key(value: str) -> str:
    normalized = value.strip()
    match = _LOCATION_ALIAS_RE.search(normalized)
    if match is not None:
        normalized = match.group("identifier").strip()
    return normalized.casefold()


def parent_name(folder_path: str) -> str:
    return next(
        (
            segment.strip()
            for segment in reversed(folder_path.split("/"))
            if segment.strip()
        ),
        "",
    )


def load_location_configs(db: Session) -> tuple[LocationConfig, ...]:
    return tuple(
        LocationConfig(
            item=_string(row.item),
            product=_string(row.product),
            dms_version=_string(row.dms_version),
            dms_coverage=_string(row.dms_coverage),
            couch=_string(row.couch),
            computer=_string(row.computer),
        )
        for row in db.execute(_LOCATION_CONFIG_QUERY)
    )


def assess_location_config(
    execution_location: str,
    folder_path: str,
    configs: tuple[LocationConfig, ...],
) -> dict[str, Any]:
    location = execution_location.strip()
    review_parent_name = parent_name(folder_path)
    base = {
        "alm_location": location,
        "folder_path": folder_path.strip(),
        "parent_name": review_parent_name,
        "candidate_count": 0,
        "candidate_items": [],
        "selected_config": None,
    }
    if not location:
        return {
            **base,
            "status": "fail",
            "failure_code": "alm_location_missing",
            "reason": "ALM Run Location is empty.",
        }

    candidates = [
        config
        for config in configs
        if location_key(config.item) == location_key(location)
    ]
    candidate_items = [config.item for config in candidates]
    candidate_base = {
        **base,
        "candidate_count": len(candidates),
        "candidate_items": candidate_items,
    }
    if not candidates:
        return {
            **candidate_base,
            "status": "fail",
            "failure_code": "location_config_not_found",
            "reason": f"No test-location configuration matches ALM Location {location}.",
        }
    if len(candidates) != 1:
        return {
            **candidate_base,
            "status": "fail",
            "failure_code": "location_config_ambiguous",
            "reason": (
                f"ALM Location {location} matches {len(candidates)} "
                "test-location configuration rows."
            ),
        }

    selected = candidates[0]
    selected_config = selected.as_dict()
    selected_base = {**candidate_base, "selected_config": selected_config}
    if not any(selected_config[field_name] for field_name in _CONFIG_FIELDS):
        return {
            **selected_base,
            "status": "fail",
            "failure_code": "location_config_empty",
            "reason": f"Test-location configuration for {location} is empty.",
        }
    if not review_parent_name:
        return {
            **selected_base,
            "status": "fail",
            "failure_code": "parent_name_missing",
            "reason": "The ALM test-set folder name is empty.",
        }
    return {
        **selected_base,
        "status": "ready",
        "failure_code": "",
        "reason": "The location has one effective configuration row.",
    }


def load_location_assessment(
    db: Session,
    execution_location: str,
    folder_path: str,
) -> dict[str, Any]:
    if db.bind is not None and db.bind.dialect.name != "mysql":
        return {
            "status": "disabled",
            "failure_code": "unsupported_database",
            "reason": "Test-location review requires the MySQL configuration schema.",
            "alm_location": execution_location.strip(),
            "folder_path": folder_path.strip(),
            "parent_name": parent_name(folder_path),
            "candidate_count": 0,
            "candidate_items": [],
            "selected_config": None,
        }
    return assess_location_config(
        execution_location,
        folder_path,
        load_location_configs(db),
    )


def location_config_policy_snapshot(db: Session) -> dict[str, Any]:
    if db.bind is not None and db.bind.dialect.name != "mysql":
        return {"status": "unsupported", "row_count": 0, "sha256": ""}
    try:
        rows = [config.as_dict() for config in load_location_configs(db)]
    except Exception:
        return {"status": "unavailable", "row_count": 0, "sha256": ""}
    serialized = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "status": "available",
        "row_count": len(rows),
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }


def required_parent_fields(value: str) -> set[str]:
    return {
        field_name
        for field_name, pattern in _PARENT_FIELD_MARKERS.items()
        if pattern.search(value)
    }


def validate_location_skill_output(
    skill_input: dict[str, Any],
    skill_output: dict[str, Any],
) -> None:
    parent = skill_input["parent_name"]
    comparisons = skill_output["comparisons"]
    returned_fields = [item["field"] for item in comparisons]
    if len(returned_fields) != len(set(returned_fields)):
        raise ValueError("Location review returned duplicate configuration fields.")
    for comparison in comparisons:
        if comparison["parent_text"].casefold() not in parent.casefold():
            raise ValueError(
                "Location review cited parent text that is not present in parent_name."
            )

    required_fields = required_parent_fields(parent)
    if not skill_output["has_configuration_claim"]:
        if skill_output["status"] != "not_applicable" or comparisons:
            raise ValueError(
                "A parent without configuration claims must be not_applicable."
            )
        if required_fields:
            raise ValueError(
                "Location review skipped explicit configuration markers for "
                f"{sorted(required_fields)!r}."
            )
        return

    if skill_output["status"] == "not_applicable" or not comparisons:
        raise ValueError("A configuration claim requires comparison details.")
    missing_fields = required_fields - set(returned_fields)
    if missing_fields:
        raise ValueError(
            "Location review omitted explicit configuration fields "
            f"{sorted(missing_fields)!r}."
        )
    configured = skill_input["location_config"]
    if any(
        item["status"] == "matched" and not configured[item["field"]].strip()
        for item in comparisons
    ):
        raise ValueError("Location review matched a field whose configured value is empty.")
    statuses = {item["status"] for item in comparisons}
    expected_status = (
        "fail"
        if "mismatched" in statuses
        else "uncertain"
        if "uncertain" in statuses
        else "pass"
    )
    if skill_output["status"] != expected_status:
        raise ValueError(
            "Location review status is inconsistent with its field comparisons."
        )


def fallback_location_skill_output(
    skill_input: dict[str, Any],
    skill_output: dict[str, Any],
    _detail: str,
) -> dict[str, Any]:
    parent = skill_input["parent_name"]
    required_fields = required_parent_fields(parent)
    comparisons = {
        item["field"]: item
        for item in skill_output.get("comparisons", [])
        if item.get("parent_text", "").casefold() in parent.casefold()
    }
    for field_name, comparison in comparisons.items():
        if (
            comparison["status"] == "matched"
            and not skill_input["location_config"][field_name].strip()
        ):
            comparison["status"] = "mismatched"
            comparison["reason"] = (
                "The parent asserts this field, but its configured value is empty."
            )
    for field_name in required_fields:
        comparisons.setdefault(
            field_name,
            {
                "field": field_name,
                "parent_text": parent,
                "status": "uncertain",
                "reason": "The AI response did not reliably assess this explicit claim.",
            },
        )
    if not comparisons and not skill_output.get("has_configuration_claim"):
        return {
            "has_configuration_claim": False,
            "status": "not_applicable",
            "comparisons": [],
            "reason": "The parent name contains no concrete configuration claim.",
        }
    normalized = list(comparisons.values())
    statuses = {item["status"] for item in normalized}
    status = "fail" if "mismatched" in statuses else "uncertain"
    return {
        "has_configuration_claim": True,
        "status": status,
        "comparisons": normalized,
        "reason": "The parent configuration could not be assessed reliably.",
    }