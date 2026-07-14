# FrameFlow AI HTTP API 契约

## 1. 约定

| 项目 | 约定 |
| --- | --- |
| 业务前缀 | `/api/v1` |
| 健康检查 | `/health/live`、`/health/ready`（不带 `/api/v1`） |
| 请求/响应 | JSON，上传端点为 `multipart/form-data` |
| 时间 | UTC ISO 8601，例如 `2026-07-13T08:30:00Z` |
| ID | 不透明字符串；客户端不推断 UUID 格式 |
| 创建语义 | 文本/上传项目返回 HTTP 202 + 已持久化 Project/Job |
| 幂等 | 创建接口可携带 `Idempotency-Key` |
| 错误 | 统一为 `{code,message,retryable,request_id,details?}` |

前端只应依赖本文和运行时 OpenAPI 中的公开字段，不应依赖数据库列名或本地文件路径。

## 2. 公共类型

### 2.1 Project

```json
{
  "id": "project-id",
  "title": "智能城市口播",
  "status": "queued",
  "input_kind": "text",
  "segment_count": 0,
  "created_at": "2026-07-13T08:30:00Z",
  "updated_at": "2026-07-13T08:30:00Z"
}
```

`status`: `queued | processing | ready | failed | canceled`。

### 2.2 Job

```json
{
  "id": "job-id",
  "project_id": "project-id",
  "kind": "pipeline",
  "status": "running",
  "stage": "matching",
  "progress": 72,
  "attempt": 1,
  "max_attempts": 3,
  "error_code": null,
  "error_message": null,
  "retryable": false,
  "created_at": "2026-07-13T08:30:00Z",
  "started_at": "2026-07-13T08:30:01Z",
  "finished_at": null
}
```

`status`: `queued | running | succeeded | failed | canceled`。

`kind`: `pipeline | preview`。主处理任务与预览渲染任务共用同一套持久化、租约、重试和事件契约。

主任务 `stage`: `validating | extracting | transcribing | segmenting | keywording | matching | persisting | completed`；预览任务使用 `preview_planning | preview_rendering | completed`。

### 2.3 JobEvent

```json
{
  "id": "event-id",
  "stage": "segmenting",
  "progress": 48,
  "message": "已生成 6 个字幕片段",
  "level": "info",
  "created_at": "2026-07-13T08:30:02Z"
}
```

### 2.4 Asset

```json
{
  "id": "asset-id",
  "name": "数据安全中心",
  "kind": "image",
  "url": "/media/seed/data-security.svg",
  "mime_type": "image/svg+xml",
  "tags": ["数据", "安全", "技术"],
  "keywords": ["网络安全", "风险", "信息"],
  "created_at": "2026-07-13T08:00:00Z",
  "updated_at": "2026-07-13T08:00:00Z"
}
```

`kind`: `image | video`。客户端应使用响应中的公开 URL，不使用 `storage_path`。

### 2.5 Segment / Recommendation / Selection

```json
{
  "id": "segment-id",
  "project_id": "project-id",
  "position": 0,
  "text": "智能城市的价值不只是更多传感器。",
  "topic": "智能城市",
  "keywords": ["智能城市", "传感器", "技术"],
  "start_ms": null,
  "end_ms": null,
  "version": 1,
  "recommendations": [
    {
      "id": "recommendation-id",
      "segment_id": "segment-id",
      "asset_id": "asset-id",
      "asset": {},
      "rank": 1,
      "total_score": 0.82,
      "tfidf_score": 0.76,
      "keyword_score": 1.0,
      "tag_score": 0.67,
      "matched_terms": ["智能城市", "技术"],
      "explanation": "命中关键词‘智能城市’，并与素材标签‘技术’重合。",
      "is_diversity_filler": false
    }
  ],
  "selection": {
    "segment_id": "segment-id",
    "asset_id": "asset-id",
    "source": "auto",
    "asset": {},
    "updated_at": "2026-07-13T08:30:03Z"
  }
}
```

