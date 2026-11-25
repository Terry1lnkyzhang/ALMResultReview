from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    TypeAdapter,
    field_validator,
    model_validator,
)

CopilotMode = Literal["skill", "open"]
COPILOT_LABELS: dict[CopilotMode, str] = {"skill": "Copilot Skill", "open": "Copilot Open"}
MAX_COPILOT_BATCH = 25


def normalize_utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


class CopilotFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    location: str = Field(min_length=1, max_length=400)
    evidence: str = Field(min_length=1, max_length=2000)
    summary: str = Field(min_length=1, max_length=500)
    rule: str = Field(default="", max_length=400)


class CopilotOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    verdict: Literal["qualified", "unqualified"] | None
    findings: list[CopilotFinding] = Field(default_factory=list, max_length=20)
    limitations: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def require_evidence(self) -> CopilotOutcome:
        if (self.verdict == "unqualified") != bool(self.findings):
            raise ValueError("Exactly unqualified outcomes must contain findings.")
        if self.verdict is None and not self.limitations:
            raise ValueError("An incomplete review must state its limitation.")
        return self


class StoredCopilotOutcome(CopilotOutcome):
    """Persisted Copilot outcome with import provenance for manual-override ordering."""

    recorded_at: datetime | None = None
    baseline_manual_decision_id: int | None = None

    @field_validator("recorded_at", mode="after")
    @classmethod
    def _normalize_recorded_at(cls, value: datetime | None) -> datetime | None:
        return normalize_utc_naive(value)

    def same_outcome(self, other: CopilotOutcome) -> bool:
        return (
            self.verdict == other.verdict
            and self.findings == other.findings
            and self.limitations == other.limitations
        )


class CopilotRunReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_result_id: PositiveInt
    source_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    outcome: CopilotOutcome


class CopilotRoundReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: CopilotMode
    results: list[CopilotRunReport] = Field(min_length=1, max_length=MAX_COPILOT_BATCH)

    @model_validator(mode="after")
    def validate_findings(self) -> CopilotRoundReport:
        ids = [result.review_result_id for result in self.results]
        if len(set(ids)) != len(ids):
            raise ValueError("A review result can appear only once per round.")
        if self.mode == "skill" and any(
            not finding.rule for result in self.results for finding in result.outcome.findings
        ):
            raise ValueError("Skill findings must identify the applicable rule.")
        return self


_REPORTS = TypeAdapter(dict[CopilotMode, StoredCopilotOutcome])


def load_copilot_reports(raw: str | None) -> dict[CopilotMode, StoredCopilotOutcome]:
    return _REPORTS.validate_json(raw) if raw else {}


def dump_copilot_reports(reports: Mapping[CopilotMode, CopilotOutcome]) -> str:
    stored = {
        mode: (
            outcome
            if isinstance(outcome, StoredCopilotOutcome)
            else StoredCopilotOutcome(
                verdict=outcome.verdict,
                findings=outcome.findings,
                limitations=outcome.limitations,
            )
        )
        for mode, outcome in reports.items()
    }
    return _REPORTS.dump_json(stored).decode("utf-8")


def copilot_rejection_active(
    report: CopilotOutcome,
    *,
    manual_decision_id: int | None = None,
    manual_decision_at: datetime | None = None,
    has_manual_decision: bool = False,
) -> bool:
    if report.verdict != "unqualified":
        return False
    if not has_manual_decision:
        return True
    baseline_id = (
        report.baseline_manual_decision_id
        if isinstance(report, StoredCopilotOutcome)
        else None
    )
    if baseline_id is not None:
        if manual_decision_id is not None:
            return manual_decision_id <= baseline_id
        return True
    recorded_at = (
        normalize_utc_naive(report.recorded_at)
        if isinstance(report, StoredCopilotOutcome)
        else None
    )
    manual_at = normalize_utc_naive(manual_decision_at)
    if recorded_at is not None and manual_at is not None:
        return manual_at < recorded_at
    return False


def copilot_findings(
    reports: Mapping[CopilotMode, CopilotOutcome],
) -> Iterator[tuple[str, CopilotFinding]]:
    for mode, label in COPILOT_LABELS.items():
        report = reports.get(mode)
        if report is not None and report.verdict == "unqualified":
            for finding in report.findings:
                yield label, finding