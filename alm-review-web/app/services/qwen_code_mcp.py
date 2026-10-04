"""Per-review MCP server exposing only the prepared, immutable review bundle."""

import json
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent


def create_server(bundle_path: Path) -> FastMCP:
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    activity_path = bundle_path.with_name("tool_activity.jsonl")
    server = FastMCP("alm-review", instructions="Read-only tools for this Run only.")

    def record(name: str, key: int | None = None) -> None:
        with activity_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"tool": name, "step": key}) + "\n")

    def step_entry(review_step: int) -> dict[str, Any]:
        return next(
            (
                item for item in bundle["steps"]
                if item["review_step"] == review_step
            ),
            {"error": "Unknown review step"},
        )

    @server.tool()
    def get_review_context() -> dict[str, Any]:
        """Get the Run's required checks, gates, and approved Step identifiers."""
        record("get_review_context")
        return {
            "run_status": bundle["run_status"],
            "checks": bundle["checks"],
            "review_steps": [step["review_step"] for step in bundle["steps"]],
        }

    @server.tool()
    def get_step_text(review_step: int) -> dict[str, Any]:
        """Read Description, Expected, Actual and recorded issues for one Step."""
        record("get_step_text", review_step)
        step = step_entry(review_step)
        if "error" in step:
            return step
        return {
            key: step[key]
            for key in (
                "review_step", "alm_step_status", "description", "expected",
                "actual", "original_status", "issues",
            )
            if key in step
        }

    @server.tool()
    def get_evidence_routes(review_step: int) -> dict[str, Any]:
        """Read application-approved reference routing, never open a path."""
        record("get_evidence_routes", review_step)
        step = step_entry(review_step)
        return {"routes": step.get("routes", [])} if "error" not in step else step

    @server.tool()
    def get_location_assessment() -> dict[str, Any]:
        """Read the effective location configuration and its review status."""
        record("get_location_assessment")
        return bundle["location"]

    @server.tool()
    def get_step_images(review_step: int) -> list[TextContent | ImageContent]:
        """Read validated images from this Run, without opening arbitrary paths."""
        record("get_step_images", review_step)
        step = step_entry(review_step)
        if "error" in step:
            return [TextContent(type="text", text=step["error"])]
        metadata = []
        media = []
        for evidence in step.get("images", []):
            metadata.append({
                "status": evidence["status"],
                "source_kind": evidence["source_kind"],
                "media": [
                    {key: value for key, value in item.items() if key != "data_url"}
                    for item in evidence["media"]
                ],
            })
            for item in evidence["media"]:
                prefix, separator, encoded = item["data_url"].partition(",")
                if separator and prefix == f"data:{item['media_type']};base64":
                    media.append(ImageContent(
                        type="image", data=encoded, mimeType=item["media_type"]
                    ))
        return [
            TextContent(type="text", text=json.dumps({
                "images": metadata,
                "images_truncated": step.get("images_truncated", False),
            })),
            *media,
        ]

    @server.tool()
    def get_html_reports(review_step: int) -> dict[str, Any]:
        """Read bounded blocks of only already-approved and parsed HTML reports."""
        record("get_html_reports", review_step)
        step = step_entry(review_step)
        return (
            {
                "reports": step.get("reports", []),
                "reports_truncated": step.get("reports_truncated", False),
            }
            if "error" not in step else step
        )

    @server.tool()
    def get_equipment_checks(review_step: int) -> dict[str, Any]:
        """Read bounded registry matches and device questions from this review."""
        record("get_equipment_checks", review_step)
        step = step_entry(review_step)
        return {"equipment": step.get("equipment", [])} if "error" not in step else step

    @server.tool()
    def get_existing_review() -> dict[str, Any]:
        """Read the original verdict and criteria only for independent comparison."""
        record("get_existing_review")
        return bundle["original_review"]

    return server


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Expected an isolated review bundle path")
    create_server(Path(sys.argv[1])).run(transport="stdio")