分数在 `[0,1]` 范围内。`asset` 为 Asset 嵌套对象；示例中用空对象缩写。`selection.source`: `auto | manual`。

### 2.6 列表响应

```json
{
  "items": [],
  "total": 0
}
```

当前 Demo 的列表体量小，未承诺稳定的分页游标契约。

### 2.7 Timeline / PreviewRender

```json
{
  "project_id": "project-id",
  "input_hash": "sha256...",
  "segment_count": 2,
  "duration_ms": 6000,
  "items": [
    {
      "segment_id": "segment-id",
      "position": 0,
      "text": "智能城市让公共服务更高效。",
      "topic": "智能城市",
      "start_ms": 0,
      "end_ms": 3000,
      "duration_ms": 3000,
      "asset": {}
    }
  ]
}
```

时间线只返回公开素材字段，不返回磁盘 `storage_path`。文本项目按字幕长度计算确定性片段时长；具有有效时间戳的字幕优先使用时间戳并限制在安全范围内。

```json
{
  "id": "preview-id",
  "project_id": "project-id",
  "job_id": "job-id",
  "input_hash": "sha256...",
  "status": "queued",
  "output_url": null,
  "duration_ms": 6000,
  "segment_count": 2,
  "error_message": null,
  "job": {},
  "created_at": "2026-07-14T08:30:00Z",
  "updated_at": "2026-07-14T08:30:00Z"
}
```

## 3. 错误契约

```json
{
  "code": "SEGMENT_VERSION_CONFLICT",
  "message": "字幕片段已被其他操作更新，请重新加载",
  "retryable": false,
  "request_id": "req_01...",
  "details": {
    "expected": 3,
    "received": 2
  }
}
```

| HTTP | 典型 `code` | 语义 |
| ---: | --- | --- |
| 400 | `INVALID_INPUT`, `INVALID_FILE_TYPE` | 请求语义/文件不合法 |
| 404 | `PROJECT_NOT_FOUND`, `JOB_NOT_FOUND`, `ASSET_NOT_FOUND` | 资源不存在 |
| 409 | `SEGMENT_VERSION_CONFLICT`, `IDEMPOTENCY_CONFLICT`, `INVALID_STATE` | 版本、幂等请求或状态迁移冲突 |
| 409 | `ASSET_IN_USE`, `MINIMUM_ASSET_GUARD` | 素材仍被片段使用，或停用后会低于 3 个启用素材 |
| 413 | `UPLOAD_TOO_LARGE` | 超过服务端上传限制 |
| 422 | `VALIDATION_ERROR` | Pydantic 字段校验失败，仍尽量包装为统一格式 |
| 429 | `RATE_LIMITED` | 超过当前客户端的读/写请求速率限制 |
| 500 | `INTERNAL_ERROR` | 未预期服务端错误，不返回堆栈 |
| 503 | `NOT_READY`, `ASR_NOT_CONFIGURED`, `AI_PROVIDER_UNAVAILABLE` | 依赖未就绪或短暂不可用 |

`retryable` 表示服务端对当前错误的建议，不代表客户端应无限自动重试。对有副作用的请求，前端必须结合幂等键或人工确认。

429 响应同时带 `Retry-After` 与 `X-Request-ID`。单机演示默认读请求 240/min、写请求 60/min；健康检查不计入限流。

## 4. 健康检查

### GET `/health/live`

证明 API 进程可响应，不代表 Worker 就绪。

```json
{"status":"ok"}
```

### GET `/health/ready`

检查数据库、种子素材与 Worker 心跳。

```json
{
  "status": "ready",
  "checks": {
    "database": "ok",
    "seed_assets": {
      "ok": true,
      "count": 12
    },
    "worker": {
      "online": true,
      "state": "idle",
      "accepting_jobs": true,
      "detail": null,
      "last_heartbeat": "2026-07-13T08:30:00Z",
      "online_workers": 2,
      "active_job_ids": [],
      "capacity": {
        "configured": 2,
        "online": 2,
        "accepting": 2,
        "busy": 0,
        "available": 2
      },
      "instances": []
    }
  }
}
```

