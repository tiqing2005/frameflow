import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle2,
  Circle,
  Clock3,
  FileSearch,
  Info,
  LoaderCircle,
  RotateCcw,
  Sparkles,
  Square,
  WandSparkles,
} from 'lucide-react'
import { api, ApiError, errorMessage } from '../api'
import { ErrorState, formatDate, InlineSpinner, PageLoader, StatusPill, useToast } from '../components/ui'
import { AppLink, navigate } from '../router'
import type { JobDetail, JobStage, Project } from '../types'

const stages: { id: JobStage; title: string; description: string }[] = [
  { id: 'validating', title: '校验输入', description: '检查内容完整性与格式' },
  { id: 'extracting', title: '提取内容', description: '读取文本或媒体音轨' },
  { id: 'transcribing', title: '语音转写', description: '生成可编辑的原始字幕' },
  { id: 'segmenting', title: '语义分段', description: '识别主题与叙事节奏' },
  { id: 'keywording', title: '关键词理解', description: '提取画面线索与语义标签' },
  { id: 'matching', title: '素材匹配', description: '计算候选得分与解释' },
  { id: 'persisting', title: '保存结果', description: '持久化分段、候选和追踪记录' },
]

const stageIndex = (stage?: string) => Math.max(0, stages.findIndex((item) => item.id === stage))

