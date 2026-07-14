# FrameFlow AI 数据模型

## 1. 范围与原则

本文描述 Demo 1.0 的实际 SQLite/SQLAlchemy 持久化模型。它服务于五个核心不变量：

1. 项目、任务、片段、推荐和人工选择在刷新/进程重启后仍存在。
2. 每个片段只有一个当前选择，每个候选在同一片段内唯一。
3. 异步处理的真实状态位于数据库，而非 API/Worker 内存。
4. 文生图的 Provider 执行状态与“是否已接受为素材”分开持久化，重启恢复不能静默重复付费调用。
5. 管理员凭据只保存密码哈希，会话只保存令牌哈希；客户端 Cookie 中的原始令牌不落库。

业务聚合主键通常是字符串 ID，客户端必须把其当作不透明值；`FaultControl` 与 `AuthIdentity` 使用整数 singleton 主键，`WorkerHeartbeat` 使用自增整数主键。时间以 UTC 写入。

## 2. 关系概览

```mermaid
erDiagram
    PROJECT ||--|| SOURCE : "has one"
    PROJECT ||--o{ JOB : "processes"
    JOB ||--o{ JOB_EVENT : "emits"
    PROJECT ||--o{ PREVIEW_RENDER : "renders"
    JOB o|--o| PREVIEW_RENDER : "backs"
    PROJECT ||--o{ SEGMENT : "contains"
    PROJECT o|--o{ AI_RUN : "traces"
    JOB o|--o{ AI_RUN : "produces"
    SEGMENT o|--o{ AI_RUN : "may trace"
    SEGMENT ||--o{ RECOMMENDATION : "receives"
    ASSET ||--o{ RECOMMENDATION : "is candidate"
    AI_RUN o|--o{ RECOMMENDATION : "explains"
    SEGMENT ||--o| SELECTION : "has current"
    ASSET ||--o{ SELECTION : "is selected"
    PROJECT o|--o{ IMAGE_GENERATION : "may request"
    SEGMENT o|--o{ IMAGE_GENERATION : "may request"
    ASSET o|--o| IMAGE_GENERATION : "may accept"
    PROJECT o|--o{ AUDIT_EVENT : "audits"
    AUTH_IDENTITY ||--o{ AUTH_SESSION : "authenticates logically"
```

运行控制实体 `IDEMPOTENCY_RECORD`、`FAULT_CONTROL` 和 `WORKER_HEARTBEAT` 不参与业务对象展示，但是幂等、故障演练和 readiness 的证据。`AUTH_IDENTITY` 与 `AUTH_SESSION` 通过规范化 username 形成逻辑关系，当前 schema 没有声明外键；删除或替换身份时必须由认证服务显式清理会话。

## 3. 业务实体

### 3.1 `projects`

| 列 | 类型 | 约束/语义 |
| --- | --- | --- |
| `id` | String(36) | PK，服务端生成 |
| `title` | String(160) | 非空，已去除首尾空白 |
| `status` | String(24) | 非空，索引；`queued/processing/ready/failed/canceled` |
| `input_kind` | String(24) | 非空；`text/audio/video` |
| `created_at` | DateTime(TZ) | 非空 |
| `updated_at` | DateTime(TZ) | 非空，更新时刷新 |

项目是聚合根。删除 Project 级联删除 Source、Job/JobEvent、Segment/Recommendation/Selection、PreviewRender 与项目审计事件；Asset 是全局素材，不随项目删除。ImageGeneration 的数据库外键声明为 `SET NULL`，但当前项目删除服务会先增加执行栅栏，再显式删除全部项目关联的 ImageGeneration 行、私有草稿和 staging；已经接受为全局 Asset 的图片继续保留。

### 3.2 `sources`

| 列 | 类型 | 约束/语义 |
| --- | --- | --- |
| `id` | String(36) | PK |
| `project_id` | FK | 非空、唯一、索引；Project CASCADE |
| `kind` | String(24) | `text/audio/video` |
| `original_filename` | String(255) | 仅元数据，不作存储路径 |
| `storage_path` | Text | 服务端私有路径，不向 API 客户端暴露 |
| `public_url` | Text | 允许客户端使用的媒体 URL |
| `mime_type` | String(160) | 媒体类型 |
| `size_bytes` | Integer | 默认 0，非空 |
| `sha256` | String(64) | 非空；用于追溯/内容摘要 |
| `content` | Text | 文本输入原文；媒体时可空 |
| `transcript_text` | Text | ASR 真实转写结果；未转写时为空 |
| `created_at` | DateTime(TZ) | 非空 |

