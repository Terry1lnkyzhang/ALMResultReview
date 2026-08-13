import json

from app.services.rich_text import render_alm_rich_text, snapshot_step_fields


def test_render_alm_rich_text_preserves_table_structure() -> None:
    source = (
        "<table><thead><tr><th>Parameter</th><th>Value</th></tr></thead>"
        "<tbody><tr><td rowspan='2'>Voltage</td><td>220 V</td></tr></tbody></table>"
    )

    rendered = str(render_alm_rich_text(source))

    assert "<table>" in rendered
    assert "<th>Parameter</th>" in rendered
    assert '<td rowspan="2">Voltage</td>' in rendered


def test_render_alm_rich_text_removes_executable_markup() -> None:
    source = (
        "<table onclick='alert(1)'><tr><td style='color:red'>Result</td></tr></table>"
        "<script>alert(2)</script><a href='javascript:alert(3)'>open</a>"
    )

    rendered = str(render_alm_rich_text(source))

    assert "<table>" in rendered
    assert "Result" in rendered
    assert "onclick" not in rendered
    assert "style=" not in rendered
    assert "<script" not in rendered
    assert "alert(2)" not in rendered
    assert "javascript:" not in rendered


def test_snapshot_step_fields_recovers_rich_text_by_step_id() -> None:
    snapshot = json.dumps(
        {
            "run": {
                "steps": [
                    {
                        "id": "42",
                        "description": "Description",
                        "expected": "<table><tr><td>Expected</td></tr></table>",
                        "actual": "Actual",
                    }
                ]
            }
        }
    )

    fields = snapshot_step_fields(snapshot)

    assert fields[42]["description"] == "Description"
    assert "<table>" in fields[42]["expected"]