import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from 'react'
import {
  Check,
  Filter,
  FolderOpen,
  Image as ImageIcon,
  Play,
  Plus,
  Search,
  Tags,
  UploadCloud,
  X,
} from 'lucide-react'
import { api, errorMessage } from '../api'
import { AssetVisual, EmptyState, ErrorState, formatDate, InlineSpinner, PageLoader, useToast } from '../components/ui'
import type { Asset } from '../types'

const ASSET_FILE_PATTERN = /\.(png|jpe?g|webp|gif|mp4|webm|mov)$/i
const IMAGE_FILE_PATTERN = /\.(png|jpe?g|webp|gif)$/i
const ASSET_FILE_ACCEPT = '.png,.jpg,.jpeg,.webp,.gif,.mp4,.webm,.mov'

export function AssetsPage() {
  const [assets, setAssets] = useState<Asset[]>([])
  const [total, setTotal] = useState(0)
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [uploadOpen, setUploadOpen] = useState(false)
  const [selected, setSelected] = useState<Asset | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const result = await api.assets({ q: query.trim() || undefined, kind: kind || undefined })
      setAssets(result.items)
      setTotal(result.total)
      setError('')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [kind, query])

  useEffect(() => {
    const timer = window.setTimeout(() => { void load() }, query ? 300 : 0)
    return () => window.clearTimeout(timer)
  }, [load, query])

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
              <div className="asset-card-visual"><AssetVisual asset={asset} />{asset.kind === 'video' && <span className="asset-play-indicator"><Play size={15} fill="currentColor" /></span>}<span className="asset-kind-chip">{asset.kind === 'video' ? '视频' : '图片'}</span></div>
              <div className="asset-card-body"><h3>{asset.name}</h3><div className="tag-row">{asset.tags.slice(0, 3).map((tag) => <span key={tag}>{tag}</span>)}{asset.tags.length === 0 && <span className="muted-tag">暂无标签</span>}</div><small>{asset.width && asset.height ? `${asset.width}×${asset.height}` : '本地素材'} · {formatDate(asset.created_at, false)}</small></div>
            </button>
          ))}
        </section>
      )}

      {uploadOpen && <UploadAssetModal onClose={() => setUploadOpen(false)} onUploaded={() => { setUploadOpen(false); void load() }} />}
      {selected && <AssetDetail asset={selected} onClose={() => setSelected(null)} onUpdated={(asset) => { setAssets((items) => items.map((item) => item.id === asset.id ? asset : item)); setSelected(asset) }} />}
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
      await api.uploadAsset(file, name.trim(), split(tags), split(keywords))
      toast('素材已上传并加入匹配池', 'success')
      onUploaded()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setUploading(false)
    }
  }

  return (
    <div className="modal-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !uploading) onClose() }}>
      <section className="modal-card" role="dialog" aria-modal="true" aria-labelledby="upload-title">
        <div className="modal-head"><div><span className="modal-icon"><UploadCloud size={20} /></span><div><h2 id="upload-title">上传新素材</h2><p>标签越准确，匹配结果越相关</p></div></div><button className="icon-button" type="button" aria-label="关闭" disabled={uploading} onClick={onClose}><X size={19} /></button></div>
        <div className="modal-body">
          {!file ? <button type="button" className={`drop-zone asset-drop${dragging ? ' dragging' : ''}`} onClick={() => input.current?.click()} onDragEnter={(event) => { event.preventDefault(); setDragging(true) }} onDragOver={(event) => event.preventDefault()} onDragLeave={() => setDragging(false)} onDrop={drop}><span className="upload-icon"><ImageIcon size={25} /></span><strong>拖放图片或视频，或点击选择</strong><span>PNG、JPG/JPEG、WebP、GIF、MP4、WebM、MOV（不支持 SVG）· 最大 100 MB</span></button> : <div className="upload-preview"><div className="upload-local-preview"><LocalAssetPreview file={file} /></div><div><strong>{file.name}</strong><span>{(file.size / 1024 / 1024).toFixed(2)} MB</span></div><button type="button" className="icon-button" onClick={() => setFile(null)}><X size={17} /></button></div>}
          <input ref={input} hidden type="file" accept={ASSET_FILE_ACCEPT} onChange={(event) => choose(event.target.files?.[0])} />
          <div className="form-field"><label htmlFor="asset-name">素材名称 <span>必填</span></label><input id="asset-name" value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：城市夜间交通" maxLength={80} /></div>
          <div className="form-field"><label htmlFor="asset-tags">主题标签 <span>逗号分隔</span></label><div className="input-with-icon"><Tags size={16} /><input id="asset-tags" value={tags} onChange={(event) => setTags(event.target.value)} placeholder="城市，交通，夜景" /></div></div>
          <div className="form-field"><label htmlFor="asset-keywords">画面关键词 <span>逗号分隔</span></label><input id="asset-keywords" value={keywords} onChange={(event) => setKeywords(event.target.value)} placeholder="车流，灯光，通勤，现代化" /></div>
          {error && <div className="form-error" role="alert">{error}</div>}
        </div>
        <div className="modal-actions"><button type="button" className="button button-secondary" disabled={uploading} onClick={onClose}>取消</button><button type="button" className="button button-primary" disabled={uploading} onClick={() => void submit()}>{uploading ? <InlineSpinner label="正在上传" /> : <><UploadCloud size={17} /> 上传素材</>}</button></div>
      </section>
    </div>
  )
}

