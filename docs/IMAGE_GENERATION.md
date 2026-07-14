# FrameFlow 文生图闭环

## 1. 能力边界

文生图是素材库与字幕工作台的补充能力，不替代原有素材匹配：

- 素材库允许用户用自然语言创建图片草稿，预览后决定入库或丢弃。
- 字幕片段没有合适素材时，可以从片段上下文生成草稿，并在明确操作后入库、应用到该片段。
- `auto_import` / `auto_select` 只代表用户在本次请求中明确授权生成成功后的动作；系统不会因为匹配分数低而静默产生付费请求。
- 图片生成失败不会伪造占位图，也不会让项目主处理任务失败；用户可以查看可重试状态后显式重试。
- 入库后的图片进入现有 Asset 生命周期，并排队执行后台 AI 标签；视觉服务不可用时如实标记文本/规则降级，不把降级结果冒充为 Gemini 或其他视觉模型。

第三方图像网关的模型名、价格、内容政策、数据留存和 SLA 不由 FrameFlow 保证。部署者必须在启用前单独确认。

## 2. 用户流程

### 2.1 素材库生成

1. 用户填写提示词、名称和 `16:9`、`1:1` 或 `9:16` 画幅。
2. API 持久化 `ImageGeneration`，立即返回；Worker 在后台调用图像 Provider。
3. 页面轮询任务详情。成功后通过同源受保护的 `content_url` 预览，不暴露供应商临时 URL。
4. 用户选择“加入素材库”时执行 accept；选择“丢弃”时删除未接受草稿文件。
5. accept 原子创建一个图片 Asset，并触发现有素材标签队列。同一个生成任务并发 accept 只会得到同一个 Asset。

若创建时明确设置 `auto_import=true`，Worker 成功后自动执行第 4 步的入库动作。未接受草稿默认保留 72 小时，之后可安全清理；已入库素材不受草稿保留期影响。

### 2.2 字幕片段生成并应用

1. 工作台通过 `/segments/{segment_id}/image-generations` 创建片段关联任务。
2. 后端保存创建时的 `segment_version`，避免把旧字幕生成结果悄悄应用到已经编辑过的新字幕。
3. 用户可先预览再 accept，也可在请求中显式设置 `auto_import=true, auto_select=true`。
4. 选择应用时必须提交预期片段版本；版本不一致返回 409，图片草稿仍保留，用户刷新后可决定只入库或重新生成。
5. 入库与 Selection 更新在同一数据库事务内完成，失败时不产生半完成选择或孤儿 Asset。

## 3. 状态与幂等

生成状态只有：

```text
queued ──Worker 领取/租约──> running ──有效图片──> succeeded
   ▲                              │
   └────────显式 retry────────────┤
                                  ├─可重试/终止错误──> failed
                                  └─取消检查点────────> canceled
```

`asset_id` 表示草稿已经入库；`discarded_at` 表示未接受草稿已被丢弃。它们与 Provider 执行状态分开，避免把“生成成功但用户未采用”误报为失败。

- 创建请求应携带稳定的 `Idempotency-Key`。同一键与相同请求体重放返回同一个任务；同一键配不同请求体返回 409。
- 片段在首次请求后被编辑，相同键重放仍返回首次任务和原片段版本，不重新解释为一笔新付费请求。
- Provider POST 可能在客户端读超时前已经产生图片或费用。只有 ConnectError/ConnectTimeout 和 HTTP 429 可做有界自动重试；502/503/504、其他 5xx、ReadError、WriteError、RemoteProtocolError 和其他 Timeout 都标记为结果未知，只允许人工确认重试。
- 租约过期后只允许新的执行代次写结果；取消、重试或租约恢复前启动的迟到响应不能覆盖新状态。
- retry 只允许可重试的失败任务；accept、cancel 与 discard 都校验当前状态。
- 单任务总 attempt 硬上限为 4；人工 retry 不能无限抬高 max_attempts。
- 并发 accept 通过数据库唯一约束和事务返回同一个 Asset，不重复写文件、不重复触发标签。
- 已 auto-import 但未选择的任务后续可把现有 Asset 应用到片段，仍必须通过最新 Segment version 校验。

## 4. Provider 契约与文件安全

当前适配器调用服务端配置的 OpenAI-compatible 接口：

```http
POST {IMAGE_API_BASE_URL}/images/generations
Authorization: Bearer <server-only key>
Content-Type: application/json
```

请求固定单张图片，并优先要求 `response_format=b64_json`。首版只信任 `data[0].b64_json`，不从模型响应返回的任意 URL 下载文件，因此不会引入远程图片下载 SSRF。

Provider 响应按以下顺序处理：

1. 在解析 JSON 前限制完整响应体大小。
2. 严格 Base64 解码，并限制解码后字节数。
3. 用图片解码器完整验证格式、宽高、总像素数和静态帧。
4. 应用 EXIF 方向并移除元数据，统一归一化为标准 PNG。
5. 在确定性 `staging/{generation_id}` 中依次原子写 submitted manifest、标准 PNG 和 ready manifest；只有 ready 完整存在才可无 Provider 重放恢复。
6. 从 staging 发布私有草稿；accept 后复制/移动到受管理素材目录，数据库回滚时同步清理本次拥有的文件。
7. 提交后的旧草稿清理为 best effort；磁盘 unlink 失败会记录告警并留给过期清理重试，不得把已经提交成功的 API 响应变成 500。

