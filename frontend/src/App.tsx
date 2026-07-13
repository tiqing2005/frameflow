import { useState } from 'react'
import {
  Beaker,
  Bot,
  ChevronLeft,
  Clapperboard,
  FolderKanban,
  Image,
  Menu,
  Plus,
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
  { label: '项目', path: '/projects', icon: FolderKanban, routes: ['dashboard', 'new', 'processing', 'project'] },
  { label: '素材库', path: '/assets', icon: Image, routes: ['assets'] },
  { label: '运行记录', path: '/runs', icon: Bot, routes: ['runs'] },
]

function PageContent({ route }: { route: Route }) {
  switch (route.name) {
    case 'dashboard': return <DashboardPage />
    case 'new': return <NewProjectPage />
    case 'processing': return <ProcessingPage key={`${route.projectId}:${route.jobId || ''}`} projectId={route.projectId} initialJobId={route.jobId} />
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
            <span className="brand-symbol"><Clapperboard size={19} strokeWidth={2.2} /></span>
            <span><b>FrameFlow</b></span>
          </AppLink>
          <button type="button" className="icon-button mobile-only" aria-label="关闭导航" onClick={() => setMenuOpen(false)}><X size={20} /></button>
        </div>
        <AppLink href="/projects/new" className="button button-primary sidebar-create" onClick={() => setMenuOpen(false)}>
          <Plus size={17} /> 新建项目
        </AppLink>
        <nav className="main-nav" aria-label="主导航">
          <span className="nav-caption">工作空间</span>
          {navItems.map(({ label, path, icon: Icon, routes }) => (
            <AppLink key={path} href={path} onClick={() => setMenuOpen(false)} className={`nav-item${routes.includes(route.name) ? ' active' : ''}`}>
              <Icon size={18} /><span>{label}</span>
            </AppLink>
          ))}
        </nav>
        <nav className="main-nav utility-nav" aria-label="系统工具">
          <span className="nav-caption">系统</span>
          <AppLink href="/demo" onClick={() => setMenuOpen(false)} className={`nav-item${route.name === 'demo' ? ' active' : ''}`}>
            <Beaker size={18} /><span>演示工具</span>
          </AppLink>
        </nav>
        <div className="sidebar-foot">
          <span className="service-dot" />
          <div><strong>FrameFlow Studio</strong><small>内容与素材工作台</small></div>
          <span className="version-chip">v1.0</span>
        </div>
      </aside>
      {menuOpen && <button className="sidebar-scrim" type="button" aria-label="关闭导航" onClick={() => setMenuOpen(false)} />}
      <section className="main-shell">
        <header className="mobile-header">
          <button type="button" className="icon-button" aria-label="打开导航" onClick={() => setMenuOpen(true)}><Menu size={21} /></button>
          <AppLink href="/projects" className="brand compact"><span className="brand-symbol"><Clapperboard size={17} /></span><span><b>FrameFlow</b></span></AppLink>
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
