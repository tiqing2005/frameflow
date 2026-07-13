import { useState } from 'react'
import {
  Beaker,
  Bot,
  ChevronLeft,
  FolderKanban,
  Image,
  Menu,
  Plus,
  Sparkles,
  X,
} from 'lucide-react'
import './App.css'
import { ToastProvider } from './components/ui'
import { AppLink, navigate, useRoute, type Route } from './router'
import { DashboardPage } from './pages/DashboardPage'
import { NewProjectPage } from './pages/NewProjectPage'
import { ProcessingPage } from './pages/ProcessingPage'
import { WorkbenchPage } from './pages/WorkbenchPage'
import { AssetsPage } from './pages/AssetsPage'
import { RunsPage } from './pages/RunsPage'
import { DemoPage } from './pages/DemoPage'

const navItems = [
  { label: '项目台', path: '/projects', icon: FolderKanban, routes: ['dashboard', 'new', 'processing', 'project'] },
  { label: '素材库', path: '/assets', icon: Image, routes: ['assets'] },
  { label: 'AI 运行记录', path: '/runs', icon: Bot, routes: ['runs'] },
  { label: '演示实验室', path: '/demo', icon: Beaker, routes: ['demo'] },
]

function PageContent({ route }: { route: Route }) {
  switch (route.name) {
    case 'dashboard': return <DashboardPage />
    case 'new': return <NewProjectPage />
    case 'processing': return <ProcessingPage projectId={route.projectId} initialJobId={route.jobId} />
    case 'project': return <WorkbenchPage projectId={route.projectId} />
    case 'assets': return <AssetsPage />
    case 'runs': return <RunsPage />
    case 'demo': return <DemoPage />
    default: return (
      <main className="page page-centered">
        <div className="not-found-code">404</div>
        <h1>这个页面不在时间轴上</h1>
        <p>地址可能已失效，返回项目台继续创作。</p>
        <button className="button button-primary" type="button" onClick={() => navigate('/projects')}>返回项目台</button>
      </main>
    )
  }
}

function AppShell() {
  const route = useRoute()
  const [menuOpen, setMenuOpen] = useState(false)
  const isWorkbench = route.name === 'project'
  return (
    <div className={`app-shell${isWorkbench ? ' app-shell-workbench' : ''}`}>
      <aside className={`sidebar${menuOpen ? ' is-open' : ''}`}>
        <div className="brand-row">
          <AppLink href="/projects" className="brand" onClick={() => setMenuOpen(false)}>
            <span className="brand-symbol"><Sparkles size={19} strokeWidth={2.4} /></span>
            <span><b>FrameFlow</b><em>AI</em></span>
          </AppLink>
          <button type="button" className="icon-button mobile-only" aria-label="关闭导航" onClick={() => setMenuOpen(false)}><X size={20} /></button>
        </div>
        <AppLink href="/projects/new" className="button button-primary sidebar-create" onClick={() => setMenuOpen(false)}>
          <Plus size={17} /> 新建匹配项目
        </AppLink>
        <nav className="main-nav" aria-label="主导航">
          <span className="nav-caption">工作空间</span>
          {navItems.map(({ label, path, icon: Icon, routes }) => (
            <AppLink key={path} href={path} onClick={() => setMenuOpen(false)} className={`nav-item${routes.includes(route.name) ? ' active' : ''}`}>
              <Icon size={18} /><span>{label}</span>
            </AppLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className="service-dot" />
          <div><strong>本地工作区</strong><small>数据实时持久化</small></div>
          <span className="version-chip">v1.0</span>
        </div>
      </aside>
      {menuOpen && <button className="sidebar-scrim" type="button" aria-label="关闭导航" onClick={() => setMenuOpen(false)} />}
      <section className="main-shell">
        <header className="mobile-header">
          <button type="button" className="icon-button" aria-label="打开导航" onClick={() => setMenuOpen(true)}><Menu size={21} /></button>
          <AppLink href="/projects" className="brand compact"><span className="brand-symbol"><Sparkles size={17} /></span><span><b>FrameFlow</b> <em>AI</em></span></AppLink>
          {route.name === 'project' ? <button className="icon-button" type="button" aria-label="返回项目台" onClick={() => navigate('/projects')}><ChevronLeft size={21} /></button> : <span className="header-spacer" />}
        </header>
        <PageContent route={route} />
      </section>
    </div>
  )
}

function App() {
  return <ToastProvider><AppShell /></ToastProvider>
}

export default App
