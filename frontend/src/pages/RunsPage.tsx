import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  ChevronDown,
  CircleX,
  Clock3,
  Cpu,
  Gauge,
  RefreshCw,
  Sparkles,
  Zap,
} from 'lucide-react'
import { api, errorMessage } from '../api'
import { EmptyState, ErrorState, formatDate, formatDuration, PageLoader, useToast } from '../components/ui'
import { formatRunTokenUsage, totalRunTokens } from '../runTokens'
import type { Run } from '../types'

const operationLabels: Record<string, string> = {
  speech_transcription: '音视频语音识别',
  semantic_segmentation: '字幕语义增强',
  asset_tagging: '素材智能标注',
  asset_matching: '素材智能匹配',
  segment_rematch: '片段重新匹配',
  preview_render: '预览视频渲染',
  preview_failure: '预览渲染失败',
  image_generation: '文生图素材生成',
  pipeline_failure: '内容处理失败',
}

function operationLabel(operation?: string) {
  return operation ? operationLabels[operation] || operation : '语义理解与标签生成'
}

function providerLabel(run: Run) {
  const provider = (run.provider || '').toLowerCase()
  const model = (run.model || '').toLowerCase()
  if (provider.includes('dashscope')) return '阿里云百炼 DashScope'
  if (provider.includes('gemini') || model.includes('gemini')) return 'Google Gemini'
  if (provider.includes('deepseek') || model.includes('deepseek')) return 'DeepSeek'
  if (provider.includes('image') || provider.includes('lanxiu')) return '图像生成 API'
  if (provider === 'openai-compatible') return 'OpenAI-compatible API'
  if (provider === 'rules') return '确定性规则'
  if (provider === 'ffmpeg') return 'FFmpeg'
  if (provider === 'worker') return '任务 Worker'
  if (provider === 'hybrid-fallback') return '混合排序降级'
  return run.provider || ''
}

function modelTraceLabel(run: Run) {
  const provider = providerLabel(run)
  if (run.degraded) return provider ? `${provider} · 降级完成` : '降级完成'
  if (provider && run.model && provider !== run.model) return `${provider} · ${run.model}`
  return run.model || provider || 'AI model'
}

