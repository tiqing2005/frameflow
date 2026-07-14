from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import socket
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import and_, delete, event, func, or_, select, text, update

from .asr import TranscriptionError, transcribe_file
from .config import Settings
from .db import Database
from .errors import APIError
from .llm import SemanticEnhancement, enhance_semantic_segments, rule_segments
from .models import (
    AIRun,
    Asset,
    AuditEvent,
    FaultControl,
    Job,
    JobEvent,
    Project,
    PreviewRender,
    Recommendation,
    Segment,
    Selection,
    Source,
    WorkerHeartbeat,
    utcnow,
)
from .embeddings import get_semantic_scorer
from .nlp import RankingTrace, rank_assets_with_trace
from .preview import PreviewRenderError, render_preview
from .services import add_audit, build_preview_plan, dumps, stable_hash

logger = logging.getLogger("frameflow.worker")

FAILURE_CATEGORY_LABELS = {
    "input": "输入永久错误",
    "transient": "临时网络错误",
    "configuration": "配置错误",
    "dependency": "依赖缺失",
    "processing": "处理错误",
}
MANUALLY_REARMABLE_FAILURE_CATEGORIES = {"configuration", "dependency"}


class WorkerFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        category: str = "processing",
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.category = category


class JobCanceled(Exception):
    pass


