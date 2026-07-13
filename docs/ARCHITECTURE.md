# FrameFlow AI 架构说明

## 1. 目标与边界

FrameFlow AI 是面向招聘实战题的中文字幕到素材匹配工作台。设计目标不是在 2～3 天内复刻完整剪辑软件，而是完成一条可实际操作、可持久化、可解释且可从故障中恢复的业务闭环：

```text
文本/音频/视频输入
  → 持久化异步任务
  → 原始字幕与语义分段
  → 每段至少 3 个可解释候选
  → 人工编辑/排序/替换
  → 刷新后仍可查看最终结果
```

当前 Demo 按“单实例、单 Worker、中小素材库”设计。它不声称支持水平扩容、大文件长视频处理或多租户生产负载。

## 2. 为什么选择 FastAPI + React

- React/Vite 适合快速构建编辑器式交互，且可以在构建后由后端同源托管。
- Python 直接复用 ffmpeg、中文 NLP、TF-IDF、Embedding 与语音识别生态。
- FastAPI 的请求模型、错误契约和 OpenAPI 便于现场讲解与调试。
- 纯 Next.js Serverless 与本地 SQLite、长时任务和持久化上传目录不匹配；为它增加额外 Node Worker 和 Redis 反而扩大交付面。

因此项目采用“模块化单体 + 独立任务执行循环”，而非微服务。

## 3. 系统视图

```text
浏览器（React SPA）
  │ REST/JSON + multipart；同源 /api/v1
  ▼
FastAPI API
  ├─ 参数校验、统一错误、幂等控制
  ├─ 项目/字幕/素材/选择/审计用例
  ├─ SQLite WAL  ←→  持久化 jobs + events
  └─ /data/uploads 与本地素材
                         ▲
独立 Worker 循环 ─────────┘
  ├─ 原子领取、租约/心跳、超时恢复
  ├─ ffmpeg / ASR 适配器
  ├─ 规则分段、关键词提取
  ├─ 混合检索与可解释推荐
  └─ 可选 LLM/Embedding 适配器 + 确定性降级
```

前端通过轮询任务详情获取持久化的阶段、进度和事件。轮询比 SSE/WebSocket 简单，对当前秒级 Demo 任务足够稳定，也更容易经过公网代理。

## 4. 主要模块

| 模块 | 职责 | 明确不负责 |
| --- | --- | --- |
| API | 校验、幂等、资源操作、错误契约 | 不在请求内执行耗时 AI 处理 |
| Job service | 任务状态、事件、重试、取消、租约 | 不将内存状态当作真实数据源 |
| Pipeline | 输入检查、转写、分段、匹配、事务化落库 | 不将演示结果写死 |
| Ranker | 分项打分、候选补齐、理由生成 | 不用不可解释的唯一总分代替证据 |
| Provider adapters | 封装 ASR/LLM/Embedding，限时、结构校验 | 不向前端下发密钥 |
| Audit | 记录输入版本、匹配运行、人工选择与故障 | 不记录 API Key，不无限制复制敏感原文 |

## 5. 数据与持久化模型

实际表名可随 ORM 调整，下列是稳定领域概念。

| 实体 | 核心字段/约束 |
| --- | --- |
| Project | `id, title, status, input_kind, created_at, updated_at` |
| Source | `project_id, storage_path, mime, size, sha256, original_name, content, transcript_text`；服务端生成安全路径 |
| Segment | `project_id, position, text, topic, keywords, version`；位置更新在事务中完成 |
| Asset | `kind, name, storage_key, tags, keywords, search_text, sha256` |
| Job | `type, status, stage, progress, attempts, lease_*, error_*, idempotency_key` |
| JobEvent | `job_id, stage, progress, level, message, created_at`；只追加 |
| AIRun | `project/job/segment, operation, provider, model, prompt_version, input_hash, degraded, output_summary`；默认如实记录 `rules` |
| Recommendation | `run_id, segment_id, asset_id, rank, component_scores, matched_terms, explanation, is_diversity_filler` |
| Selection | `segment_id` 唯一，`asset_id, source(auto/manual), updated_at` |
| AuditEvent | `entity_type/id, action, before/after, request_id, created_at` |

关键不变量：

- 每个分段最多只有一个当前选择，推荐更新不得覆盖人工选择。
- 一个幂等键只对应一次创建结果；重复请求返回已有项目/任务。
- 每次匹配保存策略版本与分项得分，不只保存最终结果。
- 上传原文件名仅作元数据，磁盘路径使用服务端 ID，避免路径穿越。

SQLite 启用 `WAL`、`foreign_keys=ON` 与 `busy_timeout`。当前单 Worker 顺序处理是有意的背压策略：它牺牲吞吐量，换取 Demo 稳定性和可恢复性。

## 6. 任务状态机

```text
queued ──领取+设置租约──> running ──成功──> succeeded
   ▲                            │
   │                            ├─不可重试错误─> failed
   │                            ├─取消检查点──> canceled
   └─租约过期/人工重试────────┘
```

`running` 内部阶段：

```text
validating → extracting → transcribing → segmenting
           → keywording → matching → persisting → completed
```