Project 与 Source 是 1:1。本版不将“同一项目替换输入”纳入范围；如需输入版本，未来应取消唯一约束并增加 `is_current/version`。

### 3.3 `segments`

| 列 | 类型 | 约束/语义 |
| --- | --- | --- |
| `id` | String(36) | PK |
| `project_id` | FK | 非空、索引；Project CASCADE |
| `position` | Integer | 项目内从 0 开始的顺序 |
| `text` | Text | 非空字幕片段 |
| `topic` | String(80) | 非空主题 |
| `keywords_json` | Text | JSON 字符串，默认 `[]` |
| `start_ms` / `end_ms` | Integer | 可空；仅当真实 ASR 有时间戳时写入 |
| `render_duration_ms` | Integer | 可空；画面展示时长覆盖值，与 ASR 时间轴分离；服务层写入时保证非空值为 1000–30000ms 且按 40ms 帧对齐 |
| `version` | Integer | 乐观锁，初始 1；正文、主题、关键词或画面时长变化时递增 |
| `created_at` / `updated_at` | DateTime(TZ) | 非空 |

唯一约束 `UNIQUE(project_id, position)`。重排时不应逐行从旧位置直接改为新位置，否则可触发中间态唯一冲突。实现应在同一事务中先移到临时偏移位置，再写入 0..N-1，并验证 ID 集完全相同。

`start_ms/end_ms` 始终描述源字幕/ASR 时间轴；预览节奏使用 `render_duration_ms`，为空时才回退到自动估算。单段调整、批量分配、正文编辑、重排、素材选择和重新匹配共享时间线写入边界，避免并发更新制造丢失写入或错误预览指纹。

### 3.4 `assets`

| 列 | 类型 | 约束/语义 |
| --- | --- | --- |
| `id` | String(64) | PK；允许稳定 seed ID |
| `name` | String(160) | 非空、索引 |
| `kind` | String(24) | 非空、索引；`image/video` |
| `public_url` | Text | 非空；页面预览使用 |
| `storage_path` | Text | 私有磁盘路径，可空 |
| `thumbnail_url` | Text | 图片自身或视频 poster 的公开预览 URL，可空 |
| `thumbnail_storage_path` | Text | 自有缩略图磁盘路径，可空；删除素材时与源文件一起清理 |
| `thumbnail_mime_type` | String(160) | 缩略图媒体类型，可空 |
| `mime_type` | String(160) | 可空 |
| `size_bytes` | Integer | 默认 0，非空 |
| `tags_json` | Text | JSON 字符串，默认 `[]` |
| `keywords_json` | Text | JSON 字符串，默认 `[]` |
| `tagging_status` | String(24) | 非空、索引；`idle/queued/running/succeeded/degraded` |
| `tagging_source` | String(24) | 最终来源 `vision/text_llm/rules`，可空 |
| `tagging_mode` | String(24) | 活动任务模式 `fill_missing/replace`，完成后清空 |
| `tagging_generation` | Integer | 用户编辑/重新打标栅栏，默认 0 |
| `tagging_attempt` | Integer | 当前 generation 的 Worker 尝试次数，默认 0 |
| `tagging_lease_owner` | String(120) | 当前标签 Worker，可空 |
| `tagging_lease_expires_at` | DateTime(TZ) | 标签任务租约，可空 |
| `tagging_requested_at` / `tagging_started_at` / `tagging_finished_at` | DateTime(TZ) | 标签生命周期时间，可空 |
| `is_seed` | Boolean | 是否为项目预置、授权安全素材 |
| `active` | Boolean | 默认 true，索引；排序与普通搜索仅用 active |
| `created_at` / `updated_at` | DateTime(TZ) | 非空 |

