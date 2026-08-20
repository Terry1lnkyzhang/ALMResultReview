from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DECLARATION_PATH = Path(__file__).resolve().parents[1] / "review_pipeline.toml"


@dataclass(frozen=True)
class StageDeclaration:
    stage_id: str
    skill_id: str
    gate: str
    writes: tuple[str, ...]


def _string_tuple(value: Any, stage_id: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"Review stage {stage_id} writes must be a string array.")
    return tuple(item.strip() for item in value)


def load_stages() -> tuple[StageDeclaration, ...]:
    manifest = tomllib.loads(DECLARATION_PATH.read_text(encoding="utf-8"))
    stages = manifest.get("stage")
    if not isinstance(stages, list) or not stages:
        raise ValueError("Review pipeline declaration must list stages.")
    declarations: list[StageDeclaration] = []
    for stage in stages:
        if not isinstance(stage, dict):
            raise ValueError("Each review stage must be a table.")
        stage_id = str(stage.get("id", "")).strip()
        if not stage_id:
            raise ValueError("Review pipeline stage requires an id.")
        declarations.append(
            StageDeclaration(
                stage_id=stage_id,
                skill_id=str(stage.get("skill", "")).strip(),
                gate=str(stage.get("gate", "")).strip(),
                writes=_string_tuple(stage.get("writes", []), stage_id),
            )
        )
    stage_ids = [item.stage_id for item in declarations]
    if len(stage_ids) != len(set(stage_ids)):
        raise ValueError("Review pipeline declares duplicate stage ids.")
    return tuple(declarations)


STAGES = load_stages()
STAGE_ORDER = tuple(stage.stage_id for stage in STAGES)
STAGE_GATES = {stage.stage_id: stage.gate for stage in STAGES if stage.gate}
STAGE_SKILLS = {stage.stage_id: stage.skill_id for stage in STAGES if stage.skill_id}


def build_pipeline_trace(
    *,
    gates: dict[str, bool],
    plan: dict[str, Any],
    stages: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    missing = set(STAGE_ORDER) - set(stages)
    if missing:
        raise ValueError(f"Review pipeline did not report stages: {sorted(missing)!r}.")
    undeclared = set(stages) - set(STAGE_ORDER)
    if undeclared:
        raise ValueError(
            f"Review pipeline reported undeclared stages: {sorted(undeclared)!r}."
        )
    required_gates = set(STAGE_GATES.values())
    if not required_gates <= set(gates):
        raise ValueError(
            f"Review pipeline is missing gate values: {sorted(required_gates - set(gates))!r}."
        )
    ordered = {stage_id: stages[stage_id] for stage_id in STAGE_ORDER}
    return {
        "version": 1,
        "external_evidence_review_enabled": gates["external_evidence_review_enabled"],
        "equipment_review_enabled": gates["equipment_review_enabled"],
        "plan": plan,
        "stages": ordered,
        "total_ai_calls": sum(stage["ai_calls"] for stage in ordered.values()),
    }