- 文本输入会跳过 `extracting/transcribing`，但仍会经历多个真实持久化阶段。
- 进度只单调递增，每个阶段写入 `JobEvent`。
- 暂时性外部错误可有限次数自动重试；输入校验错误直接失败。
- Worker 心跳中断后，过期租约任务可在启动恢复阶段重新排队。
- 处理流水线必须幂等：推荐结果在事务中换版，重跑不重复追加分段。

## 7. 字幕分析与混合检索

### 7.1 确定性主干

1. 按中英文句末符号、换行和长度切分。
2. 合并过短片段，二次切分过长片段。
3. 对中文使用关键词/字符 n-gram 提取，对任意新文本均有结果。
4. 可配置 LLM 仅作语义增强；超时、格式不合法或无密钥时回退到规则主干。

### 7.2 匹配公式

```text
0.55 × 中文字符 n-gram TF-IDF 余弦相似度
+ 0.30 × 归一化关键词重合度
+ 0.15 × 标签/主题重合度
```

对当前 12～数十个素材，在内存中计算余弦相似度比引入向量数据库更可靠。每个推荐保存三个分项得分、命中词、解释和是否为低相关补位。当高相关候选不足 3 个时，会补齐但必须如实标识，不把补位伪装成高相关。

## 8. API 契约

所有业务端点位于 `/api/v1`；健康检查是 `/health/live` 与 `/health/ready`。

| 分组 | 端点 | 主要语义 |
| --- | --- | --- |
| 项目 | `GET /dashboard`, `GET /projects`, `GET /projects/{id}` | 项目列表、聚合详情和进度 |
| 创建 | `POST /projects/text`, `POST /projects/upload` | 事务内创建项目和任务，返回 202 |
| 任务 | `GET /jobs/{id}`, `POST /jobs/{id}/retry`, `POST /jobs/{id}/cancel` | 进度/事件、重试、取消 |
| 字幕 | `PATCH /segments/{id}`, `PUT /projects/{id}/segments/order` | 带版本编辑与事务化排序 |
| 匹配 | `POST /segments/{id}/rematch`, `PUT /segments/{id}/selection` | 局部重算、幂等保存最终选择 |
| 素材 | `GET /assets`, `POST /assets`, `PATCH /assets/{id}` | 搜索/筛选、上传、编辑元数据 |
| 追溯 | `GET /runs`, `GET /audit?project_id=` | AI/匹配运行和人工操作记录 |
| 演示 | `POST /demo/faults/next` | 仅演示模式下注入一次性故障 |

统一错误：

```json
{
  "code": "AI_PROVIDER_UNAVAILABLE",
  "message": "AI 服务暂时不可用",
  "retryable": true,
  "request_id": "req_...",
  "details": {}
}
```

`Idempotency-Key` 用于创建类请求；分段编辑使用 `version` 防止丢失更新。具体返回 JSON 形状以运行时 OpenAPI 为准，验收脚本只依赖稳定的状态码和主键，不耦合页面内部结构。

## 9. 故障模型与审计

演示实验室仅在明确的 Demo 模式下提供：

- `ai_degrade`：下一次 AI 增强失败，规则引擎接管；任务可成功，但运行记录必须标记降级。
- `job_fail`：下一次处理任务失败；页面显示原因和可重试性，再次执行不继承一次性故障。
- `none`：清除尚未消费的故障注入。

故障注入不等于真实 AI 调用。它的目的是使重试和降级路径可重复验收。真实 Provider 是否配置、哪些路径使用规则实现，应在 `AI_USAGE.md` 和 `KNOWN_ISSUES.md` 中如实说明。

## 10. 安全与运行约束

- API Key 只由后端环境变量读取，不进前端包、数据库和日志。
- 公网 Demo 需要最小访问控制、上传大小限制和请求配额；当前不作多租户隔离承诺。
- 上传应检查扩展名、MIME 与实际类型，并使用随机存储名。
- 日志使用 `request_id` 与 `job_id` 关联，面试现场可从 UI 事件追到服务端。
- `/health/live` 只表示进程存活；`/health/ready` 应反映数据库与必要依赖是否就绪。

## 11. 部署拓扑

本地可运行 API 和 Worker 两个进程，共享数据目录。公网 Demo 可使用单容器进程监督器同时启动两者，或在单机 Docker Compose 中分成 web/worker；两者都必须挂载同一持久卷：

```text
/data/app.db
/data/uploads/
```

不能把 SQLite 与上传文件放在 Serverless 临时文件系统上。生产扩展路线是 PostgreSQL + 对象存储 + 专用队列，不是为 SQLite 增加多实例共享写入。

## 12. 可观测与取舍

| 决策 | 本版理由 | 扩展方向 |
| --- | --- | --- |
| SQLite WAL | 零外部数据库依赖，易备份与演示 | PostgreSQL |
| DB 持久任务 | 创建业务对象与入队可一个事务完成 | Redis/RabbitMQ + outbox |
| 轮询 | 代理兼容好，实现小而可测 | SSE/WebSocket |
| 内存 TF-IDF | 小素材集确定、快速、可解释 | pgvector/专用向量库 |
| 单 Worker | 降低 SQLite 写竞争与 Demo 资源波动 | 可原子领取的多 Worker |
| 规则主干 | 无密钥/断网时仍有真实结果 | LLM 增强和离线模型 |

这些是显式取舍，不是被隐藏的“未完成生产化”。实际已验证能力与未验证能力请以 `TEST_PLAN.md` 和 `KNOWN_ISSUES.md` 为准。