标签任务通过复合索引 `ix_assets_tagging_claim(tagging_status, tagging_requested_at, tagging_lease_expires_at)` 领取。已配置视觉服务时只处理一张归一化画面（视频为 poster/抽帧）；视觉失败后可由文本 LLM 或本地规则产生 `degraded` 结果，AIRun 保存实际 provider/model/source。generation、attempt 与 lease 共同防止旧 Worker 覆盖用户编辑或新一轮标签结果。

`active=false` 支持软禁用并保留推荐追溯；用户上传且未被 Selection 引用的素材也可由服务层物理删除，提交后再清理其受管源文件和 poster。种子素材不可物理删除，被 Selection 引用的 Asset 还有 `RESTRICT` 保护。

### 3.5 `image_generations`

`ImageGeneration` 是可恢复、可审核的文生图草稿；Provider 执行状态与素材库状态正交。`status=succeeded` 只表示私有草稿可审核，`asset_id` 非空才表示已经接受为全局 Asset。

| 列 | 类型 | 约束/语义 |
| --- | --- | --- |
| `id` | String(36) | PK |
| `project_id` | FK nullable | Project 删除时 SET NULL；素材库独立生图可空 |
| `segment_id` | FK nullable | Segment 删除时 SET NULL；片段入口使用 |
| `segment_version` | Integer | 发起时字幕版本；应用图片前做乐观锁校验 |
| `asset_id` | FK nullable | 接受后关联 Asset，`SET NULL`、唯一、索引 |
| `source` | String(32) | `library/segment_shortfall`，标识创建入口 |
| `prompt` / `effective_prompt` | Text | 用户提示词与加上画幅约束后的实际请求文本 |
| `name` | String(160) | 接受为素材时的默认名称 |
| `aspect_ratio` | String(16) | 画幅，默认 `16:9` |
| `provider` / `model` | String | 实际图像服务与模型标识 |
| `status` | String(24) | 索引；`queued/running/succeeded/failed/canceled` |
| `attempt` / `execution_generation` | Integer | Provider 调用次数与执行代次栅栏 |
| `max_attempts` | Integer | 自动恢复/人工重试上限；服务层另有 4 次硬上限 |
| `next_run_at` | DateTime(TZ) | 最早可领取时间 |
| `lease_owner` / `lease_expires_at` | String/DateTime | 独立 Image Worker 租约 |
| `output_storage_path` | Text | 未接受 PNG 的私有路径，可空；不作为公开 URL 返回 |
| `output_mime_type` / `output_size_bytes` / `output_sha256` | String/Integer/String | 归一化输出的类型、字节数与摘要 |
| `error_code` / `error_message` / `retryable` | String/Text/Boolean | 失败分类、用户消息和人工重试资格 |
| `auto_import` / `auto_select` | Boolean | 创建请求显式授权的自动入库/片段应用意图 |
| `idempotency_key` | String(200) | 可空、唯一；相同意图重放复用原任务 |
| `request_hash` | String(64) | 规范化创建意图摘要，防止同 Key 不同请求 |
| `created_at` / `started_at` / `finished_at` / `updated_at` | DateTime(TZ) | Provider 任务生命周期 |
| `accepted_at` / `discarded_at` | DateTime(TZ) | 入库或丢弃时间，可空且互斥 |
| `expires_at` | DateTime(TZ) | 未接受草稿的清理期限 |

复合索引 `ix_image_generations_claim(status, next_run_at, lease_expires_at)` 服务于原子领取和过期恢复。Image Worker 在外部调用前后写入私有 `submitted/result/ready` staging 屏障；能恢复完整 ready PNG 时不再调用 Provider，只有 submitted 且结果未知时标为 `IMAGE_PROVIDER_RESULT_UNKNOWN`，禁止自动重发。旧 execution generation、取消后的迟到响应和失去租约的 Worker 均不能覆盖当前状态。

接受草稿时在同一数据库写边界内创建 Asset、写入 `asset_id/accepted_at`、排队 Asset 标签任务，并在用户显式要求且 `segment_version` 仍匹配时 upsert `Selection(source=generated)`。文件复制在事务钩子中配合回滚/提交清理；接受重放复用同一 Asset。

## 4. 任务与恢复实体

### 4.1 `jobs`

