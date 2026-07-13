import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react'
import {
  AlertTriangle,
  ArrowLeft,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  CircleHelp,
  Clock3,
  Film,
  GripVertical,
  Image,
  Info,
  ListVideo,
  LoaderCircle,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Tags,
  TextCursorInput,
  X,
} from 'lucide-react'
import { api, ApiError, errorMessage, isAbortError, mediaUrl } from '../api'
import {
  AssetVisual,
  EmptyState,
  ErrorState,
  formatDuration,
  InlineSpinner,
  PageLoader,
  scorePercent,
  StatusPill,
  useToast,
} from '../components/ui'
import { addNavigationGuard, AppLink, navigate } from '../router'
import type { Asset, Job, PreviewRender, ProjectDetail, ProjectPreviewResponse, ProjectTimeline, Recommendation, Segment, Selection } from '../types'

interface SegmentDraft { text: string; topic: string; keywords: string }
type SaveState = 'idle' | 'dirty' | 'saving' | 'saved' | 'error'
type SaveResult = { ok: true; segmentId: string | null } | { ok: false; segmentId: string; error: unknown }
type MobilePane = 'segments' | 'editor' | 'matches'

const asDraft = (segment: Segment): SegmentDraft => ({
  text: segment.text,
  topic: segment.topic || '',
  keywords: segment.keywords.join('，'),
})

const selectionAsset = (segment?: Segment) => {
  if (!segment) return null
  if (segment.selection?.asset) return segment.selection.asset
  const selected = segment.recommendations.find((item) => item.asset.id === segment.selection?.asset_id)
  return selected?.asset || segment.recommendations[0]?.asset || null
}

