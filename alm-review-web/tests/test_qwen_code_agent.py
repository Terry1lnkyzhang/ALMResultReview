import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.models import AiConfig
from app.services.qwen_code_agent import (
    MCP_TOOLS,
    _validate_output,
    build_review_bundle,
    run_shadow_review,
)
from app.services.qwen_code_mcp import create_server


def _case():
    return {
        "run_id": "158337",
        "run_status": "Passed",
        "steps": [
            {
                "review_step": 1,
                "order": "1",
                "description": "Record the ECG simulator SN.",
                "expected": "ECG simulator SN:__",
                "actual": "ECG simulator SN: PCCSY-RD-CT-1-0175",
            },
            {
                "review_step": 2,
                "order": "2",
                "description": "Set the ECG simulator HR to 60 BPM.",
                "expected": "The ECG waveform is displayed.",
                "actual": "The ECG waveform was displayed.",
            },
        ],
    }


def test_review_bundle_lists_only_applicable_checks_and_uses_frozen_evidence():
    context = SimpleNamespace(
        content=_case(),
        location_assessment={"status": "manual", "reason": "History unknown."},
        evidence=SimpleNamespace(
            external_review_enabled=False,
            results={},
            html_results={},
        ),
        equipment_enabled=False,
        equipment_checks=[],
    )
    parsed = {
        "verdict": "needs_manual_review",
        "criteria": {"expected_vs_actual": {"status": "manual"}},
        "step_results": [
            {"review_step": 1, "status": "pass", "issues": []},
            {"review_step": 2, "status": "manual", "issues": [{"type": "expected_actual"}]},
        ],
    }

    bundle = build_review_bundle(context, parsed)

    assert bundle["target_step"] == 2
    assert {item["id"] for item in bundle["checks"]} == {
        "text:2", "routing:2", "location", "aggregation"
    }
    assert bundle["steps"][0]["actual"] == _case()["steps"][0]["actual"]
    assert bundle["location"]["status"] == "manual"
    assert "reports" not in bundle["checks"]


def test_read_only_mcp_server_is_limited_to_the_frozen_run(tmp_path):
    bundle_path = tmp_path / "case.json"
    bundle_path.write_text(
        json.dumps({
            "run_status": "Passed",
            "checks": [{"id": "text:1", "tool": "get_step_text"}],
            "steps": [{"review_step": 1, "actual": "SN: DEMO-12345"}],
            "location": {"status": "not_applicable"},
            "original_review": {"verdict": "needs_manual_review"},
        }),
        encoding="utf-8",
    )

    async def probe():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(create_server.__code__.co_filename)), str(bundle_path)],
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {item.name for item in tools.tools} == set(MCP_TOOLS)
                step_tool = next(tool for tool in tools.tools if tool.name == "get_step_text")
                assert set(step_tool.inputSchema["properties"]) == {"review_step"}
                found = await session.call_tool("get_step_text", {"review_step": 1})
                missing = await session.call_tool("get_step_text", {"review_step": 99})
                assert "DEMO-12345" in str(found.content)
                assert "Unknown review step" in str(missing.content)
                assert not hasattr(session, "query_database")

    anyio.run(probe)


def test_image_bytes_are_only_exposed_through_the_dedicated_mcp_tool(tmp_path):
    encoded = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
        "AAAAC0lEQVR42mP8/x8AAwMCAO+jR70AAAAASUVORK5CYII="
    )
    bundle_path = tmp_path / "case.json"
    bundle_path.write_text(
        json.dumps({
            "run_status": "Passed", "checks": [], "location": {}, "original_review": {},
            "steps": [{
                "review_step": 1,
                "actual": "See screenshot.",
                "images_truncated": False,
                "images": [{
                    "status": "ready", "source_kind": "approved_root",
                    "media": [{
                        "name": "Step1.png", "sha256": "a" * 64, "size_bytes": 68,
                        "media_type": "image/png", "data_url": f"data:image/png;base64,{encoded}",
                    }],
                }],
            }],
        }),
        encoding="utf-8",
    )

    async def probe():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(create_server.__code__.co_filename)), str(bundle_path)],
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                text = await session.call_tool("get_step_text", {"review_step": 1})
                images = await session.call_tool("get_step_images", {"review_step": 1})
                assert encoded not in str(text.content)
                assert encoded not in images.content[0].text
                assert any(item.type == "image" and item.data == encoded
                           for item in images.content)

    anyio.run(probe)