class DurableWorker:
    def __init__(self, database: Database, settings: Settings, worker_id: str | None = None):
        self.db = database
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.stopping = False
        self._claimed_generations: dict[str, int] = {}
        self._timed_out_pipelines: set[threading.Thread] = set()
        self._isolation_detail: str | None = None
        # Resolve the semantic scorer once: True embedding (local BGE / remote)
        # when available, else None and rank_assets falls back to char-ngram.
        self._semantic_scorer = get_semantic_scorer(settings)

    @property
    def lease_renew_interval(self) -> float:
        return max(0.05, min(5.0, self.settings.job_lease_seconds / 3.0))

    def stop(self) -> None:
        self.stopping = True

    def _refresh_timed_out_pipelines(self) -> bool:
        self._timed_out_pipelines = {
            thread for thread in self._timed_out_pipelines if thread.is_alive()
        }
        if not self._timed_out_pipelines:
            self._isolation_detail = None
        return bool(self._timed_out_pipelines)

    def heartbeat(self) -> None:
        now = utcnow()
        isolated = self._refresh_timed_out_pipelines()
        operational_state = "isolated" if isolated else "ready"
        status_detail = self._isolation_detail if isolated else None
        with self.db.session() as session:
            heartbeat = session.get(WorkerHeartbeat, 1)
            if heartbeat is None:
                session.add(
                    WorkerHeartbeat(
                        id=1,
                        worker_id=self.worker_id,
                        heartbeat_at=now,
                        operational_state=operational_state,
                        status_detail=status_detail,
                    )
                )
            else:
                heartbeat.worker_id = self.worker_id
                heartbeat.heartbeat_at = now
                heartbeat.operational_state = operational_state
                heartbeat.status_detail = status_detail

    def claim(self) -> str | None:
        was_isolated = bool(self._timed_out_pipelines)
        if self._refresh_timed_out_pipelines():
            return None
        if was_isolated:
            self.heartbeat()
        now = utcnow()
        lease_until = now + timedelta(seconds=self.settings.job_lease_seconds)
        with self.db.session() as session:
            # SQLite has no SKIP LOCKED. BEGIN IMMEDIATE serializes the very short
            # claim transaction, making the lease transition atomic.
            if self.settings.database_url.startswith("sqlite"):
                session.execute(text("BEGIN IMMEDIATE"))
            exhausted = session.scalars(
                select(Job).where(
                    Job.status == "running",
                    Job.lease_expires_at < now,
                    Job.attempt >= Job.max_attempts,
                )
            ).all()
            for stale in exhausted:
                stale.status = "failed"
                stale.retryable = False
                stale.error_code = "JOB_ATTEMPTS_EXHAUSTED"
                stale.error_message = "任务租约过期且已达到最大执行次数"
                stale.finished_at = now
                stale.lease_owner = None
                stale.lease_expires_at = None
                project = session.get(Project, stale.project_id)
                if project and stale.kind == "pipeline":
                    project.status = "failed"
                if stale.kind == "preview":
                    preview = session.scalar(
                        select(PreviewRender).where(PreviewRender.job_id == stale.id)
                    )
                    if preview:
                        preview.status = "failed"
                        preview.error_message = stale.error_message
                session.add(JobEvent(job_id=stale.id, stage=stale.stage, progress=stale.progress,
                                     message="任务崩溃恢复次数已耗尽，停止自动恢复", level="error"))
            job = session.scalar(
                select(Job)
                .where(
                    and_(
                        Job.attempt < Job.max_attempts,
                        or_(
                            (Job.status == "queued") & (Job.next_run_at <= now),
                            (Job.status == "running") & (Job.lease_expires_at < now),
                        ),
                    )
                )
                .order_by(Job.next_run_at, Job.created_at)
                .limit(1)
            )
            if job is None:
                return None
            recovered = job.status == "running"
            highest_event_progress = session.scalar(
                select(func.max(JobEvent.progress)).where(JobEvent.job_id == job.id)
            ) or 0
            job.status = "running"
            job.stage = "preview_planning" if job.kind == "preview" else "validating"
            job.progress = max(1, job.progress, highest_event_progress)
            job.attempt += 1
            job.execution_generation += 1
            job.lease_owner = self.worker_id
            job.lease_expires_at = lease_until
            job.heartbeat_at = now
            job.finished_at = None
            if job.started_at is None:
                job.started_at = now
            project = session.get(Project, job.project_id)
            if project and job.kind == "pipeline":
                project.status = "processing"
            session.add(
                JobEvent(
                    job_id=job.id,
                    stage=job.stage,
                    progress=job.progress,
                    message=(
                        "检测到过期租约，Worker 已恢复任务并从安全阶段重跑"
                        if recovered
                        else f"Worker 已领取任务（第 {job.attempt} 次执行）"
                    ),
                    level="warning" if recovered else "info",
                )
            )
            self._claimed_generations[job.id] = job.execution_generation
            return job.id

    def _fence(self, session, job_id: str, generation: int) -> Job:
        """Acquire a write fence and return the still-owned execution."""
        now = utcnow()
        result = session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == "running",
                Job.lease_owner == self.worker_id,
                Job.execution_generation == generation,
            )
            .values(heartbeat_at=now)
        )
        if result.rowcount != 1:
            current = session.get(Job, job_id)
            if current is None:
                raise WorkerFailure("JOB_NOT_FOUND", "任务不存在")
            if current.status == "canceled":
                raise JobCanceled()
            raise WorkerFailure("JOB_LEASE_LOST", "任务租约或执行代次已变化，停止旧执行", True)
        return session.get(Job, job_id)

    def _renew_lease(self, job_id: str, generation: int) -> bool:
        now = utcnow()
        with self.db.session() as session:
            result = session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.status == "running",
                    Job.lease_owner == self.worker_id,
                    Job.execution_generation == generation,
                )
                .values(
                    heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=self.settings.job_lease_seconds),
                )
            )
            if result.rowcount != 1:
                return False
            heartbeat = session.get(WorkerHeartbeat, 1)
            if heartbeat is None:
                session.add(WorkerHeartbeat(id=1, worker_id=self.worker_id, heartbeat_at=now))
            else:
                heartbeat.worker_id = self.worker_id
                heartbeat.heartbeat_at = now
        return True

    def _abandon_execution(self, job_id: str, generation: int) -> None:
        """Fence a stopping worker immediately and leave the running job recoverable."""
        now = utcnow()
        with self.db.session() as session:
            session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.status == "running",
                    Job.lease_owner == self.worker_id,
                    Job.execution_generation == generation,
                )
                .values(lease_owner=None, lease_expires_at=now, heartbeat_at=now)
            )

    def _lease_loop(
        self,
        job_id: str,
        generation: int,
        stop_event: threading.Event,
        lease_lost: threading.Event,
    ) -> None:
        while not stop_event.is_set() and not self.stopping:
            try:
                if not self._renew_lease(job_id, generation):
                    lease_lost.set()
                    return
            except Exception:
                logger.exception("Lease renewal failed job_id=%s", job_id)
            if stop_event.wait(self.lease_renew_interval):
                return

    def _stage(
        self,
        job_id: str,
        generation: int,
        stage: str,
        progress: int,
        message: str,
        level: str = "info",
    ) -> None:
        now = utcnow()
        with self.db.session() as session:
            job = self._fence(session, job_id, generation)
            # Recovery restarts the deterministic pipeline from a safe boundary,
            # but externally visible progress and events must never move backward.
            highest_event_progress = session.scalar(
                select(func.max(JobEvent.progress)).where(JobEvent.job_id == job.id)
            )
            progress = max(job.progress, highest_event_progress or 0, progress)
            job.stage = stage
            job.progress = progress
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(seconds=self.settings.job_lease_seconds)
            session.add(
                JobEvent(
                    job_id=job.id,
                    stage=stage,
                    progress=progress,
                    message=message,
                    level=level,
                )
            )
            heartbeat = session.get(WorkerHeartbeat, 1)
            if heartbeat is None:
                session.add(WorkerHeartbeat(id=1, worker_id=self.worker_id, heartbeat_at=now))
            else:
                heartbeat.worker_id = self.worker_id
                heartbeat.heartbeat_at = now
        if self.settings.stage_delay_seconds > 0:
            time.sleep(self.settings.stage_delay_seconds)

    def _consume_fault(self) -> str:
        with self.db.session() as session:
            control = session.get(FaultControl, 1)
            if control is None:
                return "none"
            mode = control.next_mode
            control.next_mode = "none"
            control.updated_at = utcnow()
            return mode

    def _load_source(self, job_id: str) -> tuple[Job, Project, Source]:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise WorkerFailure("JOB_NOT_FOUND", "任务不存在")
            project = session.get(Project, job.project_id)
            source = session.scalar(select(Source).where(Source.project_id == job.project_id))
            if project is None or source is None:
                raise WorkerFailure("SOURCE_NOT_FOUND", "项目输入源不存在")
            session.expunge(job)
            session.expunge(project)
            session.expunge(source)
            return job, project, source

    def _transcribe(self, source: Source) -> tuple[str, str]:
        if source.kind == "text" and source.content:
            return source.content, "direct-text"
        if not source.storage_path:
            raise WorkerFailure("SOURCE_FILE_MISSING", "上传文件记录缺少存储路径")
        path = Path(source.storage_path)
        if not path.is_file():
            raise WorkerFailure("SOURCE_FILE_MISSING", "上传文件已丢失，无法继续处理")
        try:
            return transcribe_file(path, source.mime_type, self.settings)
        except TranscriptionError as exc:
            raise WorkerFailure(exc.code, exc.message, exc.retryable, exc.category) from exc

    def _assets(self) -> list[dict]:
        with self.db.session() as session:
            assets = session.scalars(
                select(Asset).where(Asset.active.is_(True)).order_by(Asset.created_at, Asset.id)
            ).all()
            return [
                {
                    "id": asset.id,
                    "name": asset.name,
                    "tags": json.loads(asset.tags_json),
                    "keywords": json.loads(asset.keywords_json),
                }
                for asset in assets
            ]

    def _persist(
        self,
        job_id: str,
        generation: int,
        transcript: str,
        segment_payloads: list[dict],
        ranked_payloads: list[list],
        ranking_traces: list[RankingTrace],
        assets: list[dict],
        enhancement: SemanticEnhancement,
        transcriber: str,
        matching_duration_ms: int,
    ) -> None:
        with self.db.session() as session:
            job = self._fence(session, job_id, generation)
            project = session.get(Project, job.project_id)
            source = session.scalar(select(Source).where(Source.project_id == job.project_id))
            if project is None or source is None:
                raise WorkerFailure("PROJECT_NOT_FOUND", "项目或输入源不存在")

            old_ids = session.scalars(select(Segment.id).where(Segment.project_id == project.id)).all()
            if old_ids:
                session.execute(delete(Selection).where(Selection.segment_id.in_(old_ids)))
                session.execute(delete(Recommendation).where(Recommendation.segment_id.in_(old_ids)))
                session.execute(delete(Segment).where(Segment.id.in_(old_ids)))
                session.flush()

            source.transcript_text = transcript
            created_segments: list[Segment] = []
            for index, payload in enumerate(segment_payloads):
                segment = Segment(
                    project_id=project.id,
                    position=index,
                    text=payload["text"],
                    topic=payload["topic"],
                    keywords_json=dumps(payload["keywords"]),
                    version=1,
                )
                session.add(segment)
                created_segments.append(segment)
            session.flush()

            semantic_run = AIRun(
                project_id=project.id,
                job_id=job.id,
                operation="semantic_segmentation",
                provider=enhancement.provider,
                model=enhancement.model,
                prompt_version="semantic-segments-v1",
                input_hash=stable_hash(transcript, "semantic-segments-v1"),
                status=enhancement.status,
                degraded=enhancement.degraded,
                duration_ms=enhancement.duration_ms,
                output_summary_json=dumps(
                    {
                        "segments": len(created_segments),
                        "transcriber": transcriber,
                        "tokens": enhancement.usage,
                        "fallback": enhancement.degraded,
                    }
                ),
                error_message=enhancement.error_message,
            )
            session.add(semantic_run)
            session.flush()
            trace_payloads = [
                {
                    "segment_position": index,
                    "provider": trace.provider,
                    "model": trace.model,
                    "source": trace.source,
                    "degraded": trace.degraded,
                    "error_message": trace.error_message,
                }
                for index, trace in enumerate(ranking_traces)
            ]
            sources = {trace.source for trace in ranking_traces}
            providers = {trace.provider for trace in ranking_traces}
            models = sorted({trace.model for trace in ranking_traces})
            matching_degraded = any(trace.degraded for trace in ranking_traces)
            matching_run = AIRun(
                project_id=project.id,
                job_id=job.id,
                operation="asset_matching",
                provider=(
                    next(iter(providers))
                    if sources == {"embedding"} and len(providers) == 1
                    else "hybrid-fallback"
                ),
                model=" + ".join(models) or "char-ngram-tfidf",
                prompt_version="hybrid-ranker-v2",
                input_hash=stable_hash(
                    transcript,
                    dumps(
                        [
                            {
                                "text": item["text"],
                                "topic": item["topic"],
                                "keywords": item["keywords"],
                            }
                            for item in segment_payloads
                        ]
                    ),
                    dumps(assets),
                    "hybrid-ranker-v2",
                ),
                status="degraded" if matching_degraded else "succeeded",
                degraded=matching_degraded,
                duration_ms=matching_duration_ms,
                output_summary_json=dumps(
                    {
                        "candidates_per_segment": [len(items) for items in ranked_payloads],
                        "weights": {"semantic": 0.55, "keyword": 0.30, "tag_topic": 0.15},
                        "traces": trace_payloads,
                    }
                ),
                error_message=next(
                    (trace.error_message for trace in ranking_traces if trace.error_message),
                    None,
                ),
            )
            session.add(matching_run)
            session.flush()
            for segment, ranked in zip(created_segments, ranked_payloads):
                for item in ranked:
                    session.add(
                        Recommendation(
                            run_id=matching_run.id,
                            segment_id=segment.id,
                            asset_id=item.asset_id,
                            rank=item.rank,
                            total_score=item.total_score,
                            tfidf_score=item.tfidf_score,
                            keyword_score=item.keyword_score,
                            tag_score=item.tag_score,
                            matched_terms_json=dumps(item.matched_terms),
                            explanation=item.explanation,
                            is_diversity_filler=item.is_diversity_filler,
                        )
                    )
                session.add(Selection(segment_id=segment.id, asset_id=ranked[0].asset_id, source="auto"))
            project.status = "ready"
            job.status = "succeeded"
            job.stage = "completed"
            job.progress = 100
            job.retryable = False
            job.error_code = None
            job.error_message = None
            job.finished_at = utcnow()
            job.lease_owner = None
            job.lease_expires_at = None
            session.add(
                JobEvent(
                    job_id=job.id,
                    stage="completed",
                    progress=100,
                    message=f"处理完成：生成 {len(created_segments)} 个片段，每段已保存至少 3 个候选",
                )
            )
            add_audit(
                session,
                project.id,
                "job",
                job.id,
                "job.succeeded",
                after={
                    "segments": len(created_segments),
                    "degraded": enhancement.degraded or matching_degraded,
                    "semantic_run_id": semantic_run.id,
                    "matching_run_id": matching_run.id,
                },
                actor="worker",
            )

    def _fail(self, job_id: str, generation: int, failure: WorkerFailure) -> None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.status == "canceled":
                return
            try:
                job = self._fence(session, job_id, generation)
            except (JobCanceled, WorkerFailure):
                logger.info(
                    "Ignore stale failure job_id=%s owner=%s generation=%s",
                    job_id,
                    self.worker_id,
                    generation,
                )
                return
            project = session.get(Project, job.project_id)
            manually_rearmable = failure.category in MANUALLY_REARMABLE_FAILURE_CATEGORIES
            if failure.retryable and manually_rearmable and job.attempt >= job.max_attempts:
                job.max_attempts = job.attempt + 1
            can_retry = failure.retryable and (
                manually_rearmable or job.attempt < job.max_attempts
            )
            job.status = "failed"
            job.error_code = failure.code
            job.error_message = failure.message
            job.retryable = can_retry
            job.finished_at = utcnow()
            job.lease_owner = None
            job.lease_expires_at = None
            if project and job.kind == "pipeline":
                project.status = "failed"
            if job.kind == "preview":
                preview = session.scalar(
                    select(PreviewRender).where(PreviewRender.job_id == job.id)
                )
                if preview:
                    preview.status = "failed"
                    preview.error_message = failure.message
            category_label = FAILURE_CATEGORY_LABELS.get(failure.category, failure.category)
            session.add(
                JobEvent(
                    job_id=job.id,
                    stage=job.stage,
                    progress=job.progress,
                    message=(
                        f"第 {job.attempt} 次执行失败"
                        f"（{category_label} / {failure.code}）：{failure.message}"
                    ),
                    level="error",
                )
            )
            session.add(
                AIRun(
                    project_id=job.project_id,
                    job_id=job.id,
                    operation="preview_failure" if job.kind == "preview" else "pipeline_failure",
                    provider="worker",
                    model="durable-worker-v1",
                    prompt_version="n/a",
                    input_hash=hashlib.sha256(job.id.encode()).hexdigest(),
                    status="failed",
                    degraded=False,
                    error_message=f"{failure.code}: {failure.message}",
                    output_summary_json="{}",
                )
            )
            add_audit(
                session,
                job.project_id,
                "job",
                job.id,
                "job.failed",
                after={
                    "attempt": job.attempt,
                    "category": failure.category,
                    "code": failure.code,
                    "message": failure.message,
                    "retryable": can_retry,
                },
                actor="worker",
            )

    def _process_preview(self, job_id: str, generation: int) -> None:
        started = time.perf_counter()
        temporary_output_path: Path | None = None
        try:
            self._stage(job_id, generation, "preview_planning", 8, "正在校验时间线与已选素材")
            with self.db.session() as session:
                job = self._fence(session, job_id, generation)
                preview = session.scalar(
                    select(PreviewRender).where(PreviewRender.job_id == job.id)
                )
                if preview is None:
                    raise WorkerFailure("PREVIEW_NOT_FOUND", "预览任务记录不存在")
                plan = build_preview_plan(session, job.project_id)
                if plan["input_hash"] != preview.input_hash:
                    raise WorkerFailure(
                        "PREVIEW_INPUT_CHANGED",
                        "字幕顺序或素材选择已变化，请重新生成预览",
                        False,
                        "input",
                    )

            self._stage(job_id, generation, "preview_rendering", 35, "正在组合素材并生成 H.264 预览视频")
            output_dir = self.settings.data_dir / "media" / "previews" / plan["project_id"]
            output_path = output_dir / f"{plan['input_hash'][:20]}.mp4"
            temporary_output_path = output_dir / (
                f".{plan['input_hash'][:20]}.{job_id}.rendering.mp4"
            )
            temporary_output_path.unlink(missing_ok=True)
            try:
                result = render_preview(
                    plan,
                    temporary_output_path,
                    timeout=min(
                        self.settings.preview_timeout,
                        self.settings.job_max_execution_seconds,
                    ),
                )
            except PreviewRenderError as exc:
                message = str(exc)
                dependency = "未安装 ffmpeg" in message
                raise WorkerFailure(
                    "PREVIEW_FFMPEG_MISSING" if dependency else "PREVIEW_RENDER_FAILED",
                    message,
                    not dependency,
                    "dependency" if dependency else "processing",
                ) from exc

            self._stage(job_id, generation, "preview_finalizing", 92, "正在保存预览结果与可追溯记录")
            with self.db.session() as session:
                job = self._fence(session, job_id, generation)
                preview = session.scalar(
                    select(PreviewRender).where(PreviewRender.job_id == job.id)
                )
                if preview is None:
                    raise WorkerFailure("PREVIEW_NOT_FOUND", "预览任务记录不存在")
                output_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary_output_path, output_path)

                def cleanup_failed_publish(_session) -> None:
                    output_path.unlink(missing_ok=True)

                event.listen(session, "after_rollback", cleanup_failed_publish, once=True)
                result["output_path"] = str(output_path)
                preview.status = "succeeded"
                preview.storage_path = str(output_path)
                preview.output_url = (
                    f"/media/previews/{plan['project_id']}/{output_path.name}"
                )
                preview.duration_ms = int(plan["duration_ms"])
                preview.segment_count = int(plan["segment_count"])
                preview.error_message = None
                job.status = "succeeded"
                job.stage = "completed"
                job.progress = 100
                job.retryable = False
                job.error_code = None
                job.error_message = None
                job.finished_at = utcnow()
                job.lease_owner = None
                job.lease_expires_at = None
                run = AIRun(
                    project_id=job.project_id,
                    job_id=job.id,
                    operation="preview_render",
                    provider="ffmpeg",
                    model="h264-storyboard-v1",
                    prompt_version="timeline-v1",
                    input_hash=preview.input_hash,
                    status="succeeded",
                    degraded=not bool(result["subtitles_burned"]),
                    duration_ms=max(0, int((time.perf_counter() - started) * 1_000)),
                    output_summary_json=dumps(
                        {
                            **result,
                            "output_url": preview.output_url,
                            "timeline_version": "v1",
                        }
                    ),
                )
                session.add(run)
                session.flush()
                session.add(
                    JobEvent(
                        job_id=job.id,
                        stage="completed",
                        progress=100,
                        message="预览视频已生成，可在工作台直接播放",
                    )
                )
                add_audit(
                    session,
                    job.project_id,
                    "preview",
                    preview.id,
                    "preview.succeeded",
                    after={
                        "job_id": job.id,
                        "run_id": run.id,
                        "output_url": preview.output_url,
                        "input_hash": preview.input_hash,
                    },
                    actor="worker",
                )
        except APIError as exc:
            self._fail(
                job_id,
                generation,
                WorkerFailure(exc.code, exc.message, exc.retryable, "input"),
            )
        except JobCanceled:
            logger.info("Preview job canceled job_id=%s", job_id)
        except WorkerFailure as failure:
            logger.warning("Preview job failed job_id=%s code=%s", job_id, failure.code)
            self._fail(job_id, generation, failure)
        except Exception:
            logger.exception("Unexpected preview failure job_id=%s", job_id)
            self._fail(
                job_id,
                generation,
                WorkerFailure("PREVIEW_UNEXPECTED_ERROR", "预览生成发生未预期错误", True),
            )
        finally:
            if temporary_output_path is not None:
                temporary_output_path.unlink(missing_ok=True)

    def _process_pipeline(self, job_id: str, generation: int) -> None:
        started = time.perf_counter()
        try:
            self._stage(job_id, generation, "validating", 5, "正在校验输入内容、文件类型与持久化状态")
            job, _project, source = self._load_source(job_id)
            fault = self._consume_fault()

            self._stage(job_id, generation, "extracting", 15, "正在读取输入源并准备字幕内容")
            if fault == "job_fail":
                raise WorkerFailure("DEMO_JOB_FAILURE", "演示故障：本次处理按计划失败，可点击重试", True)

            self._stage(job_id, generation, "transcribing", 30, "正在获取或整理原始字幕")
            transcript, transcriber = self._transcribe(source)
            transcript = transcript.strip()
            if len(transcript) < 2:
                raise WorkerFailure("TRANSCRIPT_EMPTY", "输入中没有可处理的字幕内容")

            self._stage(job_id, generation, "segmenting", 48, "正在按句意和长度生成语义片段")
            if fault == "ai_degrade":
                enhancement = SemanticEnhancement(
                    segments=rule_segments(transcript),
                    provider="rules",
                    model="rule-nlp-v1",
                    degraded=True,
                    status="degraded",
                    duration_ms=0,
                    error_message="演示故障注入：已强制使用规则降级",
                )
                self._stage(
                    job_id,
                    generation,
                    "keywording",
                    62,
                    "模拟 AI 服务不可用：已自动切换确定性规则分段与关键词提取",
                    "warning",
                )
            else:
                enhancement = enhance_semantic_segments(transcript, self.settings)
                self._stage(
                    job_id,
                    generation,
                    "keywording",
                    62,
                    (
                        f"LLM 语义增强不可用：{enhancement.error_message}"
                        if enhancement.degraded
                        else "正在提取关键词并识别主题"
                    ),
                    "warning" if enhancement.degraded else "info",
                )
            segment_payloads = enhancement.segments
            if not segment_payloads:
                raise WorkerFailure("SEGMENTATION_EMPTY", "未能从字幕中生成有效片段")

            self._stage(job_id, generation, "matching", 80, "正在执行可解释的混合素材排序")
            assets = self._assets()
            if len(assets) < 3:
                raise WorkerFailure("INSUFFICIENT_ASSETS", "启用素材少于 3 个，无法完成候选匹配")
            ranked_payloads = []
            ranking_traces: list[RankingTrace] = []
            active_scorer = self._semantic_scorer
            matching_started = time.perf_counter()
            for item in segment_payloads:
                ranked, ranking_trace = rank_assets_with_trace(
                    item["text"],
                    item["topic"],
                    item["keywords"],
                    assets,
                    minimum=3,
                    semantic_scorer=active_scorer,
                )
                ranked_payloads.append(ranked)
                ranking_traces.append(ranking_trace)
                if ranking_trace.degraded:
                    # Circuit-break the failing provider for the remainder of
                    # this task instead of paying one timeout per segment.
                    active_scorer = None
            if any(len(items) < 3 for items in ranked_payloads):
                raise WorkerFailure("MATCHING_INCOMPLETE", "至少一个片段未获得 3 个唯一素材候选")
            matching_duration_ms = max(0, int((time.perf_counter() - matching_started) * 1_000))

            self._stage(job_id, generation, "persisting", 95, "正在事务化保存片段、候选、默认选择与追踪记录")
            self._persist(
                job_id,
                generation,
                transcript,
                segment_payloads,
                ranked_payloads,
                ranking_traces,
                assets,
                enhancement,
                transcriber,
                matching_duration_ms,
            )
        except JobCanceled:
            logger.info("Job canceled job_id=%s", job_id)
        except WorkerFailure as failure:
            logger.warning("Job failed job_id=%s code=%s", job_id, failure.code)
            self._fail(job_id, generation, failure)
        except Exception as exc:
            logger.exception("Unexpected worker failure job_id=%s", job_id)
            self._fail(
                job_id,
                generation,
                WorkerFailure("UNEXPECTED_WORKER_ERROR", "后台处理发生未预期错误", True),
            )

    def process(self, job_id: str, generation: int | None = None) -> None:
        generation = generation or self._claimed_generations.pop(job_id, None)
        if generation is None:
            with self.db.session() as session:
                job = session.get(Job, job_id)
                if job is None or job.lease_owner != self.worker_id:
                    return
                generation = job.execution_generation
        with self.db.session() as session:
            current = session.get(Job, job_id)
            if current is None:
                return
            job_kind = current.kind

        stop_event = threading.Event()
        lease_lost = threading.Event()
        completed = threading.Event()
        renewer = threading.Thread(
            target=self._lease_loop,
            args=(job_id, generation, stop_event, lease_lost),
            name=f"frameflow-lease-{job_id[:8]}",
            daemon=True,
        )

        def run_pipeline() -> None:
            try:
                if job_kind == "preview":
                    self._process_preview(job_id, generation)
                else:
                    self._process_pipeline(job_id, generation)
            finally:
                completed.set()

        pipeline = threading.Thread(
            target=run_pipeline,
            name=f"frameflow-job-{job_id[:8]}",
            daemon=True,
        )
        renewer.start()
        pipeline.start()
        deadline = time.monotonic() + self.settings.job_max_execution_seconds
        try:
            while not completed.wait(0.05):
                if lease_lost.is_set():
                    return
                if self.stopping:
                    self._abandon_execution(job_id, generation)
                    return
                if time.monotonic() >= deadline:
                    self._timed_out_pipelines.add(pipeline)
                    self._isolation_detail = (
                        f"job {job_id} exceeded the hard timeout; "
                        "waiting for its execution thread to exit"
                    )
                    self.heartbeat()
                    self._fail(
                        job_id,
                        generation,
                        WorkerFailure(
                            "JOB_TIMEOUT",
                            f"任务执行超过 {self.settings.job_max_execution_seconds:g} 秒",
                            True,
                        ),
                    )
                    return
        finally:
            stop_event.set()
            renewer.join(timeout=max(0.2, self.lease_renew_interval * 2))

    def run_once(self) -> bool:
        self.heartbeat()
        job_id = self.claim()
        if job_id is None:
            return False
        self.process(job_id)
        return True

    def run_forever(self) -> None:
        logger.info("FrameFlow worker started worker_id=%s", self.worker_id)
        while not self.stopping:
            try:
                processed = self.run_once()
                if not processed:
                    time.sleep(self.settings.worker_poll_seconds)
            except Exception:
                logger.exception("Worker loop error")
                time.sleep(min(5.0, max(0.2, self.settings.worker_poll_seconds)))
        logger.info("FrameFlow worker stopped")


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings.from_env()
    database = Database(settings)
    database.initialize()
    worker = DurableWorker(database, settings)

    def stop(_signum, _frame):
        worker.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
