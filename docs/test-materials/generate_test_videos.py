from __future__ import annotations

import argparse
import math
import wave
from fractions import Fraction
from pathlib import Path

import av
import numpy as np


SAMPLE_RATE = 48_000
FPS = 24


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        source_rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    if sample_width != 2:
        raise ValueError(f"Expected 16-bit PCM WAV, got {sample_width * 8}-bit: {path}")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if source_rate != SAMPLE_RATE:
        source_positions = np.linspace(0, len(samples) - 1, len(samples), dtype=np.float64)
        target_length = max(1, round(len(samples) * SAMPLE_RATE / source_rate))
        target_positions = np.linspace(0, len(samples) - 1, target_length, dtype=np.float64)
        samples = np.interp(target_positions, source_positions, samples).astype(np.float32)
    return samples


def prepare_audio(samples: np.ndarray, mode: str) -> np.ndarray:
    peak = float(np.max(np.abs(samples))) if samples.size else 1.0
    samples = samples / max(peak, 1e-6) * 0.78
    if mode == "noisy":
        rng = np.random.default_rng(20260714)
        timeline = np.arange(len(samples), dtype=np.float32) / SAMPLE_RATE
        office_hum = 0.025 * np.sin(2 * math.pi * 60 * timeline)
        broadband = rng.normal(0, 0.018, len(samples)).astype(np.float32)
        samples = samples * 0.48 + office_hum + broadband
    elif mode == "quiet":
        samples = samples * 0.34
    padding = np.zeros(round(0.45 * SAMPLE_RATE), dtype=np.float32)
    return np.clip(np.concatenate([padding, samples, padding]), -0.95, 0.95)


def visual_frame(index: int, width: int, height: int, palette: tuple[int, int, int]) -> np.ndarray:
    x = np.linspace(0, 1, width, dtype=np.float32)[None, :]
    y = np.linspace(0, 1, height, dtype=np.float32)[:, None]
    phase = index / FPS
    frame = np.empty((height, width, 3), dtype=np.uint8)
    for channel, base in enumerate(palette):
        values = base + 62 * x + 38 * y + 24 * np.sin(phase * 0.8 + channel * 1.4)
        frame[:, :, channel] = np.clip(values, 0, 255).astype(np.uint8)
    band_width = max(12, width // 24)
    band_x = int((phase * 48) % (width + band_width)) - band_width
    left, right = max(0, band_x), min(width, band_x + band_width)
    if right > left:
        frame[:, left:right, :] = np.clip(frame[:, left:right, :] + 55, 0, 255)
    box_w, box_h = width // 3, height // 5
    box_x = (width - box_w) // 2
    box_y = (height - box_h) // 2
    frame[box_y : box_y + box_h, box_x : box_x + box_w] //= 2
    meter = int((0.5 + 0.5 * math.sin(phase * 2.2)) * (box_w - 12))
    frame[box_y + box_h - 14 : box_y + box_h - 6, box_x + 6 : box_x + 6 + meter] = (245, 245, 245)
    return frame


def add_video_stream(container: av.container.OutputContainer, suffix: str, width: int, height: int):
    codec = "libvpx-vp9" if suffix == ".webm" else "libx264"
    stream = container.add_stream(codec, rate=FPS)
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"
    if suffix == ".webm":
        stream.bit_rate = 550_000
        stream.options = {"deadline": "realtime", "cpu-used": "5"}
    else:
        stream.options = {"crf": "29", "preset": "veryfast"}
    return stream


def add_audio_stream(container: av.container.OutputContainer, suffix: str):
    codec = "libopus" if suffix == ".webm" else "aac"
    stream = container.add_stream(codec, rate=SAMPLE_RATE)
    stream.layout = "mono"
    stream.bit_rate = 64_000
    return stream


def encode_video(
    output_path: Path,
    audio: np.ndarray | None,
    duration: float,
    width: int,
    height: int,
    palette: tuple[int, int, int],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(output_path), "w") as container:
        video_stream = add_video_stream(container, output_path.suffix.lower(), width, height)
        audio_stream = add_audio_stream(container, output_path.suffix.lower()) if audio is not None else None

        frame_count = max(1, math.ceil(duration * FPS))
        for index in range(frame_count):
            frame = av.VideoFrame.from_ndarray(
                visual_frame(index, width, height, palette), format="rgb24"
            )
            frame.pts = index
            frame.time_base = Fraction(1, FPS)
            for packet in video_stream.encode(frame):
                container.mux(packet)
        for packet in video_stream.encode():
            container.mux(packet)

        if audio_stream is not None and audio is not None:
            for start in range(0, len(audio), 1024):
                chunk = audio[start : start + 1024]
                frame = av.AudioFrame.from_ndarray(
                    chunk.reshape(1, -1), format="fltp", layout="mono"
                )
                frame.sample_rate = SAMPLE_RATE
                frame.pts = start
                frame.time_base = Fraction(1, SAMPLE_RATE)
                for packet in audio_stream.encode(frame):
                    container.mux(packet)
            for packet in audio_stream.encode():
                container.mux(packet)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    fixtures = [
        ("01_standard_mandarin.mp4", "standard.wav", "clean", 640, 360, (28, 70, 125)),
        ("02_numbers_and_terms.mp4", "terms.wav", "clean", 640, 360, (105, 42, 58)),
        ("03_noisy_office.webm", "noisy.wav", "noisy", 640, 360, (38, 98, 74)),
        ("04_portrait_interview.mov", "portrait.wav", "quiet", 360, 640, (82, 52, 118)),
    ]
    for filename, wav_name, mode, width, height, palette in fixtures:
        audio = prepare_audio(read_wav(args.audio_dir / wav_name), mode)
        duration = len(audio) / SAMPLE_RATE
        encode_video(args.output_dir / filename, audio, duration, width, height, palette)
        print(f"created {filename} duration={duration:.2f}s")

    encode_video(
        args.output_dir / "05_silent_video_negative.mp4",
        audio=None,
        duration=8.0,
        width=640,
        height=360,
        palette=(88, 88, 88),
    )
    print("created 05_silent_video_negative.mp4 duration=8.00s")


if __name__ == "__main__":
    main()
