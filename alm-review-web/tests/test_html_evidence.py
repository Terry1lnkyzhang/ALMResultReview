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


def test_html_result_rows_pass_only_when_every_value_is_passed(tmp_path: Path) -> None:
    report = tmp_path / "report.html"
    write_report(report, ["Passed", "Passed"])

    result = HtmlEvidenceResolver().collect(report)

    assert result.status == "pass"
    assert result.result_count == 2
    assert result.non_passed_values == ()


def test_html_result_row_reports_any_non_passed_value(tmp_path: Path) -> None:
    report = tmp_path / "report.html"
    write_report(report, ["Passed", "Failed"])

    result = HtmlEvidenceResolver().collect(report)

    assert result.status == "fail"
    assert result.non_passed_values == ("Failed",)


def test_html_result_row_does_not_treat_blank_value_as_passed(tmp_path: Path) -> None:
    report = tmp_path / "report.html"
    write_report(report, ["Passed", ""])

    result = HtmlEvidenceResolver().collect(report)

    assert result.status == "fail"
    assert result.non_passed_values == ("<empty>",)