export function ProcessingPage({ projectId, initialJobId }: { projectId: string; initialJobId?: string }) {
  const [project, setProject] = useState<Project | null>(null)
  const [jobDetail, setJobDetail] = useState<JobDetail | null>(null)
  const [jobId, setJobId] = useState(initialJobId)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [errorRequestId, setErrorRequestId] = useState('')
  const [action, setAction] = useState<'retry' | 'cancel' | null>(null)
  const timerRef = useRef<number | null>(null)
  const toast = useToast()

  const fetchState = useCallback(async (showLoader = false): Promise<string | undefined> => {
    if (showLoader) setLoading(true)
    try {
      const detail = await api.project(projectId)
      setProject(detail.project)
      setError('')
      setErrorRequestId('')
      const nextJobId = jobId || detail.current_job?.id
      if (nextJobId) {
        setJobId(nextJobId)
        const nextJob = await api.job(nextJobId)
        setJobDetail(nextJob)
        return nextJob.job.status
      } else if (detail.project.status === 'ready') {
        setJobDetail(null)
        return 'succeeded'
      }
      setError('')
    } catch (err) {
      setError(errorMessage(err))
      setErrorRequestId(err instanceof ApiError ? err.requestId || '' : '')
      return undefined
    } finally {
      setLoading(false)
    }
  }, [jobId, projectId])

  useEffect(() => {
    let alive = true
    const poll = async () => {
      const status = await fetchState(true)
      if (!alive) return
      if (!status || status === 'queued' || status === 'running') timerRef.current = window.setTimeout(poll, 1400)
    }
    void poll()
    return () => {
      alive = false
      if (timerRef.current) window.clearTimeout(timerRef.current)
    }
  }, [fetchState])

  const job = jobDetail?.job
  const activeIndex = stageIndex(job?.stage)
  const completed = job?.status === 'succeeded' || project?.status === 'ready'
  const failed = job?.status === 'failed' || project?.status === 'failed'
  const canceled = job?.status === 'canceled' || project?.status === 'canceled'
  const progress = completed ? 100 : Math.max(2, Math.min(99, job?.progress ?? 4))
  const elapsed = useMemo(() => {
    if (!job?.started_at) return null
    const end = job.finished_at ? new Date(job.finished_at).getTime() : Date.now()
    return Math.max(0, Math.round((end - new Date(job.started_at).getTime()) / 1000))
  }, [job?.finished_at, job?.started_at])

  const retry = async () => {
    if (!job || action) return
    setAction('retry')
    try {
      const result = await api.retryJob(job.id)
      const next = result.job
      setJobId(next.id)
      setJobDetail({ job: next, events: [] })
      toast('任务已重新进入队列', 'success')
    } catch (err) {
      toast(errorMessage(err), 'error')
    } finally {
      setAction(null)
    }
  }

  const cancel = async () => {
    if (!job || action || !window.confirm('确定取消这次处理吗？已生成的数据会保留。')) return
    setAction('cancel')
    try {
      await api.cancelJob(job.id)
      toast('任务已取消', 'success')
      await fetchState()
    } catch (err) {
      toast(errorMessage(err), 'error')
    } finally {
      setAction(null)
    }
  }

  if (loading && !project) return <main className="page"><PageLoader label="读取处理任务" /></main>

  return (
    <main className="page processing-page">
      <div className="page-back-row"><AppLink href="/projects" className="back-link"><ArrowLeft size={17} /> 返回项目台</AppLink></div>
      {error && !project ? <ErrorState message={error} onRetry={() => void fetchState(true)} /> : (
        <div className="processing-layout">
          <section className="processing-main">
            <div className={`processing-hero ${failed ? 'failed' : canceled ? 'canceled' : completed ? 'completed' : ''}`}>
              <div className="processing-orbit">
                {failed ? <AlertTriangle size={31} /> : canceled ? <Square size={26} /> : completed ? <Check size={32} /> : <Sparkles size={29} />}
                {!failed && !canceled && !completed && <i />}
              </div>
              <span className="eyebrow">{completed ? '处理完成' : failed ? '任务需要处理' : canceled ? '任务已停止' : 'FrameFlow AI 正在工作'}</span>
              <h1>{completed ? '画面候选已经准备好了' : failed ? '这次处理没有完成' : canceled ? '处理任务已取消' : '正在理解你的内容'}</h1>
              <p className="processing-title">{project?.title || '读取项目中…'}</p>
              {!failed && !canceled && (
                <div className="overall-progress">
                  <div className="progress-label"><span>{completed ? '全部阶段完成' : stages[activeIndex]?.title || '准备任务'}</span><strong>{progress}%</strong></div>
                  <div className="progress-track"><span style={{ width: `${progress}%` }} /></div>
                  <div className="progress-meta"><span><Clock3 size={14} /> {elapsed == null ? '正在排队' : `已用时 ${elapsed} 秒`}</span><span>结果会自动保存，可安全离开此页</span></div>
                </div>
              )}
              {completed && (
                <button className="button button-primary button-large success-action" type="button" onClick={() => navigate(`/projects/${projectId}`)}>
                  进入三栏工作台 <ArrowRight size={18} />
                </button>
              )}
              {(failed || canceled) && (
                <div className="failure-panel" role="alert">
                  <div><strong>{job?.error_code || (canceled ? 'TASK_CANCELED' : 'PROCESSING_FAILED')}</strong><p>{job?.error_message || (canceled ? '任务已由用户取消，可重新提交处理。' : '服务返回了未说明的错误，请尝试重试。')}</p></div>
                  {failed && job?.retryable === true && <button type="button" className="button button-primary" disabled={action !== null} onClick={() => void retry()}>{action === 'retry' ? <InlineSpinner label="正在重试" /> : <><RotateCcw size={17} /> 重试任务</>}</button>}
                </div>
              )}
            </div>

            {!completed && !failed && !canceled && (
              <div className="stage-card">
                <div className="section-heading compact"><div><h2>处理进度</h2><p>来自服务器的真实持久化阶段</p></div><StatusPill status={job?.status || 'queued'} pulse /></div>
                <ol className="stage-list">
                  {stages.map((stage, index) => {
                    const isDone = index < activeIndex || job?.stage === 'completed'
                    const isActive = index === activeIndex
                    return (
                      <li key={stage.id} className={isDone ? 'done' : isActive ? 'active' : ''}>
                        <span className="stage-state">{isDone ? <Check size={15} /> : isActive ? <LoaderCircle className="spin" size={16} /> : <Circle size={13} />}</span>
                        <div><strong>{stage.title}</strong><p>{isActive ? jobDetail?.events.at(-1)?.message || stage.description : stage.description}</p></div>
                        {isDone && <span className="stage-complete">完成</span>}
                        {isActive && <span className="stage-active">进行中</span>}
                      </li>
                    )
                  })}
                </ol>
              </div>
            )}
          </section>

          <aside className="processing-aside">
            <div className="aside-card event-card">
              <div className="aside-card-head"><h3>实时事件</h3><span className="live-chip"><i /> LIVE</span></div>
              {jobDetail?.events.length ? (
                <div className="event-list">
                  {[...jobDetail.events].reverse().slice(0, 8).map((event, index) => (
                    <div className={`event-item level-${event.level || 'info'}`} key={event.id || `${event.created_at}-${index}`}>
                      <span className="event-dot" />
                      <div><strong>{event.message}</strong><span>{event.stage} · {formatDate(event.created_at)}</span></div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="mini-empty"><FileSearch size={23} /><p>任务事件将在这里实时出现</p></div>
              )}
            </div>
            <div className="aside-card safe-card"><CheckCircle2 size={20} /><div><strong>处理可恢复</strong><p>任务队列和每一步事件都写入数据库。服务重启后仍会从安全阶段继续。</p></div></div>
            <div className="aside-card task-identifiers"><strong>任务标识</strong><code>{job?.id || jobId || '等待创建'}</code>{errorRequestId && <><span>最近错误请求 ID</span><code>{errorRequestId}</code></>}</div>
            {error && <div className="inline-warning"><Info size={16} /> 最近一次轮询失败：{error}</div>}
            {!completed && !failed && !canceled && (
              <button type="button" className="button button-ghost cancel-button" disabled={action !== null} onClick={() => void cancel()}>
                {action === 'cancel' ? <InlineSpinner label="正在取消" /> : <><Square size={14} /> 取消处理</>}
              </button>
            )}
            <div className="processing-help"><WandSparkles size={17} /><p>匹配会返回<strong>至少三个</strong>候选，并展示三项分数组成与命中词。</p></div>
          </aside>
        </div>
      )}
    </main>
  )
}
