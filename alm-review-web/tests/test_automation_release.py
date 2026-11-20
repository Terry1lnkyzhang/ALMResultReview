from datetime import datetime

from app.services.automation_release import (
    AutomationReleaseRecord,
    assess_automation_release,
    effective_step_execution_at,
)


def release(
    *,
    release_id: int = 1295,
    script_name: str = "CT-NMP.SRS.Fun.321_34941",
    testcase_id: str = "34941",
    document_number: str = "D002513563",
    document_revision: str = "B",
    release_revision: int = 1,
    release_time: str = "2026-07-24 17:17:00",
) -> AutomationReleaseRecord:
    return AutomationReleaseRecord(
        release_id=release_id,
        testcase_id=testcase_id,
        project_name="Earth_Kylin",
        script_name=script_name,
        file_path=f"//server/scripts/{script_name}.yaml",
        baseline_name=f"Earth Formal D002446336 Rev {document_revision}",
        release_time=release_time,
        release_version="Earth_Kylin-24-r1",
        version_number="24",
        release_revision=release_revision,
        release_version_format="project-version-revision-v1",
        document_number=document_number,
        document_revision=document_revision,
        report_link="",
    )


def report_path(script_name: str) -> tuple[str, ...]:
    return (rf"\\server\reports\{script_name}.html",)


def test_release_claim_matches_published_script_and_document() -> None:
    assessment = assess_automation_release(
        actual=(
            "Execute automated test scripts Name: CT-NMP.SRS.Fun.321_34941, "
            "which specific reference to D002513563 Automation Test script "
            "validation documentation RevB."
        ),
        project_name="Earth_Kylin",
        testcase_id="34941",
        releases=(release(),),
        html_paths=report_path("CT-NMP.SRS.Fun.321_34941"),
    )

    assert assessment["status"] == "matched"
    assert assessment["script_name_match"] == "exact"
    assert assessment["document_match"] == "exact"
    assert assessment["selected_release"]["release_id"] == 1295


def test_release_claim_matches_testcase_id_directory_in_html_path() -> None:
    script_name = "CT-NMP.SRS.Fun.147_iDose Level under iBrain_48142"
    assessment = assess_automation_release(
        actual=(
            f"Execute automated test scripts Name: {script_name}, "
            "which specific reference to D002513563 validation documentation RevB."
        ),
        project_name="Earth_Kylin",
        testcase_id="48142",
        releases=(release(script_name=script_name, testcase_id="48142"),),
        html_paths=(
            rf"\\code1\dfscle\BUSINESS\VandV\CT-SysVer\Earth\48142"
            rf"\0. Common Config\{script_name}.html",
        ),
    )

    assert assessment["status"] == "matched"
    assert assessment["html_path_testcase_ids"] == ["48142"]
    assert assessment["html_path_testcase_match"] == "exact"


def test_release_claim_rejects_testcase_id_directory_mismatch() -> None:
    script_name = "CT-NMP.SRS.Fun.147_iDose Level under iBrain_48142"
    assessment = assess_automation_release(
        actual=(
            f"Execute automated test scripts Name: {script_name}, "
            "which specific reference to D002513563 validation documentation RevB."
        ),
        project_name="Earth_Kylin",
        testcase_id="48142",
        releases=(release(script_name=script_name, testcase_id="48142"),),
        html_paths=(
            rf"\\code1\dfscle\BUSINESS\VandV\CT-SysVer\Earth\48143"
            rf"\0. Common Config\{script_name}.html",
        ),
    )

    assert assessment["status"] == "mismatch"
    assert assessment["failure_code"] == "html_path_testcase_id_mismatch"
    assert assessment["html_path_testcase_ids"] == ["48143"]
    assert assessment["html_path_testcase_match"] == "mismatch"
    assert assessment["script_name_match"] == "exact"