| 列 | 类型 | 约束/语义 |
| --- | --- | --- |
| `id` | String(36) | PK |
| `project_id` | FK | 非空、索引；Project CASCADE |
| `kind` | String(24) | 非空、索引；`pipeline/preview` 等任务类型 |
| `status` | String(24) | 非空、索引；`queued/running/succeeded/failed/canceled` |
| `stage` | String(32) | 非空；默认 `validating` |
| `progress` | Integer | 0～100，单调递增 |
| `attempt` | Integer | 当前尝试次数，默认 0 |
| `execution_generation` | Integer | 每次领取递增的执行栅栏，旧执行结果不得落库 |
| `max_attempts` | Integer | 默认 3 |
| `next_run_at` | DateTime(TZ) | Worker 最早可领取时间 |
| `lease_owner` | String(120) | 当前 Worker ID，可空 |
| `lease_expires_at` | DateTime(TZ) | 租约过期时间，可空 |
| `heartbeat_at` | DateTime(TZ) | 当前任务最后心跳，可空 |
| `error_code` / `error_message` | String/Text | 失败终态的机器/人类可读原因 |
| `retryable` | Boolean | 默认 false；人工重试的服务端判断 |
| `created_at` / `started_at` / `finished_at` / `updated_at` | DateTime(TZ) | 生命周期时间 |

复合索引 `ix_jobs_claim(status, next_run_at, lease_expires_at)` 服务于 Worker 领取和过期恢复。

状态不变量：

- 只有 queued 可被领取为 running。
- succeeded/failed/canceled 是终态；“重试”是从 failed 显式恢复 queued，不修改原历史事件。
- running 任务只有租约所有者可续租/写入阶段。
- 过期租约可恢复为 queued 或在达到 `max_attempts` 后 failed，并追加事件。
- 业务流水线必须幂等，因为 Worker 可在不确定的崩溃点后重执行。

### 4.2 `job_events`

| 列 | 类型 | 语义 |
| --- | --- | --- |
| `id` | String(36) | PK |
| `job_id` | FK | 非空、索引；Job CASCADE |
| `stage` | String(32) | 写入时所处阶段 |
| `progress` | Integer | 当时持久化进度 |
| `message` | Text | 面向用户/调试的简短消息 |
| `level` | String(16) | `info/warning/error` |
| `created_at` | DateTime(TZ) | 非空 |

JobEvent 是只追加时间线，不使用 UPDATE 重写历史。当前状态以 Job 为准，事件用于解释过程。

### 4.3 `worker_heartbeats`

| 列 | 类型 | 语义 |
| --- | --- | --- |
| `id` | Integer | 自增主键，兼容旧版 singleton 行 |
| `worker_id` | String(120) | 唯一 Worker 标识并建立索引 |
| `heartbeat_at` | DateTime(TZ) | 最近心跳 |
| `operational_state` | String(24) | `ready/isolated`，用于聚合可用容量 |
| `status_detail` | Text | 隔离原因，可空 |

每个 Worker 独立更新自己的心跳行。Readiness 聚合在线、繁忙、隔离和可用 Worker，并返回所有活动 Job ID；过期行不计入在线容量。

### 4.4 `preview_renders`

| 列 | 类型 | 约束/语义 |
| --- | --- | --- |
| `id` | String(36) | PK |
| `project_id` | FK | 非空、索引；Project CASCADE |
| `job_id` | FK nullable | Job 删除时 SET NULL；唯一、索引，每个预览至多绑定一个 Job |
| `input_hash` | String(64) | 非空、索引；字幕顺序、文本版本、素材选择和节奏的稳定指纹 |
| `status` | String(24) | 索引；`queued/succeeded/failed/canceled`，执行中的细粒度状态读取关联 Job |
| `output_url` | Text | 成功后可播放的公开媒体 URL，可空 |
| `storage_path` | Text | 服务端输出文件路径，可空 |
| `duration_ms` | Integer | 实际计划/渲染总时长，默认 0 |
| `segment_count` | Integer | 本次预览包含的片段数，默认 0 |
| `error_message` | Text | 渲染失败摘要，可空 |
| `created_at` / `updated_at` | DateTime(TZ) | 非空 |

