from pathlib import Path

from app.services.html_evidence import HtmlEvidenceResolver


def write_report(path: Path, result_values: list[str]) -> None:
    values = "".join(f"<td>{value}</td>" for value in result_values)
    path.write_text(
        "<html><body><table>"
        "<tr><td>P</td><td>&gt; 0.10</td><td>&gt; 0.10</td></tr>"
        f"<tr><td>Result (Passed/Failed)</td>{values}</tr>"
        "</table></body></html>",
        encoding="utf-8",
    )


def test_html_collects_visible_text_without_deciding_the_result(tmp_path: Path) -> None:
    report = tmp_path / "report.html"
    write_report(report, ["Passed", "Failed"])

    result = HtmlEvidenceResolver().collect(report)

    assert result.status == "ready"
    assert result.size_bytes == report.stat().st_size
    assert len(result.sha256) == 64
    visible_text = "\n".join(block.text for block in result.blocks)
    assert "Result (Passed/Failed)" in visible_text
    assert "Passed" in visible_text
    assert "Failed" in visible_text


def test_html_collect_ignores_executable_and_styling_content(tmp_path: Path) -> None:
    report = tmp_path / "report.html"
    report.write_text(
        "<html><style>hidden style</style><script>hidden script</script>"
        "<body><p>Visible result</p></body></html>",
        encoding="utf-8",
    )

    result = HtmlEvidenceResolver().collect(report)

    visible_text = "\n".join(block.text for block in result.blocks)
    assert result.status == "ready"
    assert visible_text == "Visible result"


def test_html_without_visible_text_is_not_ready_for_ai_review(tmp_path: Path) -> None:
    report = tmp_path / "report.html"
    report.write_text("<html><script>only script</script></html>", encoding="utf-8")

    result = HtmlEvidenceResolver().collect(report)

    assert result.status == "no_visible_text"
    assert result.blocks == ()


def test_html_extracts_structured_result_data_without_executing_script(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.html"
    report.write_text(
        "<html><body><p>Empty report shell</p>"
        '<script>var unrelated = "do not include";</script>'
        '<script>var resultData = {"testName":"Case A","testcaseFailed":0,'
        '"testStepsFail":0,"testResult":[{"stepName":"Case A_1",'
        '"description":"Verify value","expect":"Value is 5",'
        '"actual":"Value was 5","status":"Pass",'
        '"evidence":"../result/step1.jpg"}]};</script></body></html>',
        encoding="utf-8",
    )

    result = HtmlEvidenceResolver().collect(report)

    assert result.status == "ready"
    assert [block.block_id for block in result.blocks] == [
        "report-summary",
        "test-result-1",
    ]
    text = "\n".join(block.text for block in result.blocks)
    assert "testcaseFailed: 0" in text
    assert "description: Verify value" in text
    assert "status: Pass" in text
    assert "do not include" not in text
    assert "Empty report shell" not in text


def test_html_fallback_maps_automation_result_relative_path(tmp_path: Path) -> None:
    fallback_report = tmp_path / "34839" / "Config" / "result.html"
    fallback_report.parent.mkdir(parents=True)
    fallback_report.write_text("<p>Result: Passed</p>", encoding="utf-8")
    resolver = HtmlEvidenceResolver()

    source = resolver._fallback_source(
        r"\\server\approved\SystemVerificationAutomaionResult\34839\Config\result.html",
        r"\\server\approved",
        str(tmp_path),
    )

    assert source == fallback_report


def test_html_fallback_also_supports_a_full_approved_root_mirror(tmp_path: Path) -> None:
    fallback_report = (
        tmp_path / "Evidence" / "OtherReports" / "48153" / "result.html"
    )
    fallback_report.parent.mkdir(parents=True)
    fallback_report.write_text("<p>Result: Passed</p>", encoding="utf-8")
    resolver = HtmlEvidenceResolver()

    source = resolver._fallback_source(
        r"\\server\approved\Evidence\OtherReports\48153\result.html",
        r"\\server\approved",
        str(tmp_path),
    )

    assert source == fallback_report


def test_html_resolves_report_quoted_with_a_non_breaking_space(tmp_path: Path) -> None:
    report = tmp_path / "34943" / "0. Common Config" / "Artery  and Vein_34943.html"
    report.parent.mkdir(parents=True)
    report.write_text("<p>Result: Passed</p>", encoding="utf-8")
    quoted = (
        "\\\\server\\approved\\SystemVerificationAutomaionResult\\34943"
        "\\0. Common Config\\Artery\u00a0 and Vein_34943.html"
    )

    result = HtmlEvidenceResolver().resolve(
        quoted,
        allowed_root=r"\\server\approved",
        fallback_root=str(tmp_path),
    )

    assert result.status == "ready"


def test_html_stays_missing_when_whitespace_match_is_ambiguous(tmp_path: Path) -> None:
    folder = tmp_path / "34943"
    folder.mkdir()
    for name in ("Artery  and Vein_34943.html", "Artery and Vein_34943.html"):
        (folder / name).write_text("<p>Result: Passed</p>", encoding="utf-8")
    quoted = (
        "\\\\server\\approved\\SystemVerificationAutomaionResult\\34943"
        "\\Artery\u00a0 and Vein_34943.html"
    )

    result = HtmlEvidenceResolver().resolve(
        quoted,
        allowed_root=r"\\server\approved",
        fallback_root=str(tmp_path),
    )

    assert result.status == "missing"