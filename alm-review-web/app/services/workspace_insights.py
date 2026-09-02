from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, load_only

from app.models import AlmRun, ReviewResult
from app.services.review_status import current_reviews


def _values(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _usage_date(equipment: dict[str, Any], run: AlmRun) -> date | None:
    raw = str(equipment.get("execution_date") or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return run.execution_at.date() if run.execution_at else None


def _device_key(match: dict[str, Any], result_id: int, step: int, index: int) -> str:
    reference = str(match.get("registry_reference") or "").strip()
    if reference:
        return reference.casefold()
    equipment_id = str(match.get("equipment_id") or "").strip()
    if equipment_id:
        return equipment_id.casefold()
    serial_number = str(match.get("serial_number") or "").strip()
    if serial_number:
        return f"sn:{serial_number.casefold()}"
    return f"review:{result_id}:step:{step}:match:{index}"


def _device_reference(match: dict[str, Any], key: str) -> str:
    return str(
        match.get("registry_reference")
        or match.get("equipment_id")
        or (
            f"SN:{str(match.get('serial_number')).strip()}"
            if match.get("serial_number")
            else key
        )
    )


def _attention_identifiers(equipment: dict[str, Any]) -> list[str]:
    values = [
        *_values(equipment.get("reported_identifiers")),
        *_values(equipment.get("unknown_identifiers")),
        *_values(equipment.get("unrecognized_reported_identifiers")),
        *_values(equipment.get("pending_device_names")),
    ]
    disambiguation = equipment.get("disambiguation")
    if isinstance(disambiguation, dict):
        values.extend(_values(disambiguation.get("selected_equipment_names")))
    return list(dict.fromkeys(values))


def workspace_equipment_insights(
    db: Session,
    workspace_id: int,
    policy_key: str,
) -> dict[str, Any]:
    runs = db.scalars(
        select(AlmRun)
        .options(
            load_only(
                AlmRun.run_id,
                AlmRun.alm_run_id,
                AlmRun.test_id,
                AlmRun.test_name,
                AlmRun.execution_at,
                AlmRun.source_hash,
                AlmRun.current_revision_id,
            )
        )
        .where(AlmRun.workspace_id == workspace_id)
        .order_by(desc(AlmRun.execution_at), desc(AlmRun.run_id))
    ).all()
    reviews = current_reviews(db, runs, policy_key)
    selected_results = {
        review.result.id: review.result
        for review in reviews.values()
        if review.result is not None
    }
    result_payloads = {
        result.id: result.step_results_json
        for result in db.scalars(
            select(ReviewResult)
            .options(load_only(ReviewResult.id, ReviewResult.step_results_json))
            .where(ReviewResult.id.in_(selected_results))
        ).all()
    } if selected_results else {}

    devices: dict[str, dict[str, Any]] = {}
    analyzed_run_ids: set[int] = set()
    device_run_ids: set[int] = set()
    unresolved: list[dict[str, Any]] = []
    unresolved_keys: set[tuple[int, int, str, tuple[str, ...]]] = set()

    for run in runs:
        review = reviews[run.run_id]
        if review.result is None:
            continue
        raw_payload = result_payloads.get(review.result.id)
        try:
            steps = json.loads(raw_payload or "[]")
        except (json.JSONDecodeError, TypeError):
            steps = []
        if not isinstance(steps, list):
            continue
        for step_data in steps:
            if not isinstance(step_data, dict):
                continue
            equipment = step_data.get("equipment")
            if not isinstance(equipment, dict):
                continue
            analyzed_run_ids.add(run.run_id)
            step = int(
                step_data.get("review_step")
                or step_data.get("step_order")
                or equipment.get("review_step")
                or 0
            )
            usage_date = _usage_date(equipment, run)
            matches = equipment.get("matches")
            confirmed = matches if isinstance(matches, list) else []
            for index, match in enumerate(confirmed, start=1):
                if not isinstance(match, dict):
                    continue
                key = _device_key(match, review.result.id, step, index)
                item = devices.setdefault(
                    key,
                    {
                        "key": key,
                        "registry_reference": _device_reference(match, key),
                        "equipment_id": str(match.get("equipment_id") or ""),
                        "description": str(match.get("description") or ""),
                        "manufacturer": str(match.get("manufacturer") or ""),
                        "model_number": str(match.get("model_number") or ""),
                        "serial_number": str(match.get("serial_number") or ""),
                        "calibration_date": match.get("calibration_date"),
                        "calibration_due_date": match.get("calibration_due_date"),
                        "equipment_status": str(match.get("equipment_status") or ""),
                        "references": [],
                        "_usage_keys": set(),
                        "_run_ids": set(),
                        "_dates": [],
                    },
                )
                usage_key = (run.run_id, step)
                if usage_key in item["_usage_keys"]:
                    continue
                item["_usage_keys"].add(usage_key)
                item["_run_ids"].add(run.run_id)
                if usage_date is not None:
                    item["_dates"].append(usage_date)
                item["references"].append(
                    {
                        "run_id": run.run_id,
                        "alm_run_id": run.alm_run_id or run.run_id,
                        "test_id": run.test_id,
                        "test_name": run.test_name,
                        "step": step,
                        "execution_date": usage_date.isoformat() if usage_date else "",
                        "status": str(equipment.get("status") or ""),
                        "code": str(equipment.get("code") or ""),
                        "matched_by": _values(match.get("matched_by")),
                    }
                )
                device_run_ids.add(run.run_id)

            identifiers = _attention_identifiers(equipment)
            status = str(equipment.get("status") or "")
            code = str(equipment.get("code") or "")
            needs_attention = bool(identifiers) or (
                not confirmed and status in {"fail", "manual"}
            )
            if not needs_attention:
                continue
            attention_key = (run.run_id, step, code, tuple(identifiers))
            if attention_key in unresolved_keys:
                continue
            unresolved_keys.add(attention_key)
            unresolved.append(
                {
                    "run_id": run.run_id,
                    "alm_run_id": run.alm_run_id or run.run_id,
                    "test_id": run.test_id,
                    "test_name": run.test_name,
                    "step": step,
                    "execution_date": usage_date.isoformat() if usage_date else "",
                    "status": status,
                    "code": code,
                    "identifiers": identifiers,
                    "summary": str(equipment.get("summary") or ""),
                }
            )

    device_rows = []
    for item in devices.values():
        dates = item.pop("_dates")
        run_ids = item.pop("_run_ids")
        usage_keys = item.pop("_usage_keys")
        item["run_count"] = len(run_ids)
        item["step_count"] = len(usage_keys)
        item["first_used"] = min(dates).isoformat() if dates else ""
        item["last_used"] = max(dates).isoformat() if dates else ""
        item["references"].sort(
            key=lambda reference: (
                reference["execution_date"],
                reference["alm_run_id"],
                reference["step"],
            ),
            reverse=True,
        )
        device_rows.append(item)
    device_rows.sort(
        key=lambda item: (
            (item["description"] or item["registry_reference"]).casefold(),
            item["registry_reference"].casefold(),
        )
    )
    unresolved.sort(
        key=lambda item: (
            item["execution_date"],
            item["alm_run_id"],
            item["step"],
        ),
        reverse=True,
    )
    reviewed_runs = len(selected_results)
    return {
        "devices": device_rows,
        "unresolved": unresolved,
        "metrics": {
            "total_runs": len(runs),
            "reviewed_runs": reviewed_runs,
            "analyzed_runs": len(analyzed_run_ids),
            "uncovered_runs": len(runs) - len(analyzed_run_ids),
            "stale_review_runs": sum(
                result.review_policy_key != policy_key
                for result in selected_results.values()
            ),
            "unique_devices": len(device_rows),
            "runs_using_equipment": len(device_run_ids),
            "usage_records": sum(item["step_count"] for item in device_rows),
            "unresolved_references": len(unresolved),
        },
    }