唯一约束 `UNIQUE(project_id, input_hash)` 使相同输入可以复用活动任务或有效结果。创建预览时先持久化 PreviewRender 和 `Job(kind=preview)`；ffmpeg 在事务外渲染，文件完成后才在短事务中写 `output_url/storage_path` 并把双方置为成功。字幕、素材选择或节奏变化会产生新指纹，前端把旧结果标为过期，但不会自动消耗资源重渲染。

## 5. 匹配、选择与追溯

### 5.1 `ai_runs`

表名为历史产品概念，它记录“智能/匹配运行”，不意味着每行都是外部 AI API 调用。默认规则流水线应如实写 `provider=rules`。

| 列 | 类型 | 语义 |
| --- | --- | --- |
| `id` | String(36) | PK |
| `project_id` | FK nullable | Project 删除时 SET NULL |
| `job_id` | FK nullable | Job 删除时 SET NULL |
| `segment_id` | FK nullable | 局部重匹配时可指向 Segment；删除 SET NULL |
| `operation` | String(64) | 例如 `segment_and_match` / `rematch_segment` |
| `provider` | String(80) | 默认真实值 `rules` |
| `model` | String(120) | 例如 `hybrid-tfidf-v1`；规则/策略名也放此字段 |
| `prompt_version` | String(40) | 对规则流水线表示输入/策略版本 |
| `input_hash` | String(64) | 非空；追溯而不无限复制原文 |
| `status` | String(24) | `succeeded/failed` 等 |
| `degraded` | Boolean | 是否在本运行使用降级路径 |
| `duration_ms` | Integer | 运行耗时 |
| `output_summary_json` | Text | JSON 结果摘要，默认 `{}` |
| `error_message` | Text | 可空；不包含 Key/请求 Header |
| `created_at` | DateTime(TZ) | 非空 |

Demo 1.0 不单独建 `match_runs`表；一次混合匹配的策略版本和结果摘要统一存在 AIRun，具体候选存在 Recommendation。

### 5.2 `recommendations`

| 列 | 类型 | 语义 |
| --- | --- | --- |
| `id` | String(36) | PK |
| `run_id` | FK nullable | AIRun SET NULL；运行删除不破坏当前候选 |
| `segment_id` | FK | 非空、索引；Segment CASCADE |
| `asset_id` | FK | 非空、索引；Asset CASCADE（实际素材优先软禁用） |
| `rank` | Integer | 1 开始的展示排名 |
| `total_score` | Float | 混合分，0～1 |
| `tfidf_score` | Float | 历史兼容字段：默认保存字符 n-gram TF-IDF 余弦相似度；启用本地或远程 Embedding 时保存该语义通道的向量余弦相似度，实际来源以关联 AIRun 的 provider/model 为准 |
| `keyword_score` | Float | 归一化关键词重合 |
| `tag_score` | Float | 标签/主题重合 |
| `matched_terms_json` | Text | JSON 命中词数组 |
| `explanation` | Text | 非空中文解释 |
| `is_diversity_filler` | Boolean | 低相关候选补齐标识 |
| `created_at` | DateTime(TZ) | 非空 |

唯一约束：

- `UNIQUE(segment_id, asset_id)`：同一片段不重复推荐素材。
- `UNIQUE(segment_id, rank)`：同一片段不出现重复排名。

重新匹配应在一个事务中删除/替换当前片段候选并插入新集合。不应先提交空候选，再逐个增加。

### 5.3 `selections`

| 列 | 类型 | 语义 |
| --- | --- | --- |
| `id` | String(36) | PK |
| `segment_id` | FK | 非空、唯一、索引；Segment CASCADE |
| `asset_id` | FK | 非空、索引；Asset RESTRICT |
| `source` | String(16) | `auto/manual/generated` |
| `created_at` / `updated_at` | DateTime(TZ) | 非空 |

`UNIQUE(segment_id)` 强制每个片段一个当前选择。用户替换是 upsert，保存 `source=manual`；确认文生图并应用时保存 `source=generated`。重新匹配只更新 Recommendation，不删除已有 manual/generated Selection。

### 5.4 `audit_events`

