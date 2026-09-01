from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.evidence import html_path_testcase_ids

_SCRIPT_NAME_RE = re.compile(
    r"test\s+scripts?\s+name\s*:\s*(.+?)"
    r"(?=\s*,?\s*(?:which\s+specific|specific\s+reference|"
    r"refer(?:s|ring)?\s+to)\b|\n\s*(?:script\s+running|result)\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_DOCUMENT_RE = re.compile(
    r"\b(?P<number>D\d{9})\b.*?"
    r"\bRev(?:ision)?[ ._-]*(?P<revision>[A-Za-z0-9]+)\b",
    re.IGNORECASE | re.DOTALL,
)
_TRAILING_TESTCASE_ID_RE = re.compile(r"_(?P<testcase_id>\d{5,6})$")
_TESTCASE_ID_RE = re.compile(r"(?<!\d)(?P<testcase_id>\d{5,6})(?!\d)")
_RELEASE_QUERY = text(
    "SELECT ID, TestcaseID, ProjectName, FilePath, BaseLineName, "
    "ReleaseTime, ReleaseVersion, VersionNumber, ReleaseRevision, "
    "ReleaseVersionFormat, ReleaseDocNumber, ReportLink "
    "FROM atframeworkdb.releasetable "
    "WHERE LOWER(ProjectName) = LOWER(:project_name) "
    "AND TRIM(TestcaseID) = :testcase_id "
    "ORDER BY ReleaseRevision DESC, ReleaseTime DESC, ID DESC"
)
_RELEASE_POLICY_QUERY = text(
    "SELECT ID, TestcaseID, ProjectName, FilePath, ReleaseTime, ReleaseVersion, "
    "VersionNumber, ReleaseRevision, ReleaseVersionFormat, ReleaseDocNumber "
    "FROM atframeworkdb.releasetable "
    "WHERE LOWER(ProjectName) = LOWER(:project_name) ORDER BY ID"
)


@dataclass(frozen=True)
class AutomationReleaseRecord:
    release_id: int
    testcase_id: str
    project_name: str
    script_name: str
    file_path: str
    baseline_name: str
    release_time: str
    release_version: str
    version_number: str
    release_revision: int | None
    release_version_format: str
    document_number: str
    document_revision: str
    report_link: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _string(value: Any) -> str:
    return str(value or "").strip()


def _script_name_from_path(value: Any) -> str:
    return PurePosixPath(_string(value).replace("\\", "/")).stem.strip()


def _document_parts(value: Any) -> tuple[str, str]:
    match = _DOCUMENT_RE.search(_string(value))
    if match is None:
        return "", ""
    return match.group("number").upper(), match.group("revision").upper()


def _script_key(value: Any) -> str:
    return " ".join(_string(value).split()).strip(" ,.;").casefold()


def _script_without_testcase_id(value: Any) -> str:
    return _TRAILING_TESTCASE_ID_RE.sub("", _script_key(value))


def _claimed_script_name(actual: str) -> str:
    match = _SCRIPT_NAME_RE.search(actual)
    return " ".join(match.group(1).split()).strip(" ,.;") if match else ""


def _claimed_testcase_ids(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            match.group("testcase_id")
            for match in _TESTCASE_ID_RE.finditer(value)
        )
    )


def _claimed_testcase_id(script_name: str) -> str:
    testcase_ids = _claimed_testcase_ids(script_name)
    return testcase_ids[0] if testcase_ids else ""


def _html_script_name(path: str, testcase_id: str) -> str:
    stem = PureWindowsPath(path).stem.strip()
    without_fragment = re.sub(r"_[2-9]\d*$", "", stem)
    if testcase_id in _claimed_testcase_ids(without_fragment):
        return without_fragment
    return stem


def _script_name_match(left: str, right: str) -> str:
    left_key = _script_key(left)
    right_key = _script_key(right)
    if not left_key or not right_key:
        return "uncertain"
    if left_key == right_key:
        return "exact"
    left_without_id = _script_without_testcase_id(left_key)
    right_without_id = _script_without_testcase_id(right_key)
    if left_without_id and (
        right_without_id.endswith(left_without_id)
        or left_without_id.endswith(right_without_id)
    ):
        return "compatible"
    return "mismatch"


