from __future__ import annotations

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from ..asr import REARMABLE_ASR_ERROR_CODES
from ..errors import APIError
from ..models import FaultControl, Job, JobEvent, PreviewRender, utcnow
from ..serializers import event_dict, job_dict
from .common import _get_job, _get_project, add_audit


def get_job_detail(session: Session, job_id: str) -> dict:
    job = _get_job(session, job_id)
    events = session.scalars(
        select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.created_at, JobEvent.id)
    ).all()
    return {"job": job_dict(job), "events": [event_dict(event) for event in events]}


def retry_job(session: Session, job_id: str, request_id: str | None) -> dict:
    job = _get_job(session, job_id)
    rearmable = job.error_code in REARMABLE_ASR_ERROR_CODES
    if job.status != "failed":
        raise APIError(409, "INVALID_STATE", "只有失败且明确可重试的任务才能重试")
    if not job.retryable and not rearmable:
        raise APIError(409, "JOB_NOT_RETRYABLE", "该任务失败原因不可重试")
    next_max_attempts = job.max_attempts
    if job.attempt >= job.max_attempts:
        if not rearmable:
            raise APIError(409, "JOB_ATTEMPTS_EXHAUSTED", "任务已达到最大尝试次数")
        next_max_attempts = job.attempt + 1
    before = {
        "status": job.status,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "retryable": job.retryable,
        "attempt": job.attempt,
    }
    result = session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == "failed", Job.attempt == job.attempt)
        .values(
            status="queued",
            stage="preview_planning" if job.kind == "preview" else "validating",
            retryable=True if rearmable else job.retryable,
            max_attempts=next_max_attempts,
            finished_at=None,
            next_run_at=utcnow(),
            lease_owner=None,
            lease_expires_at=None,
        )
    )
    if result.rowcount != 1:
        session.expire_all()
        raise APIError(409, "JOB_RETRY_CONFLICT", "任务状态已变化，请刷新后再试")
    session.expire(job)
    job = session.get(Job, job.id)
    project = _get_project(session, job.project_id)
    if job.kind == "pipeline":
        project.status = "queued"
    else:
        preview = session.scalar(select(PreviewRender).where(PreviewRender.job_id == job.id))
        if preview:
            preview.status = "queued"
            preview.error_message = None
    session.add(
        JobEvent(
            job_id=job.id,
            stage=job.stage,
            progress=job.progress,
            message=(
                f"已请求重新执行原任务；保留第 {job.attempt} 次失败"
                f"（{job.error_code or 'UNKNOWN'}：{job.error_message or '未提供错误原因'}）"
            ),
        )
    )
    add_audit(
        session,
        project.id,
        "job",
        job.id,
        "job.retried",
        before=before,
        after={
            "status": "queued",
            "next_attempt": job.attempt + 1,
            "project_reused": True,
            "source_reused": True,
        },
        request_id=request_id,
    )
    session.flush()
    return get_job_detail(session, job.id)


def cancel_job(session: Session, job_id: str, request_id: str | None) -> dict:
    # A SQLite read transaction cannot safely upgrade after a concurrent
    # writer commits (SQLITE_BUSY_SNAPSHOT).  Acquire the short write lock
    # before loading the state so the following CAS observes a current row.
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    job = _get_job(session, job_id)
    if job.status == "canceled":
        return get_job_detail(session, job.id)
    if job.status in {"succeeded", "failed"}:
        raise APIError(409, "JOB_NOT_CANCELABLE", "只有排队中或运行中的任务可以取消")
    # Completion and cancellation can race in separate API/worker sessions.
    # Make cancellation a compare-and-swap on the durable state rather than
    # flushing the possibly stale object loaded above.  Whichever transition
    # commits first wins; cancellation must never overwrite a successful job.
    result = session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status.in_(("queued", "running")))
        .values(
            status="canceled",
            retryable=False,
            finished_at=utcnow(),
            lease_owner=None,
            lease_expires_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        session.expire_all()
        current = session.get(Job, job.id)
        if current is None:
            raise APIError(404, "JOB_NOT_FOUND", "任务不存在")
        if current.status == "canceled":
            return get_job_detail(session, current.id)
        raise APIError(409, "JOB_NOT_CANCELABLE", "只有排队中或运行中的任务可以取消")

    session.expire_all()
    job = session.get(Job, job.id)
    assert job is not None
    project = _get_project(session, job.project_id)
    if job.kind == "pipeline":
        project.status = "canceled"
    else:
        preview = session.scalar(select(PreviewRender).where(PreviewRender.job_id == job.id))
        if preview:
            preview.status = "canceled"
    session.add(
        JobEvent(job_id=job.id, stage=job.stage, progress=job.progress, message="任务已由用户取消", level="warning")
    )
    add_audit(
        session,
        project.id,
        "job",
        job.id,
        "job.canceled",
        after={"stage": job.stage, "progress": job.progress},
        request_id=request_id,
    )
    session.flush()
    return get_job_detail(session, job.id)


def set_fault(session: Session, mode: str, request_id: str | None) -> dict:
    control = session.get(FaultControl, 1)
    if control is None:
        control = FaultControl(id=1, next_mode=mode)
        session.add(control)
    else:
        control.next_mode = mode
        control.updated_at = utcnow()
    add_audit(
        session,
        None,
        "demo_fault",
        "1",
        "demo.fault_configured",
        after={"mode": mode},
        request_id=request_id,
    )
    session.flush()
    return {
        "mode": mode,
        "message": {
            "none": "已清除下一次故障",
            "ai_degrade": "下一次任务将模拟 AI 不可用并自动规则降级",
            "job_fail": "下一次任务将模拟可重试的处理失败",
        }[mode],
    }
