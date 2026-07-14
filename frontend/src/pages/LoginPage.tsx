import { useState, type FormEvent } from 'react'
import { AlertCircle, Clapperboard, Eye, EyeOff, LoaderCircle, LockKeyhole, ShieldCheck, UserRound } from 'lucide-react'
import { errorMessage } from '../api'
import { useAuth } from '../auth'

export function LoginPage() {
  const { session, error: sessionError, login, refresh } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!username.trim() || !password) return
    setSubmitting(true)
    setError(null)
    try {
      await login(username.trim(), password)
    } catch (reason) {
      setError(errorMessage(reason))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="login-page">
      <section className="login-story" aria-label="FrameFlow 产品介绍">
        <div className="login-brand"><span className="brand-symbol"><Clapperboard size={20} /></span><b>FrameFlow</b></div>
        <div className="login-story-copy">
          <span className="login-kicker">AI VIDEO WORKSPACE</span>
          <h1>让字幕、素材和成片<br />在一条时间线上协作。</h1>
          <p>从语义分段到素材匹配，再到可追溯的预览输出。每一步都可编辑、可恢复、可审查。</p>
        </div>
        <div className="login-security-note"><ShieldCheck size={17} /><span><b>安全会话</b><small>HttpOnly Cookie · CSRF 防护 · 12 小时自动失效</small></span></div>
      </section>
      <section className="login-panel">
        <form className="login-card" onSubmit={submit}>
          <div className="login-mobile-brand"><span className="brand-symbol"><Clapperboard size={18} /></span><b>FrameFlow</b></div>
          <span className="login-icon"><LockKeyhole size={22} /></span>
          <h2>登录工作空间</h2>
          <p>使用管理员账号继续进入 FrameFlow Studio。</p>
          {session && !session.configured && (
            <div className="login-alert" role="alert"><AlertCircle size={17} /><span><b>认证尚未配置</b><small>请先在服务端设置管理员用户名与密码哈希。</small></span></div>
          )}
          {(error || sessionError) && (
            <div className="login-alert" role="alert"><AlertCircle size={17} /><span><b>暂时无法登录</b><small>{error || sessionError}</small></span></div>
          )}
          <div className="login-field">
            <label htmlFor="login-username">用户名</label>
            <span className="login-input"><UserRound size={17} /><input id="login-username" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" placeholder="请输入管理员用户名" disabled={submitting || !session?.configured} autoFocus /></span>
          </div>
          <div className="login-field">
            <label htmlFor="login-password">密码</label>
            <span className="login-input"><LockKeyhole size={17} /><input id="login-password" type={showPassword ? 'text' : 'password'} value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" placeholder="请输入密码" disabled={submitting || !session?.configured} /><button type="button" aria-label={showPassword ? '隐藏密码' : '显示密码'} onClick={() => setShowPassword((value) => !value)}>{showPassword ? <EyeOff size={16} /> : <Eye size={16} />}</button></span>
          </div>
          <button className="button button-primary button-large login-submit" type="submit" disabled={submitting || !session?.configured || !username.trim() || !password}>
            {submitting ? <><LoaderCircle className="spin" size={17} />正在验证</> : '进入工作空间'}
          </button>
          {sessionError && <button className="button button-ghost login-retry" type="button" onClick={() => void refresh()}>重新连接服务</button>}
          <small className="login-footnote">凭据仅发送到当前 FrameFlow 服务，不会存储在浏览器。</small>
        </form>
      </section>
    </main>
  )
}
