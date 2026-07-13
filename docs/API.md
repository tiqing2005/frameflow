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

`stage`: `validating | extracting | transcribing | segmenting | keywording | matching | persisting | completed`。

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

## 3. 错误契约

```json
{
  "code": "VERSION_CONFLICT",
  "message": "字幕片段已被其他操作更新，请重新加载",
  "retryable": false,
  "request_id": "req_01...",
  "details": {
    "current_version": 3
  }
}
```

| HTTP | 典型 `code` | 语义 |
| ---: | --- | --- |
| 400 | `INVALID_INPUT`, `INVALID_FILE_TYPE` | 请求语义/文件不合法 |
| 404 | `PROJECT_NOT_FOUND`, `JOB_NOT_FOUND`, `ASSET_NOT_FOUND` | 资源不存在 |
| 409 | `VERSION_CONFLICT`, `IDEMPOTENCY_CONFLICT`, `INVALID_STATE` | 版本、幂等请求或状态迁移冲突 |
| 413 | `UPLOAD_TOO_LARGE` | 超过服务端上传限制 |
| 422 | `VALIDATION_ERROR` | Pydantic 字段校验失败，仍尽量包装为统一格式 |
| 500 | `INTERNAL_ERROR` | 未预期服务端错误，不返回堆栈 |
| 503 | `NOT_READY`, `ASR_NOT_CONFIGURED`, `AI_PROVIDER_UNAVAILABLE` | 依赖未就绪或短暂不可用 |

`retryable` 表示服务端对当前错误的建议，不代表客户端应无限自动重试。对有副作用的请求，前端必须结合幂等键或人工确认。

## 4. 健康检查

### GET `/health/live`

证明 API 进程可响应，不代表 Worker 就绪。

```json
{"status":"ok"}
```

### GET `/health/ready`

检查数据库与必要运行依赖；实现可附带 Worker 心跳摘要。

```json
{
  "status": "ready",
  "database": "ok",
  "worker": "ok"
}
```

未就绪返回 HTTP 503 和统一错误体。

## 5. 仪表盘与项目

### GET `/api/v1/dashboard`

返回首页指标、最近项目和最近运行记录。

```json
{
  "metrics": {
    "projects": 3,
    "total_assets": 12,
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

- `version` 必填；与当前版本不同返回 409 `VERSION_CONFLICT`。
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
| `tags` | 是 | 逗号分隔字符串，服务端清理为数组 |
| `keywords` | 是 | 逗号分隔字符串 |

成功返回 HTTP 201 或 200 的 Asset。上传文件使用服务端随机文件名。

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

## 9. 运行与审计

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
  "created_at": "2026-07-13T08:30:03Z"
}
```

默认运行时必须如实显示 `rules` / deterministic strategy，不伪造外部模型名。

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

## 10. 演示故障

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

## 11. 客户端重试策略

- GET 请求可对网络短断做有限自动重试。
- 创建项目仅在保留同一 `Idempotency-Key` 时重试。
- 分段 PATCH 发生 409 时不自动覆盖，而是重新加载并由用户决定。
- 选择 PUT 是幂等 upsert，网络中断时可重发相同 `asset_id`。
- 任务失败重试必须调用显式 `/retry`，不由前端重复创建项目。

数据库约束与删除语义见 `DATA_MODEL.md`，状态机与 Worker 恢复见 `ARCHITECTURE.md`。
