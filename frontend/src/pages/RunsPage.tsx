import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  ChevronDown,
  Clock3,
  Cpu,
  Gauge,
  RefreshCw,
  Sparkles,
  Zap,
} from 'lucide-react'
import { api, errorMessage } from '../api'
import { EmptyState, ErrorState, formatDate, formatDuration, PageLoader, useToast } from '../components/ui'
import type { Run } from '../types'

export function RunsPage() {
  const [runs, setRuns] = useState<Run[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
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
      degraded: runs.filter((run) => run.degraded).length,
      average: latencies.length ? Math.round(latencies.reduce((sum, value) => sum + value, 0) / latencies.length) : 0,
      tokens: runs.reduce((sum, run) => sum + (run.prompt_tokens || 0) + (run.completion_tokens || 0), 0),
    }
  }, [runs])

  if (loading && runs.length === 0) return <main className="page"><PageLoader label="加载 AI 运行记录" /></main>
  return (
    <main className="page runs-page">
      <div className="page-heading-row compact-heading">
        <div><span className="eyebrow"><Bot size={14} /> 可观测 AI 管线</span><h1>AI 运行记录</h1><p>查看每次模型调用、规则降级、耗时与错误，确保处理过程可追踪。</p></div>
        <button type="button" className="button button-secondary heading-action" disabled={loading} onClick={() => void load(true)}><RefreshCw size={16} className={loading ? 'spin' : ''} /> 刷新</button>
      </div>
      {error && <ErrorState message={error} onRetry={() => void load()} compact />}
      <section className="metric-grid run-metrics">
        <div className="metric-card"><div className="metric-icon green"><CheckCircle2 size={20} /></div><div><span>成功调用</span><strong>{summary.successful}</strong><small>共 {runs.length} 条运行</small></div></div>
        <div className="metric-card"><div className="metric-icon blue"><Gauge size={20} /></div><div><span>平均耗时</span><strong>{summary.average ? `${(summary.average / 1000).toFixed(1)}s` : '—'}</strong><small>端到端模型延迟</small></div></div>
        <div className="metric-card"><div className="metric-icon purple"><Zap size={20} /></div><div><span>Token 用量</span><strong>{summary.tokens.toLocaleString()}</strong><small>输入与输出合计</small></div></div>
        <div className="metric-card"><div className="metric-icon amber"><AlertTriangle size={20} /></div><div><span>规则降级</span><strong>{summary.degraded}</strong><small>结果仍可确定性复现</small></div></div>
      </section>
      <section className="section-block">
        <div className="section-heading"><div><h2>调用明细</h2><p>最近的记录显示在最前</p></div><span className="result-count">{runs.length} 条</span></div>
        {runs.length === 0 ? <EmptyState icon={<Cpu size={26} />} title="还没有 AI 运行记录" description="创建一个项目后，模型调用或规则降级会记录在这里。" /> : (
          <div className="run-list">
            {runs.map((run) => {
              const isSuccess = run.status === 'success' || run.status === 'succeeded'
              const open = expanded === run.id
              return <article className={`run-row${open ? ' expanded' : ''}`} key={run.id}>
                <button type="button" className="run-summary" onClick={() => setExpanded(open ? null : run.id)}>
                  <span className={`run-status-icon ${run.degraded ? 'degraded' : isSuccess ? 'success' : 'failed'}`}>{run.degraded ? <AlertTriangle size={17} /> : isSuccess ? <CheckCircle2 size={17} /> : <AlertTriangle size={17} />}</span>
                  <span className="run-operation"><strong>{run.operation || '语义理解与标签生成'}</strong><small>{run.project_title || (run.project_id ? `项目 ${run.project_id.slice(0, 8)}` : '系统任务')}</small></span>
                  <span className="model-chip"><Sparkles size={13} /> {run.degraded ? 'deterministic-rules' : run.model || run.provider || 'AI model'}</span>
                  <span className="run-duration"><Clock3 size={14} /> {formatDuration(run.latency_ms)}</span>
                  <span className="run-time">{formatDate(run.created_at)}</span>
                  <ChevronDown size={16} className={open ? 'rotated' : ''} />
                </button>
                {open && <div className="run-detail">
                  <div><span>运行 ID</span><code>{run.id}</code></div><div><span>状态</span><strong>{run.degraded ? '规则降级成功' : isSuccess ? '调用成功' : run.status || '未知'}</strong></div><div><span>模型 / 提供方</span><strong>{run.model || '—'} / {run.provider || '—'}</strong></div><div><span>Token</span><strong>{(run.prompt_tokens || 0).toLocaleString()} 输入 · {(run.completion_tokens || 0).toLocaleString()} 输出</strong></div>
                  {(run.error_message || run.degraded) && <p className="run-message"><AlertTriangle size={15} /> {run.error_message || '模型调用不可用，本次使用确定性规则完成，所有结果仍已正常持久化。'}</p>}
                </div>}
              </article>
            })}
          </div>
        )}
      </section>
    </main>
  )
}