def test_check_result_cannot_claim_supported_with_another_checks_quote(monkeypatch):
    monkeypatch.setattr("app.services.qwen_code_agent.shutil.which", lambda _name: "qwen.cmd")

    def fake_run(command, **kwargs):
        directory = Path(kwargs["cwd"])
        case = json.loads((directory / "case.json").read_text(encoding="utf-8"))
        (directory / "tool_activity.jsonl").write_text(
            "".join(
                json.dumps({"tool": entry["tool"], "step": (
                    case["target_step"] if ":" in entry["id"] else None
                )}) + "\n"
                for entry in case["checks"]
            ) + json.dumps({"tool": "get_review_context", "step": None}) + "\n",
            encoding="utf-8",
        )
        payload = {
            "review_step": 2,
            "assessment": "uncertain",
            "reason": "The location assertion cannot be verified.",
            "citations": [{"step": 1, "field": "actual", "quote": "ECG simulator SN:"}],
            "check_results": [
                {
                    "id": item["id"],
                    "status": "supported" if item["id"] == "location" else "uncertain",
                }
                for item in case["checks"]
            ],
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr("app.services.qwen_code_agent.subprocess.run", fake_run)
    result = run_shadow_review(
        _case(),
        [{"review_step": 2, "status": "manual", "issues": []}],
        {"status": "ready", "selected_config": {"dms_coverage": "4cm"}},
        AiConfig(model_name="qwen3", base_url="http://localhost:4000/v1/chat/completions"),
    )
    assert result["status"] == "invalid_output"


def test_qwen_shadow_uses_read_only_headless_cli_and_cites_prior_step(monkeypatch):
    monkeypatch.setattr(
        "app.services.qwen_code_agent.shutil.which", lambda _name: "qwen.cmd"
    )

    def fake_run(command, **kwargs):
        assert "--bare" in command
        assert command[command.index("--approval-mode") + 1] == "plan"
        assert "read_file" not in command[command.index("--allowed-tools") + 1]
        excluded = command[command.index("--exclude-tools") + 1]
        assert {"run_shell_command", "edit", "write_file", "agent", "read_file"} <= set(
            excluded.split(",")
        )
        assert "--max-tool-calls" in command
        assert "--max-wall-time" in command
        assert command[command.index("--max-tool-calls") + 1] == "16"
        assert command[command.index("--max-wall-time") + 1] == "180s"
        assert "--mcp-config" in command
        mcp = json.loads(command[command.index("--mcp-config") + 1])
        assert list(mcp["mcpServers"]) == ["alm-review"]
        assert mcp["mcpServers"]["alm-review"]["includeTools"]
        assert mcp["mcpServers"]["alm-review"]["command"]
        assert mcp["mcpServers"]["alm-review"]["env"]["OPENAI_API_KEY"] == ""
        assert "--json-schema" in command
        schema = json.loads(command[command.index("--json-schema") + 1])
        assert schema["properties"]["citations"]["minItems"] == 1
        assert "DATABASE_URL" not in kwargs["env"]
        assert "ALM_PASSWORD" not in kwargs["env"]
        assert "AI_API_KEY" not in kwargs["env"]
        directory = Path(kwargs["cwd"])
        case = json.loads((directory / "case.json").read_text(encoding="utf-8"))
        assert case["target_step"] == 2
        assert {item["id"] for item in case["checks"]} == {
            "text:2", "routing:2", "location", "aggregation"
        }
        assert not (directory / "steps").exists()
        assert "PCCSY-RD-CT-1-0175" in case["steps"][0]["actual"]
        (directory / "tool_activity.jsonl").write_text(
            "".join(
                json.dumps({"tool": name, "step": step}) + "\n"
                for name, step in (
                    ("get_review_context", None),
                    ("get_step_text", 2),
                    ("get_evidence_routes", 2),
                    ("get_location_assessment", None),
                    ("get_existing_review", None),
                )
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "review_step": 2,
                    "assessment": "uncertain",
                    "reason": "The prior Step records a simulator ID; continuity needs checking.",
                    "citations": [
                        {
                            "step": 1,
                            "field": "actual",
                            "quote": "ECG simulator SN: PCCSY-RD-CT-1-0175",
                        }
                    ],
                    "check_results": [
                        {"id": check_id, "status": "uncertain", "citations": []}
                        for check_id in ("text:2", "routing:2", "location", "aggregation")
                    ],
                }
            ),
            "",
        )

    monkeypatch.setattr("app.services.qwen_code_agent.subprocess.run", fake_run)
    result = run_shadow_review(
        _case(),
        [
            {"review_step": 1, "status": "pass", "issues": []},
            {
                "review_step": 2,
                "status": "manual",
                "issues": [{"type": "equipment", "status": "manual", "summary": "Check device."}],
            },
        ],
        {"status": "ready", "selected_config": {"dms_coverage": "4cm"}},
        AiConfig(model_name="qwen3", base_url="http://localhost:4000/v1/chat/completions"),
    )

    assert result["status"] == "completed"
    assert result["assessment"] == "uncertain"
    assert result["target_step"] == 2
    assert result["citations"][0]["step"] == 1


