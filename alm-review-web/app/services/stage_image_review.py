"""Stage 4 (`image_review`): network/attachment image preparation, batching, and AI review."""
from __future__ import annotations

from dataclasses import replace
import re
from typing import Any

import httpx

from app.models import AiConfig, EvidenceConfig
from app.services.ai_transport import ai_headers, completion_url, skill_failure
from app.services.image_evidence import (
    IMAGE_TRANSPORT_MAX_BYTES,
    IMAGE_TRANSPORT_MAX_PIXELS,
    ImageEvidenceResult,
    NetworkImageResolver,
    ResolvedImage,
    embedded_image,
    optimize_images_for_transport,
)
from app.services.review_types import (
    PreparedImageEvidence,
    ReviewContext,
    _append_step_issue,
    _clip_step_text,
    _evidence_actions,
    _recalculate_result,
    _routed_paths,
)
from app.services.skill_runner import skill_runner


def _image_audit_metadata(image: ResolvedImage) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "name": image.relative_name,
        "media_type": image.media_type,
        "size_bytes": image.size_bytes,
        "width": image.width,
        "height": image.height,
        "sha256": image.sha256,
    }
    if image.transport_optimized:
        metadata["transport_optimized"] = True
        metadata["original"] = {
            "media_type": image.original_media_type,
            "size_bytes": image.original_size_bytes,
            "width": image.original_width,
            "height": image.original_height,
            "sha256": image.original_sha256,
        }
    return metadata


# Image evidence statuses that raise an issue, mapped to severity, issue type, and text.
# Any status missing here is deliberately silent; `tests/test_image_evidence.py` pins the set.
_IMAGE_EVIDENCE_ISSUES: dict[str, tuple[str, str, str]] = {
    "missing": ("fail", "path", "配置的证据路径不存在。"),
    "no_images": ("fail", "screenshot", "证据文件夹中没有可评审的图像。"),
    "invalid_image": (
        "manual",
        "screenshot",
        "图像文件损坏或内容不完整，未送交 AI 评审；请修复原始证据并人工复核。",
    ),
    "no_usable_images": (
        "fail",
        "screenshot",
        "证据文件夹中没有可评审的图像。",
    ),
    "no_matching_images": (
        "fail",
        "screenshot",
        "证据文件夹中存在图像，但没有文件名与该步骤匹配。",
    ),
    "ambiguous_step_mapping": (
        "manual",
        "screenshot",
        "多个步骤共用一个证据文件夹，且图像文件名不含步骤标记，无法可靠匹配。",
    ),
    "denied": ("manual", "path", "证据文件夹暂时无法读取。"),
    "unavailable": ("manual", "path", "证据文件夹暂时无法读取。"),
    "outside_root": (
        "manual",
        "path",
        "证据路径包含链接或重解析点，无法确认路径始终位于批准的根目录内。",
    ),
    "transport_too_large": (
        "manual",
        "screenshot",
        "证据图像存在，但超过当前 AI 接口可接受的大小。",
    ),
    "budget_exhausted": (
        "manual",
        "screenshot",
        "图像证据数量或大小达到评审上限，该证据未检查，需人工复核。",
    ),
    "not_checked": (
        "manual",
        "screenshot",
        "图像证据已请求检查，但没有检查结果，需人工复核。",
    ),
}


