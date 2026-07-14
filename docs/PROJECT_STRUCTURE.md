# FrameFlow AI 项目结构与责任边界

## 1. 设计原则

- 仓库按“可独立构建的前端”、“可独立运行的 API/Worker”和“交付证据”分区。
- 后端保持模块化单体，业务模型与 Provider/HTTP 边界分开，不为 Demo 拆分微服务。
- API 和 Worker 共用相同的数据模型、配置和处理服务，避免两套规则。
- 前端只通过 `/api/v1` 契约读写，不直接读取 SQLite 或拼接磁盘路径。
- `docs/`、`scripts/` 和 CI 属于交付产物，不与运行时代码混在一起。

## 2. 顶层目录

```text
frameflow/
├─ .github/
│  └─ workflows/
│     └─ ci.yml                 # 后端测试、前端 lint/build、浏览器与交付门禁
├─ backend/
│  ├─ app/                       # FastAPI、领域模型、Worker 和处理管线
│  ├─ tests/                     # 单元、API 集成与持久化测试
│  ├─ .env.example               # 后端环境变量说明
│  ├─ pyproject.toml
│  ├─ requirements.txt
│  └─ requirements-dev.txt
├─ frontend/
│  ├─ public/                    # favicon 等公开静态文件
│  ├─ src/                       # React 应用
│  ├─ package.json
│  ├─ package-lock.json
│  └─ vite.config.ts
├─ demo/
│  ├─ sample-transcript.txt      # 稳定主演示文本
│  ├─ failure-retry-transcript.txt
│  └─ sample-zh.wav              # 仅在真实 ASR 已配置时用于转写验证
├─ deploy/                            # 首次部署、升级、回滚、备份、恢复与 smoke
├─ docs/                              # PRD、架构、API、测试、演示与风险文档
├─ evaluation/                        # 匹配质量离线评测与固定数据集
├─ scripts/
│  ├─ acceptance.ps1             # Windows 可重复 API 验收
│  ├─ acceptance.sh              # Linux/macOS/Git Bash 可重复 API 验收
│  ├─ start-frameflow.ps1        # Windows 本地启动器
│  ├─ test-deploy-auth.sh        # 部署鉴权脚本契约测试
│  └─ test-start-frameflow-contract.ps1
├─ .env.example                       # 项目级配置模板
├─ Dockerfile                         # 前后端多阶段生产镜像
├─ docker-compose.yml                 # 单机应用、持久卷与边缘代理编排
├─ Caddyfile                          # Caddy HTTPS 与安全头入口
├─ Makefile                           # 常用开发/运维命令
├─ PROJECT_SPEC.md                    # 项目实现合同，设计不可与其冲突
├─ start-frameflow.cmd                # Windows 双击启动入口
├─ LICENSE
└─ README.md                          # 交付入口，引用 docs 中的详细证据
```

`backend/data/`、`frontend/node_modules/`、`frontend/dist/`、本地 `.env` 和测试临时数据不应提交。

## 3. 后端结构

当前实现围绕以下文件/责任组织。如最终为减少文件数合并了小模块，不应合并其责任边界。

