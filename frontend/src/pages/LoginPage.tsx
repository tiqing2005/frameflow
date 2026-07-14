import { useState, type FormEvent } from 'react'
import { AlertCircle, Clapperboard, Eye, EyeOff, LoaderCircle, LockKeyhole, ShieldCheck, UserRound } from 'lucide-react'
import { errorMessage } from '../api'
import { useAuth } from '../auth'

export function LoginPage() {
  const { session, error: sessionError, login, setup, refresh } = useAuth()
  const [username, setUsername] = useState('admin')
  const [displayName, setDisplayName] = useState('FrameFlow 管理员')
  const [password, setPassword] = useState('')
  const [passwordConfirm, setPasswordConfirm] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const setupRequired = Boolean(session?.auth_enabled && !session.configured)
  const formEnabled = setupRequired ? Boolean(session?.setup_available) : Boolean(session?.configured)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!username.trim() || !password || !formEnabled) return
    if (setupRequired && password !== passwordConfirm) {
      setError('两次输入的密码不一致')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      if (setupRequired) await setup(username.trim(), displayName.trim(), password)
      else await login(username.trim(), password)
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
        <div className="login-security-note"><ShieldCheck size={17} /><span><b>{setupRequired ? '安全初始化' : '安全会话'}</b><small>{setupRequired ? '仅允许本机首次创建 · 密码只保存为哈希' : 'HttpOnly Cookie · CSRF 防护 · 12 小时自动失效'}</small></span></div>
      </section>
      <section className="login-panel">
        <form className="login-card" onSubmit={submit}>
          <div className="login-mobile-brand"><span className="brand-symbol"><Clapperboard size={18} /></span><b>FrameFlow</b></div>
          <span className="login-icon"><LockKeyhole size={22} /></span>
          <h2>{setupRequired ? '创建管理员账号' : '登录工作空间'}</h2>
          <p>{setupRequired ? '首次启动只需完成这一步，后续使用该账号安全登录。' : '使用管理员账号继续进入 FrameFlow Studio。'}</p>
          {setupRequired && !session?.setup_available && (
            <div className="login-alert" role="alert"><AlertCircle size={17} /><span><b>请在服务所在电脑完成初始化</b><small>首次管理员账号只能从本机创建，或由部署人员配置环境凭据。</small></span></div>
          )}
          {(error || sessionError) && (
            <div className="login-alert" role="alert"><AlertCircle size={17} /><span><b>暂时无法登录</b><small>{error || sessionError}</small></span></div>
          )}
          <div className="login-field">
            <label htmlFor="login-username">用户名</label>
            <span className="login-input"><UserRound size={17} /><input id="login-username" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" placeholder="请输入管理员用户名" disabled={submitting || !formEnabled} autoFocus /></span>
          </div>
          {setupRequired && (
            <div className="login-field">
              <label htmlFor="login-display-name">显示名称</label>
              <span className="login-input"><UserRound size={17} /><input id="login-display-name" value={displayName} onChange={(event) => setDisplayName(event.target.value)} autoComplete="name" placeholder="例如：FrameFlow 管理员" disabled={submitting || !formEnabled} /></span>
            </div>
          )}
          <div className="login-field">
            <label htmlFor="login-password">密码</label>
            <span className="login-input"><LockKeyhole size={17} /><input id="login-password" type={showPassword ? 'text' : 'password'} value={password} onChange={(event) => setPassword(event.target.value)} autoComplete={setupRequired ? 'new-password' : 'current-password'} placeholder={setupRequired ? '至少 10 位，组合字母、数字或符号' : '请输入密码'} disabled={submitting || !formEnabled} /><button type="button" aria-label={showPassword ? '隐藏密码' : '显示密码'} onClick={() => setShowPassword((value) => !value)}>{showPassword ? <EyeOff size={16} /> : <Eye size={16} />}</button></span>
          </div>
          {setupRequired && (
            <div className="login-field">
              <label htmlFor="login-password-confirm">确认密码</label>
              <span className="login-input"><LockKeyhole size={17} /><input id="login-password-confirm" type={showPassword ? 'text' : 'password'} value={passwordConfirm} onChange={(event) => setPasswordConfirm(event.target.value)} autoComplete="new-password" placeholder="再次输入密码" disabled={submitting || !formEnabled} /></span>
            </div>
          )}
          <button className="button button-primary button-large login-submit" type="submit" disabled={submitting || !formEnabled || !username.trim() || !password || (setupRequired && (!displayName.trim() || !passwordConfirm))}>
            {submitting ? <><LoaderCircle className="spin" size={17} />{setupRequired ? '正在创建' : '正在验证'}</> : setupRequired ? '创建并进入工作空间' : '进入工作空间'}
          </button>
          {sessionError && <button className="button button-ghost login-retry" type="button" onClick={() => void refresh()}>重新连接服务</button>}
          <small className="login-footnote">{setupRequired ? '密码经过 PBKDF2-SHA256 加盐哈希后保存，系统不会存储明文。' : '凭据仅发送到当前 FrameFlow 服务，不会存储在浏览器。'}</small>
        </form>
      </section>
    </main>
  )
}
