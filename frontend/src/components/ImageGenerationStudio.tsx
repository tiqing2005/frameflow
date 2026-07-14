import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import {
  AlertTriangle,
  Check,
  Clock3,
  Download,
  ImagePlus,
  Info,
  LoaderCircle,
  RefreshCw,
  ShieldAlert,
  Trash2,
  X,
} from 'lucide-react'
import { api, ApiError, errorMessage, isAbortError, mediaUrl } from '../api'
import type {
  Asset,
  ImageAspectRatio,
  ImageGeneration,
  ImageGenerationAcceptResponse,
  Segment,
} from '../types'
import { formatDate, InlineSpinner, useToast } from './ui'

const POLL_INTERVAL = 1_200
const POLL_MAX_INTERVAL = 8_000
const FOCUSABLE_SELECTOR = 'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'

const aspectRatios: { value: ImageAspectRatio; label: string; hint: string }[] = [
  { value: '16:9', label: '横版 16:9', hint: '视频画面' },
  { value: '1:1', label: '方形 1:1', hint: '通用素材' },
  { value: '9:16', label: '竖版 9:16', hint: '移动端' },
]

const styles = [
  { value: 'none', label: '不指定，由模型判断', prompt: '' },
  { value: 'natural', label: '自然纪实', prompt: '画面风格：自然纪实摄影，真实光线和材质，避免过度修饰。' },
  { value: 'commercial', label: '商业摄影', prompt: '画面风格：克制的商业摄影，主体清晰，构图适合视频字幕叠加。' },
  { value: 'product', label: '极简产品', prompt: '画面风格：极简产品摄影，干净背景，柔和棚拍光，保留充足留白。' },
  { value: 'cinematic', label: '电影叙事', prompt: '画面风格：写实电影镜头，层次清晰，光线自然，避免夸张特效。' },
  { value: 'illustration', label: '编辑插画', prompt: '画面风格：现代编辑插画，构图简洁，颜色克制，适合作为内容配图。' },
]

const isActive = (generation?: ImageGeneration | null) => generation?.status === 'queued' || generation?.status === 'running'
const isReady = (generation?: ImageGeneration | null) => generation?.status === 'succeeded'
const UNKNOWN_RESULT_CODES = new Set([
  'IMAGE_PROVIDER_TIMEOUT',
  'IMAGE_NETWORK_ERROR',
  'IMAGE_PROVIDER_RESULT_UNKNOWN',
  'IMAGE_STAGED_RESULT_MISSING',
])

function isUnknownResult(generation?: ImageGeneration | null) {
  if (!generation || generation.status !== 'failed') return false
  return UNKNOWN_RESULT_CODES.has(generation.error_code || '')
    || /手动重试|响应中断|响应超时/.test(generation.error_message || '')
}

function generationStatus(generation: ImageGeneration) {
  if (generation.accepted_at) return { label: '已加入素材库', description: '图片已经成为正式素材，后台 AI 标签任务会按当前配置继续处理。', tone: 'success' }
  if (generation.status === 'queued') return { label: '等待生成服务', description: '任务已持久化，可以离开此页，稍后回来继续查看。', tone: 'running' }
  if (generation.status === 'running') return { label: '图像模型正在生成', description: '正在等待模型返回一张完整图片，请勿重复提交。', tone: 'running' }
  if (generation.status === 'succeeded') return { label: '图片已生成', description: '请先检查画面，再决定是否加入素材库。', tone: 'success' }
  if (generation.status === 'canceled') return { label: '任务已取消', description: '本次结果不会加入素材库，可以重新发起生成。', tone: 'muted' }
  if (isUnknownResult(generation)) return { label: '生成结果待确认', description: '模型请求可能已被接收或计费，但 FrameFlow 没有收到可验证的完整结果。', tone: 'error' }
  return { label: '生成失败', description: generation.error_message || '图像服务暂时没有返回可用结果。', tone: 'error' }
}