def _prepare_image_evidence(
    content: dict[str, Any],
    evidence_config: EvidenceConfig | None,
) -> PreparedImageEvidence:
    external_review_enabled = bool(
        evidence_config and evidence_config.external_evidence_review_enabled
    )
    results: dict[int, dict[str, ImageEvidenceResult]] = {}
    if not external_review_enabled:
        return PreparedImageEvidence(
            external_review_enabled=False,
            results=results,
        )

    remaining_run_images = 24
    remaining_run_bytes = 15 * 1024 * 1024
    path_review_steps: dict[str, set[int]] = {}
    for step in content.get("steps", []):
        actions = _evidence_actions(step["evidence_profile"])
        if "load_images" not in actions and "review_attachment" not in actions:
            continue
        for path in _routed_paths(step["evidence_profile"]):
            path_review_steps.setdefault(path["raw"].casefold(), set()).add(
                step["review_step"]
            )
    for step in content.get("steps", []):
        actions = _evidence_actions(step["evidence_profile"])
        if "load_images" not in actions and "review_attachment" not in actions:
            continue
        remaining_step_images = 10
        remaining_step_bytes = 10 * 1024 * 1024
        step_order = str(step.get("order") or "").strip()
        matching_step_numbers = {
            int(step_order) if step_order.isdecimal() else step["review_step"]
        }
        step_results: dict[str, ImageEvidenceResult] = {}
        for attachment in step.get("attachment_contents", []):
            attachment_id = str(attachment.get("attachment_id") or "").strip()
            source = f"ALM attachment:{attachment_id or 'unknown'}"
            if (
                remaining_step_images <= 0
                or remaining_run_images <= 0
                or remaining_step_bytes <= 0
                or remaining_run_bytes <= 0
            ):
                step_results[source] = ImageEvidenceResult(
                    status="budget_exhausted",
                    detail="Image count or byte limit exhausted before reading this attachment.",
                    source_kind="alm_attachment",
                )
                continue
            image = embedded_image(
                name=str(attachment.get("name") or source),
                media_type=str(attachment.get("mime_type") or ""),
                data_url=str(attachment.get("data_url") or ""),
                expected_sha256=str(attachment.get("sha256") or ""),
                max_image_bytes=min(
                    10 * 1024 * 1024,
                    remaining_step_bytes,
                    remaining_run_bytes,
                ),
            )
            step_results[source] = ImageEvidenceResult(
                status="ready" if image else "invalid_image",
                images=(image,) if image else (),
                skipped_invalid=0 if image else 1,
                corrupt_images=() if image else (str(attachment.get("name") or source),),
                source_kind="alm_attachment",
            )
            if image:
                remaining_step_images -= 1
                remaining_run_images -= 1
                remaining_step_bytes -= image.size_bytes
                remaining_run_bytes -= image.size_bytes
        for path in _routed_paths(step["evidence_profile"]):
            if path.get("kind") == "html":
                continue
            if (
                remaining_step_images <= 0
                or remaining_run_images <= 0
                or remaining_step_bytes <= 0
                or remaining_run_bytes <= 0
            ):
                step_results[path["raw"]] = ImageEvidenceResult(
                    status="budget_exhausted",
                    detail="Image count or byte limit exhausted before reading this path.",
                    source_kind="not_read",
                )
                continue
            resolver = NetworkImageResolver(
                max_depth=2,
                max_images=min(remaining_step_images, remaining_run_images),
                max_image_bytes=min(
                    10 * 1024 * 1024,
                    remaining_step_bytes,
                    remaining_run_bytes,
                ),
                max_total_bytes=min(remaining_step_bytes, remaining_run_bytes),
                matching_step_numbers=matching_step_numbers,
                require_step_marker=(
                    len(path_review_steps.get(path["raw"].casefold(), set())) > 1
                ),
            )
            result = resolver.resolve(
                path["raw"],
                evidence_config.allowed_network_root,
                evidence_config.local_html_fallback_root,
            )
            step_results[path["raw"]] = result
            image_count = len(result.images)
            image_bytes = sum(image.size_bytes for image in result.images)
            remaining_step_images -= image_count
            remaining_run_images -= image_count
            remaining_step_bytes -= image_bytes
            remaining_run_bytes -= image_bytes
        ready_sources = [
            (source, result)
            for source, result in step_results.items()
            if result.status == "ready" and result.images
        ]
        step_images = tuple(
            image for _, result in ready_sources for image in result.images
        )
        if (
            sum(image.size_bytes for image in step_images) > IMAGE_TRANSPORT_MAX_BYTES
            or sum(image.width * image.height for image in step_images)
            > IMAGE_TRANSPORT_MAX_PIXELS
        ):
            optimized = optimize_images_for_transport(step_images)
            if optimized is None:
                for source, result in ready_sources:
                    step_results[source] = replace(
                        result,
                        status="transport_too_large",
                        detail="Image evidence exceeds the AI endpoint transport budget.",
                    )
            else:
                offset = 0
                for source, result in ready_sources:
                    count = len(result.images)
                    step_results[source] = replace(
                        result,
                        images=optimized[offset : offset + count],
                    )
                    offset += count
                saved_bytes = sum(image.size_bytes for image in step_images) - sum(
                    image.size_bytes for image in optimized
                )
                remaining_run_bytes += max(0, saved_bytes)
        results[step["review_step"]] = step_results
    return PreparedImageEvidence(
        external_review_enabled=True,
        results=results,
    )


def _image_review_batches(
    prepared_evidence: PreparedImageEvidence,
    batch_size: int = 4,
) -> list[list[tuple[int, str, ResolvedImage]]]:
    batches: list[list[tuple[int, str, ResolvedImage]]] = []
    for review_step, step_results in prepared_evidence.results.items():
        step_images = [
            (review_step, source_path, image)
            for source_path, result in step_results.items()
            if result.status == "ready"
            for image in result.images
        ]
        batches.extend(
            step_images[index : index + batch_size]
            for index in range(0, len(step_images), batch_size)
        )
    return batches


_REFERENCED_STEP_RE = re.compile(
    r"(?:\bstep\s*(?P<en>\d{1,3})\b|步骤\s*(?P<zh>\d{1,3}))",
    re.IGNORECASE,
)


