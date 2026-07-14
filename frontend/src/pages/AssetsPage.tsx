import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from 'react'
import {
  Check,
  Filter,
  FolderOpen,
  Image as ImageIcon,
  Play,
  Plus,
  Search,
  ShieldAlert,
  Sparkles,
  Tags,
  Trash2,
  UploadCloud,
  X,
} from 'lucide-react'
import { api, errorMessage, isAbortError } from '../api'
import { AssetVisual, EmptyState, ErrorState, formatDate, InlineSpinner, PageLoader, useToast } from '../components/ui'
import type { Asset } from '../types'

const ASSET_FILE_PATTERN = /\.(png|jpe?g|webp|gif|mp4|webm|mov)$/i
const IMAGE_FILE_PATTERN = /\.(png|jpe?g|webp|gif)$/i
const ASSET_FILE_ACCEPT = '.png,.jpg,.jpeg,.webp,.gif,.mp4,.webm,.mov'
const FOCUSABLE_SELECTOR = 'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'
const TAGGING_POLL_INTERVAL = 1_200
const TAGGING_POLL_MAX_INTERVAL = 12_000

function isTaggingActive(asset?: Asset | null) {
  return asset?.tagging_status === 'queued' || asset?.tagging_status === 'running'
}

function taggingPresentation(asset: Asset) {
  if (asset.tagging_status === 'queued') return {
    label: '等待画面识别',
    description: '任务已进入后台队列，完成后标签和关键词会自动更新。',
    tone: 'pending',
  }
  if (asset.tagging_status === 'running') return {
    label: '正在识别画面',
    description: 'AI 正在分析素材画面，可关闭详情继续处理其他任务。',
    tone: 'running',
  }
  if (asset.tagging_status === 'degraded' && asset.tagging_source === 'text_llm') return {
    label: '文本 AI 降级完成',
    description: '画面识别暂不可用，已根据素材名称和文本信息生成可用标签。',
    tone: 'degraded',
  }
  if (asset.tagging_status === 'degraded' && asset.tagging_source === 'rules') return {
    label: '本地规则降级完成',
    description: '画面识别和文本 AI 暂不可用，已使用本地规则生成可用标签。',
    tone: 'degraded',
  }
  if (asset.tagging_status === 'succeeded' && asset.tagging_source === 'vision') return {
    label: '画面识别完成',
    description: '标签和关键词已根据实际画面内容生成。',
    tone: 'succeeded',
  }
  if (asset.tagging_status === 'succeeded') return {
    label: 'AI 标签已生成',
    description: '标签和关键词已生成并保存。',
    tone: 'succeeded',
  }
  return {
    label: '尚未运行画面识别',
    description: '可以让 AI 查看画面并重新生成标签和关键词。',
    tone: 'idle',
  }
}

function useDialogFocus(onClose: () => void, canClose = true) {
  const dialogRef = useRef<HTMLElement>(null)
  const closeRef = useRef(onClose)
  const canCloseRef = useRef(canClose)
  closeRef.current = onClose
  canCloseRef.current = canClose

  useEffect(() => {
    const dialog = dialogRef.current
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    if (!dialog) return
    const focusable = () => Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
      .filter((element) => !element.hidden && element.getAttribute('aria-hidden') !== 'true')
    const frame = window.requestAnimationFrame(() => (focusable()[0] || dialog).focus())
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && canCloseRef.current) {
        event.preventDefault()
        closeRef.current()
        return
      }
      if (event.key !== 'Tab') return
      const items = focusable()
      if (items.length === 0) {
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
    return () => {
      window.cancelAnimationFrame(frame)
      document.removeEventListener('keydown', onKeyDown)
      previousFocus?.focus()
    }
  }, [])

  return dialogRef
}

