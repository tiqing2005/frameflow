from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import socket
import time
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, or_, select, text

from .asr import TranscriptionError, transcribe_file
from .config import Settings
from .db import Database
from .models import (
    AIRun,
    Asset,
    AuditEvent,
    FaultControl,
    Job,
    JobEvent,
    Project,
    Recommendation,
    Segment,
    Selection,
    Source,
    WorkerHeartbeat,
    utcnow,
)
from .nlp import extract_keywords, infer_topic, rank_assets, segment_text
from .services import add_audit, dumps, stable_hash

logger = logging.getLogger("frameflow.worker")


class WorkerFailure(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class JobCanceled(Exception):
    pass


class DurableWorker:
    def __init__(self, database: Database, settings: Settings, worker_id: str | None = None):
        self.db = database
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.stopping = False

    def heartbeat(self) -> None:
        now = utcnow()
        with self.db.session() as session:
            heartbeat = session.get(WorkerHeartbeat, 1)
            if heartbeat is None:
                session.add(WorkerHeartbeat(id=1, worker_id=self.worker_id, heartbeat_at=now))
            else:
                heartbeat.worker_id = self.worker_id
                heartbeat.heartbeat_at = now

    def claim(self) -> str | None:
        now = utcnow()
        lease_until = now + timedelta(seconds=self.settings.job_lease_seconds)
        with self.db.session() as session:
            # SQLite has no SKIP LOCKED. BEGIN IMMEDIATE serializes the very short
            # claim transaction, making the lease transition atomic.
            if self.settings.database_url.startswith("sqlite"):
                session.execute(text("BEGIN IMMEDIATE"))
            job = session.scalar(
                select(Job)
                .where(
                    or_(
                        (Job.status == "queued") & (Job.next_run_at <= now),
                        (Job.status == "running") & (Job.lease_expires_at < now),
                    )
                )
                .order_by(Job.next_run_at, Job.created_at)
                .limit(1)
            )
            if job is None:
                return None
            recovered = job.status == "running"
            job.status = "running"
            job.stage = "validating"
            job.progress = max(1, job.progress if recovered else 1)
            job.attempt += 1
            job.lease_owner = self.worker_id
            job.lease_expires_at = lease_until
            job.heartbeat_at = now
            job.error_code = None
            job.error_message = None
            job.retryable = False
            job.finished_at = None
            if job.started_at is None:
                job.started_at = now
            project = session.get(Project, job.project_id)
            if project:
                project.status = "processing"
            session.add(
                JobEvent(
                    job_id=job.id,
                    stage="validating",
                    progress=job.progress,
                    message=(
                        "检测到过期租约，Worker 已恢复任务并从安全阶段重跑"
                        if recovered
                        else f"Worker 已领取任务（第 {job.attempt} 次执行）"
                    ),
                    level="warning" if recovered else "info",
                )
            )
            return job.id

    def _stage(self, job_id: str, stage: str, progress: int, message: str, level: str = "info") -> None:
        now = utcnow()
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.status == "canceled":
                raise JobCanceled()
            if job.status != "running" or job.lease_owner != self.worker_id:
                raise WorkerFailure("JOB_LEASE_LOST", "任务租约已丢失，停止本次执行", True)
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
            raise WorkerFailure(exc.code, exc.message, exc.retryable) from exc

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
        transcript: str,
        segment_payloads: list[dict],
        ranked_payloads: list[list],
        degraded: bool,
        transcriber: str,
        started: float,
    ) -> None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.status == "canceled":
                raise JobCanceled()
            if job.status != "running" or job.lease_owner != self.worker_id:
                raise WorkerFailure("JOB_LEASE_LOST", "持久化前任务租约已丢失", True)
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

            run = AIRun(
                project_id=project.id,
                job_id=job.id,
                operation="pipeline_segment_and_match",
                provider="rules",
                model="char-ngram-tfidf-hybrid-v1",
                prompt_version="rules-v1",
                input_hash=stable_hash(transcript, "hybrid-v1"),
                status="succeeded",
                degraded=degraded,
                duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                output_summary_json=dumps(
                    {
                        "segments": len(created_segments),
                        "candidates_per_segment": [len(items) for items in ranked_payloads],
                        "transcriber": transcriber,
                        "weights": {"tfidf": 0.55, "keyword": 0.30, "tag_topic": 0.15},
                    }
                ),
            )
            session.add(run)
            session.flush()
            for segment, ranked in zip(created_segments, ranked_payloads):
                for item in ranked:
                    session.add(
                        Recommendation(
                            run_id=run.id,
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
                after={"segments": len(created_segments), "degraded": degraded, "run_id": run.id},
                actor="worker",
            )

    def _fail(self, job_id: str, failure: WorkerFailure) -> None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.status == "canceled":
                return
            project = session.get(Project, job.project_id)
            job.status = "failed"
            job.error_code = failure.code
            job.error_message = failure.message
            job.retryable = failure.retryable
            job.finished_at = utcnow()
            job.lease_owner = None
            job.lease_expires_at = None
            if project:
                project.status = "failed"
            session.add(
                JobEvent(
                    job_id=job.id,
                    stage=job.stage,
                    progress=job.progress,
                    message=f"处理失败：{failure.message}",
                    level="error",
                )
            )
            session.add(
                AIRun(
                    project_id=job.project_id,
                    job_id=job.id,
                    operation="pipeline_failure",
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
                after={"code": failure.code, "message": failure.message, "retryable": failure.retryable},
                actor="worker",
            )

    def process(self, job_id: str) -> None:
        started = time.perf_counter()
        try:
            self._stage(job_id, "validating", 5, "正在校验输入内容、文件类型与持久化状态")
            job, _project, source = self._load_source(job_id)
            fault = self._consume_fault()

            self._stage(job_id, "extracting", 15, "正在读取输入源并准备字幕内容")
            if fault == "job_fail":
                raise WorkerFailure("DEMO_JOB_FAILURE", "演示故障：本次处理按计划失败，可点击重试", True)

            self._stage(job_id, "transcribing", 30, "正在获取或整理原始字幕")
            transcript, transcriber = self._transcribe(source)
            transcript = transcript.strip()
            if len(transcript) < 2:
                raise WorkerFailure("TRANSCRIPT_EMPTY", "输入中没有可处理的字幕内容")

            self._stage(job_id, "segmenting", 48, "正在按句意和长度生成语义片段")
            raw_segments = segment_text(transcript)
            if not raw_segments:
                raise WorkerFailure("SEGMENTATION_EMPTY", "未能从字幕中生成有效片段")

            if fault == "ai_degrade":
                self._stage(
                    job_id,
                    "keywording",
                    62,
                    "模拟 AI 服务不可用：已自动切换确定性规则分段与关键词提取",
                    "warning",
                )
            else:
                self._stage(job_id, "keywording", 62, "正在提取关键词并识别主题")
            segment_payloads = []
            for value in raw_segments:
                keywords = extract_keywords(value, top_k=5)
                segment_payloads.append(
                    {"text": value, "keywords": keywords, "topic": infer_topic(value, keywords)}
                )

            self._stage(job_id, "matching", 80, "正在执行可解释的混合素材排序")
            assets = self._assets()
            if len(assets) < 3:
                raise WorkerFailure("INSUFFICIENT_ASSETS", "启用素材少于 3 个，无法完成候选匹配")
            ranked_payloads = [
                rank_assets(item["text"], item["topic"], item["keywords"], assets, minimum=3)
                for item in segment_payloads
            ]
            if any(len(items) < 3 for items in ranked_payloads):
                raise WorkerFailure("MATCHING_INCOMPLETE", "至少一个片段未获得 3 个唯一素材候选")

            self._stage(job_id, "persisting", 95, "正在事务化保存片段、候选、默认选择与追踪记录")
            self._persist(
                job_id,
                transcript,
                segment_payloads,
                ranked_payloads,
                fault == "ai_degrade",
                transcriber,
                started,
            )
        except JobCanceled:
            logger.info("Job canceled job_id=%s", job_id)
        except WorkerFailure as failure:
            logger.warning("Job failed job_id=%s code=%s", job_id, failure.code)
            self._fail(job_id, failure)
        except Exception as exc:
            logger.exception("Unexpected worker failure job_id=%s", job_id)
            self._fail(job_id, WorkerFailure("UNEXPECTED_WORKER_ERROR", "后台处理发生未预期错误", True))

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
        worker.stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    worker.run_forever()


if __name__ == "__main__":
    main()