function AssetDetail({ asset, onClose, onUpdated }: { asset: Asset; onClose: () => void; onUpdated: (asset: Asset) => void }) {
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(asset.name)
  const [tags, setTags] = useState(asset.tags.join('，'))
  const [keywords, setKeywords] = useState(asset.keywords.join('，'))
  const [saving, setSaving] = useState(false)
  const toast = useToast()

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

  return (
    <div className="drawer-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <aside className="asset-drawer" role="dialog" aria-modal="true" aria-label="素材详情">
        <div className="drawer-head"><div><span className="eyebrow">素材详情</span><h2>{asset.name}</h2></div><button type="button" className="icon-button" onClick={onClose}><X size={19} /></button></div>
        <div className="drawer-preview"><AssetVisual asset={asset} contain controls={asset.kind === 'video'} /></div>
        <div className="drawer-body">
          {editing ? <>
            <div className="form-field"><label>素材名称</label><input value={name} onChange={(event) => setName(event.target.value)} /></div>
            <div className="form-field"><label>主题标签</label><input value={tags} onChange={(event) => setTags(event.target.value)} /></div>
            <div className="form-field"><label>关键词</label><textarea value={keywords} onChange={(event) => setKeywords(event.target.value)} /></div>
          </> : <>
            <div className="detail-row"><span>类型</span><strong>{asset.kind === 'video' ? '视频' : '图片'}</strong></div>
            <div className="detail-row"><span>尺寸</span><strong>{asset.width && asset.height ? `${asset.width} × ${asset.height}` : '—'}</strong></div>
            <div className="detail-group"><span>主题标签</span><div className="tag-row">{asset.tags.map((tag) => <span key={tag}>{tag}</span>)}</div></div>
            <div className="detail-group"><span>画面关键词</span><p>{asset.keywords.join('、') || '暂未添加'}</p></div>
          </>}
        </div>
        <div className="drawer-actions">{editing ? <><button type="button" className="button button-secondary" onClick={() => setEditing(false)}>取消</button><button type="button" className="button button-primary" disabled={saving} onClick={() => void save()}>{saving ? <InlineSpinner label="保存中" /> : <><Check size={16} /> 保存更改</>}</button></> : <button type="button" className="button button-secondary" onClick={() => setEditing(true)}>编辑名称与标签</button>}</div>
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