def test_release_claim_accepts_testcase_id_before_script_suffix() -> None:
    script_name = "CT-NMP.SRS.Fun.50[SRS][iStation]TiltAngle_34526-left-right"
    assessment = assess_automation_release(
        actual=(
            f"Execute automated test scripts Name: {script_name}, "
            "which specific reference to D002513563 validation documentation RevB."
        ),
        project_name="Earth_Kylin",
        testcase_id="34526",
        releases=(release(script_name=script_name, testcase_id="34526"),),
        html_paths=report_path(script_name),
    )

    assert assessment["status"] == "matched"
    assert assessment["claimed_script_testcase_id"] == "34526"
    assert assessment["script_name_match"] == "exact"


def test_release_claim_accepts_testcase_id_elsewhere_in_actual() -> None:
    html_path = (
        r"\\server\reports\CT-NMP.SRS.Fun.147_iDose Level under iBrain_48142.html"
    )
    assessment = assess_automation_release(
        actual=(
            "Execute automated test scripts Name: FUN.174.iDose4 level under iBrain, "
            "which specific reference to D002513563 validation documentation RevB.\n"
            "Saved screenshot: refer to automation test report\n"
            + html_path
        ),
        project_name="Earth_Kylin",
        testcase_id="48142",
        releases=(
            release(
                script_name="CT-NMP.SRS.Fun.147_iDose Level under iBrain_48142",
                testcase_id="48142",
            ),
        ),
        html_paths=(html_path,),
    )

    assert assessment["claimed_script_testcase_id"] == "48142"
    assert assessment["status"] == "mismatch"
    assert assessment["failure_code"] == "actual_name_html_mismatch"
    assert assessment["actual_name_match"] == "mismatch"
    assert assessment["script_name_match"] == "exact"


def test_release_claim_rejects_missing_name_with_matching_html() -> None:
    html_path = r"\\server\reports\CT-NMP.SRS.Fun.321_34941.html"
    assessment = assess_automation_release(
        actual=(
            "Executed the released automation and referenced "
            f"D002513563 RevB.\n{html_path}"
        ),
        project_name="Earth_Kylin",
        testcase_id="34941",
        releases=(release(),),
        html_paths=(html_path,),
    )

    assert assessment["status"] == "mismatch"
    assert assessment["failure_code"] == "actual_name_missing"
    assert assessment["actual_name_match"] == "missing"
    assert assessment["script_name_match"] == "exact"


def test_missing_name_failure_precedes_release_lookup_failure() -> None:
    html_path = r"\\server\reports\CT-NMP.SRS.Fun.321_34941.html"
    assessment = assess_automation_release(
        actual=f"D002513563 RevB.\n{html_path}",
        project_name="Earth_Kylin",
        testcase_id="34941",
        releases=(),
        lookup_error="database unavailable",
        html_paths=(html_path,),
    )

    assert assessment["status"] == "mismatch"
    assert assessment["failure_code"] == "actual_name_missing"


def test_release_claim_distinguishes_release_script_mismatch() -> None:
    html_path = r"\\server\reports\CT-NMP.SRS.Fun.999_34941.html"
    assessment = assess_automation_release(
        actual=(
            "Execute automated test scripts Name: CT-NMP.SRS.Fun.999_34941, "
            "which specific reference to D002513563 validation documentation RevB.\n"
            + html_path
        ),
        project_name="Earth_Kylin",
        testcase_id="34941",
        releases=(release(),),
        html_paths=(html_path,),
    )

    assert assessment["status"] == "needs_ai"
    assert assessment["failure_code"] == ""
    assert assessment["actual_name_match"] == "exact"
    assert assessment["script_name_match"] == "mismatch"


def test_release_claim_selects_alm_id_from_multiple_script_candidates() -> None:
    script_name = "CT-NMP.SRS.Fun.103075_103074-left-right"
    assessment = assess_automation_release(
        actual=(
            f"Execute automated test scripts Name: {script_name}, "
            "which specific reference to D002513563 validation documentation RevB."
        ),
        project_name="Earth_Kylin",
        testcase_id="103074",
        releases=(release(script_name=script_name, testcase_id="103074"),),
        html_paths=report_path(script_name),
    )

    assert assessment["status"] == "matched"
    assert assessment["claimed_script_testcase_id"] == "103074"