未就绪返回 HTTP 503 和统一错误体。部分 Worker 因不可终止的超时线程进入隔离状态时，聚合状态为 `degraded`，其余 Worker 仍可接单；所有在线 Worker 都隔离时状态为 `isolated` 并返回 503。实例详情和活动任务分别见 `instances`、`active_job_ids`。

## 5. 仪表盘与项目

### GET `/api/v1/dashboard`

返回首页指标、最近项目和最近运行记录。

```json
{
  "metrics": {
    "projects": 3,
    "total_assets": 12,
    "queued_jobs": 2,
    "running_jobs": 1,
    "failed_jobs": 0
  },
  "recent_projects": [],
  "recent_runs": []
}
```

### GET `/api/v1/projects`

返回 `{items,total}`，默认按创建/更新时间倒序。

### POST `/api/v1/projects/text`

Headers：

```http
Content-Type: application/json
Idempotency-Key: demo-20260713-001
```

Body：

```json
{
  "title": "智能城市演示",
  "text": "智能城市的价值不只是更多传感器。它要让公共交通、能源和安全系统真正协同。"
}
```

校验：`title` 1～160 字符，`text` 2～100,000 字符，两者首尾空白被清理。

Response: HTTP 202

```json
{
  "project": {},
  "job": {}
}
```

相同作用域和 `Idempotency-Key`、且请求体一致时返回原有项目/任务；相同 Key 但请求体不同返回 409 `IDEMPOTENCY_CONFLICT`。

### POST `/api/v1/projects/upload`

`multipart/form-data` fields：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `title` | 是 | 1～160 字符 |
| `file` | 是 | 受支持的音频或视频，默认上限 100 MB |

返回 HTTP 202 `{project,job}`。接收文件与转写成功是两件事；未配置 ASR 时后续任务会如实失败。

### GET `/api/v1/projects/{project_id}`

聚合返回项目、当前任务、输入/字幕摘要、分段及候选/选择和追溯摘要：

```json
{
  "project": {},
  "current_job": {},
  "source": {
    "text": "...",
    "transcript": "...",
    "filename": null
  },
  "segments": [],
  "trace_summary": {
    "degraded": false,
    "ai_runs": 1,
    "audit_events": 4
  }
}
```

在项目未 ready 时 `segments` 可为空，客户端不应将其当成数据丢失。

### DELETE `/api/v1/projects/{project_id}`

删除项目及其从属任务、片段、推荐、选择和审计记录，返回 HTTP 204。公共素材不被删除。运行中项目的删除语义应由 UI 确认并由后端做状态校验。

## 6. 任务

### GET `/api/v1/jobs/{job_id}`

```json
{
  "job": {},
  "events": []
}
```

Events 按时间正序，进度不应倒退。

### POST `/api/v1/jobs/{job_id}/retry`

- 仅 failed 且可重试的任务可重试。
- 增加 `attempt`，清理当前错误并重新排队，保留原事件。
- 无效状态返回 409 `INVALID_STATE`。
- Response 返回更新后的 `{job,events}` 或至少 `{job}`。

### POST `/api/v1/jobs/{job_id}/cancel`

- queued 可立即转 canceled。
- running 设置取消意图并在安全检查点停止；不保证中断不可取消的底层系统调用。
- 终态重复取消可返回当前资源或 409，但不能产生第二个任务。

## 7. 字幕片段与选择

### PATCH `/api/v1/segments/{segment_id}`

Body：

```json
{
  "text": "修改后的字幕文本。",
  "topic": "数据治理",
  "keywords": ["数据", "治理", "信任"],
  "version": 2
}
```

