import { useState } from 'react'
import {
  AlertOctagon,
  ArrowRight,
  Beaker,
  Check,
  CircleOff,
  FlaskConical,
  Info,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
} from 'lucide-react'
import { api, errorMessage } from '../api'
import { InlineSpinner, useToast } from '../components/ui'
import { AppLink } from '../router'

type FaultMode = 'ai_degrade' | 'job_fail' | 'none'

const faultOptions: { mode: FaultMode; icon: typeof Sparkles; title: string; description: string; result: string; tone: string }[] = [
  { mode: 'ai_degrade', icon: TriangleAlert, title: '模拟 AI 降级', description: '下一次模型增强失败，系统自动切换确定性规则。', result: '项目仍会完成，并记录 degraded 运行。', tone: 'amber' },
  { mode: 'job_fail', icon: AlertOctagon, title: '模拟任务失败', description: '下一次异步任务在处理中返回可重试错误。', result: '处理页展示错误详情与真实重试入口。', tone: 'red' },
  { mode: 'none', icon: ShieldCheck, title: '恢复正常模式', description: '清除尚未触发的故障，后续任务正常执行。', result: '不会修改已经存在的项目或运行记录。', tone: 'green' },
]

export function DemoPage() {
  const [selected, setSelected] = useState<FaultMode | null>(null)
  const [setting, setSetting] = useState<FaultMode | null>(null)
  const toast = useToast()

  const setFault = async (mode: FaultMode) => {
    setSetting(mode)
    try {
      const result = await api.setNextFault(mode)
      setSelected(mode)
      toast(result.message || (mode === 'none' ? '已恢复正常处理模式' : '演示故障已设置，仅影响下一个新任务'), 'success')
    } catch (err) { toast(errorMessage(err), 'error') } finally { setSetting(null) }
  }

  return (
    <main className="page demo-page">
      <div className="page-heading-row compact-heading">
        <div><span className="eyebrow"><Beaker size={14} /> 仅供能力演示</span><h1>演示实验室</h1><p>主动触发可控故障，验证降级、持久化和重试链路。控制项只影响下一个新建任务。</p></div>
        <AppLink href="/projects/new" className="button button-primary heading-action">新建测试项目 <ArrowRight size={17} /></AppLink>
      </div>
      <div className="demo-warning"><Info size={18} /><div><strong>这是演示开关，不是生产配置</strong><p>故障模式为一次性：被下一个任务消费后自动清除。不会破坏已有项目、素材或选择。</p></div></div>
      <section className="demo-section">
        <div className="section-heading"><div><h2>选择下一次任务的行为</h2><p>设置成功后，再去新建一个文本或音视频项目</p></div>{selected && <span className="armed-chip"><i /> {selected === 'none' ? '正常模式' : '已布置一次性故障'}</span>}</div>
        <div className="fault-grid">
          {faultOptions.map(({ mode, icon: Icon, title, description, result, tone }) => (
            <article className={`fault-card tone-${tone}${selected === mode ? ' selected' : ''}`} key={mode}>
              <div className="fault-icon"><Icon size={23} /></div>
              <div className="fault-title"><h3>{title}</h3>{selected === mode && <span><Check size={13} /> 当前</span>}</div>
              <p>{description}</p>
              <small>{result}</small>
              <button type="button" className={`button ${mode === 'none' ? 'button-secondary' : 'button-outline-danger'}`} disabled={setting !== null} onClick={() => void setFault(mode)}>
                {setting === mode ? <InlineSpinner label="正在设置" /> : mode === 'none' ? <><RotateCcw size={16} /> 恢复正常</> : <><FlaskConical size={16} /> 设置一次</>}
              </button>
            </article>
          ))}
        </div>
      </section>
      <section className="demo-script">
        <div className="script-head"><span><Sparkles size={20} /></span><div><h2>3 分钟面试演示脚本</h2><p>一条路径讲清产品闭环与工程可靠性</p></div></div>
        <ol>
          <li><i>01</i><div><strong>创建真实任务</strong><p>粘贴演示文案，展示 202 返回后进入持久化异步阶段。</p></div></li>
          <li><i>02</i><div><strong>解释匹配结果</strong><p>进入工作台，展开候选分数，说明 55/30/15 混合排序。</p></div></li>
          <li><i>03</i><div><strong>证明人工可控</strong><p>修改字幕、调整顺序、搜索替换素材，再刷新页面验证保存。</p></div></li>
          <li><i>04</i><div><strong>演示可靠性</strong><p>设置 AI 降级或任务失败，展示透明标识、事件和重试。</p></div></li>
        </ol>
        <div className="script-foot"><CircleOff size={16} /> 演示结束后，请点击“恢复正常模式”。</div>
      </section>
    </main>
  )
}
