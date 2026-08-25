from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import uuid


VOICE = "zh-TW-HsiaoYuNeural"
RATE = "-6%"
PITCH = "+22Hz"
VOLUME = "+0%"


async def synthesize(text: str, output_path: Path) -> None:
    import edge_tts

    token = uuid.uuid4().hex
    raw_path = output_path.with_name(f".{output_path.name}.{token}.raw.mp3")
    processed_path = output_path.with_name(f".{output_path.name}.{token}.processed.mp3")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        communicate = edge_tts.Communicate(
            text=text,
            voice=VOICE,
            rate=RATE,
            pitch=PITCH,
            volume=VOLUME,
        )
        await communicate.save(str(raw_path))

        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path:
            subprocess.run(
                [
                    ffmpeg_path,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(raw_path),
                    "-af",
                    "volume=4dB",
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    "96k",
                    str(processed_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.replace(processed_path, output_path)
        else:
            os.replace(raw_path, output_path)
    finally:
        raw_path.unlink(missing_ok=True)
        processed_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    text = args.text_file.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("TTS text is empty")
    asyncio.run(synthesize(text, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
