from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, TypeAdapter, model_validator

CopilotMode = Literal["skill", "open"]
COPILOT_LABELS: dict[CopilotMode, str] = {"skill": "Copilot Skill", "open": "Copilot Open"}
MAX_COPILOT_BATCH = 25


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


_REPORTS = TypeAdapter(dict[CopilotMode, CopilotOutcome])


def load_copilot_reports(raw: str | None) -> dict[CopilotMode, CopilotOutcome]:
    return _REPORTS.validate_json(raw) if raw else {}


def dump_copilot_reports(reports: dict[CopilotMode, CopilotOutcome]) -> str:
    return _REPORTS.dump_json(reports).decode("utf-8")


def copilot_findings(
    reports: Mapping[CopilotMode, CopilotOutcome],
) -> Iterator[tuple[str, CopilotFinding]]:
    for mode, label in COPILOT_LABELS.items():
        report = reports.get(mode)
        if report is not None and report.verdict == "unqualified":
            for finding in report.findings:
                yield label, finding