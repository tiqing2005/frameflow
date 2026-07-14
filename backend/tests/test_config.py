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