| 列 | 类型 | 语义 |
| --- | --- | --- |
| `id` | String(36) | PK |
| `project_id` | FK nullable | Project CASCADE；全局素材事件可空 |
| `entity_type` | String(48) | `project/job/segment/selection/asset/fault` 等 |
| `entity_id` | String(64) | 目标资源 ID，可空 |
| `action` | String(80) | 索引；稳定动作名，例 `selection.updated` |
| `before_json` / `after_json` | Text | JSON 摘要，可空；不写密钥或无限原文 |
| `actor` | String(48) | 默认 `user`；Worker 可使用 `system/worker` |
| `request_id` | String(80) | 关联 HTTP 日志，可空 |
| `created_at` | DateTime(TZ) | 非空 |

AuditEvent 与 JobEvent 职责不同：JobEvent 解释某个耗时任务怎样执行；AuditEvent 解释资源被谁/什么路径修改。

## 6. 控制与认证实体

### 6.1 `idempotency_records`

| 列 | 类型 | 语义 |
| --- | --- | --- |
| `id` | String(36) | PK |
| `scope` | String(80) | 例 `projects:text` / `projects:upload` |
| `key` | String(200) | 客户端幂等键 |
| `request_hash` | String(64) | 规范化请求摘要 |
| `resource_id` | String(36) | 已创建 Project ID |
| `job_id` | String(36) | 已创建 Job ID |
| `created_at` | DateTime(TZ) | 非空 |

`UNIQUE(scope, key)`。创建 Project、Source、Job 和 IdempotencyRecord 必须在一个数据库事务中。命中同 Key 时：

- `request_hash` 相同：返回原 `resource_id/job_id`。
- `request_hash` 不同：409 `IDEMPOTENCY_CONFLICT`，不重用原结果。

### 6.2 `fault_controls`

| 列 | 类型 | 语义 |
| --- | --- | --- |
| `id` | Integer | singleton PK，默认 1 |
| `next_mode` | String(32) | `none/ai_degrade/job_fail` |
| `updated_at` | DateTime(TZ) | 最近设置时间 |

Worker 在领取相关任务时应以事务方式读取并恢复 `none`，确保故障只被一个任务消费。该表是 Demo 验收工具，不是生产 Feature Flag 系统。

### 6.3 `auth_identities`

`AuthIdentity` 保存本地首次初始化产生的单管理员身份；托管环境也可以完全使用不落库的环境凭据。

| 列 | 类型 | 语义 |
| --- | --- | --- |
| `id` | Integer | singleton PK，当前仅支持一个本地工作区管理员 |
| `username` | String(160) | 非空、唯一；登录名 |
| `display_name` | String(160) | 非空；界面显示名称 |
| `password_hash` | String(512) | PBKDF2-SHA256 编码结果；不得保存或返回明文密码 |
| `created_at` / `updated_at` | DateTime(TZ) | 非空 |

本地首次初始化只允许从服务所在机器的回环地址创建该 singleton 身份。托管部署也可完全使用环境变量提供的密码哈希；环境身份不会复制进此表。当前是单管理员 Demo，不包含注册、RBAC、租户或资源归属关系。

### 6.4 `auth_sessions`

`AuthSession` 保存应用内登录会话的服务端状态，不保存浏览器持有的原始 session token。

| 列 | 类型 | 语义 |
| --- | --- | --- |
| `id` | String(36) | PK |
| `username` | String(160) | 非空、索引；与当前身份形成逻辑关联，无数据库 FK |
| `token_hash` | String(64) | 非空、唯一、索引；Cookie 原始随机令牌的 SHA-256 摘要 |
| `csrf_token` | String(96) | 非空；写操作双提交校验使用 |
| `expires_at` | DateTime(TZ) | 非空、索引；过期会话不可继续使用 |
| `last_seen_at` | DateTime(TZ) | 最近一次有效访问时间 |
| `created_at` | DateTime(TZ) | 非空 |

登录成功只把随机原始 session token 写入 HttpOnly Cookie，数据库保存 `token_hash`。退出会删除当前会话；查询会话时同时检查到期时间。CSRF token 可由会话接口返回给同源前端，但不得替代 HttpOnly 会话 Cookie。该设计只覆盖单实例演示会话，不声称具备集中式 SSO 或分布式会话能力。

## 7. JSON 字段规则

为保持 SQLite 便携性，标签、关键词、命中词和摘要使用 Text 存储规范 JSON。边界规则：

