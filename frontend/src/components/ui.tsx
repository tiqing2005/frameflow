/* oxlint-disable react/only-export-components -- shared UI hooks and formatters intentionally live with the primitives they support */
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  CircleX,
  CloudOff,
  ImageIcon,
  LoaderCircle,
  X,
} from 'lucide-react'
import { assetFileUrl, assetPosterUrl } from '../api'
import type { Asset, JobStatus, ProjectStatus } from '../types'

type ToastTone = 'success' | 'error' | 'info'
interface ToastItem { id: number; message: string; tone: ToastTone }
type ToastContextValue = (message: string, tone?: ToastTone) => void

const ToastContext = createContext<ToastContextValue>(() => undefined)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const notify = useCallback((message: string, tone: ToastTone = 'info') => {
    const id = Date.now() + Math.random()
    setToasts((items) => [...items, { id, message, tone }])
    window.setTimeout(() => setToasts((items) => items.filter((item) => item.id !== id)), 3600)
  }, [])
  return (
    <ToastContext.Provider value={notify}>
      {children}
      <div className="toast-stack" role="region" aria-label="通知">
        {toasts.map((toast) => (
          <div className={`toast toast-${toast.tone}`} role="status" key={toast.id}>
            {toast.tone === 'success' ? <CheckCircle2 size={18} /> : toast.tone === 'error' ? <CircleX size={18} /> : <AlertCircle size={18} />}
            <span>{toast.message}</span>
            <button type="button" aria-label="关闭" onClick={() => setToasts((items) => items.filter((item) => item.id !== toast.id))}><X size={15} /></button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export const useToast = () => useContext(ToastContext)

const STATUS_LABELS: Record<string, string> = {
  queued: '等待处理',
  processing: '处理中',
  ready: '可编辑',
  failed: '处理失败',
  canceled: '已取消',
  running: '运行中',
  succeeded: '已完成',
}

export function StatusPill({ status, pulse }: { status: ProjectStatus | JobStatus | string; pulse?: boolean }) {
  return <span className={`status-pill status-${status}${pulse ? ' is-pulsing' : ''}`}><i />{STATUS_LABELS[status] || status}</span>
}

export function PageLoader({ label = '正在加载数据' }: { label?: string }) {
  return (
    <div className="page-loader" aria-busy="true">
      <div className="skeleton skeleton-title" />
      <div className="skeleton skeleton-line" />
      <div className="skeleton-grid">
        <div className="skeleton skeleton-card" />
        <div className="skeleton skeleton-card" />
        <div className="skeleton skeleton-card" />
      </div>
      <span className="sr-only">{label}</span>
    </div>
  )
}

export function InlineSpinner({ label = '处理中' }: { label?: string }) {
  return <span className="inline-spinner"><LoaderCircle size={16} className="spin" />{label}</span>
}

export function ErrorState({ message, onRetry, compact = false }: { message: string; onRetry?: () => void; compact?: boolean }) {
  return (
    <div className={`error-state${compact ? ' compact' : ''}`} role="alert">
      <div className="error-icon"><CloudOff size={compact ? 20 : 26} /></div>
      <div>
        <strong>数据暂时没能加载</strong>
        <p>{message}</p>
      </div>
      {onRetry && <button type="button" className="button button-secondary button-small" onClick={onRetry}>重新加载</button>}
    </div>
  )
}

export function EmptyState({ icon, title, description, action }: { icon?: ReactNode; title: string; description: string; action?: ReactNode }) {
  return (
    <div className="empty-state">
      <div className="empty-icon">{icon || <ImageIcon size={25} />}</div>
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  )
}

export function AssetVisual({ asset, className = '', alt, contain = false, controls = false, eager = false }: { asset?: Asset | null; className?: string; alt?: string; contain?: boolean; controls?: boolean; eager?: boolean }) {
  const [failed, setFailed] = useState(false)
  const [posterFailed, setPosterFailed] = useState(false)
  const url = asset?.kind === 'video' ? assetFileUrl(asset) : assetPosterUrl(asset) || assetFileUrl(asset)
  const poster = asset?.kind === 'video' ? assetPosterUrl(asset) : ''
  useEffect(() => {
    setFailed(false)
    setPosterFailed(false)
  }, [url, poster])
  if (url && !failed) {
    if (asset?.kind === 'video') {
      // Probe the poster independently: browsers do not fire a video error when
      // only the poster URL is 404. Once it fails, remove the poster and ask the
      // browser for metadata/first frame so cards do not remain black.
      const probe = poster && !controls ? <img className="asset-poster-probe" src={poster} alt="" aria-hidden="true" onError={() => setPosterFailed(true)} /> : null
      return <>{probe}<video className={`${className} asset-video`.trim()} src={url} poster={!posterFailed ? (poster || undefined) : undefined} aria-label={alt || asset.name || '视频素材预览'} controls={controls} muted={!controls} playsInline preload={controls || eager || !poster || posterFailed ? 'metadata' : 'none'} onError={() => setFailed(true)} style={contain ? { objectFit: 'contain' } : undefined} /></>
    }
    return <img className={className} src={url} alt={alt || asset?.name || '素材预览'} loading={eager ? 'eager' : 'lazy'} decoding="async" onError={() => setFailed(true)} style={contain ? { objectFit: 'contain' } : undefined} />
  }
  return (
    <div className={`asset-fallback ${className}`} role="img" aria-label={alt || asset?.name || '素材占位'}>
      <span className="fallback-mark">FF</span>
      <span>{asset?.name || '等待选择素材'}</span>
    </div>
  )
}

export function formatDate(value?: string | null, withTime = true) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', ...(withTime ? { hour: '2-digit', minute: '2-digit' } : {}),
  }).format(date)
}

export function formatDuration(ms?: number | null) {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  const minutes = Math.floor(ms / 60_000)
  return `${minutes}:${Math.floor((ms % 60_000) / 1000).toString().padStart(2, '0')}`
}

export function scorePercent(score?: number) {
  if (score == null || Number.isNaN(score)) return 0
  const normalized = score > 1 ? score : score * 100
  return Math.max(0, Math.min(100, Math.round(normalized)))
}
