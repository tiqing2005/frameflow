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
