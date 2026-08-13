from __future__ import annotations

import json
from html import escape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from markupsafe import Markup

ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "caption",
    "code",
    "col",
    "colgroup",
    "div",
    "em",
    "hr",
    "i",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "span",
    "strong",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
VOID_TAGS = {"br", "col", "hr"}
TABLE_SPAN_ATTRIBUTES = {"colspan", "rowspan"}
ALLOWED_LINK_SCHEMES = {"http", "https", "mailto"}
DISCARDED_CONTENT_TAGS = {"iframe", "object", "script", "style", "template"}


def _safe_link(value: str) -> str | None:
    stripped = value.strip()
    if not stripped:
        return None
    parsed = urlparse(stripped)
    if parsed.scheme.casefold() not in ALLOWED_LINK_SCHEMES:
        return None
    return stripped


def source_rich_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def snapshot_step_fields(snapshot_json: str) -> dict[int, dict[str, str]]:
    try:
        snapshot = json.loads(snapshot_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    steps = (snapshot.get("run") or {}).get("steps") or []
    fields_by_id: dict[int, dict[str, str]] = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        try:
            step_id = int(step.get("id"))
        except (TypeError, ValueError):
            continue
        fields_by_id[step_id] = {
            "description": source_rich_text(
                step.get("descriptionText", step.get("description"))
            ),
            "expected": source_rich_text(
                step.get("expectedText", step.get("expected"))
            ),
            "actual": source_rich_text(step.get("actualText", step.get("actual"))),
        }
    return fields_by_id


class _RichTextSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.discarded_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.casefold()
        if tag in DISCARDED_CONTENT_TAGS:
            self.discarded_depth += 1
            return
        if self.discarded_depth:
            return
        if tag not in ALLOWED_TAGS:
            return
        clean_attrs: list[tuple[str, str]] = []
        for name, value in attrs:
            name = name.casefold()
            value = value or ""
            if tag in {"td", "th"} and name in TABLE_SPAN_ATTRIBUTES:
                if value.isdigit() and 1 <= int(value) <= 100:
                    clean_attrs.append((name, value))
            elif tag == "a" and name == "href":
                safe_value = _safe_link(value)
                if safe_value is not None:
                    clean_attrs.append((name, safe_value))
            elif tag == "a" and name == "title":
                clean_attrs.append((name, value))
        if tag == "a":
            clean_attrs.append(("rel", "noopener noreferrer"))
        rendered_attrs = "".join(
            f' {name}="{escape(value, quote=True)}"' for name, value in clean_attrs
        )
        self.parts.append(f"<{tag}{rendered_attrs}>")

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in DISCARDED_CONTENT_TAGS and self.discarded_depth:
            self.discarded_depth -= 1
            return
        if self.discarded_depth:
            return
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.discarded_depth and (data.strip() or "\n" not in data):
            self.parts.append(escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self.discarded_depth:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.discarded_depth:
            self.parts.append(f"&#{name};")


def render_alm_rich_text(value: object) -> Markup:
    parser = _RichTextSanitizer()
    parser.feed(str(value or ""))
    parser.close()
    return Markup("".join(parser.parts))