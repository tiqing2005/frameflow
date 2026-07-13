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
  GripVertical,
  Image,
  Info,
  ListVideo,
  LoaderCircle,
  RefreshCw,
  Save,
  Search,
  Sparkles,
  Tags,
  TextCursorInput,
  WandSparkles,
  X,
} from 'lucide-react'
import { api, errorMessage } from '../api'
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
import { AppLink, navigate } from '../router'
import type { Asset, ProjectDetail, Recommendation, Segment, Selection } from '../types'

interface SegmentDraft { text: string; topic: string; keywords: string }
type SaveState = 'idle' | 'dirty' | 'saving' | 'saved' | 'error'
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
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [leftView, setLeftView] = useState<'segments' | 'transcript'>('segments')
  const [mobilePane, setMobilePane] = useState<MobilePane>('editor')
  const [query, setQuery] = useState('')
  const [searchResults, setSearchResults] = useState<Asset[]>([])
  const [searching, setSearching] = useState(false)
  const [selectingAsset, setSelectingAsset] = useState<string | null>(null)
  const [rematching, setRematching] = useState(false)
  const [reordering, setReordering] = useState(false)
  const [draggingId, setDraggingId] = useState<string | null>(null)
  const saveTimer = useRef<number | null>(null)
  const requestVersion = useRef(0)
  const toast = useToast()

  const load = useCallback(async (showLoader = false) => {
    if (showLoader) setLoading(true)
    try {
      const result = await api.project(projectId)
      if (result.project.status !== 'ready' && result.current_job?.status !== 'succeeded') {
        navigate(`/projects/${projectId}/processing`, { replace: true })
        return
      }
      const ordered = [...(result.segments || [])].sort((a, b) => a.position - b.position)
      setDetail({ ...result, segments: ordered })
      setSelectedId((current) => current && ordered.some((item) => item.id === current) ? current : ordered[0]?.id || null)
      setError('')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [projectId])

  useEffect(() => { void load(true) }, [load])

  const segments = detail?.segments || []
  const selectedIndex = segments.findIndex((segment) => segment.id === selectedId)
  const selected = selectedIndex >= 0 ? segments[selectedIndex] : undefined

  useEffect(() => {
    if (!selected) return
    setDraft(asDraft(selected))
    setSaveState('idle')
    requestVersion.current += 1
  }, [selected])

  const updateSelected = useCallback((updater: (segment: Segment) => Segment) => {
    setDetail((current) => current ? {
      ...current,
      segments: current.segments.map((segment) => segment.id === selectedId ? updater(segment) : segment),
    } : current)
  }, [selectedId])

  const save = useCallback(async () => {
    if (!selected || saveState === 'saving') return
    const currentRequest = ++requestVersion.current
    setSaveState('saving')
    try {
      const updated = await api.updateSegment(selected.id, {
        text: draft.text.trim(),
        topic: draft.topic.trim(),
        keywords: draft.keywords.split(/[，,]/).map((value) => value.trim()).filter(Boolean),
        version: selected.version,
      })
      if (currentRequest !== requestVersion.current) return
      updateSelected((segment) => ({ ...segment, ...updated, recommendations: updated.recommendations || segment.recommendations, selection: updated.selection ?? segment.selection }))
      setSaveState('saved')
      window.setTimeout(() => setSaveState((state) => state === 'saved' ? 'idle' : state), 1800)
    } catch (err) {
      if (currentRequest !== requestVersion.current) return
      setSaveState('error')
      toast(errorMessage(err), 'error')
      if ((err as { status?: number }).status === 409) void load()
    }
  }, [draft, load, saveState, selected, toast, updateSelected])

  const editDraft = (changes: Partial<SegmentDraft>) => {
    setDraft((current) => ({ ...current, ...changes }))
    setSaveState('dirty')
  }

  useEffect(() => {
    if (saveState !== 'dirty') return
    if (saveTimer.current) window.clearTimeout(saveTimer.current)
    saveTimer.current = window.setTimeout(() => { void save() }, 750)
    return () => { if (saveTimer.current) window.clearTimeout(saveTimer.current) }
  }, [draft, save, saveState])

  useEffect(() => () => { if (saveTimer.current) window.clearTimeout(saveTimer.current) }, [])

  useEffect(() => {
    if (!query.trim()) {
      setSearchResults([])
      setSearching(false)
      return
    }
    const timer = window.setTimeout(async () => {
      setSearching(true)
      try {
        const result = await api.assets({ q: query.trim() })
        setSearchResults(result.items)
      } catch (err) {
        toast(errorMessage(err), 'error')
      } finally {
        setSearching(false)
      }
    }, 350)
    return () => window.clearTimeout(timer)
  }, [query, toast])

  const selectAsset = async (asset: Asset) => {
    if (!selected || selectingAsset) return
    setSelectingAsset(asset.id)
    const previous = selected.selection
    const optimistic: Selection = { segment_id: selected.id, asset_id: asset.id, source: 'manual', asset }
    updateSelected((segment) => ({ ...segment, selection: optimistic }))
    try {
      const result = await api.selectAsset(selected.id, asset.id)
      if ('selection' in result && result.selection) updateSelected((segment) => ({ ...segment, selection: result.selection }))
      else if ('id' in result && 'text' in result) updateSelected(() => result as Segment)
      toast(`已将「${asset.name}」设为当前画面`, 'success')
      setQuery('')
    } catch (err) {
      updateSelected((segment) => ({ ...segment, selection: previous }))
      toast(errorMessage(err), 'error')
    } finally {
      setSelectingAsset(null)
    }
  }

  const rematch = async () => {
    if (!selected || rematching) return
    setRematching(true)
    try {
      if (saveState === 'dirty') await save()
      const result = await api.rematchSegment(selected.id)
      const updated = 'segment' in result ? result.segment : result
      updateSelected((segment) => ({ ...segment, ...updated }))
      toast('已根据当前文本重新生成候选', 'success')
    } catch (err) {
      toast(errorMessage(err), 'error')
    } finally {
      setRematching(false)
    }
  }

  const reorder = async (fromId: string, toIndex: number) => {
    if (!detail || reordering) return
    const fromIndex = detail.segments.findIndex((item) => item.id === fromId)
    if (fromIndex < 0 || toIndex < 0 || toIndex >= detail.segments.length || fromIndex === toIndex) return
    const previous = detail.segments
    const next = [...previous]
    const [moving] = next.splice(fromIndex, 1)
    next.splice(toIndex, 0, moving)
    const positioned = next.map((item, index) => ({ ...item, position: index }))
    setDetail({ ...detail, segments: positioned })
    setReordering(true)
    try {
      await api.reorderSegments(projectId, positioned.map((item) => item.id))
      toast('片段顺序已保存', 'success')
    } catch (err) {
      setDetail((current) => current ? { ...current, segments: previous } : current)
      toast(errorMessage(err), 'error')
    } finally {
      setReordering(false)
    }
  }

  const onDropSegment = (event: DragEvent, targetIndex: number) => {
    event.preventDefault()
    if (draggingId) void reorder(draggingId, targetIndex)
    setDraggingId(null)
  }

  const chooseSegment = (id: string) => {
    if (saveTimer.current) window.clearTimeout(saveTimer.current)
    if (saveState === 'dirty') void save()
    setSelectedId(id)
    setMobilePane('editor')
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
          <button type="button" className="button button-secondary button-small" disabled={saveState === 'saving' || saveState === 'idle'} onClick={() => void save()}><Save size={15} /> 保存</button>
        </div>
      </header>

      {error && <div className="workbench-banner"><Info size={16} /> {error}<button type="button" onClick={() => void load()}>重新加载</button></div>}

      <nav className="mobile-workbench-tabs" aria-label="工作台面板">
        <button type="button" className={mobilePane === 'segments' ? 'active' : ''} onClick={() => setMobilePane('segments')}><ListVideo size={17} /> 字幕</button>
        <button type="button" className={mobilePane === 'editor' ? 'active' : ''} onClick={() => setMobilePane('editor')}><TextCursorInput size={17} /> 编辑</button>
        <button type="button" className={mobilePane === 'matches' ? 'active' : ''} onClick={() => setMobilePane('matches')}><WandSparkles size={17} /> 候选</button>
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
                  className={`segment-item${segment.id === selectedId ? ' active' : ''}${draggingId === segment.id ? ' dragging' : ''}`}
                  draggable={!reordering}
                  onDragStart={() => setDraggingId(segment.id)}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={(event) => onDropSegment(event, index)}
                >
                  <button type="button" className="segment-open" onClick={() => chooseSegment(segment.id)}>
                    <span className="segment-index">{String(index + 1).padStart(2, '0')}</span>
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
                  <AssetVisual asset={previewAsset} className="preview-image" />
                  <div className="preview-shade" />
                  <div className="subtitle-overlay"><span>{draft.text || '输入字幕内容'}</span></div>
                  <div className="preview-badge">片段 {selectedIndex + 1} / {segments.length}</div>
                </div>
                <div className="preview-caption"><span>{previewAsset?.name || '尚未选择画面'}</span><span>{selected.selection?.source === 'manual' ? '人工选择' : '智能推荐'}</span></div>
              </div>

              <div className="editor-form">
                <div className="editor-form-head"><div><span className="editor-index">{String(selectedIndex + 1).padStart(2, '0')}</span><div><h2>编辑内容片段</h2><p>修改后将自动保存，并可重新匹配候选</p></div></div><div className="segment-nav"><button type="button" disabled={selectedIndex <= 0} onClick={() => chooseSegment(segments[selectedIndex - 1].id)}><ChevronLeft size={17} /></button><button type="button" disabled={selectedIndex >= segments.length - 1} onClick={() => chooseSegment(segments[selectedIndex + 1].id)}><ChevronRight size={17} /></button></div></div>
                <div className="form-field compact-field"><label htmlFor="segment-text">字幕文本</label><textarea id="segment-text" className="segment-textarea" value={draft.text} onChange={(event) => editDraft({ text: event.target.value })} /><small className="field-count">{draft.text.length} 字</small></div>
                <div className="editor-fields-row">
                  <div className="form-field compact-field"><label htmlFor="segment-topic">内容主题</label><input id="segment-topic" value={draft.topic} onChange={(event) => editDraft({ topic: event.target.value })} placeholder="例如：人工智能" /></div>
                  <div className="form-field compact-field"><label htmlFor="segment-keywords">关键词 <span>逗号分隔</span></label><div className="input-with-icon"><Tags size={15} /><input id="segment-keywords" value={draft.keywords} onChange={(event) => editDraft({ keywords: event.target.value })} placeholder="科技，效率，未来" /></div></div>
                </div>
                <div className="auto-save-note"><Clock3 size={14} /> {saveState === 'dirty' ? '停止输入后自动保存' : saveState === 'saving' ? '正在同步到服务器…' : saveState === 'error' ? '保存失败，请点击上方保存重试' : '编辑内容已同步到服务器'}</div>
              </div>
            </>
          ) : <EmptyState title="选择一个内容片段" description="从左侧选择片段后，在这里预览和编辑。" />}
        </section>

        <aside className={`matches-panel workbench-panel${mobilePane === 'matches' ? ' mobile-active' : ''}`}>
          <div className="matches-head">
            <div><h2>画面候选</h2><p>混合匹配 · 分数组成可解释</p></div>
            <button type="button" className="icon-button" title="根据当前文本重新匹配" disabled={rematching || !selected} onClick={() => void rematch()}>{rematching ? <LoaderCircle className="spin" size={17} /> : <RefreshCw size={17} />}</button>
          </div>
          <div className="asset-search"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索素材库并替换…" />{query && <button type="button" aria-label="清空搜索" onClick={() => setQuery('')}><X size={15} /></button>}</div>

          {query ? (
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
              {(selected?.recommendations || []).length === 0 ? (
                <EmptyState icon={<WandSparkles size={24} />} title="暂无画面候选" description="点击右上角重新匹配，生成至少三个可解释候选。" action={<button type="button" className="button button-secondary button-small" onClick={() => void rematch()}>重新匹配</button>} />
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

function CandidateCard({ candidate, index, selected, loading, disabled, onSelect }: { candidate: Recommendation; index: number; selected: boolean; loading: boolean; disabled: boolean; onSelect: () => void }) {
  const [expanded, setExpanded] = useState(index === 0)
  const total = scorePercent(candidate.total_score)
  return (
    <article className={`candidate-card${selected ? ' selected' : ''}`}>
      <div className="candidate-visual">
        <AssetVisual asset={candidate.asset} />
        <span className="rank-badge">#{candidate.rank || index + 1}</span>
        {selected && <span className="selected-badge"><Check size={13} /> 当前画面</span>}
        {candidate.is_diversity_filler && <span className="diversity-badge">多样性补位</span>}
      </div>
      <div className="candidate-body">
        <div className="candidate-title"><div><h3>{candidate.asset.name}</h3><span>{candidate.asset.tags.slice(0, 3).join(' · ')}</span></div><strong>{total}<small>分</small></strong></div>
        <div className="score-bar"><span style={{ width: `${total}%` }} /></div>
        <button type="button" className="explain-toggle" onClick={() => setExpanded(!expanded)}><Sparkles size={14} /> 为什么推荐 <ChevronDown size={14} className={expanded ? 'rotated' : ''} /></button>
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
          {loading ? <InlineSpinner label="正在选择" /> : selected ? <><Check size={16} /> 已选择</> : '使用这个画面'}
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
