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
│     └─ ci.yml                 # 后端测试、前端 lint/build、合同 smoke
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
├─ docs/                              # PRD、架构、API、测试、演示与风险文档
├─ scripts/
│  ├─ acceptance.ps1             # Windows 可重复 API 验收
│  └─ acceptance.sh              # Linux/macOS/Git Bash 可重复 API 验收
├─ .env.example                       # 项目级配置模板
├─ PROJECT_SPEC.md                    # 项目实现合同，设计不可与其冲突
├─ LICENSE
└─ README.md                          # 交付入口，引用 docs 中的详细证据
```

`backend/data/`、`frontend/node_modules/`、`frontend/dist/`、本地 `.env` 和测试临时数据不应提交。

## 3. 后端结构

当前实现围绕以下文件/责任组织。如最终为减少文件数合并了小模块，不应合并其责任边界。

```text
backend/app/
├─ __init__.py
├─ main.py          # 应用工厂/生命周期、CORS、路由注册、静态 SPA 托管
├─ config.py        # Settings；仅从环境变量建立运行配置
├─ db.py            # Engine/Session、SQLite PRAGMA、初始化与 seed
├─ models.py        # SQLAlchemy 2 持久化模型和约束
├─ schemas.py       # Pydantic 请求校验模型
├─ errors.py        # 领域错误、统一 HTTP 错误包装和 request_id
├─ nlp.py           # 规则分段、关键词、n-gram TF-IDF 混合排序
├─ asr.py           # ASR Provider 边界；未配置时返回真实错误
├─ seed.py          # 幂等创建至少 12 个本地授权安全素材
├─ services.py      # 项目、素材、编辑、选择、审计等用例（可按规模分文件）
├─ pipeline.py      # 任务阶段编排和事务化结果换版
├─ worker.py        # 原子领取、租约/心跳、执行、恢复和优雅停机
└─ api.py/routes/   # /api/v1 路由（可在 main.py 中集中，也可按资源分组）
```

### 3.1 依赖方向

```text
HTTP routes
    ↓
request schemas + domain services
    ↓
models / db / nlp / provider boundaries

worker
    ↓
pipeline + same domain services
    ↓
models / db / nlp / provider boundaries
```

禁止方向：

- `models.py` 不导入 FastAPI Request/Response。
- `nlp.py` 不自行开启 HTTP 请求或读写数据库。
- Worker 不通过调用本项目 HTTP API 执行业务，而是复用服务层。
- 路由不在请求线程内执行转写/混合匹配的完整流程。

### 3.2 开发和运行入口

```powershell
# API
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# Worker（独立终端）
cd backend
python -m app.worker
```

API 和 Worker 必须使用相同的 `FRAMEFLOW_DATA_DIR` 与 `FRAMEFLOW_DATABASE_URL`。

## 4. 前端结构

```text
frontend/src/
├─ main.tsx                   # React 根节点
├─ App.tsx                    # 应用 Shell、导航与路由分发
├─ router.ts                  # 轻量客户端路由解析/导航
├─ api.ts                     # 唯一 fetch 边界、ApiError、资源 API
├─ types.ts                   # 与 API 契约对齐的 TypeScript 类型
├─ pages/
│  ├─ DashboardPage.tsx       # 项目台
│  ├─ NewProjectPage.tsx      # 文本/文件新建
│  ├─ ProcessingPage.tsx      # 持久化任务进度、失败与重试
│  ├─ WorkbenchPage.tsx       # 字幕编辑、推荐、搜索替换
│  ├─ AssetsPage.tsx          # 素材库与上传
│  ├─ RunsPage.tsx            # AI/匹配运行记录
│  └─ DemoLabPage.tsx         # 一次性故障注入
├─ components/
│  ├─ ui.tsx                  # 精简的基础组件/图标、骨架、Toast
│  ├─ SegmentList.tsx         # 如页面过大时抽离
│  └─ CandidateCard.tsx       # 候选解释与选择
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
└─ media/
   ├─ seed/                     # 幂等种子素材
   └─ uploads/
      ├─ sources/              # 项目音频/视频
      └─ assets/               # 用户素材
```

- 数据库和媒体目录必须一起备份和恢复。
- 不将用户原文件名用作磁盘路径。
- seed 可重复执行，不重复插入资源。

## 6. 测试结构

```text
backend/tests/
├─ test_nlp.py                  # 分段、关键词、排序与补位
├─ test_idempotency.py          # 幂等键重放和冲突
├─ test_jobs.py                 # 状态、故障、重试、恢复
└─ test_api_flow.py             # 创建→任务→结果→编辑→选择→重读

scripts/
├─ acceptance.ps1               # 依赖公开 HTTP 契约的 Windows smoke
└─ acceptance.sh                # 同等 Bash smoke
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
