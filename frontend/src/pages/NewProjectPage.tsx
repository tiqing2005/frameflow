import { useMemo, useRef, useState, type DragEvent } from 'react'
import {
  ArrowLeft,
  AudioLines,
  Check,
  FileAudio,
  FileText,
  FileVideo,
  Info,
  Sparkles,
  UploadCloud,
  X,
} from 'lucide-react'
import { api, errorMessage } from '../api'
import { InlineSpinner, useToast } from '../components/ui'
import { AppLink, navigate } from '../router'

type InputMode = 'text' | 'upload'

const EXAMPLE = '生成式人工智能正在重新定义我们的工作方式。它不只是提高效率的工具，更像是一位随时在线的创意伙伴。真正重要的是，我们如何把技术能力转化为对人的理解，让每一次创新都服务于更清晰、更美好的生活。'
const ACCEPTED = ['audio/mpeg', 'audio/wav', 'audio/x-m4a', 'audio/mp4', 'video/mp4', 'video/quicktime', 'video/webm']

export function NewProjectPage() {
  const [mode, setMode] = useState<InputMode>('text')
  const [title, setTitle] = useState('')
  const [text, setText] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [dragging, setDragging] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)
  const idempotencyKey = useRef(crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`)
  const toast = useToast()

  const validation = useMemo(() => {
    if (!title.trim()) return '请先填写项目名称'
    if (mode === 'text' && text.trim().length < 20) return '文案至少需要 20 个字符，才能形成有意义的分段'
    if (mode === 'upload' && !file) return '请选择一个音频或视频文件'
    return ''
  }, [file, mode, text, title])

  const chooseFile = (next?: File) => {
    if (!next) return
    if (!ACCEPTED.includes(next.type) && !/\.(mp3|wav|m4a|mp4|mov|webm)$/i.test(next.name)) {
      setError('暂不支持此格式，请选择 MP3、WAV、M4A、MP4、MOV 或 WebM')
      return
    }
    if (next.size > 100 * 1024 * 1024) {
      setError('文件不能超过 100 MB')
      return
    }
    setFile(next)
    setError('')
    if (!title) setTitle(next.name.replace(/\.[^.]+$/, ''))
  }

  const onDrop = (event: DragEvent) => {
    event.preventDefault()
    setDragging(false)
    chooseFile(event.dataTransfer.files[0])
  }

  const submit = async () => {
    if (validation || submitting) {
      if (validation) setError(validation)
      return
    }
    setSubmitting(true)
    setError('')
    try {
      const result = mode === 'text'
        ? await api.createTextProject(title.trim(), text.trim(), idempotencyKey.current)
        : await api.createUploadProject(title.trim(), file!, idempotencyKey.current)
      toast('项目已创建，正在进入处理队列', 'success')
      navigate(`/projects/${result.project.id}/processing?job=${encodeURIComponent(result.job.id)}`)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="page new-project-page">
      <div className="page-back-row"><AppLink href="/projects" className="back-link"><ArrowLeft size={17} /> 返回项目台</AppLink></div>
      <div className="new-project-layout">
        <section className="creation-card">
          <div className="creation-head">
            <span className="eyebrow"><Sparkles size={14} /> 新建视觉匹配项目</span>
            <h1>从内容开始，生成视觉节奏</h1>
            <p>FrameFlow 会分析语义、提取关键词，并为每个内容片段匹配至少 3 个画面候选。</p>
          </div>
          <div className="source-tabs" role="tablist" aria-label="内容来源">
            <button type="button" role="tab" disabled={submitting} aria-selected={mode === 'text'} className={mode === 'text' ? 'active' : ''} onClick={() => { setMode('text'); setError('') }}><FileText size={18} /><span><b>粘贴文本</b><small>文案、脚本、字幕</small></span></button>
            <button type="button" role="tab" disabled={submitting} aria-selected={mode === 'upload'} className={mode === 'upload' ? 'active' : ''} onClick={() => { setMode('upload'); setError('') }}><AudioLines size={18} /><span><b>上传音视频</b><small>提取媒体并尝试转写</small></span></button>
          </div>

          <div className="form-field">
            <label htmlFor="project-title">项目名称 <span>必填</span></label>
            <input id="project-title" value={title} onChange={(event) => setTitle(event.target.value)} placeholder="例如：AI 与未来工作方式" maxLength={60} autoFocus />
            <small className="field-count">{title.length}/60</small>
          </div>

          {mode === 'text' ? (
            <div className="form-field">
              <div className="label-row"><label htmlFor="project-text">内容文案 <span>必填</span></label><button type="button" className="sample-button" onClick={() => { setText(EXAMPLE); if (!title) setTitle('AI 与未来工作方式') }}>填入演示文案</button></div>
              <div className="textarea-wrap">
                <textarea id="project-text" value={text} onChange={(event) => setText(event.target.value)} placeholder={'在这里粘贴口播文案或字幕…\n\n建议输入 100 字以上，分段与匹配效果会更完整。'} maxLength={20_000} />
                <div className="textarea-meta"><span>{text.trim() ? `预计 ${Math.max(1, Math.ceil(text.trim().length / 55))} 个内容片段` : '支持中英文标点智能分段'}</span><span>{text.length.toLocaleString()}/20,000</span></div>
              </div>
            </div>
          ) : (
            <div className="form-field">
              <label>音频或视频 <span>必填</span></label>
              {!file ? (
                <button
                  type="button"
                  className={`drop-zone${dragging ? ' dragging' : ''}`}
                  onClick={() => fileInput.current?.click()}
                  onDragEnter={(event) => { event.preventDefault(); setDragging(true) }}
                  onDragOver={(event) => event.preventDefault()}
                  onDragLeave={() => setDragging(false)}
                  onDrop={onDrop}
                >
                  <span className="upload-icon"><UploadCloud size={27} /></span>
                  <strong>拖放文件到这里，或点击选择</strong>
                  <span>MP3、WAV、M4A、MP4、MOV、WebM · 最大 100 MB</span>
                </button>
              ) : (
                <div className="selected-file">
                  <span className="selected-file-icon">{file.type.startsWith('video') ? <FileVideo size={25} /> : <FileAudio size={25} />}</span>
                  <div><strong>{file.name}</strong><span>{(file.size / 1024 / 1024).toFixed(1)} MB · {file.type || '媒体文件'}</span></div>
                  <span className="file-ready"><Check size={14} /> 已就绪</span>
                  <button type="button" className="icon-button" aria-label="移除文件" onClick={() => setFile(null)}><X size={18} /></button>
                </div>
              )}
              <input ref={fileInput} hidden type="file" accept="audio/*,video/*,.m4a,.mov" onChange={(event) => chooseFile(event.target.files?.[0])} />
              <div className="info-note"><Info size={16} /><span>上传文件会先持久化，再由服务端尝试转写；若未配置 ASR，处理页会如实显示失败原因。任务可恢复，离开页面不会中断队列。</span></div>
            </div>
          )}

          {error && <div className="form-error" role="alert"><Info size={17} /><span>{error}</span></div>}
          <div className="form-actions">
            {submitting ? <span className="button button-secondary" aria-disabled="true">取消</span> : <AppLink href="/projects" className="button button-secondary">取消</AppLink>}
            <button type="button" className="button button-primary button-large" disabled={submitting} onClick={() => void submit()}>
              {submitting ? <InlineSpinner label="正在创建项目" /> : <><Sparkles size={18} /> 开始智能匹配</>}
            </button>
          </div>
        </section>

        <aside className="process-guide">
          <span className="guide-kicker">接下来会发生什么</span>
          <ol>
            <li><i>1</i><div><strong>解析与转写</strong><p>{mode === 'text' ? '校验文案并识别句间结构' : '提取音轨并生成逐字稿'}</p></div></li>
            <li><i>2</i><div><strong>语义分段</strong><p>按表达主题和视觉节奏拆成片段</p></div></li>
            <li><i>3</i><div><strong>透明匹配</strong><p>综合语义、关键词和主题计算得分</p></div></li>
            <li><i>4</i><div><strong>人工精修</strong><p>调整文本、顺序与最终画面选择</p></div></li>
          </ol>
          <div className="guide-tip"><Sparkles size={17} /><p><strong>可恢复的异步处理</strong><br />即使服务重启，排队中的任务也会继续执行；重复提交不会创建两份数据。</p></div>
        </aside>
      </div>
    </main>
  )
}