export function WorkbenchPage({ projectId }: { projectId: string }) {
  const [detail, setDetail] = useState<ProjectDetail | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [draft, setDraft] = useState<SegmentDraft>({ text: '', topic: '', keywords: '' })
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [conflict, setConflict] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [leftView, setLeftView] = useState<'segments' | 'transcript'>('segments')
  const [mobilePane, setMobilePane] = useState<MobilePane>('editor')
  const [query, setQuery] = useState('')
  const [searchKind, setSearchKind] = useState('')
  const [searchTag, setSearchTag] = useState('')
  const [searchResults, setSearchResults] = useState<Asset[]>([])
  const [searching, setSearching] = useState(false)
  const [selectingAsset, setSelectingAsset] = useState<string | null>(null)
  const [rematching, setRematching] = useState(false)
  const [switchingSegment, setSwitchingSegment] = useState(false)
  const [reordering, setReordering] = useState(false)
  const [draggingId, setDraggingId] = useState<string | null>(null)
  const [dragOverId, setDragOverId] = useState<string | null>(null)
  const [timeline, setTimeline] = useState<ProjectTimeline | null>(null)
  const [preview, setPreview] = useState<PreviewRender | null>(null)
  const [previewJob, setPreviewJob] = useState<Job | null>(null)
  const [previewLoading, setPreviewLoading] = useState(true)
  const [previewCreating, setPreviewCreating] = useState(false)
  const [previewError, setPreviewError] = useState('')
  const saveTimer = useRef<number | null>(null)
  const savedResetTimer = useRef<number | null>(null)
  const detailRef = useRef<ProjectDetail | null>(null)
  const selectedIdRef = useRef<string | null>(null)
  const draftRef = useRef<SegmentDraft>(draft)
  const draftRevision = useRef(0)
  const saveStateRef = useRef<SaveState>('idle')
  const savePromise = useRef<Promise<SaveResult> | null>(null)
  const rematchPromise = useRef<Promise<void> | null>(null)
  const segmentSwitchPromise = useRef<Promise<void> | null>(null)
  const reorderingRef = useRef(false)
  const hydratedSegmentId = useRef<string | null>(null)
  const searchVersion = useRef(0)
  const loadVersion = useRef(0)
  const loadAbort = useRef<AbortController | null>(null)
  const previewLoadVersion = useRef(0)
  const previewLoadAbort = useRef<AbortController | null>(null)
  const previewActionAbort = useRef<AbortController | null>(null)
  const previewPollAbort = useRef<AbortController | null>(null)
  const previewPollTimer = useRef<number | null>(null)
  const mountedRef = useRef(true)
  const projectIdRef = useRef(projectId)
  projectIdRef.current = projectId
  const toast = useToast()

  const updateSaveState = useCallback((state: SaveState) => {
    saveStateRef.current = state
    if (mountedRef.current) setSaveState(state)
  }, [])

  const updateSegmentById = useCallback((segmentId: string, updater: (segment: Segment) => Segment, expectedProjectId = projectIdRef.current) => {
    if (!mountedRef.current || projectIdRef.current !== expectedProjectId) return
    setDetail((current) => {
      if (!current) return current
      const next = {
        ...current,
        segments: current.segments.map((segment) => segment.id === segmentId ? updater(segment) : segment),
      }
      detailRef.current = next
      return next
    })
  }, [])

  const load = useCallback(async (showLoader = false, discardDraft = false) => {
    const requestProjectId = projectId
    const currentLoad = ++loadVersion.current
    loadAbort.current?.abort()
    const controller = new AbortController()
    loadAbort.current = controller
    if (showLoader) setLoading(true)
    try {
      const result = await api.project(requestProjectId, { signal: controller.signal })
      if (!mountedRef.current || controller.signal.aborted || currentLoad !== loadVersion.current || projectIdRef.current !== requestProjectId) return
      if (result.project.status !== 'ready' && result.current_job?.status !== 'succeeded') {
        void navigate(`/projects/${requestProjectId}/processing`, { replace: true })
        return
      }
      const ordered = [...(result.segments || [])].sort((a, b) => a.position - b.position)
      const nextDetail = { ...result, segments: ordered }
      const currentSelectedId = selectedIdRef.current
      const nextSelectedId = currentSelectedId && ordered.some((item) => item.id === currentSelectedId)
        ? currentSelectedId
        : ordered[0]?.id || null
      detailRef.current = nextDetail
      selectedIdRef.current = nextSelectedId
      if (discardDraft) hydratedSegmentId.current = null
      setDetail(nextDetail)
      setSelectedId(nextSelectedId)
      setError('')
      setConflict(false)
    } catch (err) {
      if (isAbortError(err) || !mountedRef.current || currentLoad !== loadVersion.current || projectIdRef.current !== requestProjectId) return
      setError(errorMessage(err))
    } finally {
      if (mountedRef.current && currentLoad === loadVersion.current && projectIdRef.current === requestProjectId) setLoading(false)
      if (loadAbort.current === controller) loadAbort.current = null
    }
  }, [projectId])

  const loadPreviewOverview = useCallback(async (showLoader = false): Promise<ProjectPreviewResponse | null> => {
    const requestProjectId = projectId
    const currentLoad = ++previewLoadVersion.current
    previewLoadAbort.current?.abort()
    const controller = new AbortController()
    previewLoadAbort.current = controller
    if (showLoader) setPreviewLoading(true)
    try {
      const result = await api.projectPreview(requestProjectId, { signal: controller.signal })
      if (!mountedRef.current || controller.signal.aborted || currentLoad !== previewLoadVersion.current || projectIdRef.current !== requestProjectId) return null
      setTimeline(result.timeline)
      setPreview(result.preview)
      setPreviewJob(result.preview?.job || null)
      setPreviewError('')
      return result
    } catch (err) {
      if (isAbortError(err) || !mountedRef.current || currentLoad !== previewLoadVersion.current || projectIdRef.current !== requestProjectId) return null
      setPreviewError(errorMessage(err))
      return null
    } finally {
      if (mountedRef.current && currentLoad === previewLoadVersion.current && projectIdRef.current === requestProjectId) setPreviewLoading(false)
      if (previewLoadAbort.current === controller) previewLoadAbort.current = null
    }
  }, [projectId])

  const startPreviewPolling = useCallback((jobId: string) => {
    previewPollAbort.current?.abort()
    if (previewPollTimer.current) window.clearTimeout(previewPollTimer.current)
    const controller = new AbortController()
    const requestProjectId = projectId
    previewPollAbort.current = controller
    let failures = 0

    const poll = async () => {
      let delay = 1200
      try {
        const result = await api.job(jobId, { signal: controller.signal, timeoutMs: 10_000 })
        if (controller.signal.aborted || !mountedRef.current || projectIdRef.current !== requestProjectId) return
        failures = 0
        setPreviewJob(result.job)
        setPreview((current) => current ? {
          ...current,
          status: result.job.status,
          job: result.job,
          error_message: result.job.error_message || current.error_message,
        } : current)
        setPreviewError('')
        if (['succeeded', 'failed', 'canceled'].includes(result.job.status)) {
          await loadPreviewOverview(false)
          return
        }
      } catch (err) {
        if (isAbortError(err) || controller.signal.aborted || !mountedRef.current || projectIdRef.current !== requestProjectId) return
        failures += 1
        delay = Math.min(10_000, 1200 * (2 ** Math.min(failures, 3)))
        setPreviewError(`预览任务状态暂时无法更新：${errorMessage(err)}`)
      }
      if (!controller.signal.aborted && mountedRef.current && projectIdRef.current === requestProjectId) {
        previewPollTimer.current = window.setTimeout(poll, delay)
      }
    }

    void poll()
  }, [loadPreviewOverview, projectId])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      loadAbort.current?.abort()
      previewLoadAbort.current?.abort()
      previewActionAbort.current?.abort()
      previewPollAbort.current?.abort()
      if (previewPollTimer.current) window.clearTimeout(previewPollTimer.current)
      searchVersion.current += 1
    }
  }, [])

  useEffect(() => {
    loadAbort.current?.abort()
    loadVersion.current += 1
    if (saveTimer.current) window.clearTimeout(saveTimer.current)
    if (savedResetTimer.current) window.clearTimeout(savedResetTimer.current)
    detailRef.current = null
    selectedIdRef.current = null
    hydratedSegmentId.current = null
    draftRef.current = { text: '', topic: '', keywords: '' }
    draftRevision.current = 0
    savePromise.current = null
    saveStateRef.current = 'idle'
    setDetail(null)
    setSelectedId(null)
    setDraft({ text: '', topic: '', keywords: '' })
    setSaveState('idle')
    setConflict(false)
    setError('')
    setSearchResults([])
    setDraggingId(null)
    setDragOverId(null)
    reorderingRef.current = false
    setReordering(false)
    setSelectingAsset(null)
    previewLoadAbort.current?.abort()
    previewActionAbort.current?.abort()
    previewPollAbort.current?.abort()
    previewLoadVersion.current += 1
    if (previewPollTimer.current) window.clearTimeout(previewPollTimer.current)
    setTimeline(null)
    setPreview(null)
    setPreviewJob(null)
    setPreviewLoading(true)
    setPreviewCreating(false)
    setPreviewError('')
    void load(true)
    void (async () => {
      const result = await loadPreviewOverview(true)
      const activeJob = result?.preview?.job || (result?.preview?.job_id ? { id: result.preview.job_id, status: result.preview.status } : null)
      if (activeJob?.id && ['queued', 'running'].includes(activeJob.status)) startPreviewPolling(activeJob.id)
    })()
  }, [load, loadPreviewOverview, projectId, startPreviewPolling])

  const segments = detail?.segments || []
  const selectedIndex = segments.findIndex((segment) => segment.id === selectedId)
  const selected = selectedIndex >= 0 ? segments[selectedIndex] : undefined

  useEffect(() => {
    if (!selected || hydratedSegmentId.current === selected.id) return
    const nextDraft = asDraft(selected)
    hydratedSegmentId.current = selected.id
    selectedIdRef.current = selected.id
    draftRef.current = nextDraft
    draftRevision.current = 0
    setDraft(nextDraft)
    updateSaveState('idle')
  }, [selected, updateSaveState])

  useEffect(() => { detailRef.current = detail }, [detail])

  const save = useCallback((): Promise<SaveResult> => {
    if (saveTimer.current) window.clearTimeout(saveTimer.current)
    if (savePromise.current) return savePromise.current
    if (!['dirty', 'saving', 'error'].includes(saveStateRef.current)) {
      return Promise.resolve({ ok: true, segmentId: selectedIdRef.current })
    }

    const pending = (async (): Promise<SaveResult> => {
      const saveProjectId = projectIdRef.current
      const segmentId = selectedIdRef.current
      if (!segmentId) return { ok: true, segmentId: null }
      let nextVersion: number | undefined

      while (true) {
        const segment = detailRef.current?.segments.find((item) => item.id === segmentId)
        if (!segment) {
          const missing = new Error('当前字幕片段不存在，请重新加载项目')
          updateSaveState('error')
          toast(missing.message, 'error')
          return { ok: false, segmentId, error: missing }
        }

        const snapshot = { ...draftRef.current }
        const revision = draftRevision.current
        updateSaveState('saving')
        try {
          const updated = await api.updateSegment(segmentId, {
            text: snapshot.text.trim(),
            topic: snapshot.topic.trim(),
            keywords: snapshot.keywords.split(/[，,]/).map((value) => value.trim()).filter(Boolean),
            version: nextVersion ?? segment.version,
          })
          if (!mountedRef.current || projectIdRef.current !== saveProjectId) return { ok: true, segmentId }
          nextVersion = updated.version
          updateSegmentById(segmentId, (current) => ({
            ...current,
            ...updated,
            recommendations: updated.recommendations || current.recommendations,
            selection: updated.selection ?? current.selection,
          }))
          if (selectedIdRef.current !== segmentId) return { ok: true, segmentId }
          if (draftRevision.current !== revision) {
            updateSaveState('dirty')
            continue
          }
          setConflict(false)
          updateSaveState('saved')
          void loadPreviewOverview(false)
          if (savedResetTimer.current) window.clearTimeout(savedResetTimer.current)
          savedResetTimer.current = window.setTimeout(() => {
            if (selectedIdRef.current === segmentId && draftRevision.current === revision && saveStateRef.current === 'saved') {
              updateSaveState('idle')
            }
          }, 1800)
          return { ok: true, segmentId }
        } catch (err) {
          if (mountedRef.current && projectIdRef.current === saveProjectId && selectedIdRef.current === segmentId) {
            updateSaveState('error')
            toast(errorMessage(err), 'error')
            if (err instanceof ApiError && err.status === 409) setConflict(true)
          }
          return { ok: false, segmentId, error: err }
        }
      }
    })()

    savePromise.current = pending
    void pending.finally(() => {
      if (savePromise.current === pending) savePromise.current = null
    })
    return pending
  }, [loadPreviewOverview, toast, updateSaveState, updateSegmentById])

  const ensureSaved = useCallback(async () => {
    if (!['dirty', 'saving', 'error'].includes(saveStateRef.current)) return true
    return (await save()).ok
  }, [save])

  const generatePreview = async () => {
    const activePreviewJob = previewJob || preview?.job
    if (previewCreating || ['queued', 'running'].includes(activePreviewJob?.status || preview?.status || '')) return
    if (!await ensureSaved()) return
    const operationProjectId = projectIdRef.current
    previewLoadAbort.current?.abort()
    previewLoadVersion.current += 1
    const controller = new AbortController()
    previewActionAbort.current?.abort()
    previewActionAbort.current = controller
    setPreviewCreating(true)
    setPreviewError('')
    try {
      const result = await api.createProjectPreview(operationProjectId, Boolean(preview?.id), { signal: controller.signal })
      if (controller.signal.aborted || !mountedRef.current || projectIdRef.current !== operationProjectId) return
      setTimeline(result.timeline)
      setPreview(result.preview)
      setPreviewJob(result.preview.job || null)
      const jobId = result.preview.job?.id || result.preview.job_id
      if (result.idempotent_replay) toast('已复用相同时间线的预览任务', 'info')
      else toast(preview?.id ? '已开始重新生成组合预览' : '已开始生成组合预览', 'success')
      if (jobId && ['queued', 'running'].includes(result.preview.job?.status || result.preview.status)) startPreviewPolling(jobId)
      else if (result.preview.status === 'succeeded') await loadPreviewOverview(false)
    } catch (err) {
      if (!isAbortError(err) && mountedRef.current && projectIdRef.current === operationProjectId) setPreviewError(errorMessage(err))
    } finally {
      if (previewActionAbort.current === controller) previewActionAbort.current = null
      if (mountedRef.current && projectIdRef.current === operationProjectId) setPreviewCreating(false)
    }
  }

  const editDraft = (changes: Partial<SegmentDraft>) => {
    const next = { ...draftRef.current, ...changes }
    draftRef.current = next
    draftRevision.current += 1
    setDraft(next)
    updateSaveState('dirty')
  }

  useEffect(() => {
    if (saveState !== 'dirty') return
    if (saveTimer.current) window.clearTimeout(saveTimer.current)
    saveTimer.current = window.setTimeout(() => { void save() }, 750)
    return () => { if (saveTimer.current) window.clearTimeout(saveTimer.current) }
  }, [draft, save, saveState])

  useEffect(() => () => {
    if (saveTimer.current) window.clearTimeout(saveTimer.current)
    if (savedResetTimer.current) window.clearTimeout(savedResetTimer.current)
  }, [])

  useEffect(() => addNavigationGuard(() => ensureSaved()), [ensureSaved])

  useEffect(() => {
    const warnBeforeLeave = (event: BeforeUnloadEvent) => {
      if (saveState !== 'dirty' && saveState !== 'saving' && saveState !== 'error') return
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warnBeforeLeave)
    return () => window.removeEventListener('beforeunload', warnBeforeLeave)
  }, [saveState])

  useEffect(() => {
    if (!query.trim() && !searchKind && !searchTag.trim()) {
      setSearchResults([])
      setSearching(false)
      return
    }
    const currentSearch = ++searchVersion.current
    const controller = new AbortController()
    const timer = window.setTimeout(async () => {
      setSearching(true)
      try {
        const result = await api.assets({ q: query.trim() || undefined, kind: searchKind || undefined, tag: searchTag.trim() || undefined }, { signal: controller.signal })
        if (currentSearch === searchVersion.current) setSearchResults(result.items)
      } catch (err) {
        if (!isAbortError(err) && currentSearch === searchVersion.current) toast(errorMessage(err), 'error')
      } finally {
        if (currentSearch === searchVersion.current) setSearching(false)
      }
    }, 350)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [query, searchKind, searchTag, toast])

  const selectAsset = async (asset: Asset) => {
    if (!selected || selectingAsset) return
    const operationProjectId = projectIdRef.current
    const segmentId = selected.id
    setSelectingAsset(asset.id)
    const previous = selected.selection
    const optimistic: Selection = { segment_id: segmentId, asset_id: asset.id, source: 'manual', asset }
    updateSegmentById(segmentId, (segment) => ({ ...segment, selection: optimistic }))
    try {
      const result = await api.selectAsset(segmentId, asset.id)
      if (!mountedRef.current || projectIdRef.current !== operationProjectId) return
      if ('selection' in result && result.selection) updateSegmentById(segmentId, (segment) => ({ ...segment, selection: result.selection }))
      else if ('id' in result && 'text' in result) updateSegmentById(segmentId, () => result as Segment)
      toast(`已将「${asset.name}」设为当前画面`, 'success')
      void loadPreviewOverview(false)
      setQuery('')
    } catch (err) {
      if (!mountedRef.current || projectIdRef.current !== operationProjectId) return
      updateSegmentById(segmentId, (segment) => ({ ...segment, selection: previous }))
      toast(errorMessage(err), 'error')
    } finally {
      if (mountedRef.current && projectIdRef.current === operationProjectId) setSelectingAsset(null)
    }
  }

  const rematch = () => {
    if (!selectedIdRef.current || rematchPromise.current) return rematchPromise.current
    const operationProjectId = projectIdRef.current
    const segmentId = selectedIdRef.current
    const pending = (async () => {
      setRematching(true)
      try {
        if (!await ensureSaved()) return
        const result = await api.rematchSegment(segmentId)
        if (!mountedRef.current || projectIdRef.current !== operationProjectId) return
        const updated = 'segment' in result ? result.segment : result
        updateSegmentById(segmentId, (segment) => ({ ...segment, ...updated }))
        toast('已根据当前文本重新生成候选', 'success')
      } catch (err) {
        if (mountedRef.current && projectIdRef.current === operationProjectId) toast(errorMessage(err), 'error')
      } finally {
        if (mountedRef.current && projectIdRef.current === operationProjectId) setRematching(false)
      }
    })()
    rematchPromise.current = pending
    void pending.finally(() => {
      if (rematchPromise.current === pending) rematchPromise.current = null
    })
    return pending
  }

  const reorder = async (fromId: string, toIndex: number) => {
    const currentDetail = detailRef.current
    if (!currentDetail || reorderingRef.current) return
    const operationProjectId = projectIdRef.current
    const fromIndex = currentDetail.segments.findIndex((item) => item.id === fromId)
    if (fromIndex < 0 || toIndex < 0 || toIndex >= currentDetail.segments.length || fromIndex === toIndex) return
    const previous = currentDetail.segments
    const next = [...previous]
    const [moving] = next.splice(fromIndex, 1)
    next.splice(toIndex, 0, moving)
    const positioned = next.map((item, index) => ({ ...item, position: index }))
    const optimisticDetail = { ...currentDetail, segments: positioned }
    detailRef.current = optimisticDetail
    setDetail(optimisticDetail)
    reorderingRef.current = true
    setReordering(true)
    try {
      const result = await api.reorderSegments(operationProjectId, positioned.map((item) => item.id))
      if (!mountedRef.current || projectIdRef.current !== operationProjectId) return
      const serverSegments = Array.isArray(result) ? result : result.segments
      if (serverSegments) setDetail((current) => {
        if (!current) return current
        const next = { ...current, segments: [...serverSegments].sort((a, b) => a.position - b.position) }
        detailRef.current = next
        return next
      })
      toast('片段顺序已保存', 'success')
      void loadPreviewOverview(false)
    } catch (err) {
      if (mountedRef.current && projectIdRef.current === operationProjectId) {
        setDetail((current) => {
          if (!current) return current
          const next = { ...current, segments: previous }
          detailRef.current = next
          return next
        })
        toast(errorMessage(err), 'error')
      }
    } finally {
      if (projectIdRef.current === operationProjectId) reorderingRef.current = false
      if (mountedRef.current && projectIdRef.current === operationProjectId) setReordering(false)
    }
  }

  const onDropSegment = (event: DragEvent, targetIndex: number) => {
    event.preventDefault()
    const sourceId = draggingId || event.dataTransfer.getData('text/plain')
    if (sourceId) void reorder(sourceId, targetIndex)
    setDraggingId(null)
    setDragOverId(null)
  }

  const chooseSegment = (id: string) => {
    if (id === selectedIdRef.current) {
      setMobilePane('editor')
      return
    }
    if (segmentSwitchPromise.current) return
    const pending = (async () => {
      setSwitchingSegment(true)
      try {
        if (!await ensureSaved()) return
        hydratedSegmentId.current = null
        selectedIdRef.current = id
        setSelectedId(id)
        setMobilePane('editor')
      } finally {
        setSwitchingSegment(false)
      }
    })()
    segmentSwitchPromise.current = pending
    void pending.finally(() => {
      if (segmentSwitchPromise.current === pending) segmentSwitchPromise.current = null
    })
  }

  const previewAsset = selectionAsset(selected)
  const selectedAssetId = selected?.selection?.asset_id || selected?.recommendations[0]?.asset.id
  const transcript = detail?.source?.transcript || detail?.source?.text || segments.map((item) => item.text).join('\n')

  if (loading && !detail) return <main className="workbench-page"><PageLoader label="打开工作台" /></main>
  if (error && !detail) return <main className="page"><ErrorState message={error} onRetry={() => void load(true)} /></main>

  return (
    <main className="workbench-page">
      <header className="workbench-header">
        <div className="workbench-title">
          <AppLink href="/projects" className="icon-button" aria-label="返回项目台"><ArrowLeft size={19} /></AppLink>
          <div><div className="workbench-name-row"><h1>{detail?.project.title || '项目工作台'}</h1><StatusPill status="ready" /></div><p>{segments.length} 个内容片段 · {detail?.project.input_kind === 'video' ? '视频输入' : detail?.project.input_kind === 'audio' ? '音频输入' : '文本输入'}</p></div>
        </div>
        <div className="workbench-actions">
          {detail?.trace_summary?.degraded && <span className="degraded-chip" title="AI 调用不可用，已使用确定性规则完成"><AlertTriangle size={14} /> 规则降级完成</span>}
          <span className={`save-indicator state-${saveState}`}>
            {saveState === 'saving' ? <LoaderCircle size={15} className="spin" /> : saveState === 'error' ? <AlertTriangle size={15} /> : <Check size={15} />}
            {saveState === 'saving' ? '保存中' : saveState === 'dirty' ? '等待保存' : saveState === 'error' ? '保存失败' : '已自动保存'}
          </span>
          <button type="button" className="button button-secondary button-small" disabled={saveState !== 'dirty' && saveState !== 'error'} onClick={() => void save()}><Save size={15} /> 保存</button>
        </div>
      </header>

      {error && <div className="workbench-banner"><Info size={16} /> {error}<button type="button" onClick={() => void load()}>重新加载</button></div>}
      {conflict && <div className="workbench-banner conflict-banner"><AlertTriangle size={16} /> 此片段已在其他会话更新，本地草稿尚未覆盖远端版本。<button type="button" onClick={() => void load(false, true)}>放弃草稿并重新加载</button></div>}

      <nav className="mobile-workbench-tabs" aria-label="工作台面板">
        <button type="button" className={mobilePane === 'segments' ? 'active' : ''} onClick={() => setMobilePane('segments')}><ListVideo size={17} /> 字幕</button>
        <button type="button" className={mobilePane === 'editor' ? 'active' : ''} onClick={() => setMobilePane('editor')}><TextCursorInput size={17} /> 编辑</button>
        <button type="button" className={mobilePane === 'matches' ? 'active' : ''} onClick={() => setMobilePane('matches')}><Image size={17} /> 候选</button>
      </nav>

      <div className="workbench-grid">
        <aside className={`segments-panel workbench-panel${mobilePane === 'segments' ? ' mobile-active' : ''}`}>
          <div className="panel-tabs">
            <button type="button" className={leftView === 'segments' ? 'active' : ''} onClick={() => setLeftView('segments')}>内容片段 <span>{segments.length}</span></button>
            <button type="button" className={leftView === 'transcript' ? 'active' : ''} onClick={() => setLeftView('transcript')}>原始字幕</button>
          </div>
          {leftView === 'transcript' ? (
            <div className="transcript-view">
              <div className="transcript-label"><FileTranscriptIcon /> 原始输入（只读）</div>
              <p>{String(transcript || '没有可用的原始字幕。')}</p>
            </div>
          ) : segments.length === 0 ? (
            <EmptyState title="还没有内容片段" description="任务可能尚未完成，请返回处理页查看状态。" />
          ) : (
            <div className="segment-list">
              {segments.map((segment, index) => (
                <div
                  key={segment.id}
                  className={`segment-item${segment.id === selectedId ? ' active' : ''}${draggingId === segment.id ? ' dragging' : ''}${dragOverId === segment.id && draggingId !== segment.id ? ' drag-over' : ''}`}
                  data-segment-id={segment.id}
                  draggable={!reordering && !switchingSegment}
                  onDragStart={(event) => {
                    event.dataTransfer.effectAllowed = 'move'
                    event.dataTransfer.setData('text/plain', segment.id)
                    setDraggingId(segment.id)
                  }}
                  onDragEnter={() => {
                    if (draggingId !== segment.id) setDragOverId(segment.id)
                  }}
                  onDragOver={(event) => {
                    event.preventDefault()
                    event.dataTransfer.dropEffect = 'move'
                  }}
                  onDrop={(event) => onDropSegment(event, index)}
                  onDragEnd={() => {
                    setDraggingId(null)
                    setDragOverId(null)
                  }}
                >
                  <button type="button" className="segment-open" disabled={switchingSegment} onClick={() => chooseSegment(segment.id)}>
                    <span className="segment-index">{String(index + 1).padStart(2, '0')}</span>
                    <span className="segment-thumb"><AssetVisual asset={selectionAsset(segment)} /></span>
                    <span className="segment-copy"><strong>{segment.topic || `片段 ${index + 1}`}</strong><p>{segment.text}</p><small>{formatDuration(segment.start_ms)}{segment.end_ms != null ? ` – ${formatDuration(segment.end_ms)}` : ''} · {segment.keywords.slice(0, 2).join(' / ') || '等待关键词'}</small></span>
                  </button>
                  <div className="segment-controls">
                    <button type="button" title="向上移动" disabled={index === 0 || reordering} onClick={() => void reorder(segment.id, index - 1)}><ChevronUp size={14} /></button>
                    <span title="拖动排序"><GripVertical size={15} /></span>
                    <button type="button" title="向下移动" disabled={index === segments.length - 1 || reordering} onClick={() => void reorder(segment.id, index + 1)}><ChevronDown size={14} /></button>
                  </div>
                </div>
              ))}
            </div>
          )}
          <div className="panel-foot-note">{reordering ? <InlineSpinner label="正在保存顺序" /> : <><Check size={14} /> 拖动或使用箭头调整顺序</>}</div>
        </aside>

        <section className={`editor-panel workbench-panel${mobilePane === 'editor' ? ' mobile-active' : ''}`}>
          {selected ? (
            <>
              <div className="preview-stage">
                <div className="preview-toolbar"><span><Image size={15} /> 画面预览</span><span>16:9</span></div>
                <div className="video-preview">
                  <AssetVisual asset={previewAsset} className="preview-image" controls={previewAsset?.kind === 'video'} eager />
                  <div className="preview-shade" />
                  <div className="subtitle-overlay"><span>{draft.text || '输入字幕内容'}</span></div>
                  <div className="preview-badge">片段 {selectedIndex + 1} / {segments.length}</div>
                </div>
                <div className="preview-caption"><span>{previewAsset?.name || '尚未选择画面'}</span><span>{selected.selection?.source === 'manual' ? '人工选择' : '智能推荐'}</span></div>
              </div>

              <TimelinePreview
                timeline={timeline}
                preview={preview}
                job={previewJob || preview?.job || null}
                selectedSegmentId={selectedId}
                loading={previewLoading}
                creating={previewCreating}
                error={previewError}
                onSelectSegment={chooseSegment}
                onGenerate={() => void generatePreview()}
                onReload={() => void loadPreviewOverview(true)}
              />

              <div className="editor-form">
                <div className="editor-form-head"><div><span className="editor-index">{String(selectedIndex + 1).padStart(2, '0')}</span><div><h2>编辑内容片段</h2><p>修改后将自动保存，并可重新匹配候选</p></div></div><div className="segment-nav"><button type="button" disabled={switchingSegment || selectedIndex <= 0} onClick={() => chooseSegment(segments[selectedIndex - 1].id)}><ChevronLeft size={17} /></button><button type="button" disabled={switchingSegment || selectedIndex >= segments.length - 1} onClick={() => chooseSegment(segments[selectedIndex + 1].id)}><ChevronRight size={17} /></button></div></div>
                <div className="form-field compact-field"><label htmlFor="segment-text">字幕文本</label><textarea id="segment-text" className="segment-textarea" value={draft.text} onChange={(event) => editDraft({ text: event.target.value })} onBlur={() => { if (saveState === 'dirty') void save() }} /><small className="field-count">{draft.text.length} 字</small></div>
                <div className="editor-fields-row">
                  <div className="form-field compact-field"><label htmlFor="segment-topic">内容主题</label><input id="segment-topic" value={draft.topic} onChange={(event) => editDraft({ topic: event.target.value })} onBlur={() => { if (saveState === 'dirty') void save() }} placeholder="例如：人工智能" /></div>
                  <div className="form-field compact-field"><label htmlFor="segment-keywords">关键词 <span>逗号分隔</span></label><div className="input-with-icon"><Tags size={15} /><input id="segment-keywords" value={draft.keywords} onChange={(event) => editDraft({ keywords: event.target.value })} onBlur={() => { if (saveState === 'dirty') void save() }} placeholder="科技，效率，未来" /></div></div>
                </div>
                <div className="auto-save-note"><Clock3 size={14} /> {saveState === 'dirty' ? '停止输入后自动保存' : saveState === 'saving' ? '正在同步到服务器…' : saveState === 'error' ? '保存失败，请点击上方保存重试' : '编辑内容已同步到服务器'}</div>
              </div>
            </>
          ) : <EmptyState title="选择一个内容片段" description="从左侧选择片段后，在这里预览和编辑。" />}
        </section>

        <aside className={`matches-panel workbench-panel${mobilePane === 'matches' ? ' mobile-active' : ''}`}>
          <div className="matches-head">
            <div><h2>画面候选</h2><p>先看匹配依据，再决定是否采用</p></div>
            <button type="button" className="icon-button" title="根据当前文本重新匹配" disabled={rematching || !selected} onClick={() => void rematch()}>{rematching ? <LoaderCircle className="spin" size={17} /> : <RefreshCw size={17} />}</button>
          </div>
          <div className="asset-search"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索素材库并替换…" />{query && <button type="button" aria-label="清空搜索" onClick={() => setQuery('')}><X size={15} /></button>}</div>
          <div className="asset-search-filters"><select aria-label="素材类型" value={searchKind} onChange={(event) => setSearchKind(event.target.value)}><option value="">全部类型</option><option value="image">图片</option><option value="video">视频</option></select><input aria-label="素材标签" value={searchTag} onChange={(event) => setSearchTag(event.target.value)} placeholder="按标签筛选" /></div>

          {query || searchKind || searchTag ? (
            <div className="search-result-view">
              <div className="result-label"><span>素材库搜索结果</span>{searching && <LoaderCircle className="spin" size={15} />}</div>
              {!searching && searchResults.length === 0 ? <EmptyState icon={<Search size={22} />} title="没有匹配素材" description="换一个关键词，或前往素材库上传新画面。" /> : (
                <div className="replacement-grid">
                  {searchResults.map((asset) => (
                    <button type="button" key={asset.id} className={`replacement-card${selectedAssetId === asset.id ? ' selected' : ''}`} disabled={selectingAsset !== null} onClick={() => void selectAsset(asset)}>
                      <AssetVisual asset={asset} /><span><strong>{asset.name}</strong><small>{asset.tags.slice(0, 2).join(' · ') || '未添加标签'}</small></span>{selectingAsset === asset.id && <LoaderCircle className="spin card-loader" size={18} />}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <div className="candidate-list">
              {(selected?.recommendations || []).length > 0 && (selected?.recommendations || []).length < 3 && <div className="inline-warning"><AlertTriangle size={16} /> 服务端仅返回 {(selected?.recommendations || []).length} 个候选，请重新匹配后再确认画面。</div>}
              {(selected?.recommendations || []).length === 0 ? (
                <EmptyState icon={<Image size={24} />} title="暂无画面候选" description="点击右上角重新匹配，生成至少三个可解释候选。" action={<button type="button" className="button button-secondary button-small" onClick={() => void rematch()}>重新匹配</button>} />
              ) : selected?.recommendations.map((candidate, index) => (
                <CandidateCard
                  key={candidate.id || candidate.asset.id}
                  candidate={candidate}
                  index={index}
                  selected={selectedAssetId === candidate.asset.id}
                  loading={selectingAsset === candidate.asset.id}
                  disabled={selectingAsset !== null}
                  onSelect={() => void selectAsset(candidate.asset)}
                />
              ))}
            </div>
          )}
          <div className="match-method"><CircleHelp size={16} /><p><strong>匹配公式</strong><br />55% 语义相似度 + 30% 关键词重合 + 15% 主题/标签重合</p></div>
        </aside>
      </div>
    </main>
  )
}

function TimelinePreview({
  timeline,
  preview,
  job,
  selectedSegmentId,
  loading,
  creating,
  error,
  onSelectSegment,
  onGenerate,
  onReload,
}: {
  timeline: ProjectTimeline | null
  preview: PreviewRender | null
  job: Job | null
  selectedSegmentId: string | null
  loading: boolean
  creating: boolean
  error: string
  onSelectSegment: (segmentId: string) => void
  onGenerate: () => void
  onReload: () => void
}) {
  const status = job?.status || preview?.status || ''
  const running = creating || status === 'queued' || status === 'running'
  const succeeded = status === 'succeeded' && Boolean(preview?.output_url)
  const failed = status === 'failed' || status === 'canceled'
  const progress = creating ? 2 : Math.max(2, Math.min(100, job?.progress ?? (succeeded ? 100 : 4)))
  const outputUrl = mediaUrl(preview?.output_url)
  const items = timeline?.items || []

  return (
    <section className="timeline-preview" aria-labelledby="timeline-preview-title">
      <div className="timeline-preview-head">
        <div>
          <span className="timeline-kicker"><Film size={15} /> 组合预览</span>
          <h2 id="timeline-preview-title">时间线</h2>
          <p>{timeline ? `${timeline.segment_count} 个片段 · ${formatDuration(timeline.duration_ms)}` : '读取当前片段与已选素材'}</p>
        </div>
        <button type="button" className="button button-secondary button-small timeline-generate" disabled={loading || running || items.length === 0} onClick={onGenerate}>
          {running ? <InlineSpinner label={creating ? '正在创建任务' : '正在生成'} /> : preview ? <><RotateCcw size={15} /> 重新生成预览</> : <><Play size={15} fill="currentColor" /> 生成预览视频</>}
        </button>
      </div>

      {loading && !timeline ? <div className="timeline-loading"><InlineSpinner label="正在读取时间线" /></div> : items.length === 0 ? (
        <div className="timeline-empty"><Info size={16} /><span>请先为内容片段选择素材，再生成组合预览。</span></div>
      ) : (
        <div className="timeline-track" role="list" aria-label="组合预览片段">
          {items.map((item, index) => (
            <button
              type="button"
              role="listitem"
              key={item.segment_id}
              className={`timeline-clip${selectedSegmentId === item.segment_id ? ' active' : ''}`}
              style={{ minWidth: `${Math.max(92, Math.min(220, item.duration_ms / 30))}px` }}
              aria-pressed={selectedSegmentId === item.segment_id}
              onClick={() => onSelectSegment(item.segment_id)}
            >
              <span className="timeline-clip-visual"><AssetVisual asset={item.asset} alt={`片段 ${index + 1} 素材`} /><i>{String(index + 1).padStart(2, '0')}</i></span>
              <span className="timeline-clip-copy"><strong>{item.topic || `片段 ${index + 1}`}</strong><small>{formatDuration(item.duration_ms)}</small></span>
            </button>
          ))}
        </div>
      )}

      {running && (
        <div className="timeline-progress" role="status" aria-live="polite">
          <div><span>{job?.stage === 'preview_rendering' ? '正在组合画面与字幕' : job?.stage === 'preview_finalizing' ? '正在保存预览视频' : '正在准备组合预览'}</span><strong>{progress}%</strong></div>
          <i><span style={{ width: `${progress}%` }} /></i>
        </div>
      )}

      {succeeded && outputUrl && (
        <div className="timeline-output">
          <video className="timeline-output-video" src={outputUrl} controls playsInline preload="metadata" aria-label="FrameFlow 组合预览视频" />
          <div><span><Check size={15} /> 预览已生成</span><small>{preview?.segment_count || timeline?.segment_count || 0} 个片段 · {formatDuration(preview?.duration_ms ?? timeline?.duration_ms)}</small></div>
        </div>
      )}

      {(failed || error) && !running && (
        <div className="timeline-error" role="alert"><AlertTriangle size={16} /><span>{preview?.error_message || error || '预览生成失败，请重新生成。'}</span>{error && <button type="button" onClick={onReload}>刷新状态</button>}</div>
      )}
    </section>
  )
}

function CandidateCard({ candidate, index, selected, loading, disabled, onSelect }: { candidate: Recommendation; index: number; selected: boolean; loading: boolean; disabled: boolean; onSelect: () => void }) {
  const [expanded, setExpanded] = useState(false)
  const total = scorePercent(candidate.total_score)
  return (
    <article className={`candidate-card${selected ? ' selected' : ''}`}>
      <div className="candidate-visual">
        <AssetVisual asset={candidate.asset} />
        {candidate.asset.kind === 'video' && <span className="video-play-badge"><Play size={13} fill="currentColor" /> 视频</span>}
        <span className="rank-badge">#{candidate.rank || index + 1}</span>
        {selected && <span className="selected-badge"><Check size={13} /> 当前使用</span>}
        {candidate.is_diversity_filler && <span className="diversity-badge">低相关补位</span>}
      </div>
      <div className="candidate-body">
        <div className="candidate-title"><div><h3>{candidate.asset.name}</h3><span>{candidate.asset.tags.slice(0, 3).join(' · ')}</span></div><strong><small>匹配度</small>{total}%</strong></div>
        <div className="score-bar"><span style={{ width: `${total}%` }} /></div>
        <button type="button" className="explain-toggle" onClick={() => setExpanded(!expanded)}><CircleHelp size={14} /> 匹配依据 <ChevronDown size={14} className={expanded ? 'rotated' : ''} /></button>
        {expanded && (
          <div className="explanation">
            <p>{candidate.explanation || (candidate.is_diversity_filler ? '用于补充画面多样性，相关度相对较低。' : '素材与当前片段的语义和关键词具有较高关联。')}</p>
            <div className="score-breakdown">
              <ScoreItem label="语义" score={candidate.tfidf_score} />
              <ScoreItem label="关键词" score={candidate.keyword_score} />
              <ScoreItem label="主题" score={candidate.tag_score} />
            </div>
            {candidate.matched_terms?.length > 0 && <div className="matched-terms"><span>命中</span>{candidate.matched_terms.slice(0, 5).map((term) => <i key={term}>{term}</i>)}</div>}
          </div>
        )}
        <button type="button" className={`button ${selected ? 'button-selected' : 'button-secondary'} candidate-select`} disabled={disabled || selected} onClick={onSelect}>
          {loading ? <InlineSpinner label="正在选择" /> : selected ? <><Check size={16} /> 当前使用</> : '采用此画面'}
        </button>
      </div>
    </article>
  )
}

function ScoreItem({ label, score }: { label: string; score: number }) {
  const percent = scorePercent(score)
  return <div><span><i>{label}</i><b>{percent}</b></span><em><i style={{ width: `${percent}%` }} /></em></div>
}

function FileTranscriptIcon() {
  return <TextCursorInput size={15} />
}
