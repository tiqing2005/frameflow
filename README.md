# FrameFlow

[![FrameFlow CI](https://github.com/tiqing2005/frameflow/actions/workflows/ci.yml/badge.svg)](https://github.com/tiqing2005/frameflow/actions/workflows/ci.yml)
[![在线演示](https://img.shields.io/badge/Demo-在线可用-1f9d68)](https://frameflow.sbh2005.me)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

面向招聘实战题二“AI 字幕分析与素材匹配平台”的完整 Demo：把文本、音频或视频转成可恢复的异步任务，完成字幕整理、语义分段、可解释素材匹配、人工调整、持久化保存和 MP4 组合预览；在核心闭环之外，还实现了 Gemini 视觉标签和文生图素材闭环。

## 交付信息

| 项目 | 内容 |
| --- | --- |
| 候选人姓名 | 见正式投递信息；公开仓库不额外保存个人隐私 |
| 选择题目 | **题目二：AI 字幕分析与素材匹配平台** |
| 公网演示 | [https://frameflow.sbh2005.me](https://frameflow.sbh2005.me) |
| 测试账号 | 用户名 `admin`；临时密码随投递材料单独提供，避免在公开仓库长期暴露管理员凭据 |
| 源码仓库 | [https://github.com/tiqing2005/frameflow](https://github.com/tiqing2005/frameflow) |
| 演示视频 | **待补充（脚本已准备）**；正式 3～5 分钟录屏尚未交付，当前仅提供 [4 分 30 秒讲解脚本](docs/DEMO_SCRIPT.md) |
| API 文档 | 登录后访问 `/api/docs`；静态契约见 [docs/API.md](docs/API.md) |

> 公网环境是单管理员招聘 Demo。面试官可以独立操作，但请勿上传隐私、商业机密或无授权媒体。测试结束后会轮换临时密码；API Key 只存在于服务器环境变量中，不进入仓库、浏览器或数据库。

## 题目完成度

### 必须项

| 题目要求 | 实现结果 | 验收入口 |
| --- | --- | --- |
| 文本、音频、视频输入 | 已实现文本粘贴与音视频上传，最大 100 MB | 新建项目 |
| 音视频语音识别 | 已实现本地 faster-whisper，并保留 DashScope / OpenAI-compatible 适配器 | 新建项目 → 处理页 |
| 异步进度、失败原因、重试 | 持久 Job、阶段事件、心跳、租约、取消、可重试失败 | 处理页 / 演示工具 |
| 原始字幕与语义分段 | 原文回读；规则分段与 Gemini 严格 Schema 增强，失败自动降级 | 三栏工作台 |
| 人工编辑文本和顺序 | 自动保存、乐观锁、拖动和箭头排序 | 三栏工作台 |
| 主题和关键词 | 每个片段可查看、编辑并重新匹配 | 三栏工作台 |
| 图片和短视频素材库 | 24 张图片 + 6 个视频种子素材，并支持上传、编辑、删除 | 素材库 |
| 搜索和筛选 | 按名称、标签、关键词和类型过滤 | 素材库 / 候选区 |
| 每段至少 3 个候选 | 服务端强制最少 3 个，保存分项得分、命中词和中文理由 | 候选区 |
| 人工替换并刷新保存 | Selection 持久化，人工选择、排序和字幕编辑均可刷新回读 | 工作台刷新 |
| 外部 AI 失败处理 | 明确错误；字幕、视觉标签和语义匹配均有可审计降级路径 | 运行记录 / 演示工具 |
| 密钥安全 | 仅后端环境变量读取；示例配置只保留空值或占位值 | `.env.example` |

### 加分项

- 自动素材标签：图片或视频 poster 进入“Gemini 视觉 → 文本 LLM → 本地规则”三级链，真实来源写入运行记录。
- 混合排序：`55% 语义 + 30% 关键词 + 15% 主题/标签`；支持本地 BGE 或远程 Embedding，公网 Demo 当前如实使用字符 n-gram TF-IDF 回退。
- 拖动排序和快速替换：服务端事务排序、前端拖拽/箭头、搜索素材后一键采用。
- 时间线和预览：目标总时长、单段 1～30 秒精调、自动分配、过期提示和输入指纹幂等。
- MP4 预览：Worker 调用 ffmpeg 生成 H.264/MPEG-4 组合预览，并在环境支持时烧录字幕。
- 可靠任务：幂等创建、并发领取、执行代次栅栏、租约心跳、硬超时隔离、失败重试和优雅停机。
- AI 可追溯：记录 provider、model、输入哈希、策略版本、耗时、降级状态和结果摘要。
- 扩展闭环：自然语言文生图 → 私有草稿 → 用户确认入库 → Gemini 标签 → 字幕候选/时间线。

详细逐项证据见 [评分矩阵](docs/SCORING_MATRIX.md)。

## 当前产品界面

所有截图均于 **2026-07-15** 从当前公网版本实机重新采集，不是设计稿或旧版占位图。

<p align="center">
  <img src="docs/images/workbench.png" alt="FrameFlow 三栏字幕与素材匹配工作台" width="100%">
</p>
<p align="center"><sub>核心工作台：5 个语义片段、真实图片/视频、至少 3 个候选、匹配理由、人工替换、时间线与自动保存。</sub></p>

<table>
  <tr>
    <td width="50%" valign="top">
      <img src="docs/images/login.png" alt="FrameFlow 登录页" width="100%">
      <p align="center"><sub>公网登录与安全会话</sub></p>
    </td>
    <td width="50%" valign="top">
      <img src="docs/images/dashboard.png" alt="FrameFlow 项目总览" width="100%">
      <p align="center"><sub>持久化项目、素材和任务状态</sub></p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="docs/images/new-project.png" alt="FrameFlow 音视频上传" width="100%">
      <p align="center"><sub>文本 / 音频 / 视频输入</sub></p>
    </td>
    <td width="50%" valign="top">
      <img src="docs/images/processing.png" alt="FrameFlow 异步处理进度" width="100%">
      <p align="center"><sub>真实阶段、实时事件、取消与恢复</sub></p>
    </td>
  </tr>
</table>

<p align="center">
  <img src="docs/images/preview-video.png" alt="FrameFlow 时间线与预览视频" width="900">
</p>
<p align="center"><sub>20.1 秒时间线：逐段时长可调，预览完成后可直接播放或按新输入重新生成。</sub></p>

<table>
  <tr>
    <td width="50%" valign="top">
      <img src="docs/images/image-generation.png" alt="FrameFlow 文生图成功结果" width="100%">
      <p align="center"><sub>真实文生图：模型、耗时、比例和确认入库</sub></p>
    </td>
    <td width="50%" valign="top">
      <img src="docs/images/asset-ai-tags.png" alt="FrameFlow Gemini 素材标签" width="100%">
      <p align="center"><sub>Gemini 单画面理解、标签、关键词与素材治理</sub></p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="docs/images/assets.png" alt="FrameFlow 素材库" width="100%">
      <p align="center"><sub>31 项真实图片/视频与搜索筛选</sub></p>
    </td>
    <td width="50%" valign="top">
      <img src="docs/images/runs.png" alt="FrameFlow AI 运行记录" width="100%">
      <p align="center"><sub>Gemini、gpt-image-2、ffmpeg 和降级策略可追溯</sub></p>
    </td>
  </tr>
</table>

<details>
<summary>查看移动端工作台</summary>

<p align="center">
  <img src="docs/images/workbench-mobile.png" alt="FrameFlow 移动端工作台" width="390">
</p>

</details>

## 业务闭环

```text
文本 / 音频 / 视频
        │
        ▼
持久化异步 Job ── ASR / 字幕解析 ── 语义分段 ── 主题与关键词
        │                                      │
        │                                      ▼
        │                         混合排序 + 至少 3 个候选
        │                                      │
        ▼                                      ▼
失败原因 / 取消 / 重试             人工编辑 / 排序 / 搜索替换
                                               │
                                               ▼
                                  Selection 持久化 + 时间线
                                               │
                                               ▼
                                      ffmpeg MP4 预览

候选不足时：自然语言 → Image Worker → 私有草稿 → 确认入库
                                      → Gemini 标签 → 候选 / 时间线
```

## 架构与设计取舍

```text
React 19 + TypeScript + Vite
              │ 同源 REST / multipart
              ▼
宿主 Nginx（公网 Demo）/ Caddy（仓库默认部署）
              │
              ▼
FastAPI API ───────── SQLite WAL + /data/media + /data/private
     │                                  ▲
     ├── 持久 Job ─────────────── Core Worker 池
     │    ASR / 分段 / 匹配 / 标签 / ffmpeg
     │
     └── ImageGeneration ──────── 独立单并发 Image Worker
                                          │
                                OpenAI-compatible 图像服务
```

这是面向单机招聘 Demo 的“模块化单体 + 独立持久 Worker”方案。它避免为了展示而引入 Redis/Kafka 等额外运维负担，同时保留任务恢复、并发栅栏、审计轨迹以及未来拆分队列/对象存储/向量数据库的边界。

### 匹配策略

```text
final_score = 0.55 × semantic + 0.30 × keyword + 0.15 × tag_topic
```

- 语义分：默认字符 n-gram TF-IDF，零外部依赖；配置本地 `bge-small-zh-v1.5` 或远程 `/embeddings` 后切换为向量余弦相似度。
- 关键词分：字幕关键词与素材关键词的可解释重合度。
- 标签/主题分：片段主题与素材标签的重合度。
- 每个 Recommendation 保存三项分数、命中词、中文解释、运行 ID 和策略版本，不能只返回一个黑盒总分。
- Embedding、LLM 或视觉服务失败时回退到可工作的本地通道，并把降级事实显示在运行记录中。

为什么不只用大模型：小素材库需要低延迟、可复现、可解释和无 Key 也能演示；LLM 更适合增强分段与标签，不适合作为唯一排序依据。

## 快速开始

### Docker（推荐）

要求 Linux VPS、Docker Compose、已解析的域名和开放的 80/443 端口：

```bash
git clone https://github.com/tiqing2005/frameflow.git
cd frameflow
bash deploy/first-deploy.sh app.example.com ops@example.com
```

脚本会生成服务器专用 `deploy/.env`、构建多阶段镜像、执行迁移、健康检查和 HTTPS smoke。明文凭据不会写入 Git；完整 DNS、首次部署、升级、备份、恢复和回滚步骤见 [部署手册](docs/DEPLOYMENT.md)。

### 本地开发

要求 Python 3.11+、Node.js 22+、ffmpeg：

```powershell
# 后端：API + Core Worker + Image Worker 监督进程
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
python -m app.serve

# 另开终端启动前端
cd frontend
npm ci
npm run dev
```

浏览器访问 `http://localhost:5173`。全新数据目录的管理员初始化只允许从服务所在机器的回环地址调用；真实密钥只填写在 Git 忽略的 `.env`，不配置外部模型时仍可通过文本 + 规则降级完成核心流程。

## 测试与验收

```powershell
# 后端
cd backend
python -m pytest

# 前端
cd ..\frontend
npm run lint
npm run build
npm run test:browser

# API 验收（服务已启动时）
cd ..
.\scripts\acceptance.ps1 -BaseUrl http://127.0.0.1:8000
```

2026-07-15 最终门禁：

| 检查 | 结果 |
| --- | --- |
| 后端 pytest | `222 passed, 1 deselected` |
| 文生图专项 | `54 passed` |
| 前端 lint / TypeScript / production build | 通过，1789 modules |
| Chromium Playwright | `48 passed` |
| Windows 启动器契约 | `7 passed` |
| GitHub Actions | 通过 |

同日公网实机记录：管理员一次登录成功；文本项目 5 秒生成 5 段且每段不少于 3 个候选；20.1 秒 MP4 预览渲染 8.5 秒；文生图 41.5 秒；Gemini 视觉标签 4.3 秒；健康端点 HTTP 200。以上是一次受控样本，不承诺 SLA；运行记录截图和页面会保留真实 provider、model、耗时和降级状态。

测试范围、故障场景和人工验收清单见 [测试计划](docs/TEST_PLAN.md)。

## AI 使用说明

### 产品运行时

- 字幕语义增强：公网 Demo 使用 Gemini 3.1 Flash Lite Preview；输出必须通过严格 Schema、原文完整性和片段边界校验，否则回退规则分段。
- 语音识别：公网主演示路径使用本地 faster-whisper `small/int8`；DashScope Paraformer-v2 与 OpenAI-compatible ASR 为可替换适配器。
- 素材理解：只发送一张规范化图片或视频 poster，不上传整段视频；失败依次回退文本 LLM 和本地规则。
- 文生图：使用独立 `IMAGE_API_*` 配置和固定单并发 Worker；只有用户明确确认后才进入素材库，结果未知时不会自动重复产生费用。
- 密钥隔离：LLM、ASR、Vision、Embedding、Image 分别配置，后端启动时读取；审计只保存哈希、摘要和模型元数据。

### 开发过程中的 AI 辅助范围

- AI 用于需求拆解、方案比较、代码草拟、测试用例扩展、代码审查、故障定位和文档整理。
- 架构取舍、数据边界、安全策略、真实 Provider 调用和部署结论都以代码、自动测试、运行日志和公网 E2E 证据复核，不把模型回答当作完成证明。
- 没有使用公司现有系统、私有仓库代码或来源不明的商业代码；第三方依赖均可从锁定的包清单和许可证追溯。
- 面试时应能解释持久任务、幂等、租约、乐观锁、混合排序、降级链和文生图费用屏障，而不是只展示 AI 生成结果。

更完整的 Provider、隐私和回退边界见 [AI 使用说明](docs/AI_USAGE.md)。

## 已知边界与后续计划

- 公网匹配当前使用字符 n-gram TF-IDF；神经 Embedding 已有代码和测试，但需要额外模型/远程服务配置后才启用。
- 预览是素材拼接与可选字幕烧录，当前不恢复原始音轨，也不是专业剪辑器。
- 视觉标签只理解一张图片或视频 poster，不是整段视频的多帧动作理解。
- SQLite WAL + 本地卷适合单机低并发 Demo，不支持多个容器共享写入同一个数据库文件。
- 当前只有单管理员，没有用户注册、项目归属、RBAC 或多租户隔离；公网密码不会写入仓库。
- 本地 ASR 冷启动需要下载模型；长音频、噪声和并发会增加等待时间，样本耗时不是 SLA。
- 文生图上游没有可验证的费用明细和真实中间百分比；网络结果未知时，用户确认重试仍可能产生第二次费用。
- 上传有大小、类型、签名和路径保护，但没有杀毒或恶意媒体沙箱。

完整清单和改进路线见 [已知问题](docs/KNOWN_ISSUES.md)。

## 面试演示顺序

1. 登录公网 Demo，创建一段新的中文文本或上传音视频。
2. 展示持久异步阶段、实时事件、失败原因、取消与重试入口。
3. 进入工作台，解释语义片段、关键词、主题和至少 3 个候选的匹配依据。
4. 修改字幕、拖动顺序、搜索并替换素材，刷新证明结果持久化。
5. 调整时间线并生成 MP4 预览。
6. 展示文生图、确认入库和 Gemini 单画面标签。
7. 打开运行记录，核对 Gemini、ASR、排序降级、gpt-image-2 和 ffmpeg 的真实轨迹。
8. 使用演示工具注入一次失败或 AI 降级，并完成重试恢复。

详细话术与时间分配见 [演示脚本](docs/DEMO_SCRIPT.md) 和 [面试问答](docs/INTERVIEW_QA.md)。

## 文档索引

- [产品需求](docs/PRD.md)
- [架构说明](docs/ARCHITECTURE.md)
- [项目结构](docs/PROJECT_STRUCTURE.md)
- [数据模型](docs/DATA_MODEL.md)
- [UI 规范](docs/UI_SPEC.md)
- [API 契约](docs/API.md)
- [部署手册](docs/DEPLOYMENT.md)
- [测试计划](docs/TEST_PLAN.md)
- [评分矩阵](docs/SCORING_MATRIX.md)
- [文生图闭环](docs/IMAGE_GENERATION.md)

## License

[MIT](LICENSE)