def test_release_claim_extracts_only_five_or_six_digit_numeric_runs() -> None:
    assessment = assess_automation_release(
        actual=(
            "Execute automated test scripts Name: ScriptA48142B, "
            "which specific reference to D002513563 validation documentation RevB. "
            "Tracking number: 1234567."
        ),
        project_name="Earth_Kylin",
        testcase_id="48142",
        releases=(release(script_name="ScriptA48142B", testcase_id="48142"),),
        html_paths=report_path("ScriptA48142B"),
    )

    assert assessment["status"] == "matched"
    assert assessment["claimed_script_testcase_id"] == "48142"


def test_release_claim_rejects_short_name_that_differs_from_html_filename() -> None:
    assessment = assess_automation_release(
        actual=(
            "Execute automated test scripts Name: Fun.321_34941, which specific "
            "reference to D002513563 validation documentation Rev B."
        ),
        project_name="Earth_Kylin",
        testcase_id="34941",
        releases=(release(),),
        html_paths=report_path("CT-NMP.SRS.Fun.321_34941"),
    )

    assert assessment["status"] == "mismatch"
    assert assessment["failure_code"] == "actual_name_html_mismatch"
    assert assessment["actual_name_match"] == "mismatch"
    assert assessment["script_name_match"] == "exact"


def test_release_claim_selects_historical_version_effective_at_execution() -> None:
    script_name = "CT-NMP.SRS.Risk.412_48276"
    releases = (
        release(
            release_id=2718,
            script_name=script_name,
            testcase_id="48276",
            document_revision="C",
            release_time="2026-09-17 15:52:00",
        ),
        release(
            release_id=2340,
            script_name=script_name,
            testcase_id="48276",
            document_revision="B",
            release_time="2026-08-03 15:03:00",
        ),
    )
    actual_rev_b = (
        f"Execute automated test scripts Name: {script_name}, "
        "which specific reference to D002513563 validation documentation RevB."
    )

    historical = assess_automation_release(
        actual=actual_rev_b,
        project_name="Earth_Kylin",
        testcase_id="48276",
        releases=releases,
        html_paths=report_path(script_name),
        execution_at=datetime(2026, 8, 21, 15, 18, 6),
    )
    after_rev_c = assess_automation_release(
        actual=actual_rev_b,
        project_name="Earth_Kylin",
        testcase_id="48276",
        releases=releases,
        html_paths=report_path(script_name),
        execution_at=datetime(2026, 9, 18, 10, 0, 0),
    )

    assert historical["status"] == "matched"
    assert historical["selected_release"]["release_id"] == 2340
    assert historical["selected_release"]["document_revision"] == "B"
    assert after_rev_c["status"] == "mismatch"
    assert after_rev_c["failure_code"] == "document_mismatch"
    assert after_rev_c["selected_release"]["release_id"] == 2718


def test_release_claim_rejects_execution_before_earliest_release_time() -> None:
    script_name = "CT-NMP.SRS.Risk.412_48276"
    assessment = assess_automation_release(
        actual=(
            f"Execute automated test scripts Name: {script_name}, "
            "which specific reference to D002513563 validation documentation RevB."
        ),
        project_name="Earth_Kylin",
        testcase_id="48276",
        releases=(
            release(
                release_id=2340,
                script_name=script_name,
                testcase_id="48276",
                document_revision="B",
                release_time="2026-08-03 15:03:00",
            ),
        ),
        html_paths=report_path(script_name),
        execution_at=datetime(2026, 8, 1, 9, 0, 0),
    )

    assert assessment["status"] == "mismatch"
    assert assessment["failure_code"] == "release_not_effective_yet"