function elapsedSeconds(generation: ImageGeneration, now: number) {
  const start = Date.parse(generation.started_at || generation.created_at)
  const end = generation.finished_at ? Date.parse(generation.finished_at) : now
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null
  return Math.max(0, Math.round((end - start) / 1000))
}

function promptWithStyle(prompt: string, style: string) {
  const stylePrompt = styles.find((item) => item.value === style)?.prompt
  return stylePrompt ? `${prompt.trim()}\n\n${stylePrompt}` : prompt.trim()
}

function defaultAssetName(prompt: string, segmentTopic?: string) {
  if (segmentTopic?.trim()) return `${segmentTopic.trim()}配图`
  const firstLine = prompt.trim().split(/\r?\n/)[0]?.replace(/[。！？!?]+$/, '') || '生成图片'
  return firstLine.slice(0, 32)
}

interface ImageGenerationStudioProps {
  initialPrompt?: string
  segment?: Segment
  storageKey: string
  preferredGenerationId?: string | null
  onGenerationChange?: (generationId: string | null) => void
  onAccepted?: (result: ImageGenerationAcceptResponse) => void
  compact?: boolean
}

export function ImageGenerationStudio({
  initialPrompt = '',
  segment,
  storageKey,
  preferredGenerationId,
  onGenerationChange,
  onAccepted,
  compact = false,
}: ImageGenerationStudioProps) {
  const segmentId = segment?.id
  const segmentTopic = segment?.topic
  const [prompt, setPrompt] = useState(initialPrompt)
  const [style, setStyle] = useState('none')
  const [aspectRatio, setAspectRatio] = useState<ImageAspectRatio>('16:9')
  const [name, setName] = useState(() => defaultAssetName(initialPrompt, segmentTopic))
  const [generation, setGeneration] = useState<ImageGeneration | null>(null)
  const [asset, setAsset] = useState<Asset | null>(null)
  const [restoring, setRestoring] = useState(true)
  const [action, setAction] = useState<'create' | 'retry' | 'accept' | 'discard' | 'cancel' | null>(null)
  const [error, setError] = useState('')
  const [selectionConflict, setSelectionConflict] = useState(false)
  const [imageFailed, setImageFailed] = useState(false)
  const [now, setNow] = useState(Date.now())
  const generationRef = useRef<ImageGeneration | null>(null)
  const createKey = useRef<string | null>(null)
  const acceptKey = useRef<string | null>(null)
  const toast = useToast()

  const rememberGeneration = useCallback((next: ImageGeneration | null) => {
    const previousId = generationRef.current?.id || null
    generationRef.current = next
    setGeneration(next)
    if (next) window.localStorage.setItem(storageKey, next.id)
    else window.localStorage.removeItem(storageKey)
    if ((next?.id || null) !== previousId) {
      setSelectionConflict(false)
      onGenerationChange?.(next?.id || null)
    }
  }, [onGenerationChange, storageKey])

  useEffect(() => {
    setPrompt(initialPrompt)
    setName(defaultAssetName(initialPrompt, segmentTopic))
    setStyle('none')
    setAspectRatio('16:9')
    createKey.current = null
    acceptKey.current = null
  }, [initialPrompt, segmentId, segmentTopic])

  useEffect(() => {
    let stopped = false
    const controller = new AbortController()
    const restore = async () => {
      setRestoring(true)
      setError('')
      const storedId = window.localStorage.getItem(storageKey)
      const targetId = preferredGenerationId || storedId
      try {
        if (targetId) {
          try {
            const result = await api.imageGeneration(targetId, { signal: controller.signal })
            if (stopped) return
            rememberGeneration(result.generation)
            setAsset(result.asset || null)
            setPrompt(result.generation.prompt || initialPrompt)
            setAspectRatio(result.generation.aspect_ratio || '16:9')
            setName(result.generation.name || result.asset?.name || defaultAssetName(result.generation.prompt, segmentTopic))
            return
          } catch (err) {
            if (stopped || isAbortError(err)) return
            if (!(err instanceof ApiError) || ![404, 410].includes(err.status)) throw err
            if (storedId === targetId) window.localStorage.removeItem(storageKey)
          }
        }
        const result = await api.imageGenerations({ signal: controller.signal })
        if (stopped) return
        const recoverable = result.items
          .filter((item) => !item.accepted_at && !item.discarded_at)
          .filter((item) => segmentId ? item.segment_id === segmentId : !item.segment_id)
          .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))[0]
        if (recoverable) {
          rememberGeneration(recoverable)
          setPrompt(recoverable.prompt || initialPrompt)
          setAspectRatio(recoverable.aspect_ratio || '16:9')
          setName(recoverable.name || defaultAssetName(recoverable.prompt, segmentTopic))
        } else {
          setAsset(null)
          rememberGeneration(null)
        }
      } catch (err) {
        if (!isAbortError(err) && !stopped) {
          setError(`无法恢复生成任务：${errorMessage(err)}`)
        }
      } finally {
        if (!stopped) setRestoring(false)
      }
    }
    void restore()
    return () => {
      stopped = true
      controller.abort()
    }
  }, [initialPrompt, preferredGenerationId, rememberGeneration, segmentId, segmentTopic, storageKey])

  const activeGenerationId = isActive(generation) ? generation?.id || null : null

  useEffect(() => {
    if (!activeGenerationId) return
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [activeGenerationId])

  useEffect(() => {
    const currentGeneration = generationRef.current
    if (!activeGenerationId || !currentGeneration) return
    let stopped = false
    let timer: number | undefined
    let controller: AbortController | null = null
    let failures = 0
    let unchanged = 0
    let snapshot = `${currentGeneration.status}:${currentGeneration.progress ?? ''}:${currentGeneration.updated_at || ''}`

    const schedule = (delay: number) => {
      timer = window.setTimeout(() => { void poll() }, document.hidden ? Math.max(delay, 12_000) : delay)
    }
    const poll = async () => {
      controller = new AbortController()
      try {
        const result = await api.imageGeneration(activeGenerationId, { signal: controller.signal, timeoutMs: 10_000 })
        if (stopped || controller.signal.aborted) return
        failures = 0
        const nextSnapshot = `${result.generation.status}:${result.generation.progress ?? ''}:${result.generation.updated_at || ''}`
        unchanged = nextSnapshot === snapshot ? unchanged + 1 : 0
        snapshot = nextSnapshot
        rememberGeneration(result.generation)
        setAsset(result.asset || null)
        setError('')
        if (isActive(result.generation)) schedule(Math.min(POLL_MAX_INTERVAL, Math.round(POLL_INTERVAL * (1.45 ** Math.min(unchanged, 5)))))
      } catch (err) {
        if (stopped || isAbortError(err)) return
        failures += 1
        setError(`任务状态暂时无法更新：${errorMessage(err)}`)
        schedule(Math.min(POLL_MAX_INTERVAL, POLL_INTERVAL * (2 ** Math.min(failures, 3))))
      }
    }

    schedule(POLL_INTERVAL)
    return () => {
      stopped = true
      if (timer != null) window.clearTimeout(timer)
      controller?.abort()
    }
  }, [activeGenerationId, rememberGeneration])

  useEffect(() => {
    createKey.current = null
  }, [aspectRatio, name, prompt, style])

  useEffect(() => {
    acceptKey.current = null
  }, [name])

  const submit = async (event?: FormEvent) => {
    event?.preventDefault()
    if (action || isActive(generation)) return
    if (prompt.trim().length < 4) {
      setError('请至少用 4 个字描述希望生成的画面。')
      return
    }
    const idempotencyKey = createKey.current || crypto.randomUUID()
    createKey.current = idempotencyKey
    setAction('create')
    setError('')
    setImageFailed(false)
    try {
      const input = {
        prompt: promptWithStyle(prompt, style),
        name: name.trim() || defaultAssetName(prompt, segmentTopic),
        aspect_ratio: aspectRatio,
        auto_import: false,
        auto_select: false,
      }
      const result = segment
        ? await api.createSegmentImageGeneration(segment.id, input, idempotencyKey)
        : await api.createImageGeneration(input, idempotencyKey)
      createKey.current = null
      acceptKey.current = null
      setSelectionConflict(false)
      setAsset(null)
      rememberGeneration(result.generation)
      setNow(Date.now())
      toast(result.idempotent_replay ? '已恢复相同的生成任务' : '图片生成任务已创建', result.idempotent_replay ? 'info' : 'success')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setAction(null)
    }
  }

  const retry = async () => {
    if (!generation || action || isActive(generation)) return
    // The retry endpoint deliberately accepts only retryable failures. A
    // successful/canceled/non-retryable task must create a new durable task;
    // otherwise the visible “重新生成” action would always end in HTTP 409.
    if (generation.status !== 'failed' || !generation.retryable) {
      await submit()
      return
    }
    setAction('retry')
    setError('')
    setImageFailed(false)
    try {
      const result = await api.retryImageGeneration(generation.id)
      rememberGeneration(result.generation)
      setSelectionConflict(false)
      setAsset(null)
      setNow(Date.now())
      toast('已重新发起图片生成，本次会产生一次新的模型调用', 'info')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setAction(null)
    }
  }

  const accept = async (selectForSegment = Boolean(segment)) => {
    if (!generation || !isReady(generation) || generation.accepted_at || action) return
    if (!name.trim()) {
      setError('请填写加入素材库后的素材名称。')
      return
    }
    const idempotencyKey = acceptKey.current || crypto.randomUUID()
    acceptKey.current = idempotencyKey
    setAction('accept')
    setError('')
    try {
      const result = await api.acceptImageGeneration(generation.id, {
        name: name.trim(),
        select_for_segment: selectForSegment,
        expected_segment_version: selectForSegment ? segment?.version ?? null : null,
      }, idempotencyKey)
      acceptKey.current = null
      setSelectionConflict(false)
      rememberGeneration(result.generation)
      setAsset(result.asset)
      onAccepted?.(result)
      toast(
        segment
          ? selectForSegment
            ? '图片已加入素材库并用于当前片段'
            : '图片已加入素材库，未替换当前片段；后台 AI 标签任务已排队'
          : '图片已加入素材库，后台 AI 标签任务已排队',
        'success',
      )
    } catch (err) {
      if (segment && selectForSegment && err instanceof ApiError && err.code === 'IMAGE_SEGMENT_VERSION_CONFLICT') {
        acceptKey.current = null
        setSelectionConflict(true)
        setError('字幕片段已在生成期间更新，旧图片不能直接替换新内容。你可以仅将图片加入素材库（不替换当前片段），或按最新字幕重新生成。')
      } else {
        setError(errorMessage(err))
      }
    } finally {
      setAction(null)
    }
  }

  const clearGeneration = useCallback(() => {
    rememberGeneration(null)
    setAsset(null)
    setError('')
    setSelectionConflict(false)
    setImageFailed(false)
    createKey.current = null
    acceptKey.current = null
  }, [rememberGeneration])

  const discard = async () => {
    if (!generation || action) return
    const canceling = isActive(generation)
    setAction(canceling ? 'cancel' : 'discard')
    setError('')
    try {
      if (canceling) {
        const result = await api.cancelImageGeneration(generation.id)
        rememberGeneration(result.generation)
        setAsset(null)
        setNow(Date.now())
        toast('生成任务已取消；可保留记录或手动清除', 'info')
      } else {
        await api.discardImageGeneration(generation.id)
        clearGeneration()
        toast('临时生成结果已放弃', 'info')
      }
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setAction(null)
    }
  }

  const status = generation ? generationStatus(generation) : null
  const elapsed = generation ? elapsedSeconds(generation, now) : null
  const contentUrl = generation?.content_url ? mediaUrl(generation.content_url) : ''
  const canAccept = Boolean(generation && isReady(generation) && !generation.accepted_at)
  const resultUnknown = isUnknownResult(generation)
  const promptCount = prompt.trim().length

  return (
    <div className={`image-generation-studio${compact ? ' compact' : ''}`}>
      <form className="generation-form-panel" onSubmit={(event) => void submit(event)}>
        <div className="generation-section-head">
          <span className="generation-step">01</span>
          <div><h2>描述画面</h2><p>写清主体、环境、构图和光线，不需要使用模型指令。</p></div>
        </div>

        {segment && <div className="generation-context"><span>当前片段</span><strong>{segment.topic || `片段 ${segment.position + 1}`}</strong><small>确认使用后会加入素材库，并替换当前片段画面。</small></div>}

        <div className="form-field generation-prompt-field">
          <label htmlFor={`generation-prompt-${segment?.id || 'library'}`}>画面描述 <span>{promptCount}/1200</span></label>
          <textarea
            id={`generation-prompt-${segment?.id || 'library'}`}
            data-generation-autofocus={compact ? 'true' : undefined}
            value={prompt}
            maxLength={1200}
            rows={compact ? 6 : 8}
            placeholder="例如：一瓶高端护肤精华置于浅色石台，柔和侧光，背景干净，真实商业摄影质感，画面右侧留出字幕空间。"
            onChange={(event) => setPrompt(event.target.value)}
          />
        </div>

        <div className="generation-control-grid">
          <div className="form-field">
            <label htmlFor={`generation-style-${segment?.id || 'library'}`}>画面风格</label>
            <select id={`generation-style-${segment?.id || 'library'}`} value={style} onChange={(event) => setStyle(event.target.value)}>
              {styles.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select>
          </div>
          <div className="form-field">
            <label htmlFor={`generation-name-${segment?.id || 'library'}`}>素材名称</label>
            <input id={`generation-name-${segment?.id || 'library'}`} value={name} maxLength={80} onChange={(event) => setName(event.target.value)} placeholder="加入素材库时使用" />
          </div>
        </div>

        <fieldset className="aspect-ratio-fieldset">
          <legend>画面比例</legend>
          <div className="aspect-ratio-options">
            {aspectRatios.map((item) => (
              <label key={item.value} className={aspectRatio === item.value ? 'selected' : ''}>
                <input type="radio" name={`aspect-${segment?.id || 'library'}`} value={item.value} checked={aspectRatio === item.value} onChange={() => setAspectRatio(item.value)} />
                <span className={`ratio-shape ratio-${item.value.replace(':', '-')}`} aria-hidden="true" />
                <span><strong>{item.label}</strong><small>{item.hint}</small></span>
              </label>
            ))}
          </div>
        </fieldset>

        <button type="submit" className="button button-primary generation-submit" disabled={Boolean(action) || restoring || isActive(generation)}>
          {action === 'create' ? <InlineSpinner label="正在提交" /> : isActive(generation) ? <><LoaderCircle className="spin" size={17} /> 生成进行中</> : <><ImagePlus size={17} /> {generation ? '生成另一张' : '生成 1 张图片'}</>}
        </button>

        <div className="generation-policy-note">
          <ShieldAlert size={17} />
          <p><strong>调用、费用与内容责任</strong><span>每次生成或重新生成都会向第三方图像模型发起一次可能计费的调用。提示词会发送给模型服务商；请勿输入无授权商标、受版权保护角色或真实人物敏感信息，商用前请人工确认。</span></p>
        </div>
      </form>

      <section className="generation-result-panel" aria-labelledby={`generation-result-title-${segment?.id || 'library'}`} aria-busy={restoring || isActive(generation)}>
        <div className="generation-section-head result-head">
          <span className="generation-step">02</span>
          <div><h2 id={`generation-result-title-${segment?.id || 'library'}`}>检查结果</h2><p>生成结果不会自动进入素材库，确认后才会保存。</p></div>
          {generation && <span className={`generation-status-chip ${status?.tone}`}>{status?.label}</span>}
        </div>

        {restoring ? (
          <div className="generation-empty" role="status"><LoaderCircle size={24} className="spin" /><strong>正在恢复生成任务</strong><span>刷新页面不会丢失已经提交的任务。</span></div>
        ) : !generation ? (
          <div className="generation-empty"><ImagePlus size={29} /><strong>等待画面描述</strong><span>左侧提交后，这里会显示真实任务状态和生成图片。</span></div>
        ) : (
          <div className="generation-result-content">
            <div className={`generation-preview ratio-${generation.aspect_ratio.replace(':', '-')}`}>
              {isReady(generation) && contentUrl && !imageFailed
                ? <img src={contentUrl} alt={name.trim() || '文生图生成结果'} onError={() => setImageFailed(true)} />
                : <div className={`generation-preview-state ${status?.tone}`} role="status" aria-live="polite">
                    {generation.status === 'failed' ? <AlertTriangle size={30} /> : generation.status === 'canceled' ? <X size={30} /> : <LoaderCircle size={30} className={isActive(generation) ? 'spin' : ''} />}
                    <strong>{status?.label}</strong>
                    <span>{status?.description}</span>
                  </div>}
              {imageFailed && <div className="generation-image-error" role="alert"><AlertTriangle size={23} /><strong>图片预览加载失败</strong><span>可尝试下载原图，或刷新任务状态。</span></div>}
            </div>

            <div className={`generation-task-card ${status?.tone}`} aria-live="polite">
              <div><span>{status?.label}</span>{elapsed != null && <small><Clock3 size={13} /> {elapsed} 秒</small>}</div>
              <p>{status?.description}</p>
              {generation.progress != null && isActive(generation) && <div className="generation-progress" role="progressbar" aria-label="图片生成进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={generation.progress}><span style={{ width: `${Math.max(0, Math.min(100, generation.progress))}%` }} /></div>}
              <dl>
                <div><dt>比例</dt><dd>{generation.aspect_ratio}</dd></div>
                <div><dt>数量</dt><dd>1 张</dd></div>
                <div><dt>模型</dt><dd>{generation.model || '服务端配置'}</dd></div>
                <div><dt>创建</dt><dd>{formatDate(generation.created_at)}</dd></div>
              </dl>
              {generation.expires_at && !generation.accepted_at && <small className="generation-expiry"><Info size={13} /> 临时结果保留至 {formatDate(generation.expires_at)}</small>}
            </div>

            {error && <div className="generation-error" role="alert"><AlertTriangle size={16} /><span>{error}</span></div>}

            {resultUnknown && <div className="generation-error" role="alert"><ShieldAlert size={16} /><span>结果未知：本次请求可能已经被模型服务商接收并计费。请先核对服务商记录；点击“确认再次调用”会发起一笔新的可能计费请求。</span></div>}

            {canAccept && (
              <div className="generation-result-actions">
                <button type="button" className="button button-primary" disabled={Boolean(action)} onClick={() => void accept(selectionConflict ? false : Boolean(segment))}>
                  {action === 'accept' ? <InlineSpinner label="正在加入素材库" /> : <><Check size={17} /> {selectionConflict ? '仅加入素材库（不替换当前片段）' : segment ? '使用并加入素材库' : '加入素材库'}</>}
                </button>
                <button type="button" className="button button-secondary" disabled={Boolean(action)} onClick={() => void retry()}>{action === 'retry' ? <InlineSpinner label="正在重新生成" /> : <><RefreshCw size={16} /> 重新生成</>}</button>
                {contentUrl && <a className="button button-secondary" href={contentUrl} download={`${name.trim() || 'frameflow-generated'}.png`}><Download size={16} /> 下载原图</a>}
                <button type="button" className="button button-ghost generation-discard" disabled={Boolean(action)} onClick={() => void discard()}><Trash2 size={15} /> 放弃</button>
                <p><Info size={14} /> 加入后会进入后台 AI 标签任务；具体使用视觉、文本模型或规则降级，以素材库显示的标签来源为准，不会阻塞当前图片使用。</p>
              </div>
            )}

            {isActive(generation) && <button type="button" className="button button-secondary generation-cancel" disabled={Boolean(action)} onClick={() => void discard()}>{action === 'cancel' ? <InlineSpinner label="正在取消" /> : '取消本次生成'}</button>}

            {(generation.status === 'failed' || generation.status === 'canceled') && (
              <div className="generation-recovery-actions">
                <button type="button" className="button button-primary" disabled={Boolean(action)} onClick={() => void retry()}>{action === 'retry' ? <InlineSpinner label="正在重试" /> : <><RefreshCw size={16} /> {resultUnknown ? '确认再次调用' : '重新生成'}</>}</button>
                <button type="button" className="button button-secondary" disabled={Boolean(action)} onClick={() => void discard()}>清除任务</button>
              </div>
            )}

            {generation.accepted_at && asset && (
              <div className="generation-accepted" role="status"><Check size={18} /><div><strong>“{asset.name}”已加入素材库</strong><span>{segment ? '当前片段画面已同步更新。' : '后台 AI 标签任务正在处理，结果以素材库显示为准。'}</span></div><button type="button" className="button button-secondary button-small" onClick={() => void submit()}>再生成一张</button></div>
            )}
          </div>
        )}

        {!restoring && !generation && error && <div className="generation-error standalone" role="alert"><AlertTriangle size={16} /><span>{error}</span></div>}
      </section>
    </div>
  )
}

interface SegmentImageGenerationDialogProps {
  segment: Segment
  initialPrompt: string
  onClose: () => void
  onAccepted: (result: ImageGenerationAcceptResponse) => void
}

export function SegmentImageGenerationDialog({ segment, initialPrompt, onClose, onAccepted }: SegmentImageGenerationDialogProps) {
  const dialogRef = useRef<HTMLElement>(null)

  useEffect(() => {
    const dialog = dialogRef.current
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    if (!dialog) return
    const focusable = () => Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter((element) => !element.hidden)
    const frame = window.requestAnimationFrame(() => (dialog.querySelector<HTMLElement>('[data-generation-autofocus]') || focusable()[0] || dialog).focus())
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const items = focusable()
      if (!items.length) {
        event.preventDefault()
        dialog.focus()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    document.body.classList.add('generation-dialog-open')
    return () => {
      window.cancelAnimationFrame(frame)
      document.removeEventListener('keydown', onKeyDown)
      document.body.classList.remove('generation-dialog-open')
      previousFocus?.focus()
    }
  }, [onClose])

  return (
    <div className="generation-dialog-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <section ref={dialogRef} className="generation-dialog" role="dialog" aria-modal="true" aria-labelledby="segment-generation-title" aria-describedby="segment-generation-description" tabIndex={-1}>
        <header className="generation-dialog-head">
          <div><span className="modal-icon"><ImagePlus size={19} /></span><div><h2 id="segment-generation-title">为当前字幕生成画面</h2><p id="segment-generation-description">检查结果后再使用，不会自动覆盖当前素材。</p></div></div>
          <button type="button" className="icon-button" aria-label="关闭图片生成器" onClick={onClose}><X size={19} /></button>
        </header>
        <div className="generation-dialog-body">
          <ImageGenerationStudio
            compact
            key={segment.id}
            segment={segment}
            initialPrompt={initialPrompt}
            storageKey={`frameflow:image-generation:segment:${segment.id}`}
            onAccepted={onAccepted}
          />
        </div>
      </section>
    </div>
  )
}
