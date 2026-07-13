import { useCallback, useEffect, useState } from 'react'
import {
  ArrowRight,
  AudioLines,
  CircleAlert,
  CircleHelp,
  Clock3,
  FileText,
  FilePlus2,
  FolderKanban,
  Image,
  MoreHorizontal,
  Plus,
  Trash2,
} from 'lucide-react'
import { api, errorMessage } from '../api'
import { EmptyState, ErrorState, formatDate, PageLoader, StatusPill, useToast } from '../components/ui'
import { AppLink, navigate } from '../router'
import type { Dashboard, Project } from '../types'

export function DashboardPage() {
  const [data, setData] = useState<Dashboard | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [deleting, setDeleting] = useState<string | null>(null)
  const [menuFor, setMenuFor] = useState<string | null>(null)
  const toast = useToast()

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const dashboard = await api.dashboard()
      setData({
        metrics: dashboard.metrics || { projects: 0, total_assets: 0, running_jobs: 0, failed_jobs: 0 },
        recent_projects: dashboard.recent_projects || [],
        recent_runs: dashboard.recent_runs || [],
      })
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const openProject = (project: Project) => {
    navigate(project.status === 'ready' ? `/projects/${project.id}` : `/projects/${project.id}/processing`)
  }

  const remove = async (project: Project) => {
    if (!window.confirm(`确定删除「${project.title}」？项目、分段和选择记录都会被移除。`)) return
    setDeleting(project.id)
    setMenuFor(null)
    try {
      await api.deleteProject(project.id)
      toast('项目已删除', 'success')
      await load()
    } catch (err) {
      toast(errorMessage(err), 'error')
    } finally {
      setDeleting(null)
    }
  }

  if (loading) return <main className="page"><PageLoader /></main>

  return (
    <main className="page dashboard-page">
      <div className="page-heading-row">
        <div>
          <h1>项目</h1>
          <p>管理脚本、内容片段和素材匹配进度。</p>
        </div>
        <AppLink href="/projects/new" className="button button-primary heading-action"><Plus size={18} /> 新建项目</AppLink>
      </div>

      {error && <ErrorState message={error} onRetry={() => void load()} />}

      <section className="metric-grid" aria-label="工作区概览">
        <div className="metric-card metric-featured">
          <div className="metric-icon"><FolderKanban size={20} /></div>
          <div><span>全部项目</span><strong>{data?.metrics.projects ?? 0}</strong></div>
        </div>
        <div className="metric-card">
          <div className="metric-icon purple"><Image size={20} /></div>
          <div><span>素材</span><strong>{data?.metrics.total_assets ?? 0}</strong></div>
        </div>
        <div className="metric-card">
          <div className="metric-icon amber"><Clock3 size={20} /></div>
          <div><span>处理中</span><strong>{data?.metrics.running_jobs ?? 0}</strong></div>
        </div>
        <div className="metric-card">
          <div className="metric-icon red"><CircleAlert size={20} /></div>
          <div><span>待处理</span><strong>{data?.metrics.failed_jobs ?? 0}</strong></div>
        </div>
      </section>

      <section className="section-block">
        <div className="section-heading">
          <div><h2>最近项目</h2><p>继续上一次编辑，所有选择都会自动保存</p></div>
          {(data?.recent_projects.length ?? 0) > 0 && <span className="result-count">最近 {data?.recent_projects.length} 个</span>}
        </div>
        {!error && data?.recent_projects.length === 0 ? (
          <EmptyState
            icon={<FilePlus2 size={26} />}
            title="还没有项目"
            description="导入文案或音视频，开始拆分内容片段并匹配素材。"
            action={<AppLink href="/projects/new" className="button button-primary"><Plus size={17} /> 创建首个项目</AppLink>}
          />
        ) : (
          <div className="project-table-wrap">
            <table className="project-table">
              <thead><tr><th>项目</th><th>输入</th><th>状态</th><th>最后更新</th><th><span className="sr-only">操作</span></th></tr></thead>
              <tbody>
                {data?.recent_projects.map((project) => (
                  <tr key={project.id}>
                    <td>
                      <button className="project-name" type="button" onClick={() => openProject(project)}>
                        <span className={`file-kind kind-${project.input_kind || project.input_type || 'text'}`}>
                          {(project.input_kind || project.input_type) === 'text' ? <FileText size={18} /> : <AudioLines size={18} />}
                        </span>
                        <span><strong>{project.title}</strong><small>{project.segment_count ? `${project.segment_count} 个片段` : `ID · ${project.id.slice(0, 8)}`}</small></span>
                      </button>
                    </td>
                    <td><span className="input-kind">{project.input_kind === 'video' ? '视频' : project.input_kind === 'audio' ? '音频' : '文本'}</span></td>
                    <td><StatusPill status={project.status} pulse={project.status === 'processing'} /></td>
                    <td><span className="date-cell">{formatDate(project.updated_at)}</span></td>
                    <td className="row-actions">
                      <button className="button button-ghost button-small open-project" type="button" onClick={() => openProject(project)}>打开 <ArrowRight size={15} /></button>
                      <div className="popover-anchor">
                        <button className="icon-button small" type="button" aria-label="更多操作" aria-expanded={menuFor === project.id} onClick={() => setMenuFor(menuFor === project.id ? null : project.id)}><MoreHorizontal size={18} /></button>
                        {menuFor === project.id && (
                          <div className="action-popover">
                            <button type="button" className="danger-action" disabled={deleting === project.id} onClick={() => void remove(project)}><Trash2 size={15} /> {deleting === project.id ? '删除中…' : '删除项目'}</button>
                          </div>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="workflow-callout">
        <div className="callout-icon"><CircleHelp size={22} /></div>
        <div><strong>匹配规则</strong><p>候选会展示命中词、匹配依据和补位标识，方便人工判断。</p></div>
        <AppLink href="/demo" className="text-link">查看演示工具 <ArrowRight size={15} /></AppLink>
      </section>
    </main>
  )
}
