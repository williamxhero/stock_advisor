from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$")
_LIST = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+)$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_FIELD = re.compile(r"^\s*([a-z\u4e00-\u9fff][a-z0-9_\u4e00-\u9fff]{0,40})\s*[:：]\s*(.+?)\s*$", re.I)
_ISO_DAY = re.compile(r"(?<!\d)(20\d{2})[-‐‑‒–—−](\d{2})[-‐‑‒–—−](\d{2})(?:[T\s]([0-2]\d):([0-5]\d)(?::[0-5]\d)?(?:\.\d+)?(?:Z|[+-][0-2]\d:?\d\d)?)?(?!\d)")
_URL = re.compile(r"https?://[^\s)>]+")

_INTERNAL_FIELDS = {
    "task_key", "stage", "protocol", "reference_at", "model", "token",
    "状态", "status",
}
_REPORT_LABELS = {
    "盘前研判", "盘前结论", "盘中结论", "执行结论", "执行回执", "盘前研究回执",
    "结论", "市场基线", "新增事件", "题材判断", "题材状态", "市场层", "组合处理",
    "持仓处理", "政策材料", "事件记录与证据限制", "组合相关未知项",
}
_INTERNAL_TOKEN = re.compile(
    r"\b(?:time_scope|reference_at|next_trading_session|same_trading_session|"
    r"unqualified|task_key|protocol|token)\b|\bAsia/Shanghai\b|"
    r"\b[A-Za-z][A-Za-z0-9]+-v\d+(?:\.\d+)+\b",
    re.I,
)


class MessageQualificationError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = tuple(problems)
        super().__init__("message qualification failed: " + ", ".join(problems))

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
    kind: str = "ai_chat"
    contract_version: int = 2
    qualification_problems: tuple[str, ...] = ()

    def message(self) -> dict[str, Any]:
        return {
            "contract": "companion-published-message/v2",
            "kind": self.kind,
            "parts": list(self.parts),
            "text_projection": self.markdown,
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "presentation": {
                "version": self.contract_version,
                "parts": list(self.parts),
            },
            "published_message": self.message(),
            "qualification": {
                "state": "passed" if not self.qualification_problems else "rejected",
                "problems": list(self.qualification_problems),
                "gate": "companion-message-qualification/v2",
            },
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
            parts.append({"kind": "speech", "text": natural})
    for material in materials:
        url = _first_url(material)
        title = _source_title(material)
        excerpt = _bound_material(material, title, url)
        rendered.append(excerpt)
        parts.append({
            "kind": "material", "markdown": excerpt,
            "source_title": title, "source_url": url,
        })
    if not rendered:
        natural = _naturalize_speech(source, as_of, allow_structured_format)
        rendered.append(natural)
        parts.append({"kind": "speech", "text": natural})
    problems = _qualification_problems(parts, allow_structured_format)
    if problems:
        raise MessageQualificationError(problems)
    return PresentedMessage("\n\n".join(rendered), tuple(parts), kind=kind)


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
            continue
        normalized_line = _normalized(line)
        if normalized_line in _REPORT_LABELS:
            continue
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
            normalized_key = _normalized(key).lower()
            if normalized_key in {"状态", "status"} and _normalized(value).lower() == "unqualified":
                paragraphs.append("现有信息还不够，我先不下判断。")
                continue
            if normalized_key in _INTERNAL_FIELDS:
                continue
            if normalized_key == "time_scope":
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
    result = re.sub(r"\s*Asia/Shanghai\b", "", result, flags=re.I)
    return result


def _qualification_problems(parts: list[dict[str, Any]], allow_structured_format: bool) -> list[str]:
    problems: list[str] = []
    for part in parts:
        if part.get("kind") != "speech":
            continue
        speech = str(part.get("text") or "")
        normalized = _normalized(speech)
        if _INTERNAL_TOKEN.search(normalized):
            problems.append("internal_token")
        if _ISO_DAY.search(normalized):
            problems.append("machine_date")
        for line in normalized.splitlines():
            stripped = line.strip()
            field = _FIELD.match(stripped)
            if field and ("_" in field.group(1) or _normalized(field.group(1)).lower() in _INTERNAL_FIELDS):
                problems.append("machine_field")
            if stripped in _REPORT_LABELS:
                problems.append("report_label")
            if not allow_structured_format and (_HEADING.match(stripped) or _LIST.match(stripped) or _TABLE_RULE.match(stripped)):
                problems.append("unrequested_structure")
    return list(dict.fromkeys(problems))


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(str.maketrans("‐‑‒–—−", "------"))


def _human_day(match: re.Match[str], as_of: str) -> str:
    try:
        day = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date()
        reference = datetime.fromisoformat(as_of.replace("Z", "+00:00")).date()
    except ValueError:
        return match.group(0)
    delta = (day - reference).days
    if delta == 0:
        spoken = "今天"
    elif delta == 1:
        spoken = "明天"
    elif 1 < delta <= 3:
        spoken = "周" + "一二三四五六日"[day.weekday()]
    else:
        spoken = f"{day.month}月{day.day}日"
    if match.group(4) is not None:
        spoken += _spoken_clock(int(match.group(4)), int(match.group(5)))
    return spoken


def _spoken_clock(hour: int, minute: int) -> str:
    period = "早上" if 5 <= hour < 12 else "下午" if 12 <= hour < 18 else "晚上" if hour >= 18 else "凌晨"
    display_hour = hour if 1 <= hour <= 11 else 12 if hour in {0, 12} else hour - 12
    numerals = "零一二三四五六七八九十"
    spoken_hour = numerals[display_hour] if display_hour <= 10 else "十一" if display_hour == 11 else "十二"
    if minute == 0:
        return f"{period}{spoken_hour}点"
    spoken_minute = str(minute) if minute < 10 else ("十" if minute == 10 else f"{numerals[minute // 10]}十{numerals[minute % 10] if minute % 10 else ''}")
    return f"{period}{spoken_hour}点{spoken_minute}分"


def _bound_material(markdown: str, title: str, url: str | None) -> str:
    max_characters = 1_200
    if len(markdown) <= max_characters:
        return markdown
    if url:
        return f"[查看{title}]({url})"
    raise MessageQualificationError(["long_material_without_source"])


def _source_title(markdown: str) -> str:
    for line in markdown.splitlines():
        candidate = line.strip().lstrip(">").strip().rstrip("：:")
        if not candidate or candidate.startswith(("-", "*", "+", "[", "http")):
            continue
        if len(candidate) <= 40:
            return candidate
    link = re.search(r"\[([^\]]+)\]\(https?://[^)]+\)", markdown)
    return link.group(1) if link else "原文"


def _first_url(text: str) -> str | None:
    markdown = re.search(r"\]\((https?://[^)]+)\)", text)
    if markdown:
        return markdown.group(1)
    plain = _URL.search(text)
    return plain.group(0) if plain else None
