from __future__ import annotations

import logging
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

logger = logging.getLogger(__name__)

# This file is created under the already-public seed mount. It is deliberately
# an image fallback rather than the source video, so browsers always receive a
# valid poster MIME type even when ffmpeg is not installed.
VIDEO_THUMBNAIL_PLACEHOLDER_URL = "/media/seed/video-placeholder.svg"


@dataclass(frozen=True, slots=True)
class ThumbnailResult:
    url: str
    storage_path: str | None
    mime_type: str
    generated: bool


def ensure_video_placeholder(settings: Settings) -> Path:
    path = settings.data_dir / "media" / "seed" / "video-placeholder.svg"
    if not path.exists():
        path.write_text(
            """<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"1280\" height=\"720\" viewBox=\"0 0 1280 720\">
<rect width=\"1280\" height=\"720\" fill=\"#202735\"/>
<circle cx=\"640\" cy=\"300\" r=\"76\" fill=\"none\" stroke=\"#f97316\" stroke-width=\"8\"/>
<path d=\"M620 255l90 45-90 45z\" fill=\"#f97316\"/>
<text x=\"640\" y=\"500\" text-anchor=\"middle\" fill=\"#f8fafc\" font-size=\"42\" font-family=\"Arial,sans-serif\">FrameFlow video</text>
</svg>""",
            encoding="utf-8",
        )
    return path


def _valid_jpeg(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 128:
        return False
    try:
        with path.open("rb") as stream:
            starts_as_jpeg = stream.read(3) == b"\xff\xd8\xff"
            stream.seek(-2, os.SEEK_END)
            return starts_as_jpeg and stream.read(2) == b"\xff\xd9"
    except (OSError, ValueError):
        return False


def _run_ffmpeg(source: Path, target: Path, timeout_seconds: float) -> None:
    # A fixed 16:9 canvas gives all posters a consistent card shape while
    # preserving aspect ratio and padding narrow/portrait videos safely.
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        "0.1",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-vf",
        "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=0x202735",
        "-q:v",
        "3",
        str(target),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=max(1.0, timeout_seconds),
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "ffmpeg failed").strip()[-500:]
        raise RuntimeError(detail)
    if not _valid_jpeg(target):
        raise RuntimeError("ffmpeg produced an invalid JPEG poster")


def materialize_video_thumbnail(
    source: Path,
    target: Path,
    public_url: str,
    settings: Settings,
) -> ThumbnailResult:
    """Create one durable poster, with a safe image fallback on any failure."""
    ensure_video_placeholder(settings)
    if _valid_jpeg(target):
        return ThumbnailResult(public_url, str(target), "image/jpeg", generated=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp.jpg")
    try:
        _run_ffmpeg(source, temporary, getattr(settings, "thumbnail_timeout", 20.0))
        temporary.replace(target)
        return ThumbnailResult(public_url, str(target), "image/jpeg", generated=True)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        temporary.unlink(missing_ok=True)
        logger.warning("video thumbnail generation failed for %s: %s", source.name, exc)
        return ThumbnailResult(
            VIDEO_THUMBNAIL_PLACEHOLDER_URL,
            None,
            "image/svg+xml",
            generated=False,
        )