不得只相信文件扩展名、MIME 或图片魔数。畸形 Base64、非图片、解压炸弹、超大像素和超大响应都必须失败且不落盘。

## 5. 配置

开发环境修改根目录 `.env`，服务器修改权限为 `600` 且不会提交的 `deploy/.env`：

```dotenv
IMAGE_API_BASE_URL=https://image-gateway.example.com/v1
IMAGE_API_KEY=
IMAGE_MODEL=gpt-image-2
IMAGE_API_TIMEOUT=180
IMAGE_MAX_RESPONSE_MB=25
IMAGE_MAX_OUTPUT_MB=15
IMAGE_MAX_PIXELS=24000000
IMAGE_DRAFT_RETENTION_HOURS=72
IMAGE_DAILY_LIMIT=50
IMAGE_MAX_PENDING=5
FRAMEFLOW_STOP_GRACE_PERIOD=240s
```

`IMAGE_API_BASE_URL` 或 `IMAGE_API_KEY` 为空时功能明确不可用，不会继承 `LLM_API_KEY`、`VISION_API_KEY`、`OPENAI_API_KEY` 或其他凭据。`IMAGE_DAILY_LIMIT=0` 表示关闭应用内每日额度，不表示禁用 Provider；公网环境不建议设为 0。

图像生成由 `app.serve` 在核心 Worker 池之外固定监督的 1 个 `app.image_worker` 进程处理，不需要额外暴露端口或启动第二套容器。当前专用服务器继续保持 `FRAMEFLOW_WORKER_CONCURRENCY=1` 和容器 3.5 CPU / 4 GB 上限；外部生图等待不会占用核心 ASR 槽，但两类进程仍共享 CPU、内存、SQLite 和 `/data`，需压测并发生图时的 ASR 延迟与数据库写竞争。Compose 的 `FRAMEFLOW_STOP_GRACE_PERIOD` 必须不小于 `IMAGE_API_TIMEOUT + 30s`，默认使用 `240s`；应用会按同一公式等待受监督 Worker，避免容器先强杀。

## 6. 密钥、费用与内容治理

- 图像 Key 只存在服务端运行时环境，不进入前端包、数据库、日志、审计内容或 Git。
- 聊天、截图、终端历史或公开日志中出现过的 Key 必须在供应商侧撤销并轮换后再部署。
- 每次只生成一张；每日任务数与同时 pending 数均有服务端上限，通用 HTTP 写限流不能替代付费任务配额。
- 自动入库和自动应用必须由本次用户请求显式授权；批量生成上线前还需要数量/费用确认。
- 提示词和生成图片会发送给第三方。敏感字幕、个人信息、未成年人、真人冒充、违法或侵权内容不应提交；部署者需确认供应商内容政策、商业授权和数据保留条款。
- 后台 AI 自动标签只是检索元数据能力，不等同于安全审核。面向匿名公网用户前还需要独立内容审核、租户配额和滥用处置。

## 7. 验收清单

- 配置为空、错误 Key、429、5xx、连接失败、读超时均返回统一错误且不泄露网关详情。
- 正常 Base64、错误 Base64、空 data、非图、超大响应、超大输出和超大像素均有确定性测试。
- 相同幂等键重放不增加任务；不同请求体冲突返回 409。
- Worker 租约过期可恢复；旧执行代次的迟到响应不能落盘或改写终态。
- Provider 返回且 ready staging 已落盘、数据库写失败或 Worker 重启时，恢复过程复用同一 PNG，Provider 调用总数仍为 1。
- 只有 submitted manifest、没有 ready 时视为外部结果未知，停止自动处理并要求人工确认。
- succeeded + auto-import + asset_id 为空的进程窗口会由 Worker 继续入库，不再次调用 Provider。
- queued/running 可取消；失败任务按 `retryable` 显式重试，尝试次数有界。
- accept 并发重放只产生一个 Asset；accept 与 discard 竞态只有一个合法终态。
- 已入库任务后续 select 可成功；旧 Segment version 仍返回受控 409。
- after_commit 文件清理失败不把已提交事务变成 500，后续清理仍可重试。
- attempt 达到 4 后人工 retry 返回 `IMAGE_RETRY_LIMIT_REACHED`。
- 字幕版本冲突不覆盖新字幕选择；重新加载后可以只入库或重新生成。
- 数据库提交失败、磁盘写失败和素材创建失败均不留下孤儿文件/记录。
- 入库成功后，视觉标签成功路径和视觉不可用时的文本/规则降级路径都如实记录。
- 容器重启后任务和草稿仍可读，备份恢复后已接受素材仍可访问。
- 真实 Provider 验收只在人工受控环境执行，不进入默认 CI，也不把 Key、真实网关或付费响应提交仓库。