def test_qwen_shadow_unavailable_without_cli(monkeypatch):
    monkeypatch.setattr("app.services.qwen_code_agent.shutil.which", lambda _name: None)
    result = run_shadow_review(
        _case(),
        [{"review_step": 2, "status": "manual", "issues": [{"type": "equipment"}]}],
        {},
        AiConfig(model_name="qwen3"),
    )

    assert result["status"] == "unavailable"


def test_qwen_shadow_rejects_unverifiable_citation(monkeypatch):
    monkeypatch.setattr("app.services.qwen_code_agent.shutil.which", lambda _name: "qwen.cmd")
    monkeypatch.setattr(
        "app.services.qwen_code_agent.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "review_step": 2,
                    "assessment": "supported",
                    "reason": "A made-up ID supports the Step.",
                    "citations": [{"step": 1, "field": "actual", "quote": "SN: NOT-IN-ALM"}],
                }
            ),
            "",
        ),
    )

    result = run_shadow_review(
        _case(),
        [{"review_step": 2, "status": "manual", "issues": [{"type": "equipment"}]}],
        {},
        AiConfig(model_name="qwen3", base_url="http://localhost:4000/v1/chat/completions"),
    )

    assert result["status"] == "invalid_output"


def test_qwen_shadow_rejects_extra_status_field(monkeypatch):
    monkeypatch.setattr("app.services.qwen_code_agent.shutil.which", lambda _name: "qwen.cmd")
    monkeypatch.setattr(
        "app.services.qwen_code_agent.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "review_step": 2,
                    "assessment": "uncertain",
                    "reason": "Need a reviewer.",
                    "citations": [
                        {"step": 1, "field": "actual", "quote": "ECG simulator SN:"}
                    ],
                    "status": "completed",
                }
            ),
            "",
        ),
    )
    result = run_shadow_review(
        _case(),
        [{"review_step": 2, "status": "manual", "issues": []}],
        {},
        AiConfig(model_name="qwen3", base_url="http://localhost:4000/v1/chat/completions"),
    )

    assert result["status"] == "invalid_output"


