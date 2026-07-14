from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path

from sqlalchemy import and_, or_, select, text, update

from .config import Settings
from .db import Database
from .errors import APIError
from .image_generation import (
    IMAGE_GENERATION_HARD_MAX_ATTEMPTS,
    PROMPT_VERSION,
    GeneratedImage,
    ImageGenerationFailure,
    generate_image,
    image_generation_request_context,
)
from .models import AIRun, ImageGeneration, utcnow
from .schemas import ImageGenerationAccept
from .services.common import add_audit, dumps, stable_hash
from .services.image_generations import accept_image_generation
from .services.projects import _delete_after_commit, _delete_after_rollback


logger = logging.getLogger(__name__)
_GENERATION_ID_PATTERN = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_DRAFT_FILENAME = re.compile(
    rf"^{_GENERATION_ID_PATTERN}-[1-9][0-9]*-[0-9a-f]{{8}}\.png$"
)
_DRAFT_TEMP_FILENAME = re.compile(
    rf"^\.{_GENERATION_ID_PATTERN}-[0-9a-f]{{32}}\.tmp$"
)


@dataclass(frozen=True, slots=True)
class ImageGenerationClaim:
    generation_id: str
    execution_generation: int
    attempt: int
    worker_id: str
    recovered: bool = False
    reuse_staged: bool = False
    manual_retry_authorized: bool = False


@dataclass(frozen=True, slots=True)
class ImageGenerationSnapshot:
    effective_prompt: str
    aspect_ratio: str
    model: str
    request_hash: str


