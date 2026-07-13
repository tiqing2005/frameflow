from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path


class PreviewRenderError(RuntimeError):
    pass


class PreviewRenderTimeout(PreviewRenderError):
    pass


def _srt_time(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def _run(command: list[str], *, cwd: Path, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PreviewRenderTimeout("预览渲染超过允许时间")
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=remaining,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PreviewRenderError("服务器未安装 ffmpeg，无法生成预览视频") from exc
    except subprocess.TimeoutExpired as exc:
        raise PreviewRenderTimeout("预览渲染超过允许时间") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "ffmpeg failed").strip()[-1_000:]
        raise PreviewRenderError(f"ffmpeg 渲染失败：{detail}")


def _video_encoder(cwd: Path, deadline: float) -> tuple[str, list[str]]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PreviewRenderTimeout("预览渲染超过允许时间")
    try:
        completed = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=remaining,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PreviewRenderError("服务器未安装可用的 ffmpeg 编码器") from exc
    except subprocess.TimeoutExpired as exc:
        raise PreviewRenderTimeout("预览渲染超过允许时间") from exc
    available = completed.stdout + completed.stderr
    if "libx264" in available:
        return "libx264", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    if "libopenh264" in available:
        return "libopenh264", ["-c:v", "libopenh264", "-b:v", "2500k"]
    if "h264_mf" in available:
        return "h264_mf", ["-c:v", "h264_mf", "-b:v", "2500k"]
    if " mpeg4 " in available:
        return "mpeg4", ["-c:v", "mpeg4", "-q:v", "4"]
    raise PreviewRenderError("ffmpeg 没有可用的 H.264/MPEG-4 视频编码器")


def render_preview(
    plan: dict,
    output_path: Path,
    *,
    timeout: float,
    width: int = 1280,
    height: int = 720,
    fps: int = 25,
) -> dict:
    """Render selected images/videos into one normalized MP4 preview.

    Each source first becomes an H.264 clip with an identical stream layout,
    then the clips are concatenated losslessly. Subtitle burn-in is attempted;
    when the host ffmpeg lacks libass/fonts, the visual preview still succeeds
    and the caller receives ``subtitles_burned=False``.
    """

    items = list(plan.get("items") or [])
    if not items:
        raise PreviewRenderError("预览时间线为空")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    filter_chain = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={fps},setsar=1,format=yuv420p"
    )

    with tempfile.TemporaryDirectory(prefix="frameflow-preview-", dir=output_path.parent) as raw_tmp:
        tmp = Path(raw_tmp)
        encoder, encoder_args = _video_encoder(tmp, deadline)
        concat_lines: list[str] = []
        srt_blocks: list[str] = []
        for index, item in enumerate(items):
            source = Path(str(item["storage_path"]))
            if not source.is_file():
                raise PreviewRenderError(f"素材文件不存在：{source.name}")
            duration = max(1.0, float(item["duration_ms"]) / 1_000)
            clip = tmp / f"clip-{index:03}.mp4"
            asset = item.get("asset") or {}
            kind = asset.get("kind")
            if kind == "image":
                input_args = ["-loop", "1", "-i", str(source)]
            elif kind == "video":
                input_args = ["-stream_loop", "-1", "-i", str(source)]
            else:
                raise PreviewRenderError(f"不支持的预览素材类型：{kind}")
            _run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    *input_args,
                    "-t",
                    f"{duration:.3f}",
                    "-an",
                    "-vf",
                    filter_chain,
                    *encoder_args,
                    "-movflags",
                    "+faststart",
                    clip.name,
                ],
                cwd=tmp,
                deadline=deadline,
            )
            concat_lines.append(f"file '{clip.name}'")
            text = str(item.get("text") or "").replace("\r", " ").replace("\n", " ").strip()
            if text:
                srt_blocks.append(
                    f"{index + 1}\n{_srt_time(int(item['start_ms']))} --> "
                    f"{_srt_time(int(item['end_ms']))}\n{text}\n"
                )

        concat_file = tmp / "concat.txt"
        concat_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
        joined = tmp / "joined.mp4"
        _run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_file.name,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                joined.name,
            ],
            cwd=tmp,
            deadline=deadline,
        )

        subtitles_burned = False
        if srt_blocks:
            captions = tmp / "captions.srt"
            captions.write_text("\n".join(srt_blocks), encoding="utf-8")
            try:
                _run(
                    [
                        "ffmpeg",
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        joined.name,
                        "-vf",
                        "subtitles=captions.srt:force_style='FontName=Noto Sans CJK SC,"
                        "FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H90000000,"
                        "BorderStyle=3,Outline=1,Shadow=0,MarginV=30'",
                        *encoder_args,
                        "-an",
                        "-movflags",
                        "+faststart",
                        str(output_path),
                    ],
                    cwd=tmp,
                    deadline=deadline,
                )
                subtitles_burned = True
            except PreviewRenderTimeout:
                # A hard deadline is a task failure, not an optional subtitle
                # capability downgrade. Propagate it so the durable job can be
                # retried and does not report a false success.
                raise
            except PreviewRenderError:
                # Preview generation remains useful even on minimal ffmpeg
                # builds without libass/CJK fonts.
                shutil.copyfile(joined, output_path)
        else:
            shutil.copyfile(joined, output_path)

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise PreviewRenderError("预览视频输出为空")
    return {
        "output_path": str(output_path),
        "size_bytes": output_path.stat().st_size,
        "duration_ms": int(plan["duration_ms"]),
        "segment_count": len(items),
        "subtitles_burned": subtitles_burned,
        "codec": encoder,
        "resolution": f"{width}x{height}",
        "fps": fps,
    }