```text
backend/app/
├─ __init__.py
├─ main.py          # 应用工厂/生命周期、CORS、错误处理、路由装配、静态 SPA 托管（纯装配）
├─ serve.py         # 生产入口；监督 API、核心 Worker 池和独立 Image Worker
├─ config.py        # Settings；仅从环境变量建立运行配置
├─ db.py            # Engine/Session、SQLite PRAGMA、初始化与 seed
├─ models.py        # SQLAlchemy 2 持久化模型和约束
├─ schemas.py       # Pydantic 请求校验模型
├─ serializers.py   # ORM 实体到稳定 API 响应的序列化边界
├─ errors.py        # 领域错误、统一 HTTP 错误包装和 request_id
├─ middleware.py    # 单机演示读写分桶限流、标准 429 与 Retry-After
├─ auth.py          # 密码哈希、身份校验、会话令牌与过期清理
├─ nlp.py           # 规则分段、关键词、n-gram TF-IDF 混合排序
├─ embeddings.py    # 语义相似度 Provider 边界（本地 BGE / 远程 / 字符回退）
├─ llm.py           # 可选 LLM 语义分段增强；失败回退确定性规则
├─ asr.py           # ASR Provider 边界；未配置时返回真实错误
├─ vision.py        # 单帧视觉标签 Provider、响应校验与可追溯结果
├─ image_generation.py # OpenAI-compatible 文生图边界、响应/图片安全校验
├─ image_worker.py  # 独立文生图持久队列、租约栅栏、付费重试边界与草稿清理
├─ preview.py       # ffmpeg 组合预览渲染、编码器探测、字幕与媒体规范化
├─ thumbnails.py    # 视频 poster 抽取与失败占位图
├─ seed.py          # 幂等创建 30 个本地授权安全种子（24 图 + 6 视频）
├─ worker.py        # 原子领取、租约/心跳、流水线执行、恢复和优雅停机
│                   # （任务阶段编排 worker._process_pipeline + 事务化结果换版 _persist）
├─ routers/         # /api/v1 路由按资源分组
│   ├─ _deps.py     # get_session / get_settings 依赖
│   ├─ health.py    # /health/live、/health/ready（同时挂 api 与根路径）
│   ├─ auth.py      # /auth/session、/auth/login、/auth/setup、/auth/logout
│   ├─ asr.py       # 带签名的临时 ASR 源文件读取
│   ├─ projects.py  # /projects、/projects/text、/projects/upload、/dashboard
│   ├─ jobs.py      # /jobs/*
│   ├─ segments.py  # /segments/*、/projects/{id}/segments*
│   ├─ assets.py    # /assets/*
│   ├─ image_generations.py # /image-generations 与片段生图、重试/取消/入库
│   ├─ runs.py      # /runs
│   ├─ audit.py     # /audit
│   ├─ previews.py  # /projects/{id}/timeline 与 /preview
│   └─ demo.py      # /demo/faults/next
└─ services/        # 用例服务层按用例分组（__init__ 重新导出全部公共符号）
    ├─ common.py    # dumps、stable_hash、_get_*、add_audit
    ├─ projects.py  # 创建/列表/详情/删除/仪表盘
    ├─ segments.py  # 片段详情、重匹配、编辑、重排
    ├─ selections.py# 自动、人工与生成素材选择
    ├─ assets.py    # 素材列表/上传/编辑/删除和标签任务入队
    ├─ asset_tagging.py # 视觉→文本→规则标签链、租约栅栏与结果落库
    ├─ image_generations.py # 生图创建、状态操作、草稿入库与片段应用
    ├─ jobs.py      # 任务详情/重试/取消/演示故障
    ├─ previews.py  # 时间线计划、指纹幂等与预览 Job 创建/查询
    ├─ runs.py      # AI 运行记录
    └─ audit.py     # 审计事件
```

### 3.1 依赖方向

```text
HTTP routes (routers/)
    ↓
request schemas + domain services (services/)
    ↓
models / db / nlp / provider boundaries

worker
    ↓
_process_pipeline + _persist + same domain services (services/)
    ↓
models / db / nlp / provider boundaries

image_worker
    ↓
image_generation provider + durable staging barrier
    ↓
ImageGeneration / Asset / asset_tagging services
```

禁止方向：

- `models.py` 不导入 FastAPI Request/Response。
- `nlp.py` 不自行开启 HTTP 请求或读写数据库。
- Worker 不通过调用本项目 HTTP API 执行业务，而是复用服务层。
- 路由不在请求线程内执行转写/混合匹配的完整流程。

### 3.2 开发和运行入口

```powershell
# 推荐：同一进程组监督 API、核心 Worker 池和独立 Image Worker
cd backend
python -m app.serve

# 开发调试时也可分别启动
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
python -m app.worker
python -m app.image_worker
```

生产容器使用 `app.serve`，并由它负责优雅停止所有子进程。分别启动时，API、核心 Worker 和 Image Worker 必须使用相同的 `FRAMEFLOW_DATA_DIR` 与 `FRAMEFLOW_DATABASE_URL`；同一数据库只应运行一个 Image Worker。

核心 Worker 处理项目流水线、预览和素材标签；Image Worker 固定单并发等待外部文生图 Provider，避免付费请求占用本地 ASR/ffmpeg 的核心执行槽。两者仍共享单机 SQLite 和数据目录，不代表支持多机水平扩容。

## 4. 前端结构