- ORM 层不把 JSON 字符串直接返回前端；响应必须是数组/对象。
- 写入前去除空白、空项和重复项，并限制数量/单项长度。
- JSON 解析失败时按数据完整性错误处理并留下 request/job ID，不静默返回伪造默认值。
- 未来迁移 PostgreSQL 时可转为 JSONB，但不改变 API 契约。

## 8. 关键事务边界

### 8.1 创建项目

```text
BEGIN
  validate/claim idempotency key
  INSERT project
  INSERT source
  INSERT job(status=queued)
  INSERT initial job_event
  INSERT idempotency_record
  INSERT audit_event
COMMIT
```

任一步失败时不得留下无 Job 的 Project 或无 Project 的 IdempotencyRecord。

### 8.2 Worker 完成处理

耗时计算可在事务外完成；最终落库需在短事务中：

```text
BEGIN
  replace/create segments idempotently
  replace recommendations for affected segments
  upsert automatic selections only when no manual selection exists
  insert ai_run + audit summary
  set project=ready
  set job=succeeded, stage=completed, progress=100
  append final job_event
COMMIT
```

不应在事务中等待 ASR/LLM/ffmpeg 等外部耗时操作。

### 8.3 人工选择

```text
BEGIN
  validate segment and active asset
  read before selection
  upsert selection(source=manual)
  insert audit_event(before, after)
COMMIT
```

相同选择重放应返回当前结果，不创建第二行。

### 8.4 文生图接受与片段应用

```text
BEGIN WRITE LOCK
  validate ImageGeneration=succeeded and private draft exists
  validate optional Segment version and active state
  create Asset once and enqueue asset tagging
  set image_generation.asset_id + accepted_at
  optional upsert selection(source=generated)
  insert audit_event
  register file cleanup for COMMIT/ROLLBACK
COMMIT
```

同一 generation 的重复接受必须返回同一 Asset。片段版本冲突时不得覆盖当前 Selection；调用方可以改为“仅入库”完成 Asset 创建。数据库提交成功后才清理私有草稿和 staging；回滚时清理本事务创建的公开素材文件。

### 8.5 预览渲染完成

ffmpeg 计划与文件渲染在事务外执行。只有输出文件完整存在且当前 `Job.execution_generation` 仍匹配时，Worker 才在短事务中同时更新 PreviewRender、Job、AIRun 与事件；失去租约或旧代次的迟到结果只能清理自己的文件，不能覆盖新任务。

## 9. SQLite 运行设置

连接建立后设置：

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=10000;
PRAGMA synchronous=NORMAL;
```

同时使用 SQLAlchemy `pool_pre_ping=True`。这些设置只是为单机 API + 有界 Worker 池提高可靠性，不使 SQLite 变成跨机分布式数据库。

## 10. 初始化、迁移与备份

- Demo 1.0 启动时由 SQLAlchemy metadata 创建缺失的表，再幂等 seed 本地素材。
- 本方式适合一次性 Demo 初始化，但不支持可审核的破坏性 schema 升级。正式长期运行前应引入 Alembic 版本迁移。
- 备份必须覆盖整个业务数据集：`frameflow.db`、`media/` 与 `private/`；可排除能重新下载的模型/缓存，但仅复制 DB 会留下失效媒体、源文件或未接受生图草稿引用。
- 在 WAL 模式下应使用 SQLite online backup API 或在受控停写窗口备份，不应只拷贝主 DB 文件而忽略 WAL。
- 数据保留与上传配额尚未作为多租户产品实现，见 `KNOWN_ISSUES.md`。

## 11. 生产迁移路线

| Demo 1.0 | 生产方向 | 不变契约 |
| --- | --- | --- |
| SQLite | PostgreSQL | Project/Job/Segment 状态和 ID 语义 |
| DB 持久队列 | PostgreSQL outbox + Redis/RabbitMQ | 创建事务原子性、幂等键 |
| 本地文件 | S3/R2/OSS | Source/Asset 的公开 URL 契约 |
| Text JSON | JSONB/关系标签表 | API 数组格式 |
| 内存 TF-IDF | 向量索引 + 关键词候选融合 | Recommendation 分项证据与解释 |

迁移不能破坏人工 Selection 优先、推荐可追溯和任务幂等三个核心不变量。