def load_automation_releases(
    db: Session,
    project_name: str,
    testcase_id: str,
) -> tuple[AutomationReleaseRecord, ...]:
    rows = db.execute(
        _RELEASE_QUERY,
        {"project_name": project_name.strip(), "testcase_id": testcase_id.strip()},
    ).mappings()
    releases: list[AutomationReleaseRecord] = []
    for row in rows:
        document_number, document_revision = _document_parts(row["ReleaseDocNumber"])
        release_time = row["ReleaseTime"]
        releases.append(
            AutomationReleaseRecord(
                release_id=int(row["ID"]),
                testcase_id=_string(row["TestcaseID"]),
                project_name=_string(row["ProjectName"]),
                script_name=_script_name_from_path(row["FilePath"]),
                file_path=_string(row["FilePath"]),
                baseline_name=_string(row["BaseLineName"]),
                release_time=(
                    release_time.isoformat(sep=" ")
                    if isinstance(release_time, datetime)
                    else _string(release_time)
                ),
                release_version=_string(row["ReleaseVersion"]),
                version_number=_string(row["VersionNumber"]),
                release_revision=(
                    int(row["ReleaseRevision"])
                    if row["ReleaseRevision"] is not None
                    else None
                ),
                release_version_format=_string(row["ReleaseVersionFormat"]),
                document_number=document_number,
                document_revision=document_revision,
                report_link=_string(row["ReportLink"]),
            )
        )
    return tuple(releases)


def automation_release_policy_snapshot(
    db: Session,
    project_name: str,
) -> dict[str, Any]:
    if not project_name.strip():
        return {"status": "disabled"}
    try:
        rows = [
            {
                key: (
                    value.isoformat(sep=" ")
                    if isinstance(value, datetime)
                    else value
                )
                for key, value in row.items()
            }
            for row in db.execute(
                _RELEASE_POLICY_QUERY,
                {"project_name": project_name.strip()},
            ).mappings()
        ]
    except Exception:
        return {"status": "unavailable"}
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