```text
frontend/src/
├─ main.tsx                   # React 根节点
├─ App.tsx                    # 应用 Shell、导航与路由分发
├─ router.ts                  # 轻量客户端路由解析/导航
├─ api.ts                     # 唯一 fetch 边界、ApiError、资源 API
├─ auth.tsx                   # 会话加载、登录态 Context 与退出
├─ types.ts                   # 与 API 契约对齐的 TypeScript 类型
├─ runTokens.ts               # AI 运行 Token 字段的规范化统计
├─ pages/
│  ├─ LoginPage.tsx           # 登录与仅回环可用的首次管理员初始化
│  ├─ DashboardPage.tsx       # “项目”总览、指标与最近项目
│  ├─ NewProjectPage.tsx      # 文本/文件新建
│  ├─ ProcessingPage.tsx      # 持久化任务进度、失败与重试
│  ├─ WorkbenchPage.tsx       # 字幕编辑/排序、快速替换、时间线与预览播放
│  ├─ AssetsPage.tsx          # 素材库、上传、自动标签与文生图入口
│  ├─ ImageGenerationPage.tsx # 独立文生图工作台路由页
│  ├─ RunsPage.tsx            # 侧栏“运行记录”对应的 AI/匹配追溯页
│  └─ DemoPage.tsx            # 侧栏“演示工具”对应的一次性故障注入页
├─ components/
│  ├─ ui.tsx                  # 精简的基础组件/图标、骨架、Toast
│  └─ ImageGenerationStudio.tsx # 素材库/字幕共用的生图表单、轮询与入库交互
├─ index.css                  # 全局 token、reset、可访问性基线
└─ App.css                    # 布局/页面样式
```

当前页面数和体量不需要引入大型状态管理库。服务端数据以页面加载 + 任务轮询为主，局部表单状态留在组件内。若后续引入 TanStack Query，`api.ts` 仍应保持为 HTTP 契约边界。

### 4.1 前端命令

```powershell
cd frontend
npm ci
npm run dev
npm run lint
npm run build
```

`VITE_API_BASE_URL` 仅在前后端分离开发时设置；同源交付默认使用 `/api/v1`。任何密钥都不能以 `VITE_` 变量存在。

## 5. 数据目录

默认开发数据位于 `backend/data/`，公网容器应将 `FRAMEFLOW_DATA_DIR` 设为 `/data`。

```text
/data/
├─ frameflow.db
├─ private/
│  ├─ sources/                  # 项目原始音频/视频，仅 Worker 读取，不公开挂载
│  └─ image-generations/        # 未接受的生成草稿与恢复 staging，按保留期清理
└─ media/
   ├─ seed/                     # 幂等种子素材
   ├─ previews/                 # 可公开播放的组合预览 MP4
   └─ uploads/
      └─ assets/                # 用户素材
```

- 数据库和媒体目录必须一起备份和恢复。
- 不将用户原文件名用作磁盘路径。
- seed 可重复执行，不重复插入资源。
- 原始项目源文件和未接受的生图草稿不生成公开 `/media` URL；只有已入库素材与成功预览位于公开 `/media` 路径。

## 6. 测试结构

```text
backend/tests/
├─ test_api_flow.py             # 创建→任务→结果→编辑→选择→重读
├─ test_auth.py                 # 初始化、登录、会话、CSRF 与限流
├─ test_asset_tagging.py        # 视觉/文本/规则标签链和租约栅栏
├─ test_image_generation_provider.py # Provider 响应与图片安全边界
├─ test_image_generation_flow.py # 生图状态机、崩溃恢复与入库闭环
├─ test_image_generation_project_delete.py # 项目删除与生图执行栅栏
├─ test_preview.py              # ffmpeg 计划、幂等渲染与失败路径
├─ test_timeline_timing.py      # 目标/单段时长与并发写边界
├─ test_worker_lifecycle.py     # Worker 租约、硬超时和优雅停止
├─ test_worker_concurrency_safety.py # 并发写入与旧执行代次栅栏
└─ test_nlp.py                  # 分段、关键词、排序与补位

scripts/
├─ acceptance.ps1               # 依赖公开 HTTP 契约的 Windows smoke
├─ acceptance.sh                # 同等 Bash smoke
├─ test-deploy-auth.sh          # 部署鉴权契约
└─ test-start-frameflow-contract.ps1 # 启动器能力探测契约
```

测试不应共用开发数据库。每个 API 集成测试使用临时 `FRAMEFLOW_DATA_DIR`，故障注入在测试结束时恢复 `none`。

## 7. 文档优先级与真实性

- `PROJECT_SPEC.md`：实现合同，比说明性文档优先级更高。
- `PRD.md`：定义为什么做、做什么和什么不做。
- `API.md` / `DATA_MODEL.md`：前后端与持久化契约。
- `ARCHITECTURE.md`：解释系统分解与决策。
- `TEST_PLAN.md`：证明哪些能力已实测。
- `KNOWN_ISSUES.md`：如实披露尚未实现或未验证的内容。

若文档与运行时 OpenAPI/测试结果不一致，必须在提交前修正文档，不能以“设计上支持”代替真实实现。
