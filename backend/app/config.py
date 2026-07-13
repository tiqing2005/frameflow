from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(slots=True)
class Settings:
    data_dir: Path
    database_url: str
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://localhost:3000")
    max_upload_bytes: int = 100 * 1024 * 1024
    worker_poll_seconds: float = 0.75
    stage_delay_seconds: float = 0.18
    job_lease_seconds: int = 300
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    asr_model: str = "gpt-4o-mini-transcribe"
    asr_provider: str = "auto"
    whisper_model: str = "tiny"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    frontend_dir: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        backend_dir = Path(__file__).resolve().parents[1]
        data_dir = Path(os.getenv("FRAMEFLOW_DATA_DIR", backend_dir / "data")).resolve()
        database_url = os.getenv(
            "FRAMEFLOW_DATABASE_URL",
            f"sqlite:///{(data_dir / 'frameflow.db').as_posix()}",
        )
        cors = tuple(
            origin.strip()
            for origin in os.getenv(
                "FRAMEFLOW_CORS_ORIGINS", "http://localhost:5173,http://localhost:3000"
            ).split(",")
            if origin.strip()
        )
        frontend_raw = os.getenv("FRAMEFLOW_FRONTEND_DIR")
        frontend_dir = Path(frontend_raw).resolve() if frontend_raw else backend_dir.parent / "frontend" / "dist"
        key = os.getenv("OPENAI_API_KEY", "").strip() or None
        return cls(
            data_dir=data_dir,
            database_url=database_url,
            cors_origins=cors,
            max_upload_bytes=_int("FRAMEFLOW_MAX_UPLOAD_MB", 100) * 1024 * 1024,
            worker_poll_seconds=_float("FRAMEFLOW_WORKER_POLL_SECONDS", 0.75),
            stage_delay_seconds=_float("FRAMEFLOW_STAGE_DELAY_SECONDS", 0.18),
            job_lease_seconds=_int("FRAMEFLOW_JOB_LEASE_SECONDS", 300),
            openai_api_key=key,
            openai_base_url=os.getenv("FRAMEFLOW_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            asr_model=os.getenv("FRAMEFLOW_ASR_MODEL", "gpt-4o-mini-transcribe"),
            asr_provider=os.getenv("FRAMEFLOW_ASR_PROVIDER", "auto").strip().lower(),
            whisper_model=os.getenv("FRAMEFLOW_WHISPER_MODEL", "tiny").strip(),
            whisper_device=os.getenv("FRAMEFLOW_WHISPER_DEVICE", "cpu").strip(),
            whisper_compute_type=os.getenv("FRAMEFLOW_WHISPER_COMPUTE_TYPE", "int8").strip(),
            frontend_dir=frontend_dir,
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "media" / "seed").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "media" / "uploads" / "sources").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "media" / "uploads" / "assets").mkdir(parents=True, exist_ok=True)