def _referenced_step_context(
    review_step: int,
    step_by_number: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    current = step_by_number[review_step]
    combined = "\n".join(
        str(current.get(field) or "")
        for field in ("description", "expected", "actual")
    )
    referenced_numbers = sorted(
        {
            int(match.group("en") or match.group("zh"))
            for match in _REFERENCED_STEP_RE.finditer(combined)
        }
        - {review_step}
    )
    context: list[dict[str, Any]] = []
    for number in referenced_numbers[:4]:
        other = step_by_number.get(number)
        if other is None:
            continue
        context.append(
            {
                "review_step": number,
                "description": _clip_step_text(other.get("description"))[0][:1500],
                "expected": _clip_step_text(other.get("expected"))[0][:1500],
                "actual": _clip_step_text(other.get("actual"))[0][:1500],
            }
        )
    return context


def _run_image_review_skill(
    ai_config: AiConfig,
    batch: list[tuple[int, str, ResolvedImage]],
    content: dict[str, Any],
) -> dict[str, Any]:
    step_by_number = {
        int(step["review_step"]): step for step in content.get("steps", [])
    }
    images_by_step: dict[int, list[dict[str, Any]]] = {}
    media_parts: list[dict[str, Any]] = []
    allowed_media: dict[int, set[str]] = {}
    for index, (review_step, source_path, image) in enumerate(batch, start=1):
        media_id = f"step-{review_step}-{index}-{image.sha256[:12]}"
        images_by_step.setdefault(review_step, []).append(
            {
                "media_id": media_id,
                "source_path": source_path,
                "relative_name": image.relative_name,
                "mime_type": image.media_type,
                "sha256": image.sha256,
                "width": image.width or None,
                "height": image.height or None,
            }
        )
        allowed_media.setdefault(review_step, set()).add(media_id)
        media_parts.extend(
            [
                {"type": "text", "text": f"media_id: {media_id}"},
                {"type": "image_url", "image_url": {"url": image.data_url}},
            ]
        )
    skill_input = {
        "alm_run_status": str(content.get("run_status") or "Passed"),
        "steps": [
            {
                "review_step": review_step,
                "alm_step_status": str(
                    step_by_number[review_step].get("status") or "Passed"
                ),
                "description": _clip_step_text(
                    step_by_number[review_step].get("description")
                )[0],
                "expected": _clip_step_text(
                    step_by_number[review_step].get("expected")
                )[0],
                "actual": _clip_step_text(
                    step_by_number[review_step].get("actual")
                )[0],
                "images": images,
                "referenced_steps": _referenced_step_context(
                    review_step, step_by_number
                ),
            }
            for review_step, images in images_by_step.items()
        ],
    }

    def validate_image_output(
        _validated_input: dict[str, Any], output: dict[str, Any]
    ) -> None:
        for assessment in output["assessments"]:
            review_step = int(assessment["review_step"])
            observed = assessment["observed_media_ids"]
            if len(observed) != len(set(observed)) or set(observed) != allowed_media[
                review_step
            ]:
                raise ValueError(
                    "Image evidence Skill did not exactly cover supplied media "
                    f"for Step {review_step}."
                )

    trace = skill_runner.run(
        "image-evidence-review",
        skill_input,
        endpoint=completion_url(ai_config.base_url),
        model_name=ai_config.model_name,
        headers=ai_headers(ai_config),
        timeout_seconds=ai_config.timeout_seconds,
        granted_capabilities={
            "review.step_text",
            "evidence.image.metadata",
            "evidence.image.content",
        },
        media_parts=media_parts,
        request_post=httpx.post,
        output_validator=validate_image_output,
    )
    if trace.get("status") != "completed":
        if trace.get("failure_kind") == "invalid_output":
            trace["manual_fallback"] = "invalid_output"
            return trace
        raise skill_failure(trace, "Image evidence Skill failed.")
    return trace


def _merge_image_skill_trace(
    text_result: dict[str, Any],
    trace: dict[str, Any],
) -> None:
    for assessment in trace["output"]["assessments"]:
        if assessment["status"] == "pass":
            continue
        target = next(
            step
            for step in text_result["step_results"]
            if step["review_step"] == assessment["review_step"]
        )
        if not any(item["type"] == "screenshot" for item in target["issues"]):
            target["issues"].append(
                {
                    "status": assessment["status"],
                    "type": "screenshot",
                    "summary": assessment["reason"][:200],
                }
            )


def run_image_review_stage(ctx: ReviewContext) -> dict[str, Any]:
    ai_calls = 0
    manual_fallbacks = 0
    for batch in _image_review_batches(ctx.evidence):
        trace = _run_image_review_skill(ctx.ai_config, batch, ctx.content)
        ctx.evidence.image_skill_traces.append(trace)
        ai_calls += int(trace.get("ai_calls", 0))
        if trace.get("manual_fallback") == "invalid_output":
            manual_fallbacks += 1
            for review_step in {step for step, _, _ in batch}:
                _append_step_issue(
                    ctx.text_result,
                    review_step,
                    "manual",
                    "screenshot",
                    "图像 AI 输出未通过格式或证据覆盖校验，未采信模型结论；请人工复核。",
                )
        else:
            _merge_image_skill_trace(ctx.text_result, trace)
    ctx.text_result = _recalculate_result(ctx.text_result)
    if not ctx.evidence.external_review_enabled:
        status = "disabled"
    elif ai_calls:
        status = "completed"
    else:
        status = "not_applicable"
    return {
        "status": status,
        "ai_calls": ai_calls,
        "manual_fallbacks": manual_fallbacks,
        "skills": ctx.evidence.image_skill_traces,
    }
