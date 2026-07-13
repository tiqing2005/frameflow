# FrameFlow AI

[公开源码仓库](https://github.com/tiqing2005/frameflow) · 中文优先的字幕语义分段与可解释素材匹配工作台

FrameFlow 把文本、音频或视频输入转成持久化异步任务，生成语义片段，为每个片段给出至少 3 个带分项得分和中文理由的素材候选，并允许人工编辑字幕、调整顺序、搜索替换素材。页面刷新后，任务进度、最终选择、AI/规则运行记录和审计记录仍然存在。

## 产品界面

![FrameFlow 项目台](docs/images/dashboard.png)

![FrameFlow 三栏字幕配镜工作台](docs/images/workbench.png)

移动端使用“字幕 / 编辑 / 候选”工作区切换，完整截图见 [移动端工作台](docs/images/workbench-mobile.png)。

## 核心亮点

- 真实业务闭环：输入 → 异步处理 → 分段 → 可解释匹配 → 人工编辑 → 持久化结果。
- 可恢复任务：数据库任务队列、租约、心跳、超时接管、重试/取消与单调进度事件。
- 可解释排序：`0.55 × 字符 n-gram TF-IDF + 0.30 × 关键词重合 + 0.15 × 标签/主题重合`。
- 工程完整性：统一错误结构、幂等创建、乐观锁编辑、事务化排序、健康检查和审计轨迹。
- 可演示故障：一次性 AI 降级和任务失败注入，可现场展示 fallback 与 retry。
- 可部署：多阶段 Docker 构建、非 root 运行、Caddy 自动 HTTPS、`/data` 持久化、备份恢复和健康回滚。
- 公网演示可选整站 Basic Auth；默认关闭，不影响评审直接访问，需要时复制 Caddy 片段即可启用。

## 技术架构

```text
浏览器 / React 19 + Vite
          │ 同源 REST / multipart
          ▼
  Caddy（HTTPS、压缩、安全头）
          ▼
 FastAPI API ───── SQLite WAL + /data/media
          │             ▲
          └─ 持久化 Job ─┤
                        Worker
             分段 / 关键词 / 混合排序 / ASR
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

完整的 DNS、环境变量、HTTPS、备份、恢复、升级和故障排查说明见 [部署手册](docs/DEPLOYMENT.md)。

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

访问 `http://localhost:5173`；Vite 会把 `/api` 与 `/media` 代理到 `127.0.0.1:8000`。API 文档位于 `http://127.0.0.1:8000/api/docs`。

## 测试与质量检查

```bash
cd backend
pytest

cd ../frontend
npm run lint
npm run build
```

后端测试覆盖分段、关键词、混合排序、幂等与主要 API 流程。更完整的验收范围见 [测试计划](docs/TEST_PLAN.md) 和 [评分矩阵](docs/SCORING_MATRIX.md)。

## AI 使用边界

- 文本主流程使用确定性分段、关键词提取和可解释混合排序；没有密钥、模型超时或结果不合格时仍可工作。
- 可选语义增强支持 OpenAI-compatible 与 DeepSeek `/chat/completions`；模型只负责分段、主题和关键词，输出仍经过严格 Schema 与原字幕完整性校验。
- 音视频转写支持 OpenAI 兼容 ASR，或在构建时选择安装 `faster-whisper` 本地 ASR。
- 运行记录会如实标记 provider、model、策略版本与是否降级，不把规则结果伪装成大模型结果。
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

模型 ID 是否可用取决于账号或兼容网关；不可用、超时或返回格式不合格时，任务会自动使用确定性规则完成并如实记录降级。

详见 [AI 使用说明](docs/AI_USAGE.md)。

## 关键取舍与已知边界

- SQLite WAL + 单 Worker 适合单机演示和低并发，不支持多实例共享写入。
- 前端轮询持久化任务事件，简单且容易穿过代理；大规模实时任务可升级为 SSE/WebSocket。
- 小素材库在内存计算 TF-IDF 更透明；规模增长后可迁移向量数据库或搜索服务。
- 当前版本没有用户登录、租户隔离、速率限制和恶意文件扫描，公网演示应限制访问范围和运行时间。
- 本地 ASR 依赖较重且首次需要下载模型；小 VPS 更适合远程兼容 ASR。
- Hugging Face 与 `faster-whisper` 模型缓存位于持久化卷 `/data/models/huggingface`，容器升级不会重复丢失缓存。

详见 [已知问题](docs/KNOWN_ISSUES.md)、[架构说明](docs/ARCHITECTURE.md) 和 [数据模型](docs/DATA_MODEL.md)。

## 面试演示建议

1. 创建中文文本项目，展示 202 响应与真实多阶段进度。
2. 进入工作台，解释 3 个候选的 TF-IDF、关键词、标签分项和命中词。
3. 修改字幕、重新匹配、手动换素材并刷新页面，证明持久化与乐观锁。
4. 注入 `ai_degrade`，展示规则接管与运行记录中的降级标识。
5. 注入 `job_fail`，展示失败原因、可重试判断与成功恢复。
6. 打开 AI 运行记录、审计记录、健康检查和公开 Git 提交历史。

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
