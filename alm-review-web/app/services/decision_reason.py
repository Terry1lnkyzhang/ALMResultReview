"""Structured reasons for manual qualified decisions: one answer per AI finding."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from app.models import ReviewResult

_BLOCKING_STATUSES = frozenset({"fail", "manual"})
_KIND_LABELS = {
    "fail": "失败",
    "manual": "需人工复核",
    "warning": "警告",
    "review_failed": "评审失败",
}
_TYPE_LABELS = {
    "location": "测试位置",
    "language": "语言质量",
    "minor_language": "语言质量",
    "expected_actual": "Expected 与 Actual",
    "screenshot": "截图证据",
    "html": "截图证据",
    "folder": "截图证据",
    "path": "路径校验",
    "html_sequence": "HTML 报告序列",
    "html_filename": "HTML 报告序列",
    "html_testcase_id": "自动化结果",
    "automation_result": "自动化结果",
    "reference_data": "参考数据",
    "equipment": "设备追溯",
    "equipment_status": "设备状态",
    "equipment_name_shared": "设备名称",
    "review_failed": "AI 评审失败",
    "summary": "AI 结论",
}
_HINTS = {
    "equipment": "如：免校准模体 / 系统自带部件 / Bay 编号不是设备",
    "evidence": "如：证据为 MP4 视频 / 截图编号 1-1 对应 Step1",
    "path": "如：该路径是 Expected 要求记录的本地保存位置",
    "text": "AI 误解了什么，正确的理解是什么",
    "report": "报告哪部分覆盖了该步骤，或哪部分为人工执行",
    "language": "如：已在 ALM 修正 / 原文就是协议名称",
    "location": "执行时的实际配置，或配置更换的时间",
    "default": "AI 哪里错了，或为什么不影响合格",
}
_HINT_GROUPS = {
    "equipment": "equipment",
    "equipment_status": "equipment",
    "equipment_name_shared": "equipment",
    "reference_data": "equipment",
    "screenshot": "evidence",
    "html": "evidence",
    "folder": "evidence",
    "path": "path",
    "expected_actual": "text",
    "automation_result": "report",
    "html_testcase_id": "report",
    "html_sequence": "report",
    "html_filename": "report",
    "language": "language",
    "minor_language": "language",
    "location": "location",
}

# Words that carry no review-specific information on their own.
_CHINESE_FILLERS = tuple(
    sorted(
        {
            "没有问题", "没问题", "无问题", "无误", "合格", "确认", "人工", "正确",
            "正常", "通过", "检查", "核对", "核验", "查看", "证据", "图片", "截图",
            "存在", "符合要求", "符合", "满足", "一致", "没有", "问题", "可以",
            "打开", "结果", "执行", "实际", "预期", "记录", "已经", "步骤", "有",
            "是", "的", "了", "都", "均", "和", "与", "及", "已", "也", "能", "第",
            "步", "过", "看",
        },
        key=len,
        reverse=True,
    )
)
_ENGLISH_FILLERS = frozenset(
    {
        "ok", "okay", "pass", "passed", "fine", "good", "done", "no", "issue",
        "issues", "problem", "problems", "evidence", "result", "results",
        "actual", "expect", "expected", "consistent", "correct", "correctly",
        "check", "checked", "confirm", "confirmed", "verify", "verified",
        "manual", "manually", "validation", "validated", "satisfied", "same",
        "the", "a", "an", "was", "were", "is", "are", "be", "been", "has",
        "have", "had", "with", "and", "it", "to", "of", "in", "on", "as", "all",
        "this", "that", "step", "steps",
    }
)
_STEP_REFERENCE_RE = re.compile(
    r"(?:step|步骤|第)\s*\d+(?:\s*(?:[,，、和&/-]|and)\s*\d+)*\s*步?|\bs\d{1,2}\b"
)
_TOKEN_RE = re.compile(r"[a-z0-9]+|[^\x00-\x7f]+")
MIN_SUBSTANCE = 3


@dataclass(frozen=True)
class DecisionRow:
    kind: str
    type: str
    summary: str
    steps: tuple[int, ...] = ()
    required: bool = True

    @property
    def label(self) -> str:
        return _TYPE_LABELS.get(self.type, self.type or "AI 结论")

    @property
    def kind_label(self) -> str:
        return _KIND_LABELS.get(self.kind, self.kind)

    @property
    def step_label(self) -> str:
        if self.steps:
            return "步骤 " + ", ".join(str(step) for step in self.steps)
        return "整体"

    @property
    def explanation_hint(self) -> str:
        return _HINTS[_HINT_GROUPS.get(self.type, "default")]


@dataclass(frozen=True)
class DecisionAnswer:
    explanation: str = ""

    @property
    def is_blank(self) -> bool:
        return not self.explanation.strip()


def decision_rows(
    result: ReviewResult | None,
    failure_detail: str = "",
) -> list[DecisionRow]:
    """Rows the operator must answer; identical AI findings share one row."""
    if result is None:
        return [
            DecisionRow(
                "review_failed",
                "review_failed",
                failure_detail or "AI 未生成评审结果。",
            )
        ]
    blocking = _merged(_blocking_findings(result))
    warnings = _merged(_warning_findings(result))
    if not blocking and not warnings:
        return [
            DecisionRow("manual", "summary", result.issue_summary or "AI 未给出具体问题。")
        ]
    return [
        *(
            DecisionRow(kind, finding_type, summary, steps)
            for (kind, finding_type, summary), steps in blocking.items()
        ),
        # Warnings only gate the decision when they are all that is left to resolve.
        *(
            DecisionRow("warning", finding_type, summary, steps, required=not blocking)
            for (_, finding_type, summary), steps in warnings.items()
        ),
    ]


def answer_problems(row: DecisionRow, answer: DecisionAnswer) -> list[str]:
    if not row.required and answer.is_blank:
        return []
    problems = []
    explanation = _normalized(answer.explanation)
    if _substance(answer.explanation) < MIN_SUBSTANCE:
        problems.append(
            "“AI 哪里错了 / 为什么不影响”过于笼统，请写清具体原因，"
            "不能只写“合格”“没问题”。"
        )
    elif len(explanation) >= 10 and explanation in _normalized(row.summary):
        problems.append("“AI 哪里错了 / 为什么不影响”不能直接复制 AI 结论。")
    return problems


def validate_answers(
    rows: Sequence[DecisionRow],
    answers: Sequence[DecisionAnswer],
) -> dict[int, list[str]]:
    return {
        index: problems
        for index, (row, answer) in enumerate(zip(rows, answers, strict=True))
        if (problems := answer_problems(row, answer))
    }


def compose_reason(
    rows: Sequence[DecisionRow],
    answers: Sequence[DecisionAnswer],
) -> str:
    return "\n".join(
        f"[{row.step_label} · {row.label}] {answer.explanation.strip()}"
        for row, answer in zip(rows, answers, strict=True)
        if not answer.is_blank
    )


def decision_items_json(
    rows: Sequence[DecisionRow],
    answers: Sequence[DecisionAnswer],
) -> str:
    return json.dumps(
        [
            {
                "kind": row.kind,
                "type": row.type,
                "label": row.label,
                "steps": list(row.steps),
                "step_label": row.step_label,
                "ai_summary": row.summary,
                "explanation": answer.explanation.strip(),
            }
            for row, answer in zip(rows, answers, strict=True)
            if not answer.is_blank
        ],
        ensure_ascii=False,
    )


def load_decision_items(items_json: str | None) -> list[dict[str, Any]]:
    return [item for item in _json(items_json, []) if isinstance(item, dict)]


def _blocking_findings(
    result: ReviewResult,
) -> Iterator[tuple[str, str, str, Any]]:
    location = _json(result.criteria_json, {}).get("location_consistency")
    if isinstance(location, dict) and location.get("status") in _BLOCKING_STATUSES:
        yield location["status"], "location", str(location.get("summary") or ""), None
    for step_result in _json(result.step_results_json, []):
        if not isinstance(step_result, dict):
            continue
        for issue in step_result.get("issues") or []:
            if isinstance(issue, dict) and issue.get("status") in _BLOCKING_STATUSES:
                yield (
                    issue["status"],
                    str(issue.get("type") or ""),
                    str(issue.get("summary") or ""),
                    step_result.get("review_step"),
                )


def _warning_findings(result: ReviewResult) -> Iterator[tuple[str, str, str, Any]]:
    for warning in _json(result.warnings_json, []):
        if isinstance(warning, dict):
            yield (
                "warning",
                str(warning.get("type") or ""),
                str(warning.get("summary") or ""),
                warning.get("step"),
            )


def _merged(
    findings: Iterable[tuple[str, str, str, Any]],
) -> dict[tuple[str, str, str], tuple[int, ...]]:
    merged: dict[tuple[str, str, str], set[int]] = {}
    for kind, finding_type, summary, step in findings:
        steps = merged.setdefault((kind, finding_type, summary), set())
        if isinstance(step, int) and not isinstance(step, bool):
            steps.add(step)
    return {key: tuple(sorted(steps)) for key, steps in merged.items()}


def _json(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return default
    return value if isinstance(value, type(default)) else default


def _normalized(text: str) -> str:
    return "".join(
        char
        for char in text.casefold()
        if not char.isspace() and unicodedata.category(char)[0] not in "PSZ"
    )


def _substance(text: str) -> int:
    """Length of what is left once step numbers and filler words are removed."""
    size = 0
    text = _STEP_REFERENCE_RE.sub(" ", text.casefold())
    for token in _TOKEN_RE.findall(text):
        if token.isascii():
            if token not in _ENGLISH_FILLERS:
                size += len(token)
            continue
        token = "".join(
            char for char in token if unicodedata.category(char)[0] not in "PSZ"
        )
        for filler in _CHINESE_FILLERS:
            token = token.replace(filler, "")
        size += len(token)
    return size
