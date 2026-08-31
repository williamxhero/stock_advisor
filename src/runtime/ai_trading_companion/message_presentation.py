from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$")
_LIST = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+)$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_FIELD = re.compile(r"^\s*([a-z][a-z0-9_]{1,40})\s*:\s*(.+?)\s*$", re.I)
_ISO_DAY = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?:[T\s][0-2]\d:[0-5]\d(?::[0-5]\d)?(?:\.\d+)?(?:Z|[+-][0-2]\d:?\d\d)?)?(?!\d)")
_URL = re.compile(r"https?://[^\s)>]+")

_ENUM_WORDS = {
    "next_trading_session": "下一个交易日",
    "same_trading_session": "今天这段交易时间",
    "short_term": "短线",
    "medium_term": "中期",
    "long_term": "长期",
}


@dataclass(frozen=True)
class PresentedMessage:
    """The sealed, user-visible form of one companion message.

    Model output is evidence for an attempt.  This object is the separate
    presentation contract that gets sealed into the immutable artifact.
    """

    markdown: str
    parts: tuple[dict[str, Any], ...]
    contract_version: int = 1

    def metadata(self) -> dict[str, Any]:
        return {
            "presentation": {
                "version": self.contract_version,
                "parts": list(self.parts),
            }
        }


def present_message(
    text: str,
    *,
    as_of: str,
    kind: str,
    allow_structured_format: bool = False,
) -> PresentedMessage:
    """Prepare a message for a chat bubble without changing its judgment.

    This is deliberately deterministic and conservative: it removes only the
    report scaffolding that has no investment meaning, keeps emphasis and
    links, and keeps externally attributable quotations in Markdown blocks.
    """
    del kind  # kept in the public contract so future kinds cannot bypass it.
    source = str(text or "").replace("\r\n", "\n").strip()
    if not source:
        source = "我这次没有形成可发布的内容。"
    speech, materials = _split_parts(source)
    parts: list[dict[str, Any]] = []
    rendered: list[str] = []
    if speech:
        natural = _naturalize_speech(speech, as_of, allow_structured_format)
        if natural:
            rendered.append(natural)
            parts.append({"kind": "speech", "markdown": natural})
    for material in materials:
        excerpt = _bound_material(material)
        rendered.append(excerpt)
        url = _first_url(excerpt)
        parts.append({"kind": "material", "markdown": excerpt, "source_url": url})
    if not rendered:
        natural = _naturalize_speech(source, as_of, allow_structured_format)
        rendered.append(natural)
        parts.append({"kind": "speech", "markdown": natural})
    return PresentedMessage("\n\n".join(rendered), tuple(parts))


def explicit_format_requested(text: str) -> bool:
    """A request for three risks is content; an explicit list/table is layout."""
    compact = re.sub(r"\s+", "", text or "")
    return bool(re.search(r"(?:用|按|给我)(?:markdown)?(?:列表|表格|项目符号|编号)", compact, re.I))


def _split_parts(text: str) -> tuple[str, list[str]]:
    speech: list[str] = []
    materials: list[str] = []
    current: list[str] = []
    in_quote = False
    for line in text.split("\n"):
        quoted = line.lstrip().startswith(">")
        if quoted and not in_quote:
            if current:
                speech.extend(current)
                current = []
            in_quote = True
        if not quoted and in_quote:
            materials.append("\n".join(current).strip())
            current = []
            in_quote = False
        current.append(line)
    if current:
        (materials if in_quote else speech).append("\n".join(current).strip())

    # A fenced block is a forwarded excerpt even when a provider did not add
    # quote markers.  It remains visibly distinct in the desktop Markdown UI.
    chunks = "\n".join(speech).split("```")
    if len(chunks) > 1:
        speech = []
        for index, chunk in enumerate(chunks):
            if not chunk.strip():
                continue
            if index % 2:
                materials.append(f"```\n{chunk.strip()}\n```")
            else:
                speech.append(chunk.strip())
    attributed = [item for item in materials if _first_url(item)]
    # A quote without a source is still a claim made by the companion.  It
    # cannot borrow the looser material formatting simply by adding `>`.
    for item in materials:
        if item not in attributed:
            speech.append("\n".join(line.lstrip()[1:].lstrip() if line.lstrip().startswith(">") else line for line in item.split("\n")))
    return "\n".join(speech).strip(), attributed


def _naturalize_speech(text: str, as_of: str, allow_structured_format: bool) -> str:
    paragraphs: list[str] = []
    list_items: list[str] = []
    table_headers: list[str] | None = None
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            if list_items:
                paragraphs.append(_join_list_items(list_items, allow_structured_format))
                list_items = []
            continue
        if line.startswith("```"):
            continue
        heading = _HEADING.match(line)
        if heading:
            line = heading.group(1)
        if _TABLE_RULE.match(line):
            continue
        if "|" in line and line.count("|") >= 2:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if table_headers is None:
                table_headers = cells
                continue
            pairs = [f"{key}是{value}" for key, value in zip(table_headers, cells) if value]
            paragraphs.append("，".join(pairs) + "。")
            continue
        item = _LIST.match(line)
        if item:
            if allow_structured_format:
                list_items.append("- " + _humanize(item.group(1), as_of))
            else:
                list_items.append(_humanize(item.group(1), as_of))
            continue
        if list_items:
            paragraphs.append(_join_list_items(list_items, allow_structured_format))
            list_items = []
        field = _FIELD.match(line)
        if field:
            key, value = field.groups()
            if key.lower() in {"task_key", "stage", "protocol", "status", "model", "token"}:
                continue
            if key.lower() == "time_scope":
                line = value
        paragraphs.append(_humanize(line, as_of))
    if list_items:
        paragraphs.append(_join_list_items(list_items, allow_structured_format))
    result = "\n\n".join(item.strip() for item in paragraphs if item.strip())
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def _join_list_items(items: list[str], allow_structured_format: bool) -> str:
    if allow_structured_format:
        return "\n".join(items)
    if len(items) == 1:
        return items[0]
    labels = ("一是", "二是", "三是", "四是", "五是")
    return "，".join(f"{labels[index] if index < len(labels) else '另外'}{item}" for index, item in enumerate(items))


def _humanize(text: str, as_of: str) -> str:
    result = text.strip()
    for raw, spoken in _ENUM_WORDS.items():
        result = re.sub(rf"\b{re.escape(raw)}\b", spoken, result)
    result = _ISO_DAY.sub(lambda match: _human_day(match, as_of), result)
    return result


def _human_day(match: re.Match[str], as_of: str) -> str:
    try:
        day = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date()
        reference = datetime.fromisoformat(as_of.replace("Z", "+00:00")).date()
    except ValueError:
        return match.group(0)
    if day == reference:
        return "今天"
    return f"{day.month}月{day.day}日"


def _bound_material(markdown: str) -> str:
    max_characters = 1_200
    if len(markdown) <= max_characters:
        return markdown
    url = _first_url(markdown)
    suffix = f"\n\n原文较长，我先摘到这里。{url or ''}".rstrip()
    return markdown[: max_characters - len(suffix)].rstrip() + suffix


def _first_url(text: str) -> str | None:
    markdown = re.search(r"\]\((https?://[^)]+)\)", text)
    if markdown:
        return markdown.group(1)
    plain = _URL.search(text)
    return plain.group(0) if plain else None