def assess_automation_release(
    *,
    actual: str,
    html_paths: tuple[str, ...],
    project_name: str,
    testcase_id: str,
    releases: tuple[AutomationReleaseRecord, ...],
    lookup_error: str = "",
) -> dict[str, Any]:
    project_name = project_name.strip()
    testcase_id = testcase_id.strip()
    if not project_name:
        return {
            "status": "disabled",
            "project_name": "",
            "testcase_id": testcase_id,
            "candidate_count": 0,
            "claimed_script_name": "",
            "claimed_script_testcase_id": "",
            "html_script_names": [],
            "html_path_testcase_ids": [],
            "html_path_testcase_match": "not_checked",
            "actual_name_match": "not_checked",
            "failure_code": "",
            "claimed_document_number": "",
            "claimed_document_revision": "",
            "selected_release": None,
            "script_name_match": "not_checked",
            "document_match": "not_checked",
            "reason": "未配置自动化发布记录校验。",
        }

    claimed_script_name = _claimed_script_name(actual)
    claimed_script_testcase_ids = _claimed_testcase_ids(actual)
    claimed_script_testcase_id = (
        testcase_id
        if testcase_id in claimed_script_testcase_ids
        else claimed_script_testcase_ids[0]
        if claimed_script_testcase_ids
        else ""
    )
    html_script_names = list(
        dict.fromkeys(
            _html_script_name(path, testcase_id)
            for path in html_paths
            if path.strip()
        )
    )
    path_testcase_ids = list(
        dict.fromkeys(
            path_testcase_id
            for path in html_paths
            for path_testcase_id in html_path_testcase_ids(path)
        )
    )
    if not path_testcase_ids:
        html_path_testcase_match = "not_checked"
    elif all(value == testcase_id for value in path_testcase_ids):
        html_path_testcase_match = "exact"
    else:
        html_path_testcase_match = "mismatch"
    actual_name_matches = [
        _script_name_match(claimed_script_name, html_name)
        for html_name in html_script_names
    ]
    if not claimed_script_name:
        actual_name_match = "missing"
    elif actual_name_matches and all(
        match == "exact" for match in actual_name_matches
    ):
        actual_name_match = "exact"
    else:
        actual_name_match = "mismatch"
    claimed_document_number, claimed_document_revision = _document_parts(actual)
    base = {
        "project_name": project_name,
        "testcase_id": testcase_id,
        "candidate_count": len(releases),
        "claimed_script_name": claimed_script_name,
        "claimed_script_testcase_id": claimed_script_testcase_id,
        "html_script_names": html_script_names,
        "html_path_testcase_ids": path_testcase_ids,
        "html_path_testcase_match": html_path_testcase_match,
        "actual_name_match": actual_name_match,
        "failure_code": "",
        "claimed_document_number": claimed_document_number,
        "claimed_document_revision": claimed_document_revision,
        "selected_release": releases[0].as_dict() if releases else None,
    }
    if not claimed_script_testcase_id:
        return {
            **base,
            "status": "mismatch",
            "failure_code": "actual_testcase_id_missing",
            "script_name_match": "mismatch",
            "document_match": "not_checked",
            "reason": "Actual 未包含连续的 5 位或 6 位数字 Testcase ID。",
        }
    if claimed_script_testcase_id != testcase_id:
        claimed_ids = ", ".join(claimed_script_testcase_ids)
        return {
            **base,
            "status": "mismatch",
            "failure_code": "actual_testcase_id_mismatch",
            "script_name_match": "mismatch",
            "document_match": "not_checked",
            "reason": (
                f"Actual 中记录的 Testcase ID 为 {claimed_ids}，"
                f"但 ALM Test ID 为 {testcase_id}。"
            ),
        }
    if not html_script_names:
        return {
            **base,
            "status": "mismatch",
            "failure_code": "html_script_missing",
            "script_name_match": "not_checked",
            "document_match": "not_checked",
            "reason": "Actual 未提供可用于发布校验的 HTML 报告文件名。",
        }
    script_name_match = "not_checked"
    if releases and releases[0].testcase_id == testcase_id:
        release_script_matches = [
            _script_name_match(html_name, releases[0].script_name)
            for html_name in html_script_names
        ]
        if release_script_matches and all(
            match == "exact" for match in release_script_matches
        ):
            script_name_match = "exact"
        elif release_script_matches and all(
            match in {"exact", "compatible"} for match in release_script_matches
        ):
            script_name_match = "compatible"
        else:
            script_name_match = "mismatch"
    if html_path_testcase_match == "mismatch":
        return {
            **base,
            "status": "mismatch",
            "failure_code": "html_path_testcase_id_mismatch",
            "script_name_match": script_name_match,
            "document_match": "not_checked",
            "reason": (
                "HTML 报告路径中的 Testcase ID 目录 "
                f"{', '.join(path_testcase_ids)} 与 ALM Test ID "
                f"{testcase_id} 不一致。"
            ),
        }
    if actual_name_match == "missing":
        return {
            **base,
            "status": "mismatch",
            "failure_code": "actual_name_missing",
            "script_name_match": script_name_match,
            "document_match": "not_checked",
            "reason": "Actual 缺少必需的自动化脚本 Name 声明。",
        }
    if actual_name_match == "mismatch":
        return {
            **base,
            "status": "mismatch",
            "failure_code": "actual_name_html_mismatch",
            "script_name_match": script_name_match,
            "document_match": "not_checked",
            "reason": "Actual 的 Name 声明与引用的 HTML 报告文件名不一致。",
        }
    if lookup_error:
        return {
            **base,
            "status": "unavailable",
            "script_name_match": "uncertain",
            "document_match": "uncertain",
            "reason": "无法查询自动化发布数据库。",
        }
    if not releases:
        return {
            **base,
            "status": "not_found",
            "failure_code": "release_not_found",
            "script_name_match": "not_found",
            "document_match": "not_found",
            "reason": (
                f"在已配置项目中未找到 ALM Test ID {testcase_id} 对应的"
                "已发布自动化脚本。"
            ),
        }

    selected = releases[0]
    if selected.testcase_id != testcase_id:
        return {
            **base,
            "status": "mismatch",
            "failure_code": "release_testcase_id_mismatch",
            "script_name_match": "not_checked",
            "document_match": "not_checked",
            "reason": (
                f"Release Table Testcase ID 为 {selected.testcase_id}，"
                f"但 ALM Test ID 为 {testcase_id}。"
            ),
        }
    document_complete = bool(claimed_document_number and claimed_document_revision)
    document_matches = document_complete and (
        claimed_document_number == selected.document_number
        and claimed_document_revision == selected.document_revision
    )
    document_match = (
        "exact"
        if document_matches
        else "mismatch"
        if document_complete
        else "uncertain"
    )
    if document_match == "mismatch":
        status = "mismatch"
        failure_code = "document_mismatch"
        reason = "Actual 中声明的验证文档编号或版本与发布记录不一致。"
    elif script_name_match == "mismatch":
        status = "needs_ai"
        failure_code = ""
        reason = "HTML 报告脚本名称无法确定性匹配，需要与已发布脚本名称进行语义比对。"
    elif not document_complete:
        status = "incomplete"
        failure_code = "document_incomplete"
        reason = "Actual 未包含完整的已发布验证文档引用。"
    else:
        status = "matched"
        failure_code = ""
        reason = "ALM Test ID、自动化脚本名称和验证文档均与发布记录一致。"
    return {
        **base,
        "status": status,
        "failure_code": failure_code,
        "script_name_match": script_name_match,
        "document_match": document_match,
        "reason": reason,
    }
