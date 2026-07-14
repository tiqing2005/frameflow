from __future__ import annotations

from app.config import Settings


def test_dashscope_key_never_falls_back_to_openai_key(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEFLOW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFLOW_FRONTEND_DIR", str(tmp_path / "frontend"))
    monkeypatch.setenv("OPENAI_API_KEY", "openai-only-key")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    settings = Settings.from_env()

    assert settings.openai_api_key == "openai-only-key"
    assert settings.dashscope_api_key is None


def test_dashscope_uses_only_its_explicit_key(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEFLOW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFLOW_FRONTEND_DIR", str(tmp_path / "frontend"))
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-key")

    settings = Settings.from_env()

    assert settings.openai_api_key == "openai-key"
    assert settings.dashscope_api_key == "dashscope-key"


def test_vision_defaults_to_disabled_and_never_inherits_other_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEFLOW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFLOW_FRONTEND_DIR", str(tmp_path / "frontend"))
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("LLM_API_KEY", "llm-key")
    monkeypatch.delenv("VISION_PROVIDER", raising=False)
    monkeypatch.delenv("VISION_API_KEY", raising=False)

    settings = Settings.from_env()

    assert settings.vision_provider == "none"
    assert settings.vision_api_key is None


def test_vision_uses_only_explicit_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEFLOW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFLOW_FRONTEND_DIR", str(tmp_path / "frontend"))
    monkeypatch.setenv("VISION_PROVIDER", " OPENAI-COMPATIBLE ")
    monkeypatch.setenv("VISION_BASE_URL", "https://vision.example.invalid/v1/")
    monkeypatch.setenv("VISION_API_KEY", "vision-key")
    monkeypatch.setenv("VISION_MODEL", "vision-model")
    monkeypatch.setenv("VISION_TIMEOUT", "12.5")

    settings = Settings.from_env()

    assert settings.vision_provider == "openai-compatible"
    assert settings.vision_base_url == "https://vision.example.invalid/v1"
    assert settings.vision_api_key == "vision-key"
    assert settings.vision_model == "vision-model"
    assert settings.vision_timeout == 12.5


def test_image_generation_never_inherits_other_provider_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEFLOW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFLOW_FRONTEND_DIR", str(tmp_path / "frontend"))
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("LLM_API_KEY", "llm-key")
    monkeypatch.setenv("VISION_API_KEY", "vision-key")
    monkeypatch.delenv("IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("IMAGE_API_BASE_URL", raising=False)

    settings = Settings.from_env()

    assert settings.image_api_key is None
    assert settings.image_api_base_url == ""


def test_image_generation_reads_and_bounds_explicit_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEFLOW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFLOW_FRONTEND_DIR", str(tmp_path / "frontend"))
    monkeypatch.setenv("IMAGE_API_BASE_URL", "https://images.example.invalid/v1/")
    monkeypatch.setenv("IMAGE_API_KEY", "image-test-key")
    monkeypatch.setenv("IMAGE_MODEL", "image-test-model")
    monkeypatch.setenv("IMAGE_API_TIMEOUT", "2")
    monkeypatch.setenv("IMAGE_MAX_RESPONSE_MB", "0")
    monkeypatch.setenv("IMAGE_MAX_OUTPUT_MB", "0")
    monkeypatch.setenv("IMAGE_MAX_PIXELS", "42")
    monkeypatch.setenv("IMAGE_DRAFT_RETENTION_HOURS", "0")
    monkeypatch.setenv("IMAGE_DAILY_LIMIT", "0")
    monkeypatch.setenv("IMAGE_MAX_PENDING", "0")

    settings = Settings.from_env()

    assert settings.image_api_base_url == "https://images.example.invalid/v1"
    assert settings.image_api_key == "image-test-key"
    assert settings.image_model == "image-test-model"
    assert settings.image_timeout == 5
    assert settings.image_max_response_bytes == 1024 * 1024
    assert settings.image_max_output_bytes == 1024 * 1024
    assert settings.image_max_pixels == 1_000_000
    assert settings.image_draft_retention_hours == 1
    assert settings.image_daily_limit == 0
    assert settings.image_max_pending == 1