class DurableImageWorker:
    """Single-purpose durable worker for externally billed image generation."""

    def __init__(
        self, database: Database, settings: Settings, worker_id: str | None = None
    ) -> None:
        self.db = database
        self.settings = settings
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:image:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        self.stopping = False
        self._next_cleanup_at = 0.0

    @property
    def staging_root(self) -> Path:
        return self.settings.data_dir / "private" / "image-generations" / "staging"

    def _staging_directory(self, generation_id: str) -> Path:
        return self.staging_root / generation_id

    @staticmethod
    def _bundle_prefix(claim: ImageGenerationClaim) -> str:
        return f"a{claim.attempt:06d}-g{claim.execution_generation:06d}"

    def _bundle_paths(self, claim: ImageGenerationClaim) -> tuple[Path, Path, Path]:
        directory = self._staging_directory(claim.generation_id)
        prefix = self._bundle_prefix(claim)
        return (
            directory / f"{prefix}.submitted.json",
            directory / f"{prefix}.result.png",
            directory / f"{prefix}.ready.json",
        )

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def _atomic_write(self, path: Path, content: bytes) -> None:
        self.staging_root.mkdir(parents=True, exist_ok=True)
        root = self.staging_root.resolve(strict=True)
        if path.parent.is_symlink():
            raise OSError("image staging directory must not be a symbolic link")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.resolve(strict=True).parent != root:
            raise OSError("image staging path escaped its private root")
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _atomic_json(self, path: Path, payload: dict[str, object]) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._atomic_write(path, encoded)

    def _write_submission_marker(
        self, claim: ImageGenerationClaim, snapshot: ImageGenerationSnapshot
    ) -> None:
        submitted_path, _result_path, _ready_path = self._bundle_paths(claim)
        self._atomic_json(
            submitted_path,
            {
                "schema_version": 1,
                "generation_id": claim.generation_id,
                "attempt": claim.attempt,
                "execution_generation": claim.execution_generation,
                "request_hash": snapshot.request_hash,
                "prompt_hash": stable_hash(snapshot.effective_prompt),
                "model": snapshot.model,
                "aspect_ratio": snapshot.aspect_ratio,
            },
        )

    def _persist_ready_bundle(
        self,
        claim: ImageGenerationClaim,
        snapshot: ImageGenerationSnapshot,
        result: GeneratedImage,
    ) -> Path:
        _submitted_path, result_path, ready_path = self._bundle_paths(claim)
        sha256 = hashlib.sha256(result.png_bytes).hexdigest()
        self._atomic_write(result_path, result.png_bytes)
        # The ready marker is written last. Its presence is the durable proof
        # that the provider response was fully received and validated.
        self._atomic_json(
            ready_path,
            {
                "schema_version": 1,
                "generation_id": claim.generation_id,
                "attempt": claim.attempt,
                "execution_generation": claim.execution_generation,
                "request_hash": snapshot.request_hash,
                "requested_model": snapshot.model,
                "model": result.model,
                "aspect_ratio": snapshot.aspect_ratio,
                "provider": result.provider,
                "sha256": sha256,
                "size_bytes": len(result.png_bytes),
                "width": result.width,
                "height": result.height,
                "duration_ms": result.duration_ms,
                "usage": result.usage,
            },
        )
        return result_path

    def _load_ready_bundle(
        self,
        generation_id: str,
        request_hash: str,
        model: str,
        aspect_ratio: str,
    ) -> tuple[GeneratedImage, Path] | None:
        candidate_directory = self._staging_directory(generation_id)
        if candidate_directory.is_symlink():
            return None
        directory = candidate_directory.resolve(strict=False)
        if directory.parent != self.staging_root.resolve(strict=False):
            return None
        if not directory.is_dir():
            return None
        try:
            candidates = sorted(
                directory.glob("a*-g*.ready.json"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            return None
        for ready_path in candidates:
            try:
                if ready_path.is_symlink():
                    continue
                if ready_path.stat().st_size > 64 * 1024:
                    continue
                metadata = json.loads(ready_path.read_text(encoding="utf-8"))
                if not isinstance(metadata, dict):
                    continue
                if (
                    metadata.get("schema_version") != 1
                    or metadata.get("generation_id") != generation_id
                    or metadata.get("request_hash") != request_hash
                    or metadata.get("requested_model", metadata.get("model")) != model
                    or metadata.get("aspect_ratio") != aspect_ratio
                ):
                    continue
                result_path = ready_path.with_name(
                    ready_path.name.removesuffix(".ready.json") + ".result.png"
                )
                if result_path.is_symlink():
                    continue
                expected_size = metadata.get("size_bytes")
                expected_sha = metadata.get("sha256")
                width = metadata.get("width")
                height = metadata.get("height")
                duration_ms = metadata.get("duration_ms")
                if (
                    type(expected_size) is not int
                    or expected_size <= 0
                    or expected_size > self.settings.image_max_output_bytes
                    or not isinstance(expected_sha, str)
                    or len(expected_sha) != 64
                    or type(width) is not int
                    or width <= 0
                    or type(height) is not int
                    or height <= 0
                    or type(duration_ms) is not int
                    or duration_ms < 0
                ):
                    continue
                content = result_path.read_bytes()
                if (
                    len(content) != expected_size
                    or not content.startswith(b"\x89PNG\r\n\x1a\n")
                    or hashlib.sha256(content).hexdigest() != expected_sha
                ):
                    continue
                raw_usage = metadata.get("usage")
                usage = (
                    {
                        str(key): value
                        for key, value in raw_usage.items()
                        if isinstance(key, str) and type(value) is int and value >= 0
                    }
                    if isinstance(raw_usage, dict)
                    else {}
                )
                provider = metadata.get("provider")
                result_model = metadata.get("model")
                return (
                    GeneratedImage(
                        png_bytes=content,
                        width=width,
                        height=height,
                        provider=provider if isinstance(provider, str) else "openai-compatible",
                        model=result_model if isinstance(result_model, str) else model,
                        duration_ms=duration_ms,
                        usage=usage,
                    ),
                    result_path,
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue
        return None

    def _submission_marker_exists(
        self, generation_id: str, attempt: int, execution_generation: int
    ) -> bool:
        claim = ImageGenerationClaim(
            generation_id,
            execution_generation,
            attempt,
            self.worker_id,
        )
        submitted_path, _result_path, _ready_path = self._bundle_paths(claim)
        return submitted_path.is_file()

    def _remove_bundle(self, claim: ImageGenerationClaim) -> None:
        for path in self._bundle_paths(claim):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            self._staging_directory(claim.generation_id).rmdir()
        except OSError:
            pass

    def _remove_staging_directory(self, generation_id: str) -> None:
        root = self.staging_root.resolve(strict=False)
        candidate = self._staging_directory(generation_id)
        try:
            if candidate.is_symlink():
                candidate.unlink(missing_ok=True)
                return
        except OSError:
            return
        target = candidate.resolve(strict=False)
        if target.parent != root:
            logger.warning(
                "Refusing image staging cleanup outside root generation_id=%s",
                generation_id,
            )
            return
        try:
            if candidate.is_dir():
                shutil.rmtree(candidate)
            elif candidate.exists():
                candidate.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "Image staging cleanup deferred generation_id=%s error_type=%s",
                generation_id,
                type(exc).__name__,
            )

    def _cleanup_fenced_staging(self, generation_id: str) -> None:
        """Delete late output for terminal cancellation without racing recovery."""

        with self.db.session() as session:
            generation = session.get(ImageGeneration, generation_id)
            should_remove = generation is None or (
                generation.asset_id is not None
                or generation.discarded_at is not None
                or generation.status in {"succeeded", "canceled"}
            )
        if should_remove:
            self._remove_staging_directory(generation_id)

    def cleanup_staging_orphans(self) -> None:
        """Remove terminal/unknown staging safely while preserving recoverable billing evidence."""

        root = self.staging_root
        if not root.is_dir():
            return
        try:
            directories = [path for path in root.iterdir() if path.is_dir()][:100]
        except OSError:
            return
        if not directories:
            return

        generation_ids = [path.name for path in directories]
        with self.db.session() as session:
            generations = {
                item.id: item
                for item in session.scalars(
                    select(ImageGeneration).where(ImageGeneration.id.in_(generation_ids))
                ).all()
            }

        now = utcnow()
        stale_before = time.time() - max(300, self.settings.job_lease_seconds * 2)
        for directory in directories:
            generation = generations.get(directory.name)
            expired = False
            if generation is not None:
                expires_at = generation.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                expired = expires_at <= now
            remove_directory = generation is not None and (
                generation.asset_id is not None
                or generation.discarded_at is not None
                or generation.status in {"succeeded", "canceled"}
                or (
                    generation.status == "failed"
                    and (
                        not generation.retryable
                        or expired
                    )
                )
            )
            if generation is None:
                try:
                    remove_directory = directory.stat().st_mtime < stale_before
                except OSError:
                    remove_directory = False
            if remove_directory:
                self._remove_staging_directory(directory.name)
                continue

            # A result sidecar is only committed when its ready marker exists.
            # Old temp/partial files carry no recovery proof and can be removed
            # once no active provider write can reasonably still own them.
            if generation is not None and generation.status == "running":
                continue
            try:
                files = list(directory.iterdir())
            except OSError:
                continue
            for path in files:
                try:
                    if path.stat().st_mtime >= stale_before:
                        continue
                    if path.name.startswith(".") and path.name.endswith(".tmp"):
                        path.unlink(missing_ok=True)
                    elif path.name.endswith(".result.png"):
                        ready = path.with_name(
                            path.name.removesuffix(".result.png") + ".ready.json"
                        )
                        if not ready.is_file():
                            path.unlink(missing_ok=True)
                    elif path.name.endswith(".ready.json"):
                        result = path.with_name(
                            path.name.removesuffix(".ready.json") + ".result.png"
                        )
                        if not result.is_file():
                            path.unlink(missing_ok=True)
                except OSError:
                    continue

    def cleanup_output_orphans(self) -> None:
        """Reclaim unpublished draft files left by a process crash."""

        root = self.settings.data_dir / "private" / "image-generations"
        if not root.is_dir():
            return
        resolved_root = root.resolve(strict=True)
        with self.db.session() as session:
            raw_references = session.scalars(
                select(ImageGeneration.output_storage_path).where(
                    ImageGeneration.output_storage_path.is_not(None)
                )
            ).all()
        references: set[Path] = set()
        for raw_path in raw_references:
            if not raw_path:
                continue
            candidate = Path(raw_path).resolve(strict=False)
            if candidate.parent == resolved_root:
                references.add(candidate)

        stale_before = time.time() - max(300, self.settings.job_lease_seconds * 2)
        try:
            candidates = list(root.iterdir())
        except OSError:
            return
        for path in candidates:
            if not (
                _DRAFT_FILENAME.fullmatch(path.name)
                or _DRAFT_TEMP_FILENAME.fullmatch(path.name)
            ):
                continue
            try:
                if path.lstat().st_mtime >= stale_before:
                    continue
                resolved = path.resolve(strict=False)
                if resolved in references:
                    continue
                # Only direct children with worker-owned filename shapes are
                # eligible. unlink removes a symlink itself, never its target.
                if path.parent.resolve(strict=True) != resolved_root:
                    continue
                path.unlink(missing_ok=True)
            except OSError:
                continue

    @property
    def lease_renew_interval(self) -> float:
        return max(0.1, min(5.0, self.settings.job_lease_seconds / 3.0))

    def stop(self) -> None:
        self.stopping = True

    def cleanup_expired_drafts(self) -> None:
        now = utcnow()
        with self.db.session() as session:
            dialect = session.get_bind().dialect.name
            if dialect == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            query = (
                select(ImageGeneration)
                .where(
                    ImageGeneration.asset_id.is_(None),
                    ImageGeneration.discarded_at.is_(None),
                    ImageGeneration.expires_at <= now,
                    ImageGeneration.status.in_(("succeeded", "failed", "canceled")),
                )
                .limit(20)
            )
            if dialect == "postgresql":
                query = query.with_for_update(skip_locked=True)
            root = (self.settings.data_dir / "private" / "image-generations").resolve()
            for generation in session.scalars(query).all():
                generation.discarded_at = now
                if generation.output_storage_path:
                    path = Path(generation.output_storage_path).resolve(strict=False)
                    if path.is_relative_to(root):
                        _delete_after_commit(session, path)
                    generation.output_storage_path = None
                add_audit(
                    session,
                    generation.project_id,
                    "image_generation",
                    generation.id,
                    "image_generation.expired",
                    after={"status": generation.status},
                    actor="image_worker",
                )

        self.cleanup_staging_orphans()
        self.cleanup_output_orphans()

    def claim(self) -> ImageGenerationClaim | None:
        now = utcnow()
        lease_until = now + timedelta(seconds=self.settings.job_lease_seconds)
        with self.db.session() as session:
            dialect = session.get_bind().dialect.name
            if dialect == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))

            query = (
                select(ImageGeneration)
                .where(
                    ImageGeneration.discarded_at.is_(None),
                    ImageGeneration.asset_id.is_(None),
                    or_(
                        and_(
                            ImageGeneration.status == "queued",
                            ImageGeneration.next_run_at <= now,
                        ),
                        and_(
                            ImageGeneration.status == "running",
                            ImageGeneration.lease_expires_at < now,
                        ),
                    ),
                )
                .order_by(ImageGeneration.next_run_at, ImageGeneration.created_at)
                .limit(20)
            )
            if dialect == "postgresql":
                query = query.with_for_update(skip_locked=True)
            for generation in session.scalars(query).all():
                recovered = generation.status == "running"
                staged = self._load_ready_bundle(
                    generation.id,
                    generation.request_hash,
                    generation.model,
                    generation.aspect_ratio,
                )
                reuse_staged = staged is not None
                submitted_without_ready = (
                    recovered
                    and not reuse_staged
                    and self._submission_marker_exists(
                        generation.id,
                        generation.attempt,
                        generation.execution_generation,
                    )
                )
                if submitted_without_ready:
                    # The previous process crossed the paid-request boundary,
                    # but no complete response is durable. Fencing the stale
                    # execution and requiring a human retry avoids double billing.
                    generation.status = "failed"
                    generation.execution_generation += 1
                    generation.error_code = "IMAGE_PROVIDER_RESULT_UNKNOWN"
                    hard_limit_reached = (
                        generation.attempt >= IMAGE_GENERATION_HARD_MAX_ATTEMPTS
                    )
                    generation.error_message = (
                        "服务商请求可能已经完成，但未恢复到完整图片结果；"
                        + (
                            f"已达到 {IMAGE_GENERATION_HARD_MAX_ATTEMPTS} "
                            "次调用上限，不能继续重试。"
                            if hard_limit_reached
                            else "请确认任务状态后再手动重试。"
                        )
                    )
                    generation.retryable = not hard_limit_reached
                    generation.finished_at = now
                    generation.lease_owner = None
                    generation.lease_expires_at = None
                    session.add(
                        AIRun(
                            project_id=generation.project_id,
                            segment_id=generation.segment_id,
                            operation="image_generation",
                            provider=generation.provider,
                            model=generation.model,
                            prompt_version=PROMPT_VERSION,
                            input_hash=stable_hash(
                                generation.prompt,
                                generation.aspect_ratio,
                                PROMPT_VERSION,
                            ),
                            status="failed",
                            degraded=False,
                            duration_ms=0,
                            output_summary_json=dumps(
                                {
                                    "generation_id": generation.id,
                                    "attempt": generation.attempt,
                                    "ambiguous_submission": True,
                                    "recovered_from_stale_lease": True,
                                }
                            ),
                            error_message=(
                                "IMAGE_PROVIDER_RESULT_UNKNOWN: durable response missing"
                            ),
                        )
                    )
                    add_audit(
                        session,
                        generation.project_id,
                        "image_generation",
                        generation.id,
                        "image_generation.result_unknown",
                        after={
                            "attempt": generation.attempt,
                            "code": generation.error_code,
                            "manual_retry_required": True,
                        },
                        actor="image_worker",
                    )
                    continue

                if (
                    not recovered
                    and not reuse_staged
                    and generation.attempt
                    >= min(
                        generation.max_attempts,
                        IMAGE_GENERATION_HARD_MAX_ATTEMPTS,
                    )
                ):
                    generation.status = "failed"
                    generation.error_code = "IMAGE_ATTEMPTS_EXHAUSTED"
                    hard_limit_reached = (
                        generation.attempt >= IMAGE_GENERATION_HARD_MAX_ATTEMPTS
                    )
                    generation.error_message = (
                        "图像生成已达到 "
                        f"{IMAGE_GENERATION_HARD_MAX_ATTEMPTS} 次调用上限，不能继续重试。"
                        if hard_limit_reached
                        else "当前自动调用次数已用完，需要用户明确确认后才能重试。"
                    )
                    generation.retryable = not hard_limit_reached
                    generation.finished_at = now
                    generation.lease_owner = None
                    generation.lease_expires_at = None
                    add_audit(
                        session,
                        generation.project_id,
                        "image_generation",
                        generation.id,
                        "image_generation.failed",
                        after={
                            "code": generation.error_code,
                            "attempt": generation.attempt,
                        },
                        actor="image_worker",
                    )
                    continue

                manual_retry_authorized = (
                    generation.error_code == "IMAGE_MANUAL_RETRY_AUTHORIZED"
                )
                generation.status = "running"
                # Stale work that never wrote submitted.json did not cross the
                # provider boundary. A ready result also already consumed its attempt.
                if not recovered and not reuse_staged:
                    generation.attempt += 1
                generation.execution_generation += 1
                generation.lease_owner = self.worker_id
                generation.lease_expires_at = lease_until
                generation.retryable = False
                generation.error_code = None
                generation.error_message = None
                generation.finished_at = None
                if generation.started_at is None:
                    generation.started_at = now
                add_audit(
                    session,
                    generation.project_id,
                    "image_generation",
                    generation.id,
                    (
                        "image_generation.recovered"
                        if recovered or reuse_staged
                        else "image_generation.started"
                    ),
                    after={
                        "attempt": generation.attempt,
                        "execution_generation": generation.execution_generation,
                        "reused_staged_result": reuse_staged,
                        "manual_retry_authorized": manual_retry_authorized,
                    },
                    actor="image_worker",
                )
                return ImageGenerationClaim(
                    generation.id,
                    generation.execution_generation,
                    generation.attempt,
                    self.worker_id,
                    recovered=recovered,
                    reuse_staged=reuse_staged,
                    manual_retry_authorized=manual_retry_authorized,
                )
            return None

    def _renew_lease(self, claim: ImageGenerationClaim) -> bool:
        now = utcnow()
        with self.db.session() as session:
            result = session.execute(
                update(ImageGeneration)
                .where(
                    ImageGeneration.id == claim.generation_id,
                    ImageGeneration.status == "running",
                    ImageGeneration.execution_generation == claim.execution_generation,
                    ImageGeneration.attempt == claim.attempt,
                    ImageGeneration.lease_owner == claim.worker_id,
                )
                .values(
                    lease_expires_at=now
                    + timedelta(seconds=self.settings.job_lease_seconds),
                    updated_at=now,
                )
            )
            return result.rowcount == 1

    def _lease_loop(
        self,
        claim: ImageGenerationClaim,
        stop_event: threading.Event,
        lease_lost: threading.Event,
    ) -> None:
        while not stop_event.wait(self.lease_renew_interval):
            try:
                if not self._renew_lease(claim):
                    lease_lost.set()
                    return
            except Exception:
                logger.warning(
                    "Image generation lease renewal failed generation_id=%s",
                    claim.generation_id,
                )

    def _snapshot(self, claim: ImageGenerationClaim) -> ImageGenerationSnapshot | None:
        with self.db.session() as session:
            generation = session.scalar(
                select(ImageGeneration).where(
                    ImageGeneration.id == claim.generation_id,
                    ImageGeneration.status == "running",
                    ImageGeneration.execution_generation == claim.execution_generation,
                    ImageGeneration.attempt == claim.attempt,
                    ImageGeneration.lease_owner == claim.worker_id,
                )
            )
            if generation is None:
                return None
            return ImageGenerationSnapshot(
                effective_prompt=generation.effective_prompt,
                aspect_ratio=generation.aspect_ratio,
                model=generation.model,
                request_hash=generation.request_hash,
            )

    def _apply_success(
        self, claim: ImageGenerationClaim, result: GeneratedImage
    ) -> tuple[bool, bool]:
        root = self.settings.data_dir / "private" / "image-generations"
        root.mkdir(parents=True, exist_ok=True)
        temporary = root / f".{claim.generation_id}-{uuid.uuid4().hex}.tmp"
        final_path = root / (
            f"{claim.generation_id}-{claim.execution_generation}-{uuid.uuid4().hex[:8]}.png"
        )
        try:
            with temporary.open("xb") as output:
                output.write(result.png_bytes)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, final_path)
            with self.db.session() as session:
                dialect = session.get_bind().dialect.name
                if dialect == "sqlite":
                    session.execute(text("BEGIN IMMEDIATE"))
                query = select(ImageGeneration).where(
                    ImageGeneration.id == claim.generation_id,
                    ImageGeneration.status == "running",
                    ImageGeneration.execution_generation == claim.execution_generation,
                    ImageGeneration.attempt == claim.attempt,
                    ImageGeneration.lease_owner == claim.worker_id,
                )
                if dialect == "postgresql":
                    query = query.with_for_update()
                generation = session.scalar(query)
                if generation is None:
                    final_path.unlink(missing_ok=True)
                    return False, False
                previous_path = generation.output_storage_path
                _delete_after_rollback(session, final_path)
                generation.status = "succeeded"
                generation.provider = result.provider
                generation.model = result.model
                generation.output_storage_path = str(final_path)
                generation.output_mime_type = "image/png"
                generation.output_size_bytes = len(result.png_bytes)
                generation.output_sha256 = hashlib.sha256(result.png_bytes).hexdigest()
                generation.error_code = None
                generation.error_message = None
                generation.retryable = False
                generation.finished_at = utcnow()
                generation.lease_owner = None
                generation.lease_expires_at = None
                generation.expires_at = utcnow() + timedelta(
                    hours=self.settings.image_draft_retention_hours
                )
                if previous_path:
                    old = Path(previous_path).resolve(strict=False)
                    allowed = root.resolve()
                    if old != final_path and old.is_relative_to(allowed):
                        _delete_after_commit(session, old)
                session.add(
                    AIRun(
                        project_id=generation.project_id,
                        segment_id=generation.segment_id,
                        operation="image_generation",
                        provider=result.provider,
                        model=result.model,
                        prompt_version=PROMPT_VERSION,
                        input_hash=stable_hash(
                            generation.prompt,
                            generation.aspect_ratio,
                            PROMPT_VERSION,
                        ),
                        status="succeeded",
                        degraded=False,
                        duration_ms=result.duration_ms,
                        output_summary_json=dumps(
                            {
                                "generation_id": generation.id,
                                "aspect_ratio": generation.aspect_ratio,
                                "width": result.width,
                                "height": result.height,
                                "size_bytes": len(result.png_bytes),
                                "tokens": result.usage,
                                "provider_revised_prompt": bool(result.revised_prompt),
                            }
                        ),
                    )
                )
                add_audit(
                    session,
                    generation.project_id,
                    "image_generation",
                    generation.id,
                    "image_generation.succeeded",
                    after={
                        "attempt": generation.attempt,
                        "width": result.width,
                        "height": result.height,
                        "size_bytes": len(result.png_bytes),
                        "sha256": generation.output_sha256,
                    },
                    actor="image_worker",
                )
                auto_import = generation.auto_import
            return True, auto_import
        except Exception:
            temporary.unlink(missing_ok=True)
            # If the transaction failed before its rollback callback was
            # installed, this explicit cleanup still prevents orphan drafts.
            final_path.unlink(missing_ok=True)
            raise

    def _apply_failure(
        self,
        claim: ImageGenerationClaim,
        failure: ImageGenerationFailure,
        duration_ms: int = 0,
    ) -> None:
        provider_error_code = failure.code
        if (
            failure.code == "IMAGE_INTERNAL_ERROR"
            and self._submission_marker_exists(
                claim.generation_id,
                claim.attempt,
                claim.execution_generation,
            )
        ):
            # An unexpected exception after submitted.json cannot prove whether
            # the billed request completed. Never turn it into an auto retry.
            failure.ambiguous_submission = True
        if failure.ambiguous_submission:
            failure.code = "IMAGE_PROVIDER_RESULT_UNKNOWN"
            failure.message = (
                "服务商请求可能已经完成，但没有可恢复的完整结果；"
                "请确认任务状态后再手动重试。"
            )
            failure.retryable = False
        now = utcnow()
        with self.db.session() as session:
            dialect = session.get_bind().dialect.name
            if dialect == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            query = select(ImageGeneration).where(
                ImageGeneration.id == claim.generation_id,
                ImageGeneration.status == "running",
                ImageGeneration.execution_generation == claim.execution_generation,
                ImageGeneration.attempt == claim.attempt,
                ImageGeneration.lease_owner == claim.worker_id,
            )
            if dialect == "postgresql":
                query = query.with_for_update()
            generation = session.scalar(query)
            if generation is None:
                return
            hard_limit_reached = (
                generation.attempt >= IMAGE_GENERATION_HARD_MAX_ATTEMPTS
            )
            safe_auto_retry = (
                failure.retryable
                and not failure.ambiguous_submission
                and generation.attempt
                < min(
                    generation.max_attempts,
                    IMAGE_GENERATION_HARD_MAX_ATTEMPTS,
                )
            )
            generation.error_code = failure.code
            if hard_limit_reached:
                generation.error_message = (
                    f"{failure.message} 已达到 "
                    f"{IMAGE_GENERATION_HARD_MAX_ATTEMPTS} 次调用上限，不能继续重试。"
                )
            else:
                generation.error_message = failure.message
            generation.lease_owner = None
            generation.lease_expires_at = None
            if safe_auto_retry:
                generation.status = "queued"
                generation.next_run_at = now + timedelta(
                    seconds=min(30, 2 ** max(1, generation.attempt))
                )
                generation.retryable = True
                generation.finished_at = None
            else:
                generation.status = "failed"
                generation.retryable = (
                    (failure.retryable or failure.ambiguous_submission)
                    and not hard_limit_reached
                )
                generation.finished_at = now
            session.add(
                AIRun(
                    project_id=generation.project_id,
                    segment_id=generation.segment_id,
                    operation="image_generation",
                    provider=generation.provider,
                    model=generation.model,
                    prompt_version=PROMPT_VERSION,
                    input_hash=stable_hash(
                        generation.prompt,
                        generation.aspect_ratio,
                        PROMPT_VERSION,
                    ),
                    status="failed",
                    degraded=False,
                    duration_ms=max(0, duration_ms),
                    output_summary_json=dumps(
                        {
                            "generation_id": generation.id,
                            "attempt": generation.attempt,
                            "retry_scheduled": safe_auto_retry,
                            "ambiguous_submission": failure.ambiguous_submission,
                            "provider_error_code": provider_error_code,
                        }
                    ),
                    error_message=f"{failure.code}: {failure.message}",
                )
            )
            add_audit(
                session,
                generation.project_id,
                "image_generation",
                generation.id,
                (
                    "image_generation.retry_scheduled"
                    if safe_auto_retry
                    else "image_generation.failed"
                ),
                after={
                    "attempt": generation.attempt,
                    "code": failure.code,
                    "retryable": generation.retryable,
                    "ambiguous_submission": failure.ambiguous_submission,
                },
                actor="image_worker",
            )

    def _claim_auto_import(self) -> str | None:
        now = utcnow()
        lease_until = now + timedelta(seconds=self.settings.job_lease_seconds)
        with self.db.session() as session:
            dialect = session.get_bind().dialect.name
            if dialect == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            query = (
                select(ImageGeneration)
                .where(
                    ImageGeneration.status == "succeeded",
                    ImageGeneration.auto_import.is_(True),
                    ImageGeneration.asset_id.is_(None),
                    ImageGeneration.discarded_at.is_(None),
                    ImageGeneration.next_run_at <= now,
                    or_(
                        ImageGeneration.lease_owner.is_(None),
                        ImageGeneration.lease_expires_at < now,
                    ),
                )
                .order_by(ImageGeneration.next_run_at, ImageGeneration.created_at)
                .limit(1)
            )
            if dialect == "postgresql":
                query = query.with_for_update(skip_locked=True)
            generation = session.scalar(query)
            if generation is None:
                return None
            generation.lease_owner = self.worker_id
            generation.lease_expires_at = lease_until
            return generation.id

    def _defer_auto_import(self, generation_id: str, code: str) -> None:
        now = utcnow()
        with self.db.session() as session:
            dialect = session.get_bind().dialect.name
            if dialect == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            query = select(ImageGeneration).where(
                ImageGeneration.id == generation_id,
                ImageGeneration.status == "succeeded",
                ImageGeneration.asset_id.is_(None),
                ImageGeneration.discarded_at.is_(None),
                or_(
                    ImageGeneration.lease_owner.is_(None),
                    ImageGeneration.lease_owner == self.worker_id,
                ),
            )
            if dialect == "postgresql":
                query = query.with_for_update()
            generation = session.scalar(query)
            if generation is None:
                return
            prefix = "IMAGE_AUTO_IMPORT_RETRY_"
            retry_number = 0
            if (generation.error_code or "").startswith(prefix):
                try:
                    retry_number = int((generation.error_code or "")[len(prefix) :])
                except ValueError:
                    retry_number = 0
            retry_number += 1
            delay = min(300, 2 ** min(8, retry_number))
            generation.error_code = f"{prefix}{retry_number}"
            generation.error_message = "生成图片已保留，自动入库将在稍后重试"
            generation.next_run_at = now + timedelta(seconds=delay)
            generation.lease_owner = None
            generation.lease_expires_at = None
            add_audit(
                session,
                generation.project_id,
                "image_generation",
                generation.id,
                "image_generation.auto_import_deferred",
                after={"code": code, "retry_in_seconds": delay},
                actor="image_worker",
            )

    def _auto_accept(self, generation_id: str) -> bool:
        try:
            # Read the immutable acceptance options in a separate transaction;
            # accept_image_generation then starts its own SQLite write lock.
            with self.db.session() as session:
                generation = session.get(ImageGeneration, generation_id)
                if generation is None or not generation.auto_import:
                    return
                auto_select = generation.auto_select
                segment_version = generation.segment_version
            with self.db.session() as session:
                accept_image_generation(
                    session,
                    self.settings,
                    generation_id,
                    ImageGenerationAccept(
                        select_for_segment=auto_select,
                        expected_segment_version=segment_version,
                    ),
                    None,
                    actor="image_worker",
                )
            return True
        except APIError as exc:
            if exc.code in {
                "IMAGE_SEGMENT_VERSION_CONFLICT",
                "IMAGE_SEGMENT_MISSING",
            }:
                try:
                    # Segment state changed after generation. Preserve the paid
                    # output by importing it without silently changing a user's
                    # current subtitle selection.
                    with self.db.session() as session:
                        accept_image_generation(
                            session,
                            self.settings,
                            generation_id,
                            ImageGenerationAccept(select_for_segment=False),
                            None,
                            actor="image_worker",
                        )
                    return True
                except Exception as fallback_exc:
                    logger.warning(
                        "Image auto-import fallback deferred generation_id=%s error_type=%s",
                        generation_id,
                        type(fallback_exc).__name__,
                    )
            logger.warning(
                "Image auto-import deferred generation_id=%s code=%s",
                generation_id,
                exc.code,
            )
            self._defer_auto_import(generation_id, exc.code)
        except Exception as exc:
            logger.warning(
                "Image auto-import deferred generation_id=%s error_type=%s",
                generation_id,
                type(exc).__name__,
            )
            self._defer_auto_import(generation_id, "IMAGE_AUTO_IMPORT_INTERNAL_ERROR")
        return False

    def process(self, claim: ImageGenerationClaim) -> None:
        snapshot = self._snapshot(claim)
        if snapshot is None:
            return
        stop_event = threading.Event()
        lease_lost = threading.Event()
        renewer = threading.Thread(
            target=self._lease_loop,
            args=(claim, stop_event, lease_lost),
            name=f"frameflow-image-lease-{claim.generation_id[:8]}",
            daemon=True,
        )
        renewer.start()
        started = time.perf_counter()
        try:
            staged = self._load_ready_bundle(
                claim.generation_id,
                snapshot.request_hash,
                snapshot.model,
                snapshot.aspect_ratio,
            )
            if staged is not None:
                result, _staged_path = staged
            else:
                if claim.reuse_staged:
                    raise ImageGenerationFailure(
                        "IMAGE_STAGED_RESULT_MISSING",
                        "已持久化的图像结果当前无法读取，请确认后手动重试。",
                        ambiguous_submission=True,
                    )
                if self._submission_marker_exists(
                    claim.generation_id,
                    claim.attempt,
                    claim.execution_generation,
                ):
                    raise ImageGenerationFailure(
                        "IMAGE_PROVIDER_RESULT_UNKNOWN",
                        "服务商请求可能已经完成，但没有可恢复的完整结果，请确认后手动重试。",
                        ambiguous_submission=True,
                    )
                # Verify ownership immediately before crossing the externally
                # billed request boundary, then persist submitted.json first.
                if not self._renew_lease(claim):
                    return
                self._write_submission_marker(claim, snapshot)
                with image_generation_request_context(
                    model=snapshot.model,
                    idempotency_key=(
                        f"frameflow-image-{claim.generation_id}-a{claim.attempt}"
                    ),
                ):
                    result = generate_image(
                        snapshot.effective_prompt,
                        snapshot.aspect_ratio,
                        self.settings,
                    )
                self._persist_ready_bundle(claim, snapshot, result)
            applied, auto_import = self._apply_success(claim, result)
            if applied:
                # DB success is now the canonical copy. Clear all attempt
                # sidecars, including an older bundle reused after a restart.
                self._remove_staging_directory(claim.generation_id)
            else:
                self._cleanup_fenced_staging(claim.generation_id)
            if applied and auto_import:
                self._auto_accept(claim.generation_id)
        except ImageGenerationFailure as failure:
            self._apply_failure(
                claim,
                failure,
                max(0, int((time.perf_counter() - started) * 1_000)),
            )
            if not failure.ambiguous_submission:
                self._remove_bundle(claim)
        except Exception as exc:
            logger.warning(
                "Unexpected image generation failure generation_id=%s error_type=%s",
                claim.generation_id,
                type(exc).__name__,
            )
            if self._load_ready_bundle(
                claim.generation_id,
                snapshot.request_hash,
                snapshot.model,
                snapshot.aspect_ratio,
            ) is not None:
                # The paid output is durable. Leave the running row for normal
                # stale-lease recovery instead of downgrading it to a failure.
                return
            self._apply_failure(
                claim,
                ImageGenerationFailure(
                    "IMAGE_INTERNAL_ERROR", "图像生成处理发生内部错误"
                ),
                max(0, int((time.perf_counter() - started) * 1_000)),
            )
        finally:
            stop_event.set()
            renewer.join(timeout=max(0.2, self.lease_renew_interval * 2))
        if lease_lost.is_set():
            logger.info(
                "Image generation result fenced after lease loss generation_id=%s",
                claim.generation_id,
            )

    def run_once(self) -> bool:
        now = time.monotonic()
        if now >= self._next_cleanup_at:
            self.cleanup_expired_drafts()
            self._next_cleanup_at = now + 60.0
        auto_import_id = self._claim_auto_import()
        if auto_import_id is not None:
            self._auto_accept(auto_import_id)
            return True
        claim = self.claim()
        if claim is None:
            return False
        self.process(claim)
        return True

    def run_forever(self) -> None:
        logger.info("FrameFlow image worker started worker_id=%s", self.worker_id)
        while not self.stopping:
            try:
                if not self.run_once():
                    time.sleep(self.settings.worker_poll_seconds)
            except Exception:
                logger.exception("Image worker loop error")
                time.sleep(min(5.0, max(0.2, self.settings.worker_poll_seconds)))
        logger.info("FrameFlow image worker stopped")


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings.from_env()
    database = Database(settings)
    if (
        os.getenv("FRAMEFLOW_DATABASE_INITIALIZED") != "1"
        and os.getenv("FRAMEFLOW_RUNTIME_INITIALIZED") != "1"
    ):
        database.initialize()
    worker = DurableImageWorker(
        database,
        settings,
        worker_id=os.getenv("FRAMEFLOW_IMAGE_WORKER_ID") or None,
    )

    def stop(_signum, _frame):
        worker.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
