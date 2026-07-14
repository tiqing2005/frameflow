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


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    data_dir: Path
    database_url: str
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://localhost:3000")
    max_upload_bytes: int = 100 * 1024 * 1024
    worker_poll_seconds: float = 0.75
    stage_delay_seconds: float = 0.18
    job_lease_seconds: float = 300.0
    job_max_execution_seconds: float = 1800.0
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    asr_model: str = "gpt-4o-mini-transcribe"
    asr_provider: str = "auto"
    asr_timeout: float = 120.0
    local_asr_timeout: float = 600.0
    whisper_model: str = "tiny"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_download_root: Path | None = None
    hf_home: Path | None = None
    llm_provider: str = "rules"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str | None = None
    llm_model: str = "gpt-4.1-mini"
    llm_timeout: float = 20.0
    embedding_provider: str = "auto"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str | None = None
    embedding_timeout: float = 30.0
    embedding_device: str = "cpu"
    demo_mode: bool = False
    preview_timeout: float = 300.0
    thumbnail_timeout: float = 20.0
    preview_max_seconds: int = 180
    max_subtitle_chars: int = 200_000
    read_rate_limit_per_minute: int = 240
    write_rate_limit_per_minute: int = 60
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
        whisper_download_root = Path(
            os.getenv("FRAMEFLOW_WHISPER_DOWNLOAD_ROOT", data_dir / "models" / "whisper")
        ).resolve()
        hf_home = Path(os.getenv("HF_HOME", data_dir / "models" / "huggingface")).resolve()
        return cls(
            data_dir=data_dir,
            database_url=database_url,
            cors_origins=cors,
            max_upload_bytes=_int("FRAMEFLOW_MAX_UPLOAD_MB", 100) * 1024 * 1024,
            worker_poll_seconds=_float("FRAMEFLOW_WORKER_POLL_SECONDS", 0.75),
            stage_delay_seconds=_float("FRAMEFLOW_STAGE_DELAY_SECONDS", 0.18),
            job_lease_seconds=max(0.3, _float("FRAMEFLOW_JOB_LEASE_SECONDS", 300.0)),
            job_max_execution_seconds=max(
                0.1, _float("FRAMEFLOW_JOB_MAX_EXECUTION_SECONDS", 1800.0)
            ),
            openai_api_key=key,
            openai_base_url=os.getenv("FRAMEFLOW_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            asr_model=os.getenv("FRAMEFLOW_ASR_MODEL", "gpt-4o-mini-transcribe"),
            asr_provider=os.getenv("FRAMEFLOW_ASR_PROVIDER", "auto").strip().lower(),
            asr_timeout=max(0.1, _float("FRAMEFLOW_ASR_TIMEOUT", 120.0)),
            local_asr_timeout=max(0.1, _float("FRAMEFLOW_LOCAL_ASR_TIMEOUT", 600.0)),
            whisper_model=os.getenv("FRAMEFLOW_WHISPER_MODEL", "tiny").strip(),
            whisper_device=os.getenv("FRAMEFLOW_WHISPER_DEVICE", "cpu").strip(),
            whisper_compute_type=os.getenv("FRAMEFLOW_WHISPER_COMPUTE_TYPE", "int8").strip(),
            whisper_download_root=whisper_download_root,
            hf_home=hf_home,
            llm_provider=os.getenv("LLM_PROVIDER", "rules").strip().lower(),
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            llm_api_key=os.getenv("LLM_API_KEY", "").strip() or None,
            llm_model=os.getenv("LLM_MODEL", "gpt-4.1-mini").strip(),
            llm_timeout=max(0.1, _float("LLM_TIMEOUT", 20.0)),
            embedding_provider=os.getenv("EMBEDDING_PROVIDER", "auto").strip().lower(),
            embedding_model=os.getenv("FRAMEFLOW_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5").strip(),
            embedding_base_url=os.getenv(
                "FRAMEFLOW_EMBEDDING_BASE_URL", "https://api.openai.com/v1"
            ).rstrip("/"),
            embedding_api_key=os.getenv("FRAMEFLOW_EMBEDDING_API_KEY", "").strip() or None,
            embedding_timeout=max(0.1, _float("FRAMEFLOW_EMBEDDING_TIMEOUT", 30.0)),
            embedding_device=os.getenv("FRAMEFLOW_EMBEDDING_DEVICE", "cpu").strip(),
            demo_mode=_bool("DEMO_MODE", False),
            preview_timeout=max(5.0, _float("FRAMEFLOW_PREVIEW_TIMEOUT", 300.0)),
            thumbnail_timeout=max(1.0, _float("FRAMEFLOW_THUMBNAIL_TIMEOUT", 20.0)),
            preview_max_seconds=max(5, _int("FRAMEFLOW_PREVIEW_MAX_SECONDS", 180)),
            max_subtitle_chars=max(1_000, _int("FRAMEFLOW_MAX_SUBTITLE_CHARS", 200_000)),
            read_rate_limit_per_minute=max(
                0, _int("FRAMEFLOW_READ_RATE_LIMIT_PER_MINUTE", 240)
            ),
            write_rate_limit_per_minute=max(
                0, _int("FRAMEFLOW_WRITE_RATE_LIMIT_PER_MINUTE", 60)
            ),
            frontend_dir=frontend_dir,
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "media" / "seed").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "media" / "uploads" / "sources").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "media" / "uploads" / "assets").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "media" / "previews").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "private" / "sources").mkdir(parents=True, exist_ok=True)
        (self.whisper_download_root or self.data_dir / "models" / "whisper").mkdir(
            parents=True, exist_ok=True
        )
        (self.hf_home or self.data_dir / "models" / "huggingface").mkdir(parents=True, exist_ok=True)
