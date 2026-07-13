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

const configErrorCodes = new Set([
  'ASR_OPENAI_KEY_MISSING',
  'ASR_PROVIDER_INVALID',
  'ASR_PROVIDER_AUTH_ERROR',
  'ASR_PROVIDER_CONFIGURATION_ERROR',
  'ASR_MODEL_CONFIGURATION_ERROR',
])

const dependencyErrorCodes = new Set([
  'ASR_LOCAL_DEPENDENCY_MISSING',
  'ASR_LOCAL_RUNTIME_MISSING',
])

const transientErrorCodes = new Set([
  'ASR_TIMEOUT',
  'ASR_NETWORK_ERROR',
  'ASR_PROVIDER_TIMEOUT',
  'ASR_PROVIDER_RATE_LIMITED',
  'ASR_PROVIDER_UNAVAILABLE',
  'ASR_PROVIDER_RESPONSE_INVALID',
  'ASR_MODEL_DOWNLOAD_NETWORK_ERROR',
  'ASR_LOCAL_TIMEOUT',
  'ASR_LOCAL_BUSY',
])

const inputErrorCodes = new Set([
  'SUBTITLE_ENCODING',
  'ASR_INPUT_REJECTED',
  'ASR_INPUT_UNSUPPORTED',
  'ASR_NO_SPEECH',
  'SOURCE_FILE_MISSING',
  'TRANSCRIPT_EMPTY',
])

function failureGuidance(errorCode?: string | null, retryable = false) {
  if (errorCode && configErrorCodes.has(errorCode)) {
    return {
      title: 'ASR 配置需要修复',
      hint: retryable
        ? '请更新服务端 API Key、Provider、模型或服务地址。配置生效后可直接重新执行原任务，无需上传媒体。'
        : '请更新服务端 API Key、Provider、模型或服务地址。当前任务已不可重试或达到尝试上限。',
      action: '配置已修复，重新执行',
    }
  }
  if (errorCode && dependencyErrorCodes.has(errorCode)) {
    return {
      title: '本地 ASR 依赖缺失',
      hint: retryable
        ? '请安装本地 ASR 依赖或运行库并重启 Worker。依赖可用后可复用当前项目和原媒体重新执行。'
        : '请安装本地 ASR 依赖或运行库并重启 Worker。当前任务已不可重试或达到尝试上限。',
      action: '依赖已修复，重新执行',
    }
  }
  if (errorCode && transientErrorCodes.has(errorCode)) {
    return {
      title: 'ASR 服务暂时不可用',
      hint: retryable
        ? '这是超时、限流或网络类错误。请确认服务恢复后重新执行，原项目与媒体会继续保留。'
        : '这是超时、限流或网络类错误，但当前任务已不可重试或达到尝试上限。',
      action: '重新执行原任务',
    }
  }
  if (errorCode && inputErrorCodes.has(errorCode)) {
    return {
      title: '输入媒体无法处理',
      hint: '这是输入内容、编码或媒体格式的永久错误。请检查或更换源文件后创建新任务。',
      action: '重新执行原任务',
    }
  }
  return {
    title: retryable ? '任务可以重新执行' : '任务无法直接重试',
    hint: retryable
      ? '问题修复或服务恢复后，可复用当前项目与原媒体重新执行。'
      : '当前错误被标记为不可重试，请根据错误原因修复输入或联系管理员。',
    action: '重新执行原任务',
  }
}

export function ProcessingPage({ projectId, initialJobId }: { projectId: string; initialJobId?: string }) {
  const [project, setProject] = useState<Project | null>(null)
  const [jobDetail, setJobDetail] = useState<JobDetail | null>(null)
  const [jobId, setJobId] = useState(initialJobId)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [errorRequestId, setErrorRequestId] = useState('')
  const [action, setAction] = useState<'retry' | 'cancel' | null>(null)
  const [pollGeneration, setPollGeneration] = useState(0)
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
  }, [fetchState, pollGeneration])

  const job = jobDetail?.job
  const activeIndex = stageIndex(job?.stage)
  const completed = job ? job.status === 'succeeded' : project?.status === 'ready'
  const failed = job ? job.status === 'failed' : project?.status === 'failed'
  const canceled = job ? job.status === 'canceled' : project?.status === 'canceled'
  const progress = completed ? 100 : Math.max(2, Math.min(99, job?.progress ?? 4))
  const canRetry = failed
    && job?.status === 'failed'
    && job.retryable === true
    && (job.attempt ?? 0) < (job.max_attempts ?? Number.POSITIVE_INFINITY)
  const guidance = failureGuidance(job?.error_code, canRetry)
  const elapsed = useMemo(() => {
    if (!job?.started_at) return null
    const end = job.finished_at ? new Date(job.finished_at).getTime() : Date.now()
    return Math.max(0, Math.round((end - new Date(job.started_at).getTime()) / 1000))
  }, [job?.finished_at, job?.started_at])

  const retry = async () => {
    if (!job || action || job.status !== 'failed' || !canRetry) return
    setAction('retry')
    try {
      const result = await api.retryJob(job.id)
      const next = result.job
      setJobId(next.id)
      setJobDetail(result)
      setPollGeneration((value) => value + 1)
      toast('原任务已重新进入队列，媒体无需重新上传', 'success')
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
                  <div>
                    <strong>{canceled ? 'TASK_CANCELED' : job?.error_code || 'PROCESSING_FAILED'}</strong>
                    <h3>{canceled ? '任务已取消' : guidance.title}</h3>
                    <p>{canceled ? '任务已由用户取消。' : job?.error_message || '服务返回了未说明的错误。'}</p>
                    <p className="failure-guidance">{canceled ? '已取消任务不会被错误重试；原始媒体仍保留在项目中。' : guidance.hint}</p>
                    {job?.attempt != null && <span className="failure-attempt">已执行 {job.attempt} 次{job.max_attempts ? ` · 最多 ${job.max_attempts} 次` : ''}</span>}
                  </div>
                  {canRetry && <button type="button" className="button button-primary" disabled={action !== null} onClick={() => void retry()}>{action === 'retry' ? <InlineSpinner label="正在重新执行" /> : <><RotateCcw size={17} /> {guidance.action}</>}</button>}
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
            <div className="aside-card task-identifiers"><strong>任务标识</strong><button type="button" title="复制任务 ID" onClick={() => void navigator.clipboard.writeText(job?.id || jobId || '')}><code>{job?.id || jobId || '等待创建'}</code></button>{errorRequestId && <><span>最近错误请求 ID</span><button type="button" title="复制请求 ID" onClick={() => void navigator.clipboard.writeText(errorRequestId)}><code>{errorRequestId}</code></button></>}</div>
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
