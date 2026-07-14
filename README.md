# FrameFlow

[![FrameFlow CI](https://github.com/tiqing2005/frameflow/actions/workflows/ci.yml/badge.svg)](https://github.com/tiqing2005/frameflow/actions/workflows/ci.yml)

[公开源码仓库](https://github.com/tiqing2005/frameflow) · 中文优先、AI 辅助的字幕语义分段与可解释素材匹配工作台

FrameFlow 把文本、音频或视频输入转成持久化异步任务，生成语义片段，为每个片段给出至少 3 个带分项得分和中文理由的素材候选，并允许人工编辑字幕、拖动排序、搜索替换素材。系统还能把最终选择组织成可视化时间线，并异步生成带匹配素材的 MP4 组合预览。应用内管理员登录保护工作台写操作，用户上传且未被引用的素材可安全删除；页面刷新后，任务进度、最终选择、预览结果、运行记录中的 AI/规则来源和审计记录仍然存在。

## 产品界面

新版界面使用深色侧栏和橙色强调色。工作区导航统一为“项目 / 素材库 / 运行记录”，系统工具集中在“演示工具”；项目总览可查看素材与任务状态，创建项目时可选择文本、音频或视频输入，再进入工作台完成分段、候选比较、人工调整和组合预览。

<p align="center">
  <img src="docs/images/dashboard.png" alt="FrameFlow 项目总览" width="900">
</p>
<p align="center"><sub>项目总览：统计项目、素材和任务状态，并继续最近编辑。</sub></p>

<p align="center">
  <img src="docs/images/workbench.png" alt="FrameFlow 字幕与素材匹配工作台" width="900">
</p>
<p align="center"><sub>项目工作台：检查语义片段、比较可解释候选、调整字幕与素材选择。</sub></p>

<table>
  <tr>
    <td width="50%" valign="top">
      <img src="docs/images/new-project.png" alt="FrameFlow 新建项目" width="100%">
      <p align="center"><sub>新建项目</sub></p>
    </td>
    <td width="50%" valign="top">
      <img src="docs/images/assets.png" alt="FrameFlow 素材库" width="100%">
      <p align="center"><sub>素材库</sub></p>
    </td>
  </tr>
</table>

<p align="center">
  <img src="docs/images/workbench-mobile.png" alt="FrameFlow 移动端工作台" width="320">
</p>
<p align="center"><sub>移动端工作台会把同一编辑流程压缩为适合窄屏操作的分区视图。</sub></p>

## 核心亮点

- 真实业务闭环：输入 → 异步处理 → 分段 → 可解释匹配 → 人工编辑 → 持久化结果。
- 自动素材理解：上传素材时自动建议中文标签和画面关键词，LLM 不可用时使用确定性规则回退，并保存实际运行来源。
- 可恢复任务：数据库任务队列、租约、心跳、硬超时保护、耗尽控制、重试/取消与单调进度事件。
- 可解释排序：`0.55 × 语义相似 + 0.30 × 关键词重合 + 0.15 × 标签/主题重合`。语义通道默认字符 n-gram TF-IDF（零依赖），启用本地 BGE 向量或远程 `/embeddings` 后升级为真·语义相似度，并保留三项分数与命中词便于人工判断。
- 时间线与组合预览：字幕片段按时长形成素材时间线，预览请求按输入指纹幂等，Worker 使用 ffmpeg 生成 H.264/MPEG-4 MP4，并在环境支持时烧录中文字幕。
- 可追溯 AI：语义分段、素材匹配、自动标签和预览渲染分别记录 provider、model、输入哈希、策略版本、耗时、结果摘要与降级状态。
- 应用内登录：单管理员 HttpOnly Cookie 会话、CSRF 写操作校验、登录失败限流与退出；本地首次使用可在回环地址安全创建账号，公网部署则预置密码哈希并关闭远程首次认领。
- 安全素材治理：上传素材支持编辑与删除；种子素材、正在被项目片段引用的素材和会破坏最低可演示素材数的删除会被明确拒绝，避免数据库与媒体文件失配。
- 工程完整性：统一错误结构、幂等创建、乐观锁编辑、事务化排序、进程内读写限流、健康检查和审计轨迹。
- 可演示故障：一次性 AI 降级和任务失败注入，可现场展示 fallback 与 retry。
- 可部署：多阶段 Docker 构建、非 root 运行、Caddy 自动 HTTPS、`/data` 持久化、备份恢复和健康回滚。
- 公网演示首次部署默认强制整站 Basic Auth，并同时启用请求体限制、安全响应头和容器资源限制；只有在可信内网或外层已有强鉴权时才应显式关闭。

## 技术架构

```text
浏览器 / React 19 + Vite
          │ 同源 REST / multipart
          ▼
  Caddy（HTTPS、压缩、安全头）
          ▼
 FastAPI API ───── SQLite WAL + /data/media + /data/private
          │             ▲
          └─ 持久化 Job ─┤
                        Worker
        分段 / 自动标签 / 混合排序 / ASR / ffmpeg 预览
```

这是“模块化单体 + 独立任务循环”的有意选择：招聘作业体量下，它比引入 Redis、消息队列和多服务部署更容易验收，同时保留任务恢复、追踪和未来拆分边界。

## 快速开始

### Docker（推荐）

准备域名和 Linux VPS 后：

```bash
git clone https://github.com/tiqing2005/frameflow.git
cd frameflow
bash deploy/first-deploy.sh app.example.com ops@example.com
```

脚本会分别配置应用内管理员登录和可选的 Caddy 整站 Basic Auth；两层密码可不同，明文只在部署终端中短暂读取，服务器的 `deploy/.env` 仅保存密码哈希。完整的 DNS、环境变量、HTTPS、备份、恢复、升级和故障排查说明见 [部署手册](docs/DEPLOYMENT.md)。

目标公开演示地址为 [https://frameflow.sbh2005.me](https://frameflow.sbh2005.me)。该地址只有在本次发布的 HTTPS、登录、ready、主流程和重启持久化检查全部通过后，才能在提交信息中标记为“已上线”；截至本段文档同步时尚未写入公网 PASS 结论。

### 本地开发

要求 Python 3.11+、Node.js 22+。

```bash
cd backend
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
python -m app.worker
```

另开终端启动 API：

```bash
cd backend
uvicorn app.main:app --reload --port 8000
```

再启动前端：

```bash
cd frontend
npm ci
npm run dev
```

访问 `http://localhost:5173`；Vite 会把 `/api` 与 `/media` 代理到 `127.0.0.1:8000`。全新本地数据目录首次打开会进入管理员初始化页，该入口只允许从服务所在机器的回环地址调用；创建后使用同一页面登录。API 文档位于 `http://127.0.0.1:8000/api/docs`。

## 测试与质量检查

```bash
cd backend
pytest                       # 单元 + 契约测试（live 测试默认跳过）

# 验证真实 provider 连通性（部署或更换 key/模型后运行，需联网与密钥）
FRAMEFLOW_RUN_LIVE=1 pytest -m live

cd ../frontend
npm run lint
npm run build
npx playwright test          # 浏览器 E2E（含完整 happy-path 闭环）
```

后端测试覆盖分段、关键词、自动标签、混合排序、语义回退、任务恢复、并发幂等、认证/CSRF、素材删除、限流、时间线和预览渲染契约。2026-07-14 最终本地门禁为：后端 `89 passed, 1 deselected`，前端 lint/build 通过，Chromium Playwright `28 passed`，启动器契约 3 个场景通过。`evaluation/evaluate.py` 在 34 条字幕（24 easy + 10 hard）上对比关键词基线 / 字符 TF-IDF / 混合排序，并在启用 embedding 时追加“混合排序(向量)”行；本地向量混排结果为 Hit@3 `0.9412`、nDCG@3 `0.8288`。公网 smoke 与 acceptance 必须在实际部署后另行记录，不能由本地结果替代。更完整的验收范围见 [测试计划](docs/TEST_PLAN.md) 和 [评分矩阵](docs/SCORING_MATRIX.md)。

## AI 使用边界

- 文本主流程使用确定性分段、关键词提取和可解释混合排序；没有密钥、模型超时或结果不合格时仍可工作。
- 可选语义增强支持 OpenAI-compatible 与 DeepSeek `/chat/completions`；模型只负责分段、主题和关键词，输出仍经过严格 Schema 与原字幕完整性校验。
- 匹配的语义通道默认字符 n-gram TF-IDF（零依赖）；安装 `requirements-embeddings-local.txt`（本地 BGE `bge-small-zh-v1.5`）或配置远程 `/embeddings` 后，0.55 权重升级为真·向量余弦相似度，任何失败回退字符相似。
- 音视频转写支持 OpenAI 兼容 ASR，或在构建时选择安装 `faster-whisper` 本地 ASR。
- 上传新素材时若 LLM 已配置，可自动建议中文标签与画面关键词；未配置则回退规则提取。
- 运行记录会如实标记 provider、model、策略版本、相似度来源（embedding / char-ngram）与是否降级，不把规则结果伪装成大模型结果。
- API Key 只从后端环境变量读取，不进入 Vite 构建产物、数据库或审计日志。
- `IMAGE_API_*` 目前只是部署预留变量；当前交付支持内置素材和用户上传素材，尚未宣称已完成业务内图像生成。

DeepSeek V4 Pro 部署示例（写入 `deploy/.env`，不要提交真实密钥）：

```dotenv
LLM_PROVIDER=deepseek
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=你的服务端密钥
LLM_MODEL=deepseek-v4-pro
LLM_TIMEOUT=20
```

启用本地 BGE 向量语义（可选，需联网首次下载约 95MB 模型到 `HF_HOME`）：

```bash
pip install -r backend/requirements-embeddings-local.txt
# 或 Docker 构建：--build-arg INSTALL_LOCAL_EMBEDDINGS=true
# .env: EMBEDDING_PROVIDER=auto（默认）即可
```

模型 ID 是否可用取决于账号或兼容网关；不可用、超时或返回格式不合格时，任务会自动使用确定性规则完成并如实记录降级。

详见 [AI 使用说明](docs/AI_USAGE.md)。

## 关键取舍与已知边界

- SQLite WAL + 单 Worker 适合单机演示和低并发，不支持多实例共享写入。
- 前端轮询持久化任务事件，简单且容易穿过代理；大规模实时任务可升级为 SSE/WebSocket。
- 小素材库在内存计算 TF-IDF 更透明；规模增长后可迁移向量数据库或搜索服务。
- 当前版本已有单管理员应用内登录与部署层 Basic Auth，但没有用户注册、项目归属、RBAC 或多租户隔离；进程内限流也不等同于分布式配额。
- 媒体上传有大小、类型和私有源文件保护，但不包含杀毒或恶意媒体沙箱扫描，公网演示仍应限制访问范围和运行时间。
- 本地 ASR 依赖较重且首次需要下载模型；小 VPS 更适合远程兼容 ASR。
- Hugging Face 与 `faster-whisper` 模型缓存位于持久化卷 `/data/models/huggingface`，容器升级不会重复丢失缓存。

详见 [已知问题](docs/KNOWN_ISSUES.md)、[架构说明](docs/ARCHITECTURE.md) 和 [数据模型](docs/DATA_MODEL.md)。

## 面试演示建议

1. 创建中文文本项目，展示 202 响应与真实多阶段进度。
2. 进入工作台，解释 3 个候选的 TF-IDF、关键词、标签分项和命中词。
3. 拖动字幕排序、快速替换素材并刷新页面，证明事务化持久化与乐观锁。
4. 打开素材时间线并生成 MP4 组合预览，展示预览任务的幂等、进度和最终播放。
5. 上传一份测试素材，展示自动元数据建议、编辑和安全删除；同时说明种子/已引用素材不能删除。
6. 注入 `ai_degrade`，展示规则接管与运行记录中的降级标识。
7. 注入 `job_fail`，展示失败原因、可重试判断与成功恢复。
8. 打开运行记录，核对 AI/规则来源、审计记录、健康检查和公开 Git 提交历史。

详细讲稿见 [演示脚本](docs/DEMO_SCRIPT.md) 和 [面试问答](docs/INTERVIEW_QA.md)。

## 项目文档

- [产品需求](docs/PRD.md)
- [项目结构](docs/PROJECT_STRUCTURE.md)
- [UI 规范](docs/UI_SPEC.md)
- [API 契约](docs/API.md)
- [部署手册](docs/DEPLOYMENT.md)
- [测试计划](docs/TEST_PLAN.md)

## License

[MIT](LICENSE)