export function RunsPage() {
  const [runs, setRuns] = useState<Run[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<'all' | 'failed'>('all')
  const toast = useToast()

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true)
    try {
      const result = await api.runs()
      setRuns(result.items)
      setError('')
      if (quiet) toast('运行记录已刷新', 'success')
    } catch (err) { setError(errorMessage(err)) } finally { setLoading(false) }
  }, [toast])
  useEffect(() => { void load() }, [load])

  const summary = useMemo(() => {
    const successful = runs.filter((run) => run.status === 'succeeded' || run.status === 'success').length
    const latencies = runs.map((run) => run.latency_ms).filter((value): value is number => value != null)
    return {
      successful,
      failed: runs.filter((run) => run.status?.toLowerCase() === 'failed').length,
      degraded: runs.filter((run) => run.degraded).length,
      average: latencies.length ? Math.round(latencies.reduce((sum, value) => sum + value, 0) / latencies.length) : 0,
      tokens: totalRunTokens(runs),
    }
  }, [runs])
  const failureFilterActive = statusFilter === 'failed' && summary.failed > 0
  const visibleRuns = failureFilterActive
    ? runs.filter((run) => run.status?.toLowerCase() === 'failed')
    : runs

  if (loading && runs.length === 0) return <main className="page"><PageLoader label="加载 AI 运行记录" /></main>
  return (
    <main className="page runs-page">
      <div className="page-heading-row compact-heading">
        <div><span className="eyebrow"><Bot size={14} /> 可观测 AI 管线</span><h1>AI 运行记录</h1><p>查看每次模型调用、降级处理、耗时与错误，确保处理过程可追踪。</p></div>
        <button type="button" className="button button-secondary heading-action" disabled={loading} onClick={() => void load(true)}><RefreshCw size={16} className={loading ? 'spin' : ''} /> 刷新</button>
      </div>
      {error && <ErrorState message={error} onRetry={() => void load()} compact />}
      <section className="metric-grid run-metrics">
        <div className="metric-card"><div className="metric-icon green"><CheckCircle2 size={20} /></div><div><span>成功调用</span><strong>{summary.successful}</strong><small>共 {runs.length} 条运行</small></div></div>
        <div className="metric-card"><div className="metric-icon blue"><Gauge size={20} /></div><div><span>平均耗时</span><strong>{summary.average ? `${(summary.average / 1000).toFixed(1)}s` : '—'}</strong><small>端到端模型延迟</small></div></div>
        <div className="metric-card"><div className="metric-icon purple"><Zap size={20} /></div><div><span>Token 用量</span><strong>{summary.tokens == null ? '—' : summary.tokens.toLocaleString()}</strong><small>输入与输出合计</small></div></div>
        <div className="metric-card"><div className="metric-icon amber"><AlertTriangle size={20} /></div><div><span>降级完成</span><strong>{summary.degraded}</strong><small>首选能力不可用时的可用结果</small></div></div>
        <button
          type="button"
          className="metric-card run-metric-filter"
          aria-pressed={failureFilterActive}
          disabled={summary.failed === 0}
          title={summary.failed === 0 ? '当前没有失败调用' : failureFilterActive ? '显示全部运行记录' : '只查看失败调用'}
          onClick={() => {
            setExpanded(null)
            setStatusFilter(failureFilterActive ? 'all' : 'failed')
          }}
        >
          <div className="metric-icon red"><CircleX size={20} /></div><div><span>失败调用</span><strong>{summary.failed}</strong><small>需要检查的模型或任务错误</small></div>
        </button>
      </section>
      <section className="section-block">
        <div className="section-heading"><div><h2>调用明细</h2><p>{failureFilterActive ? '仅显示需要检查的失败记录' : '最近的记录显示在最前'}</p></div><span className="result-count">{failureFilterActive ? `${visibleRuns.length} 条失败` : `${runs.length} 条`}</span></div>
        {runs.length === 0 ? <EmptyState icon={<Cpu size={26} />} title="还没有 AI 运行记录" description="创建一个项目后，模型调用或降级处理会记录在这里。" /> : (
          <div className="run-list">
            {visibleRuns.map((run) => {
              const isSuccess = run.status === 'success' || run.status === 'succeeded'
              const open = expanded === run.id
              const generatedImages = run.operation === 'image_generation' ? (run.image_count ?? (isSuccess ? 1 : null)) : null
              return <article className={`run-row${open ? ' expanded' : ''}`} key={run.id}>
                <button type="button" className="run-summary" onClick={() => setExpanded(open ? null : run.id)}>
                  <span className={`run-status-icon ${run.degraded ? 'degraded' : isSuccess ? 'success' : 'failed'}`}>{run.degraded ? <AlertTriangle size={17} /> : isSuccess ? <CheckCircle2 size={17} /> : <AlertTriangle size={17} />}</span>
                  <span className="run-operation"><strong>{operationLabel(run.operation)}</strong><small>{run.project_title || (run.project_id ? `项目 ${run.project_id.slice(0, 8)}` : '系统任务')}</small></span>
                  <span className="model-chip"><Sparkles size={13} /> {modelTraceLabel(run)}</span>
                  <span className="run-duration"><Clock3 size={14} /> {formatDuration(run.latency_ms)}</span>
                  <span className="run-time">{formatDate(run.created_at)}</span>
                  <ChevronDown size={16} className={open ? 'rotated' : ''} />
                </button>
                {open && <div className="run-detail">
                  <div><span>运行 ID</span><code>{run.id}</code></div><div><span>状态</span><strong>{run.degraded ? '降级完成' : isSuccess ? '调用成功' : run.status || '未知'}</strong></div><div><span>模型 / 提供方</span><strong>{run.model || '—'} / {providerLabel(run) || '—'}</strong></div><div><span>{run.operation === 'image_generation' ? '生成数量' : 'Token'}</span><strong>{run.operation === 'image_generation' ? (generatedImages == null ? '—' : `${generatedImages} 张`) : formatRunTokenUsage(run)}</strong></div>
                  {(run.error_message || run.degraded) && <p className="run-message"><AlertTriangle size={15} /> {run.error_message || '首选模型能力暂不可用，本次已通过备用能力完成，结果已正常持久化。'}</p>}
                </div>}
              </article>
            })}
          </div>
        )}
      </section>
    </main>
  )
}