export function AssetsPage() {
  const [assets, setAssets] = useState<Asset[]>([])
  const [total, setTotal] = useState(0)
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [uploadOpen, setUploadOpen] = useState(false)
  const [selected, setSelected] = useState<Asset | null>(null)
  const loadVersion = useRef(0)
  const loadAbort = useRef<AbortController | null>(null)

  const load = useCallback(async () => {
    const currentLoad = ++loadVersion.current
    loadAbort.current?.abort()
    const controller = new AbortController()
    loadAbort.current = controller
    setLoading(true)
    try {
      const result = await api.assets({ q: query.trim() || undefined, kind: kind || undefined }, { signal: controller.signal })
      if (controller.signal.aborted || currentLoad !== loadVersion.current) return
      setAssets(result.items)
      setSelected((current) => current
        ? result.items.find((item) => item.id === current.id) || current
        : current)
      setTotal(result.total)
      setError('')
    } catch (err) {
      if (!isAbortError(err) && currentLoad === loadVersion.current) setError(errorMessage(err))
    } finally {
      if (currentLoad === loadVersion.current) setLoading(false)
      if (loadAbort.current === controller) loadAbort.current = null
    }
  }, [kind, query])

  useEffect(() => {
    const timer = window.setTimeout(() => { void load() }, query ? 300 : 0)
    return () => {
      window.clearTimeout(timer)
      loadAbort.current?.abort()
    }
  }, [load, query])

  const hasActiveTagging = assets.some(isTaggingActive)
  useEffect(() => {
    if (!hasActiveTagging) return
    let stopped = false
    let timer: number | undefined
    let controller: AbortController | null = null
    let failureCount = 0

    const schedule = (delay: number) => {
      timer = window.setTimeout(() => { void poll() }, delay)
    }
    const poll = async () => {
      controller = new AbortController()
      try {
        const result = await api.assets(
          { q: query.trim() || undefined, kind: kind || undefined },
          { signal: controller.signal },
        )
        if (stopped || controller.signal.aborted) return
        failureCount = 0
        setAssets(result.items)
        setSelected((current) => current
          ? result.items.find((item) => item.id === current.id) || current
          : current)
        setTotal(result.total)
        if (result.items.some(isTaggingActive)) schedule(TAGGING_POLL_INTERVAL)
      } catch (err) {
        if (stopped || isAbortError(err)) return
        failureCount += 1
        schedule(Math.min(TAGGING_POLL_INTERVAL * (2 ** failureCount), TAGGING_POLL_MAX_INTERVAL))
      }
    }

    schedule(TAGGING_POLL_INTERVAL)
    return () => {
      stopped = true
      if (timer != null) window.clearTimeout(timer)
      controller?.abort()
    }
  }, [hasActiveTagging, kind, query])

  return (
    <main className="page assets-page">
      <div className="page-heading-row compact-heading">
        <div><h1>素材库</h1><p>管理图片和视频，为内容片段提供可检索的画面来源。</p></div>
        <button type="button" className="button button-primary heading-action" onClick={() => setUploadOpen(true)}><Plus size={17} /> 上传素材</button>
      </div>

      <div className="asset-toolbar">
        <div className="search-field"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="按名称、标签或关键词搜索素材…" />{query && <button type="button" aria-label="清空搜索" onClick={() => setQuery('')}><X size={15} /></button>}</div>
        <div className="filter-field"><Filter size={16} /><select value={kind} onChange={(event) => setKind(event.target.value)} aria-label="素材类型"><option value="">全部类型</option><option value="image">图片</option><option value="video">视频</option></select></div>
        <span className="asset-total">{loading ? '读取中…' : `${total} 项素材`}</span>
      </div>

      {error && <ErrorState message={error} onRetry={() => void load()} compact />}
      {loading && assets.length === 0 ? <PageLoader label="加载素材" /> : assets.length === 0 ? (
        <EmptyState
          icon={query || kind ? <Search size={25} /> : <FolderOpen size={26} />}
          title={query || kind ? '没有找到匹配素材' : '素材库还是空的'}
          description={query || kind ? '试试更短的关键词，或清除筛选条件。' : '上传一张图片并补充语义标签，它就能进入智能匹配候选池。'}
          action={query || kind ? <button type="button" className="button button-secondary" onClick={() => { setQuery(''); setKind('') }}>清除筛选</button> : <button type="button" className="button button-primary" onClick={() => setUploadOpen(true)}><UploadCloud size={17} /> 上传首个素材</button>}
        />
      ) : (
        <section className={`asset-gallery${loading ? ' is-refreshing' : ''}`} aria-busy={loading}>
          {assets.map((asset) => (
            <button type="button" className="asset-card" key={asset.id} onClick={() => setSelected(asset)}>
              <div className="asset-card-visual">
                <AssetVisual asset={asset} />
                {asset.kind === 'video' && <span className="asset-play-indicator"><Play size={15} fill="currentColor" /></span>}
                <span className="asset-kind-chip">{asset.kind === 'video' ? '视频' : '图片'}</span>
                {isTaggingActive(asset) && <span className={`asset-tagging-chip ${asset.tagging_status}`}><Sparkles size={11} /> {asset.tagging_status === 'queued' ? 'AI 排队中' : 'AI 识别中'}</span>}
                {asset.tagging_status === 'degraded' && <span className="asset-tagging-chip degraded"><ShieldAlert size={11} /> 降级完成</span>}
              </div>
              <div className="asset-card-body"><h3>{asset.name}</h3><div className="tag-row">{asset.tags.slice(0, 3).map((tag) => <span key={tag}>{tag}</span>)}{asset.tags.length === 0 && <span className="muted-tag">暂无标签</span>}</div><small>{asset.width && asset.height ? `${asset.width}×${asset.height}` : '本地素材'} · {formatDate(asset.created_at, false)}</small></div>
            </button>
          ))}
        </section>
      )}

      {uploadOpen && <UploadAssetModal onClose={() => setUploadOpen(false)} onUploaded={() => { setUploadOpen(false); void load() }} />}
      {selected && <AssetDetail asset={selected} onClose={() => setSelected(null)} onUpdated={(asset) => { setAssets((items) => items.map((item) => item.id === asset.id ? asset : item)); setSelected(asset) }} onDeleted={(assetId) => { setAssets((items) => items.filter((item) => item.id !== assetId)); setTotal((count) => Math.max(0, count - 1)); setSelected(null) }} />}
    </main>
  )
}