- `version` 必填；与当前版本不同返回 409 `SEGMENT_VERSION_CONFLICT`。
- `text/topic/keywords` 至少提供一个。
- 关键词清理空值、不区分大小写去重，最多 20 个，单项最多 60 字符。
- 成功返回更新后 Segment，`version + 1`，写入 AuditEvent。

### PUT `/api/v1/projects/{project_id}/segments/order`

```json
{
  "segment_ids": ["segment-3", "segment-1", "segment-2"]
}
```

- ID 不得重复，且必须与该项目当前分段集完全一致。
- 在一个数据库事务内更新所有 `position`，不暴露中间重复位置。
- Response 返回 `{"segments": [...]}` 或 Segment 数组；前端应以返回顺序为准。

### POST `/api/v1/segments/{segment_id}/rematch`

- 根据当前文本/主题/关键词重建至少 3 个候选。
- 推荐结果在事务中换版。
- 保留 `source=manual` 的 Selection；如果已选素材仍有效，不转回 auto。
- 返回更新 Segment 或 `{"segment": ...}`。

### PUT `/api/v1/segments/{segment_id}/selection`

```json
{"asset_id":"asset-id"}
```

- Asset 必须存在且 active。
- 幂等 upsert；相同选择重复提交不创建重复记录。
- 用户接口写入 `source=manual`，同时记录 AuditEvent。
- 返回更新 Segment 或 `{"selection": ...}`。

## 8. 素材库

### GET `/api/v1/assets?q=&kind=&tag=`

| 参数 | 语义 |
| --- | --- |
| `q` | 在名称、标签、关键词中检索 |
| `kind` | `image` 或 `video` |
| `tag` | 按单个标签筛选 |

默认只返回 active 素材，响应 `{items,total}`。

### POST `/api/v1/assets`

`multipart/form-data` fields：

| 字段 | 必填 | 格式 |
| --- | --- | --- |
| `file` | 是 | 受支持图片或短视频 |
| `name` | 是 | 1～160 字符 |
| `tags` | 否 | 逗号分隔字符串，服务端清理为数组 |
| `keywords` | 否 | 逗号分隔字符串 |

成功返回 HTTP 201 的 Asset。上传文件使用服务端随机文件名。标签或关键词为空时，服务端会尝试 LLM 自动建议；未配置、超时或输出不合格时回退到确定性关键词提取，并记录 `asset_tagging` AIRun 的实际 provider/model/degraded 状态。

### PATCH `/api/v1/assets/{asset_id}`

```json
{
  "name": "云上数据安全",
  "tags": ["云计算", "安全"],
  "keywords": ["数据保护", "风险管理"],
  "active": true
}
```

至少提供一个字段。禁用已被 Selection 引用的素材时，服务端应拒绝或显式要求替换，不能制造悬空选择。

## 9. 时间线与组合预览

### GET `/api/v1/projects/{project_id}/timeline`

返回当前字幕顺序和最终素材选择组成的公开时间线。项目必须为 `ready`，每个片段必须有有效且磁盘文件存在的素材选择，否则返回 409：

- `PROJECT_NOT_READY`
- `PREVIEW_SEGMENTS_EMPTY`
- `PREVIEW_SELECTION_MISSING`
- `PREVIEW_ASSET_MISSING`

### GET `/api/v1/projects/{project_id}/preview`

返回：

```json
{
  "preview": null,
  "timeline": {}
}
```

已有预览时 `preview` 为 PreviewRender，并嵌套其当前 Job；尚未生成时为 `null`。

### POST `/api/v1/projects/{project_id}/preview`

Body：

```json
{"force": false}
```

Response: HTTP 202

```json
{
  "preview": {},
  "timeline": {},
  "idempotent_replay": false
}
```

服务端根据片段版本、顺序、字幕、素材及片段时长计算 `input_hash`。相同时间线正在渲染或已有有效输出时复用原任务/结果并返回 `idempotent_replay=true`；`force=true` 仅允许在没有活动预览任务时重新生成。总时长超过配置上限返回 422 `PREVIEW_TOO_LONG`。

