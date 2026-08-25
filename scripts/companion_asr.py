#!/usr/bin/env python3
"""Local-only Chinese stock voice transcription for the Decision Center companion."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lexicon(root: Path, extra: Path | None) -> list[str]:
    context_terms: set[str] = set()
    terms: set[str] = {
        "集合竞价", "涨停", "跌停", "封板", "炸板", "承接", "分歧", "弱转强", "强更强",
        "北向资金", "主力资金", "龙虎榜", "量化", "成交额", "换手率", "情绪周期",
    }
    for relative in ("data/state/10_THEME_STATE.csv", "data/state/11_STOCK_STATE.csv", "data/logs/12_OPPORTUNITY_LOG.csv"):
        path = root / relative
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                for value in row.values():
                    if value and 2 <= len(value.strip()) <= 24:
                        terms.add(value.strip())
    if extra and extra.exists():
        context = extra.read_text(encoding="utf-8")
        context_terms.update(
            token for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,24}", context)
            if not token.isdigit() or len(token) == 6
        )
    prioritized = sorted(context_terms, key=lambda value: (-len(value), value))
    general = sorted(terms - context_terms, key=lambda value: (-len(value), value))
    return (prioritized + general)[:350]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--context-file")
    parser.add_argument("--model", default="Systran/faster-whisper-medium")
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    args = parser.parse_args()
    source, output, root = Path(args.audio).resolve(), Path(args.output).resolve(), Path(args.project_root).resolve()
    if not source.is_file():
        raise SystemExit(f"audio not found: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise SystemExit(f"refusing to overwrite: {output}")
    from faster_whisper import WhisperModel

    words = lexicon(root, Path(args.context_file) if args.context_file else None)
    initial_prompt = "以下是当前A股研判对话的口述。结合当前任务上下文优先识别股票、题材和交易术语，但不要凭上下文补写没有说出的内容。候选词包括：" + "、".join(words[:180])
    requested = "cuda" if args.device in {"auto", "cuda"} else "cpu"
    device, compute_type = requested, "float16" if requested == "cuda" else "int8"
    fallback: str | None = None
    try:
        model = WhisperModel(args.model, device=device, compute_type=compute_type, local_files_only=True)
    except Exception as exc:
        if args.device == "cuda":
            raise
        fallback = str(exc)[-500:]
        device, compute_type = "cpu", "int8"
        model = WhisperModel(args.model, device=device, compute_type=compute_type, local_files_only=True)
    segments, info = model.transcribe(
        str(source), language="zh", beam_size=5, vad_filter=True, word_timestamps=True,
        initial_prompt=initial_prompt, condition_on_previous_text=False,
    )
    rendered: list[dict[str, Any]] = []
    for segment in segments:
        rendered.append({
            "start": round(segment.start, 3), "end": round(segment.end, 3), "text": segment.text.strip(),
            "avg_logprob": segment.avg_logprob, "no_speech_prob": segment.no_speech_prob,
            "words": [{"start": round(word.start, 3), "end": round(word.end, 3), "word": word.word, "probability": word.probability} for word in (segment.words or [])],
        })
    raw = "".join(item["text"] for item in rendered).strip()
    result = {
        "contract": "companion-asr/v1", "source": str(source), "source_sha256": sha256(source),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "engine": "faster-whisper", "model": args.model, "device": device, "compute_type": compute_type,
        "local_files_only": True, "language": info.language, "language_probability": info.language_probability,
        "settings": {"beam_size": 5, "vad_filter": True, "word_timestamps": True, "condition_on_previous_text": False},
        "lexicon": words, "fallback": fallback, "raw_text": raw, "corrected_text": raw,
        "correction_note": "未做语义臆测；需由用户确认的疑似同音词保留在原始转写。", "segments": rendered,
    }
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "text": raw, "device": device}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