def test_effective_step_execution_at_prefers_step_timestamp_over_run() -> None:
    step = {"execution_date": "2026-08-21", "execution_time": "10:18:06"}
    resolved = effective_step_execution_at(
        step,
        datetime(2026, 9, 20, 10, 0, 0),
    )

    assert resolved is not None
    assert resolved.date().isoformat() == "2026-08-21"


def test_release_claim_rejects_script_name_without_testcase_id() -> None:
    assessment = assess_automation_release(
        actual=(
            "Execute automated test scripts Name: Fun.321, which specific "
            "reference to D002513563 validation documentation Rev B."
        ),
        project_name="Earth_Kylin",
        testcase_id="34941",
        releases=(release(),),
        html_paths=report_path("Fun.321_34941"),
    )

    assert assessment["status"] == "mismatch"
    assert assessment["claimed_script_testcase_id"] == ""
    assert "未包含连续的 5 位或 6 位数字 Testcase ID" in assessment["reason"]


def test_release_claim_rejects_release_table_testcase_id_conflict() -> None:
    conflicting = release()
    object.__setattr__(conflicting, "testcase_id", "34942")
    assessment = assess_automation_release(
        actual=(
            "Execute automated test scripts Name: CT-NMP.SRS.Fun.321_34941, "
            "which specific reference to D002513563 validation documentation RevB."
        ),
        project_name="Earth_Kylin",
        testcase_id="34941",
        releases=(conflicting,),
        html_paths=report_path("CT-NMP.SRS.Fun.321_34941"),
    )

    assert assessment["status"] == "mismatch"
    assert "Release Table Testcase ID 为 34942" in assessment["reason"]


def test_release_claim_rejects_explicit_testcase_id_conflict() -> None:
    assessment = assess_automation_release(
        actual=(
            "Execute automated test scripts Name: CT-NMP.SRS.Fun.321_103075, "
            "which specific reference to D002513563 validation documentation RevB."
        ),
        project_name="Earth_Kylin",
        testcase_id="103074",
        releases=(release(script_name="CT-NMP.SRS.Fun.321_103074"),),
        html_paths=report_path("CT-NMP.SRS.Fun.321_103075"),
    )

    assert assessment["status"] == "mismatch"
    assert assessment["claimed_script_testcase_id"] == "103075"
    assert "103074" in assessment["reason"]


def test_release_claim_rejects_document_revision_conflict() -> None:
    assessment = assess_automation_release(
        actual=(
            "Execute automated test scripts Name: CT-NMP.SRS.Fun.321_34941, "
            "which specific reference to D002513563 validation documentation RevC."
        ),
        project_name="Earth_Kylin",
        testcase_id="34941",
        releases=(release(),),
        html_paths=report_path("CT-NMP.SRS.Fun.321_34941"),
    )

    assert assessment["status"] == "mismatch"
    assert assessment["document_match"] == "mismatch"
    assert assessment["failure_code"] == "document_mismatch"
    assert assessment["claimed_document_revision"] == "C"


def test_release_claim_rejects_document_number_conflict() -> None:
    assessment = assess_automation_release(
        actual=(
            "Execute automated test scripts Name: CT-NMP.SRS.Fun.321_34941, "
            "which specific reference to D002513564 validation documentation RevB."
        ),
        project_name="Earth_Kylin",
        testcase_id="34941",
        releases=(release(),),
        html_paths=report_path("CT-NMP.SRS.Fun.321_34941"),
    )

    assert assessment["status"] == "mismatch"
    assert assessment["failure_code"] == "document_mismatch"
    assert assessment["claimed_document_number"] == "D002513564"
    assert assessment["selected_release"]["document_number"] == "D002513563"


def test_release_claim_without_document_reference_requires_manual_review() -> None:
    assessment = assess_automation_release(
        actual=(
            "Execute automated test scripts Name: CT-NMP.SRS.Fun.321_34941\n"
            "Script running completed, result passed."
        ),
        project_name="Earth_Kylin",
        testcase_id="34941",
        releases=(release(),),
        html_paths=report_path("CT-NMP.SRS.Fun.321_34941"),
    )

    assert assessment["status"] == "incomplete"
    assert assessment["failure_code"] == "document_incomplete"
    assert assessment["document_match"] == "uncertain"
    assert assessment["claimed_document_number"] == ""


