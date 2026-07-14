from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def _clean_list(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw).strip()
        key = value.lower()
        if value and key not in seen:
            result.append(value[:60])
            seen.add(key)
    return result[:20]


class TextProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=2, max_length=100_000)

    @field_validator("title", "text", mode="before")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class SegmentPatch(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=10_000)
    topic: str | None = Field(default=None, min_length=1, max_length=80)
    keywords: list[str] | None = None
    version: int = Field(ge=1)

    @field_validator("text", "topic", mode="before")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("keywords")
    @classmethod
    def clean_keywords(cls, value: list[str] | None) -> list[str] | None:
        return _clean_list(value)

    @model_validator(mode="after")
    def ensure_change(self):
        if self.text is None and self.topic is None and self.keywords is None:
            raise ValueError("至少提供一个要修改的字段")
        return self


class SegmentOrder(BaseModel):
    segment_ids: list[str] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.segment_ids)) != len(self.segment_ids):
            raise ValueError("segment_ids 不能重复")
        return self


class SegmentTimingPatch(BaseModel):
    duration_ms: int | None = Field(ge=1_000, le=30_000)
    version: int = Field(ge=1)


class TimelineTimingUpdate(BaseModel):
    action: Literal["fit", "restore_auto"]
    target_duration_ms: int | None = Field(default=None, ge=1_000)
    strategy: Literal["text", "current", "equal"] = "text"
    expected_input_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_action_fields(self):
        if self.action == "fit" and self.target_duration_ms is None:
            raise ValueError("适配总时长时必须提供 target_duration_ms")
        if self.action == "restore_auto" and self.target_duration_ms is not None:
            raise ValueError("恢复自动时长时不能提供 target_duration_ms")
        return self


class SelectionPut(BaseModel):
    asset_id: str = Field(min_length=1, max_length=64)

    @field_validator("asset_id", mode="before")
    @classmethod
    def clean_asset_id(cls, value: str) -> str:
        return value.strip()


class PreviewCreate(BaseModel):
    force: bool = False


class AssetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    tags: list[str] | None = None
    keywords: list[str] | None = None
    active: bool | None = None

    @field_validator("name", mode="before")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("tags", "keywords")
    @classmethod
    def clean_values(cls, value: list[str] | None) -> list[str] | None:
        return _clean_list(value)

    @model_validator(mode="after")
    def ensure_change(self):
        if self.name is None and self.tags is None and self.keywords is None and self.active is None:
            raise ValueError("至少提供一个要修改的字段")
        return self


class ImageGenerationCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=2_000)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    aspect_ratio: Literal["16:9", "1:1", "9:16"] = "16:9"
    segment_id: str | None = Field(default=None, min_length=1, max_length=36)
    auto_import: bool = False
    auto_select: bool = False

    @field_validator("prompt", "name", "segment_id", mode="before")
    @classmethod
    def strip_generation_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def validate_generation_options(self):
        if self.auto_select and (not self.auto_import or not self.segment_id):
            raise ValueError("auto_select requires auto_import and segment_id")
        return self


class SegmentImageGenerationCreate(BaseModel):
    prompt: str | None = Field(default=None, min_length=1, max_length=2_000)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    aspect_ratio: Literal["16:9", "1:1", "9:16"] = "16:9"
    auto_import: bool = False
    auto_select: bool = False

    @field_validator("prompt", "name", mode="before")
    @classmethod
    def strip_segment_generation_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def validate_segment_generation_options(self):
        if self.auto_select and not self.auto_import:
            raise ValueError("auto_select requires auto_import")
        return self


class ImageGenerationAccept(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    select_for_segment: bool = False
    expected_segment_version: int | None = Field(default=None, ge=1)

    @field_validator("name", mode="before")
    @classmethod
    def strip_accept_name(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class FaultNext(BaseModel):
    mode: Literal["ai_degrade", "job_fail", "none"]