function UploadAssetModal({ onClose, onUploaded }: { onClose: () => void; onUploaded: () => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [name, setName] = useState('')
  const [tags, setTags] = useState('')
  const [keywords, setKeywords] = useState('')
  const [dragging, setDragging] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const input = useRef<HTMLInputElement>(null)
  const toast = useToast()
  const dialogRef = useDialogFocus(onClose, !uploading)

  const choose = (next?: File) => {
    if (!next) return
    if (!ASSET_FILE_PATTERN.test(next.name)) {
      setError('仅支持 PNG、JPG/JPEG、WebP、GIF、MP4、WebM 或 MOV；出于安全原因不支持 SVG')
      return
    }
    if (next.size > 100 * 1024 * 1024) { setError('单个素材不能超过 100 MB'); return }
    setFile(next)
    setName((current) => current || next.name.replace(/\.[^.]+$/, ''))
    setError('')
  }

  const drop = (event: DragEvent) => { event.preventDefault(); setDragging(false); choose(event.dataTransfer.files[0]) }
  const submit = async () => {
    if (!file || !name.trim()) { setError('请选择文件并填写素材名称'); return }
    setUploading(true)
    setError('')
    try {
      const nextTags = split(tags)
      const nextKeywords = split(keywords)
      await api.uploadAsset(file, name.trim(), nextTags, nextKeywords)
      toast(
        nextTags.length === 0 || nextKeywords.length === 0
          ? '素材已上传，AI 将在后台识别画面并补充标签'
          : '素材已上传并加入匹配池',
        'success',
      )
      onUploaded()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setUploading(false)
    }
  }

  return (
    <div className="modal-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !uploading) onClose() }}>
      <section ref={dialogRef} className="modal-card" role="dialog" aria-modal="true" aria-labelledby="upload-title" tabIndex={-1}>
        <div className="modal-head"><div><span className="modal-icon"><UploadCloud size={20} /></span><div><h2 id="upload-title">上传新素材</h2><p>标签或关键词留空时，上传后将由 AI 自动补充</p></div></div><button className="icon-button" type="button" aria-label="关闭" disabled={uploading} onClick={onClose}><X size={19} /></button></div>
        <div className="modal-body">
          {!file ? <button type="button" className={`drop-zone asset-drop${dragging ? ' dragging' : ''}`} onClick={() => input.current?.click()} onDragEnter={(event) => { event.preventDefault(); setDragging(true) }} onDragOver={(event) => event.preventDefault()} onDragLeave={() => setDragging(false)} onDrop={drop}><span className="upload-icon"><ImageIcon size={25} /></span><strong>拖放图片或视频，或点击选择</strong><span>PNG、JPG/JPEG、WebP、GIF、MP4、WebM、MOV（不支持 SVG）· 最大 100 MB</span></button> : <div className="upload-preview"><div className="upload-local-preview"><LocalAssetPreview file={file} /></div><div><strong>{file.name}</strong><span>{(file.size / 1024 / 1024).toFixed(2)} MB</span></div><button type="button" className="icon-button" aria-label="移除待上传文件" onClick={() => setFile(null)}><X size={17} /></button></div>}
          <input ref={input} hidden type="file" accept={ASSET_FILE_ACCEPT} onChange={(event) => choose(event.target.files?.[0])} />
          <div className="form-field"><label htmlFor="asset-name">素材名称 <span>必填</span></label><input id="asset-name" value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：城市夜间交通" maxLength={80} /></div>
          <div className="form-field"><label htmlFor="asset-tags">主题标签 <span>逗号分隔</span></label><div className="input-with-icon"><Tags size={16} /><input id="asset-tags" value={tags} onChange={(event) => setTags(event.target.value)} placeholder="城市，交通，夜景" /></div></div>
          <div className="form-field"><label htmlFor="asset-keywords">画面关键词 <span>逗号分隔</span></label><input id="asset-keywords" value={keywords} onChange={(event) => setKeywords(event.target.value)} placeholder="车流，灯光，通勤，现代化" /></div>
          <div className="vision-privacy-note"><ShieldAlert size={17} /><p><strong>画面识别提示</strong><span>启用视觉服务时，系统会在后台向第三方模型网关发送一张归一化画面。敏感素材请同时填写标签与关键词，避免使用自动识别。</span></p></div>
          {error && <div className="form-error" role="alert">{error}</div>}
        </div>
        <div className="modal-actions"><button type="button" className="button button-secondary" disabled={uploading} onClick={onClose}>取消</button><button type="button" className="button button-primary" disabled={uploading} onClick={() => void submit()}>{uploading ? <InlineSpinner label="正在上传" /> : <><UploadCloud size={17} /> 上传素材</>}</button></div>
      </section>
    </div>
  )
}

function AssetDetail({ asset, onClose, onUpdated, onDeleted }: { asset: Asset; onClose: () => void; onUpdated: (asset: Asset) => void; onDeleted: (assetId: string) => void }) {
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(asset.name)
  const [tags, setTags] = useState(asset.tags.join('，'))
  const [keywords, setKeywords] = useState(asset.keywords.join('，'))
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [retagging, setRetagging] = useState(false)
  const toast = useToast()
  const busy = saving || deleting || retagging
  const taggingActive = isTaggingActive(asset)
  const tagging = taggingPresentation(asset)
  const onUpdatedRef = useRef(onUpdated)
  const detailReadAbort = useRef<AbortController | null>(null)
  onUpdatedRef.current = onUpdated
  const dialogRef = useDialogFocus(onClose, !busy)

  useEffect(() => {
    if (editing) return
    setName(asset.name)
    setTags(asset.tags.join('，'))
    setKeywords(asset.keywords.join('，'))
  }, [asset, editing])

  useEffect(() => {
    if (taggingActive) setEditing(false)
  }, [taggingActive])

  useEffect(() => {
    const controller = new AbortController()
    detailReadAbort.current = controller
    void api.asset(asset.id, { signal: controller.signal })
      .then((latest) => {
        if (!controller.signal.aborted) onUpdatedRef.current(latest)
      })
      .catch(() => undefined)
    return () => {
      controller.abort()
      if (detailReadAbort.current === controller) detailReadAbort.current = null
    }
  }, [asset.id])

  useEffect(() => {
    if (taggingActive) return
    const refresh = () => {
      if (document.visibilityState === 'hidden') return
      detailReadAbort.current?.abort()
      const controller = new AbortController()
      detailReadAbort.current = controller
      void api.asset(asset.id, { signal: controller.signal })
        .then((latest) => {
          if (!controller.signal.aborted) onUpdatedRef.current(latest)
        })
        .catch(() => undefined)
    }
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      window.removeEventListener('focus', refresh)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [asset.id, taggingActive])

  useEffect(() => {
    if (!taggingActive) return
    let stopped = false
    let timer: number | undefined
    let controller: AbortController | null = null
    let failureCount = 0

    const schedule = (delay: number) => {
      timer = window.setTimeout(() => { void poll() }, delay)
    }
    const poll = async () => {
      controller = new AbortController()
      try {
        const latest = await api.asset(asset.id, { signal: controller.signal })
        if (stopped || controller.signal.aborted) return
        failureCount = 0
        onUpdatedRef.current(latest)
        if (isTaggingActive(latest)) schedule(TAGGING_POLL_INTERVAL)
      } catch (err) {
        if (stopped || isAbortError(err)) return
        failureCount += 1
        schedule(Math.min(TAGGING_POLL_INTERVAL * (2 ** failureCount), TAGGING_POLL_MAX_INTERVAL))
      }
    }

    schedule(TAGGING_POLL_INTERVAL)
    return () => {
      stopped = true
      if (timer != null) window.clearTimeout(timer)
      controller?.abort()
    }
  }, [asset.id, taggingActive])

  const save = async () => {
    setSaving(true)
    try {
      const updated = await api.updateAsset(asset.id, { name: name.trim(), tags: split(tags), keywords: split(keywords) })
      onUpdated(updated)
      setEditing(false)
      toast('素材信息已保存', 'success')
    } catch (err) {
      toast(errorMessage(err), 'error')
    } finally { setSaving(false) }
  }

  const remove = async () => {
    if (asset.is_seed) return
    if (!window.confirm(`确定删除「${asset.name}」？删除后将从素材库和服务器文件中移除，无法恢复。`)) return
    setDeleting(true)
    try {
      await api.deleteAsset(asset.id)
      onDeleted(asset.id)
      toast('素材已删除', 'success')
    } catch (err) {
      toast(errorMessage(err), 'error')
    } finally {
      setDeleting(false)
    }
  }

  const retag = async () => {
    if (taggingActive || editing) return
    if (!window.confirm('AI 重新生成会替换当前的主题标签和画面关键词。启用视觉服务时，一张归一化画面会发送至第三方模型网关。确定继续吗？')) return
    detailReadAbort.current?.abort()
    setRetagging(true)
    try {
      const updated = await api.retagAsset(asset.id)
      onUpdated(updated)
      toast('已加入后台画面识别队列', 'success')
    } catch (err) {
      toast(errorMessage(err), 'error')
    } finally {
      setRetagging(false)
    }
  }

  return (
    <div className="drawer-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose() }}>
      <aside ref={dialogRef} className="asset-drawer" role="dialog" aria-modal="true" aria-label="素材详情" tabIndex={-1}>
        <div className="drawer-head"><div><span className="eyebrow">素材详情</span><h2>{asset.name}</h2></div><button type="button" className="icon-button" aria-label="关闭素材详情" disabled={busy} onClick={onClose}><X size={19} /></button></div>
        <div className="drawer-preview"><AssetVisual asset={asset} contain controls={asset.kind === 'video'} /></div>
        <div className="drawer-body">
          <section className={`asset-tagging-panel ${tagging.tone}`} aria-live="polite">
            <div className="asset-tagging-status"><span><Sparkles size={16} className={taggingActive ? 'spin' : ''} /></span><div><strong>{tagging.label}</strong><p>{tagging.description}</p>{asset.tagging_finished_at && !taggingActive && <small>最近完成：{formatDate(asset.tagging_finished_at)}</small>}</div></div>
            <button type="button" className="button button-secondary asset-retag-button" disabled={busy || taggingActive || editing} onClick={() => void retag()}>{retagging ? <InlineSpinner label="正在提交" /> : taggingActive ? <InlineSpinner label={asset.tagging_status === 'queued' ? '等待识别' : '正在识别'} /> : <><Sparkles size={15} /> AI 重新生成标签</>}</button>
            <p className="asset-tagging-privacy"><ShieldAlert size={13} /> 启用视觉服务时会向第三方模型网关发送一张画面；敏感素材请谨慎使用。</p>
          </section>
          {editing ? <>
            <div className="form-field"><label htmlFor="asset-detail-name">素材名称</label><input id="asset-detail-name" value={name} onChange={(event) => setName(event.target.value)} /></div>
            <div className="form-field"><label htmlFor="asset-detail-tags">主题标签</label><input id="asset-detail-tags" value={tags} onChange={(event) => setTags(event.target.value)} /></div>
            <div className="form-field"><label htmlFor="asset-detail-keywords">关键词</label><textarea id="asset-detail-keywords" value={keywords} onChange={(event) => setKeywords(event.target.value)} /></div>
          </> : <>
            <div className="detail-row"><span>类型</span><strong>{asset.kind === 'video' ? '视频' : '图片'}</strong></div>
            <div className="detail-row"><span>尺寸</span><strong>{asset.width && asset.height ? `${asset.width} × ${asset.height}` : '—'}</strong></div>
            <div className="detail-group"><span>主题标签</span><div className="tag-row">{asset.tags.map((tag) => <span key={tag}>{tag}</span>)}{asset.tags.length === 0 && <span className="muted-tag">暂未添加</span>}</div></div>
            <div className="detail-group"><span>画面关键词</span><p>{asset.keywords.join('、') || '暂未添加'}</p></div>
          </>}
        </div>
        <div className="drawer-actions">{editing ? <><button type="button" className="button button-secondary" disabled={saving} onClick={() => setEditing(false)}>取消</button><button type="button" className="button button-primary" disabled={saving || taggingActive} onClick={() => void save()}>{saving ? <InlineSpinner label="保存中" /> : <><Check size={16} /> 保存更改</>}</button></> : <>{!asset.is_seed && <button type="button" className="button button-danger" disabled={busy || taggingActive} onClick={() => void remove()}>{deleting ? <InlineSpinner label="删除中" /> : <><Trash2 size={15} /> 删除素材</>}</button>}<button type="button" className="button button-secondary" disabled={busy || taggingActive} onClick={() => setEditing(true)}>编辑名称与标签</button></>}</div>
      </aside>
    </div>
  )
}

const split = (value: string) => value.split(/[，,]/).map((item) => item.trim()).filter(Boolean)

function LocalAssetPreview({ file }: { file: File }) {
  const url = useMemo(() => URL.createObjectURL(file), [file])
  useEffect(() => () => URL.revokeObjectURL(url), [url])
  if (IMAGE_FILE_PATTERN.test(file.name)) return <img src={url} alt="待上传素材预览" />
  return <video src={url} muted playsInline preload="metadata" aria-label="待上传视频预览" />
}