def test_release_claim_requires_document_revision_not_only_number() -> None:
    assessment = assess_automation_release(
        actual=(
            "Execute automated test scripts Name: CT-NMP.SRS.Fun.321_34941, "
            "which specific reference to D002513563 validation documentation."
        ),
        project_name="Earth_Kylin",
        testcase_id="34941",
        releases=(release(),),
        html_paths=report_path("CT-NMP.SRS.Fun.321_34941"),
    )

    assert assessment["status"] == "incomplete"
    assert assessment["failure_code"] == "document_incomplete"
    assert assessment["claimed_document_revision"] == ""


def test_nonmatching_script_name_without_id_conflict_requires_ai_review() -> None:
    script_name = "Axial-slice thickness-1.0_34941"
    assessment = assess_automation_release(
        actual=(
            f"Execute automated test scripts Name: {script_name}, "
            "which specific reference to D002513563 validation documentation RevB."
        ),
        project_name="Earth_Kylin",
        testcase_id="34941",
        releases=(release(),),
        html_paths=report_path(script_name),
    )

    assert assessment["status"] == "needs_ai"
    assert assessment["script_name_match"] == "mismatch"


def test_release_lookup_failure_requires_manual_review() -> None:
    assessment = assess_automation_release(
        actual="Execute automated test scripts Name: CT-NMP.SRS.Fun.321_34941",
        project_name="Earth_Kylin",
        testcase_id="34941",
        releases=(),
        lookup_error="permission denied",
        html_paths=report_path("CT-NMP.SRS.Fun.321_34941"),
    )

    assert assessment["status"] == "unavailable"
    assert "无法查询自动化发布数据库" in assessment["reason"]


def test_release_claim_strips_two_digit_html_fragments_and_yaml_extension() -> None:
    script_name = "CT-NMP.SRS.PhyChar.7[PhyChar]Low contrast resolution_35163"
    html_paths = tuple(
        rf"\\server\reports\{script_name}"
        + ("" if idx == 1 else f"_{idx}")
        + ".html"
        for idx in range(1, 11)
    )
    assessment = assess_automation_release(
        actual=(
            f"Execute automated test scripts Name: {script_name.lower()}.yaml, "
            "which specific reference to D001841128 Automation Test script "
            "validation documentation RevI."
        ),
        project_name="Taichi",
        testcase_id="35163",
        releases=(
            release(
                script_name=script_name,
                testcase_id="35163",
                document_number="D001841128",
                document_revision="I",
            ),
        ),
        html_paths=html_paths,
    )

    assert assessment["html_script_names"] == [script_name]
    assert assessment["actual_name_match"] == "exact"
    assert assessment["status"] == "matched"


def test_release_not_found_is_distinct_from_lookup_failure() -> None:
    assessment = assess_automation_release(
        actual="Execute automated test scripts Name: CT-NMP.SRS.Fun.321_34941",
        project_name="Earth_Kylin",
        testcase_id="34941",
        releases=(),
        html_paths=report_path("CT-NMP.SRS.Fun.321_34941"),
    )

    assert assessment["status"] == "not_found"


def test_release_claim_treats_numbered_html_fragments_as_one_script() -> None:
    script_name = "CT-NMP.SRS.Fun.321_34941"
    assessment = assess_automation_release(
        actual=(
            f"Execute automated test scripts Name: {script_name}, "
            "which specific reference to D002513563 validation documentation RevB."
        ),
        project_name="Earth_Kylin",
        testcase_id="34941",
        releases=(release(),),
        html_paths=(
            rf"\\server\reports\{script_name}.html",
            rf"\\server\reports\{script_name}_2.html",
        ),
    )

    assert assessment["status"] == "matched"
    assert assessment["html_script_names"] == [script_name]
    assert assessment["actual_name_match"] == "exact"