def test_qwen_shadow_skips_without_manual_step(monkeypatch):
    monkeypatch.setattr(
        "app.services.qwen_code_agent.shutil.which",
        lambda _name: (_ for _ in ()).throw(AssertionError("CLI should not start")),
    )
    result = run_shadow_review(_case(), [{"review_step": 2, "status": "pass"}], {}, AiConfig())

    assert result["status"] == "not_applicable"


def test_qwen_shadow_rejects_non_json_response(monkeypatch):
    monkeypatch.setattr("app.services.qwen_code_agent.shutil.which", lambda _name: "qwen.cmd")
    monkeypatch.setattr(
        "app.services.qwen_code_agent.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "plain text", ""),
    )

    result = run_shadow_review(
        _case(),
        [{"review_step": 2, "status": "manual", "issues": []}],
        {},
        AiConfig(model_name="qwen3", base_url="http://localhost:4000/v1/chat/completions"),
    )

    assert result["status"] == "invalid_output"


def test_truncated_report_cannot_support_a_confident_check():
    bundle = {
        "target_step": 2,
        "checks": [{"id": "report:2", "tool": "get_html_reports"}],
        "steps": [{
            "review_step": 2,
            "reports_truncated": False,
            "reports": [{
                "report_id": "report-1",
                "status": "ready",
                "blocks_truncated": True,
                "blocks": [{"block_id": "result-1", "text": "Actual: 60 BPM recorded"}],
            }],
        }],
    }
    output = {
        "review_step": 2,
        "assessment": "supported",
        "reason": "The report supports this result.",
        "citations": [{"step": 2, "field": "actual", "quote": "60 BPM"}],
        "check_results": [{
            "id": "report:2",
            "status": "supported",
            "citations": [{
                "source": "html:2:report-1:result-1",
                "quote": "Actual: 60 BPM recorded",
            }],
        }],
    }
    activity = [
        {"tool": "get_review_context", "step": None},
        {"tool": "get_html_reports", "step": 2},
    ]
    assert not _validate_output(output, 2, {2: {"actual": "60 BPM"}}, bundle, activity)
    bundle["steps"][0]["reports"][0]["blocks_truncated"] = False
    assert _validate_output(output, 2, {2: {"actual": "60 BPM"}}, bundle, activity)
    assert not _validate_output(output, 2, {2: {"actual": "60 BPM"}}, bundle, activity[:1])
    bundle["steps"][0]["reports"].append({
        "report_id": "report-2",
        "status": "ready",
        "blocks_truncated": False,
        "blocks": [{"block_id": "result-2", "text": "Actual: waveform visible"}],
    })
    assert not _validate_output(output, 2, {2: {"actual": "60 BPM"}}, bundle, activity)


def test_incomplete_image_set_cannot_justify_visual_conclusion():
    bundle = {
        "target_step": 2,
        "checks": [{"id": "image:2", "tool": "get_step_images"}],
        "steps": [{
            "review_step": 2, "images_truncated": True,
            "images": [{
                "status": "ready",
                "media": [{"sha256": "a" * 64, "data_url": "data:image/png;base64,AA=="}],
            }],
        }],
    }
    output = {
        "review_step": 2, "assessment": "supported", "reason": "The image looks sufficient.",
        "citations": [{"step": 2, "field": "actual", "quote": "screenshot"}],
        "check_results": [{
            "id": "image:2", "status": "supported",
            "citations": [{"source": f"image:2:{'a' * 64}", "quote": "a" * 64}],
        }],
    }
    activity = [
        {"tool": "get_review_context", "step": None},
        {"tool": "get_step_images", "step": 2},
    ]
    assert not _validate_output(output, 2, {2: {"actual": "screenshot"}}, bundle, activity)
    bundle["steps"][0]["images_truncated"] = False
    assert _validate_output(output, 2, {2: {"actual": "screenshot"}}, bundle, activity)