成功后 `output_url` 指向 `/media/previews/{project_id}/...mp4`。生成能力依赖 ffmpeg；字幕烧录取决于部署镜像中的字幕滤镜和字体支持。

## 10. 运行与审计

### GET `/api/v1/runs`

返回 `{items,total}`。每项至少包含：

```json
{
  "id": "run-id",
  "project_id": "project-id",
  "project_title": "智能城市演示",
  "operation": "segment_and_match",
  "provider": "rules",
  "model": "hybrid-tfidf-v1",
  "status": "succeeded",
  "degraded": false,
  "latency_ms": 41,
  "input_tokens": null,
  "output_tokens": null,
  "total_tokens": null,
  "created_at": "2026-07-13T08:30:03Z"
}
```

默认运行时必须如实显示 `rules` / deterministic strategy，不伪造外部模型名。

Token 用量只使用以下三个顶层字段作为公开 API 契约：

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `input_tokens` | `integer \| null` | Provider 报告的输入 Token 数 |
| `output_tokens` | `integer \| null` | Provider 报告的输出 Token 数 |
| `total_tokens` | `integer \| null` | Provider 报告的总 Token 数；Provider 未单独报告时可由前两项相加得到 |

- 产生 Token 的模型调用返回非负整数；规则任务、Worker 失败记录或 Provider 未报告用量时，三个字段返回 `null`，不能用 `0` 表示“没有 Token 数据”。真实的零用量只有在 Provider 明确报告 `0` 时才返回 `0`。
- 客户端统计只累计 `total_tokens` 非空的运行；详情对 `null` 显示“—”或“未产生 Token”。
- 历史记录可能把相同结构存放在内部持久化字段 `output_summary.tokens`。服务端仅在读取旧记录时兼容该结构，并将其规范化为上述三个顶层字段；`output_summary.tokens` 不是公开响应字段，也不会作为并行 Token 契约继续维护。
- API 不定义 `prompt_tokens`、`completion_tokens`、`tokens` 等同义字段。Provider 原始命名应在服务端适配层完成映射，客户端不得依赖这些名称。

### GET `/api/v1/audit?project_id=`

返回 `{items,total}`；`project_id` 可选。

```json
{
  "id": "audit-id",
  "project_id": "project-id",
  "entity_type": "segment",
  "entity_id": "segment-id",
  "action": "segment.updated",
  "summary": "修改字幕片段",
  "details": {},
  "created_at": "2026-07-13T08:35:00Z"
}
```

审计返回不包含 API Key、Authorization Header、服务端绝对路径或未经限制的全量 Provider 响应。

## 11. 演示故障

### POST `/api/v1/demo/faults/next`

```json
{"mode":"ai_degrade"}
```

`mode`: `ai_degrade | job_fail | none`。

Response：

```json
{
  "mode": "ai_degrade",
  "message": "下一个匹配任务将记录为 AI 降级并使用规则引擎"
}
```

- 故障是一次性的，由下一个相关任务原子消费。
- `ai_degrade` 任务最终成功，运行记录 `degraded=true`。
- `job_fail` 任务最终失败且可重试，重试不再自动继承该一次性故障。
- `none` 清除尚未消费的故障。
- 该接口只应在明确 Demo 环境可用，生产多用户版必须移除或受管理员权限保护。

## 12. 客户端重试策略

- GET 请求可对网络短断做有限自动重试。
- 创建项目仅在保留同一 `Idempotency-Key` 时重试。
- 分段 PATCH 发生 409 时不自动覆盖，而是重新加载并由用户决定。
- 选择 PUT 是幂等 upsert，网络中断时可重发相同 `asset_id`。
- 任务失败重试必须调用显式 `/retry`，不由前端重复创建项目。

数据库约束与删除语义见 `DATA_MODEL.md`，状态机与 Worker 恢复见 `ARCHITECTURE.md`。
