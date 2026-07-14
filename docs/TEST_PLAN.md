# FrameFlow AI 测试计划

## 1. 目标

本计划验证的不是“页面看起来像完成”，而是原题最关键的可观察事实：

- 新输入能进入真实持久化异步任务，并通过多个服务端阶段。
- 处理后的每个片段有关键词/主题、至少 3 个唯一候选和可读理由。
- 人工文本编辑、顺序与素材选择经刷新/API 重读不丢。
- 幂等、版本冲突、失败、重试、取消与 Worker 恢复路径可重复。
- 素材、运行记录和审计记录是真实数据，不依赖前端写死结果。
- 自动标签、向量混排、时间线和预览渲染记录实际 provider/model/输入版本与降级状态。
- 文生图从幂等创建、异步生成、草稿预览到入库/片段应用均可追溯，付费 Provider 不在默认测试中被调用。
- 预览任务的重复请求、并发请求、超时、失败和重试不会产生孤儿任务或覆盖错误结果。
- 无 ASR/LLM 时系统如实失败或降级，不伪造外部 AI 调用。

## 2. 测试层次

| 层次 | 目的 | 工具 | 是否必须 |
| --- | --- | --- | --- |
| 纯函数单元 | 证明分段、关键词、混排和补位确定性 | pytest | 是 |
| 数据库/服务集成 | 验证约束、状态迁移、幂等和持久化 | pytest + 临时 SQLite | 是 |
| HTTP API 契约 | 验证状态码、错误体和主业务流 | pytest/httpx | 是 |
| 前端静态门禁 | TypeScript/build/lint 通过 | tsc/Vite/Oxlint | 是 |
| 运行时 smoke | 对真实 API + Worker 跑健康、seed、幂等创建和终态 | `scripts/acceptance.*` | 是 |
| 浏览器业务流 | 证明编辑/替换/刷新及响应式体验 | 手工；可选 Playwright | 是（手工也需留记录） |
| 公网验收 | 证明部署地址、持久卷和反向代理不破坏流程 | 验收脚本 + 手工 | 是 |

## 3. 环境矩阵

| 环境 | 用途 | 数据 | 注意 |
| --- | --- | --- | --- |
| Python 3.11/3.12 + 临时目录 | 单元/API CI | 每次全新 SQLite | 不读开发 DB，不需要 ASR Key |
| Node LTS + npm ci | 前端 CI | 无后端数据 | 使用 lockfile，不提交 node_modules |
| 本地 API + Worker | 集成/smoke/录屏前演练 | 独立本地 data dir | API/Worker 必须指向同一目录 |
| Docker 单实例 + 持久卷 | 部署验收 | 专用 Demo 数据 | 重启后重跑主流程/重读 |
| 公网 HTTPS | 最终提交 | 可重置 Demo 数据 | 启用访问控制/配额，不暴露 Key |

支持的浏览器验收基线：Chrome/Edge 当前稳定版。移动端验收是响应式布局，不承诺原生移动端功能。

## 4. 快速执行命令

### 4.1 后端测试（PowerShell）

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m pytest
```

### 4.2 后端测试（Bash）

```bash
cd backend
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m pytest
```

### 4.3 前端门禁

```bash
cd frontend
npm ci
npm run lint
npm run build
```

### 4.4 本地启动

终端 A：

```powershell
cd backend
$env:FRAMEFLOW_DATA_DIR = "$PWD\data"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

终端 B（使用完全相同环境变量）：

```powershell
cd backend
$env:FRAMEFLOW_DATA_DIR = "$PWD\data"
python -m app.worker
```

终端 C：

```powershell
cd frontend
$env:VITE_API_BASE_URL = "http://127.0.0.1:8000/api/v1"
npm run dev
```

### 4.5 运行 smoke

```powershell
.\scripts\acceptance.ps1 -BaseUrl http://127.0.0.1:8000
```

```bash
bash scripts/acceptance.sh http://127.0.0.1:8000
```

脚本会创建一个唯一命名的演示文本项目，但不删除或修改已有项目。对公网环境运行前需确认可接受该测试数据。

## 5. 标准测试数据

### TD-01：主流程

`demo/sample-transcript.txt`，覆盖城市/交通、团队/数据、自然/健康三类素材。

### TD-02：故障与重试

`demo/failure-retry-transcript.txt`，用于 `job_fail` 和 `ai_degrade`。

### TD-03：分段边界

```text
这是短句。

这是第二句！Does English punctuation work? 当文本很长而且没有标点时系统仍应该按可控长度切分并保证所有非空字符存在
```

### TD-04：幂等冲突

- Key: `contract-idem-001`
- Request A: `{title:"A", text:"相同内容。"}`
- Request B: 完全相同，应返回同一 Project/Job。
- Request C: 相同 Key，`text` 改为“不同内容。”，应 409。

### TD-05：文件

- 1 张小型 PNG/JPEG、一个允许的短视频（若支持）。
- 1 个伪装扩展名或不支持类型。
- 1 个超过 `FRAMEFLOW_MAX_UPLOAD_MB` 的测试文件（在专用临时环境生成，不提交仓库）。
- `demo/sample-zh.wav` 仅在真实本地/外部 ASR Provider 已安装并可访问时作为 ASR 证据。

### TD-06：文生图固定数据

- 提示词：`一张干净的城市夜景商业配图，蓝紫色灯光，无文字，无水印`。
- 画幅：`16:9`；测试 Provider 返回仓库内固定的最小 PNG Base64，不调用真实网关。
- 幂等 Key：`image-contract-001`；相同请求重放，修改 prompt 后构造冲突。
- 非法响应：错误 Base64、文本文件、超出响应/输出字节上限、超大像素头。
- 片段场景：创建后编辑 Segment，使 accept 携带旧 `expected_segment_version`。

## 6. 自动化测试矩阵

### 6.1 分段、关键词与排序

| ID | 用例 | 方法 | 通过标准 |
| --- | --- | --- | --- |
| UT-NLP-01 | 中文句末符号分段 | 输入 TD-01/简化文本 | 片段非空，连接后不遗漏有效内容 |
| UT-NLP-02 | 换行/中英文标点 | 输入 TD-03 | 不产生空片段，中英文句末均有效 |
| UT-NLP-03 | 无标点长文 | 输入超过最大长度的字符串 | 被可控切分，每段不超限 |
| UT-NLP-04 | 短句合并 | 多个极短句 | 不产生大量无信息极短片段 |
| UT-KEY-01 | 关键词去重/停用词 | 重复词与停用词 | 结果非空、无重复、数量受限 |
| UT-RANK-01 | 语义类别基本相关性 | TD-01 三类片段 + seed assets | 每类前列至少有一个合理素材 |
| UT-RANK-02 | 分数公式 | 构造已知分项 | `total≈0.55*tfidf+0.30*keyword+0.15*tag` |
| UT-RANK-03 | 候选去重/排名 | 多个同分素材 | 至少 3 个唯一 asset_id，rank 连续且唯一 |
| UT-RANK-04 | 低相关补位 | 无命中文本 | 仍返回 3 个且 filler 标识/理由真实 |
| UT-RANK-05 | 可解释性 | 有/无命中词两种情况 | 解释非空，matched_terms 与分项证据一致 |

### 6.2 API 与持久化

| ID | 用例 | 通过标准 |
| --- | --- | --- |
| API-01 | live/ready | API 存活时 live=200；DB/Worker 未就绪时 ready 如实反映 |
| API-02 | 文本创建 | 202，同时返回已持久化 Project/Job，Job=queued/running |
| API-03 | 幂等重放 | TD-04 A/B 返回同一 Project/Job，数据库无第二行 |
| API-04 | 幂等冲突 | TD-04 C 返回 409 + 统一错误，无新资源 |
| API-05 | 完整处理 | Job 最终 succeeded/completed/100，Project=ready，多个 JobEvent |
| API-06 | 聚合详情 | 每个 Segment 有关键词/主题、>=3 唯一候选和 Selection |
| API-07 | Segment 编辑 | 正确 version 成功且 version+1，重读不丢，Audit 增加 |
| API-08 | 乐观锁冲突 | 旧 version 返回 409，服务端数据未被覆盖 |
| API-09 | 排序 | 完整 ID 集可重排；重复/缺失/跨项目 ID 被拒绝 |
| API-10 | 人工选择 | PUT 后 source=manual，重放无重复行，重读不丢 |
| API-11 | 重匹配 | 候选事务化换版，仍 >=3，manual Selection 不变 |
| API-12 | 素材 seed/搜索 | 初始 active 素材 >=12，关键词/标签/类型筛选有效 |
| API-13 | 素材上传 | 真实写文件与 DB，随机存储名，公开 URL 可读 |
| API-14 | 非法/超限上传 | 4xx + 统一错误，不留孤儿 DB 记录/部分文件 |
| API-15 | 项目删除 | 从属项目数据清理，全局 Asset 保留 |
| API-16 | 运行记录 Token 序列化 | 新记录与历史 `output_summary.tokens` 均只对外返回顶层 `input_tokens/output_tokens/total_tokens`，不返回同义并行字段 |
| API-17 | 无 Token 运行记录 | 规则任务或 Provider 未报告用量时三个 Token 字段为 `null`，不得序列化成伪造的 `0` |
| API-18 | 素材自动标签 | tags/keywords 留空时得到规则或 LLM 建议，并保存 `asset_tagging` AIRun 的实际来源 |
| API-19 | Embedding 校验与回退 | 数量/维度/有限值异常时安全回退字符相似；正常向量记录真实模型与来源 |
| API-20 | 时间线 | 按片段顺序返回连续时长和公开素材，不泄露 `storage_path` |
| API-21 | 预览幂等 | 同一输入指纹只保留一个活动预览 Job；成功结果可复用，force 不制造孤儿任务 |
| API-22 | 预览 Worker | 成功、可重试失败、耗尽失败均更新 Job/Preview/AIRun/Audit，输出 URL 仅在文件成功后出现 |
| API-23 | 单片段展示时长 | 1–30 秒、40ms 帧归一、版本冲突、恢复自动、总长上限和“不触发 rematch”均符合契约 |
| API-24 | 目标总时长 | 三种分配策略精确达到归一化目标；上下界、指纹 409、批量恢复和审计均正确 |
| API-25 | 时间线并发写 | 正文、排序、素材选择、rematch 与时长调整并发时无 500、无丢失写入；旧 attempt/旧 hash 不覆盖新状态 |
| API-26 | API 限流 | 读写桶独立；超限返回 429、`Retry-After`、request_id，健康接口不受影响 |
| API-27 | 文生图创建幂等 | 相同 Key/请求体返回同一 ImageGeneration 且 `idempotent_replay=true`；同 Key 不同请求体返回 409 |
| API-28 | 文生图 Provider 边界 | MockTransport 覆盖合法 Base64、非法 Base64、非图片、超大响应/输出/像素；不发真实网络请求，不留部分文件 |
| API-29 | 文生图状态迁移 | queued/running/succeeded/failed/canceled 的 retry、cancel、content、accept、discard 只允许合法迁移 |
| API-30 | 文生图入库幂等 | accept 并发/重放只创建一个 Asset 和一个受管理文件，标签任务只排队一次 |
| API-31 | 文生图片段版本锁 | 旧 `expected_segment_version` 返回 409，不覆盖新字幕 Selection；图片仍可只入库 |
| API-32 | 文生图文件事务 | Provider 成功后 DB/文件失败均回滚拥有的文件；discard 与 accept 竞态只有一个终态，无孤儿文件 |
| API-33 | 文生图标签闭环 | 入库后进入 Gemini 视觉标签；视觉未配置/失败时如实记录文本或规则降级，不伪造 Gemini 成功 |
| API-34 | 片段生图幂等快照 | 首次创建后编辑 Segment，相同 Key/请求仍返回原任务与原 `segment_version`，不冲突、不产生第二次计费 |
| API-35 | 已入库后应用 | auto-import 后再次 accept/select 复用同一 Asset；旧版本返回受控 409，当前版本更新 Selection 为 generated |
| API-36 | 提交后文件清理失败 | after_commit unlink 抛出 OSError 时事务与 HTTP 仍成功，记录可供后续清理且不产生第二 Asset |
| API-37 | 项目删除与文生图栅栏 | 删除项目会移除 queued/running/草稿任务并清理 staging，不再触发 Provider；已入库的全局 Asset 保留 |

### 6.3 故障、重试和恢复

| ID | 用例 | 通过标准 |
| --- | --- | --- |
| FAIL-01 | `ai_degrade` | 一次性消费；Job succeeded，AIRun degraded=true/provider 如实，结果完整 |
| FAIL-02 | `job_fail` | 一次性消费；Job/Project failed，错误码/消息/retryable 可见 |
| FAIL-03 | 人工重试 | 失败 Job 增加 attempt 并重入队，原事件保留，最终 succeeded |
| FAIL-04 | 无效重试 | succeeded/running/不可重试失败返回 409，不新建 Job |
| FAIL-05 | queued 取消 | Job/Project 进 canceled，Worker 不处理业务产物 |
| FAIL-06 | Worker 崩溃恢复 | 租约过期后可重排/续处理，结果不重复 |
| FAIL-07 | API 重启持久化 | 重启后项目/任务/编辑/选择/事件仍可读 |
| FAIL-08 | 无本地 ASR/无 Key | 媒体任务显式 `ASR_*` 配置/依赖错误，不生成假字幕 |
| FAIL-09 | Worker 尝试耗尽 | 恢复任务达到 `max_attempts` 后不再领取，记录 `JOB_ATTEMPTS_EXHAUSTED` |
| FAIL-10 | 硬超时线程未退出 | 当前任务按策略失败/重试，Worker 暂停领取新任务，不持续累积线程 |
| FAIL-11 | 文生图租约恢复 | 过期 running 任务可重新领取，新 execution generation 生效，旧 Worker 迟到结果被丢弃并清理 |
| FAIL-12 | 文生图取消迟到响应 | Provider 阻塞期间取消；响应随后到达不能改回 succeeded、落盘或自动入库 |
| FAIL-13 | 文生图重试边界 | 429/明确可重试错误按上限重试；认证、内容拒绝、非法响应不可自动重试；attempt 不越界 |
| FAIL-14 | 生图歧义错误分类 | 502/503/504、Read/Write/RemoteProtocol/其他 Timeout 进入 failed + 人工 retryable/ambiguous，不自动请求；仅 Connect/429 自动 |
| FAIL-15 | staging 结果恢复 | submitted→result→ready 后 DB 写失败/Worker 重启，复用 ready PNG 且 Provider 总调用 1 次；只有 submitted 时不得自动重发 |
| FAIL-16 | auto-import 窗口恢复 | succeeded 但 asset_id 为空的 auto-import 任务由租约步骤完成入库，Provider 不重复调用 |
| FAIL-17 | 人工重试硬上限 | attempt=3 最后一次允许入队并把上限固定为 4；attempt>=4 返回 409 `IMAGE_RETRY_LIMIT_REACHED` |
| FAIL-18 | 优雅停机 | serve 等待窗口为 `max(10, IMAGE_API_TIMEOUT+30)`，Compose stop grace 不短于该值，所有子进程共享截止时间并被回收 |
| FAIL-19 | PostgreSQL 写锁 | `_lock_writes` 的 PostgreSQL 路径执行事务级 advisory lock，SQLite 路径保持 BEGIN IMMEDIATE |

### 6.4 前端运行记录统计

| ID | 用例 | 通过标准 |
| --- | --- | --- |
| UI-RUN-01 | Token 总计 | 仅累计各运行非空的 `total_tokens`，不重复相加输入/输出或读取历史嵌套字段 |
| UI-RUN-02 | Token 详情 | 有用量时显示规范输入/输出/总计；三个字段为 `null` 时显示“—”或“未产生 Token”，不显示为真实 `0` |

## 7. Worker 恢复手工演练

为避免等待默认 300 秒租约，只在专用临时数据目录中设置短租约：

1. 设置 `FRAMEFLOW_JOB_LEASE_SECONDS=5` 和较长 `FRAMEFLOW_STAGE_DELAY_SECONDS`，用临时 data dir 启动 API/Worker。
2. 创建文本任务，等待 Job=running。
3. 终止 Worker 进程，不终止 API；确认 Job 仍持久化为 running，而不是前端自动成功。
4. 等待租约过期，重启 Worker。
5. 确认增加恢复/重试 JobEvent，attempt 符合设计，任务最终 succeeded。
6. 查项目详情：Segment 数量没有加倍；每段 Recommendation 的 asset/rank 唯一；Selection 仍每段最多一个。

该测试不应在共享公网 Demo 上执行，避免影响其他验收者。

## 8. 浏览器手工验收

### E2E-01：核心闭环

1. “项目”页无数据时显示真实空状态，有数据时指标与最近项目表格一致。
2. 新建文本项目，提交期间按钮禁用，无重复项目。
3. 处理页显示服务端阶段/进度/事件，任务成功后进工作台。
4. 原始字幕完整；每段有主题/关键词；每段至少 3 候选且理由可见。
5. 编辑一段文本/关键词，看到保存中/已保存。
6. 改变片段顺序，看到排序保存反馈。
7. 搜索“数据”或“健康”，替换一张不是当前选择的素材，看到 manual 标记。
8. Ctrl+R 强制刷新；文本、顺序与手工选择不变。

### E2E-02：故障与重试

1. 从侧栏“演示工具”进入的演示实验室明确声明故障注入性质。
2. 设置 `job_fail`，创建 TD-02 项目。
3. 任务 failed；错误码、人类可读原因、失败阶段和重试按钮可见。
4. 点击重试，按钮在请求中禁用；attempt 增加，事件历史保留。
5. 重试最终 ready，不多创建项目。
6. 再设置 `ai_degrade` 并创建项目；结果 ready，运行记录 degraded=true/provider 如实。

### E2E-03：空、错误和重复操作

- 素材搜索不存在的词：显示搜索空状态与“清除筛选”。
- 无输入创建：表单本地校验，不发请求。
- 快速双击创建/重试/采用：按钮禁用且服务端幂等，无重复数据。
- 停止 API 后操作：显示网络错误和重试入口，已有页面不伪装已保存。
- 恢复 API 后重试查询，已持久数据重新出现。

### E2E-04：时间线与组合预览

1. 工作台展示与字幕顺序一致的素材时间线，片段宽度反映时长。
2. 点击时间线片段能定位对应字幕，390px 视口可横向滚动且无全页溢出。
3. 先保存字幕草稿，再创建预览；按钮在活动任务期间禁用重复创建。
4. 页面轮询预览 Job，显示进度/错误；完成后使用后端 `output_url` 播放 MP4。
5. 展开“调整节奏”，验证 15/30/60 秒与自定义目标、三种分配策略、单片段精确输入和约 ±0.5 秒操作。
6. 后端按 40ms 归一后，输入框、卡片、总时长和成功反馈必须显示真实响应值；恢复单段/全部自动后来源标识同步更新。
7. 修改时长不立即创建预览任务，旧视频隐藏并显示“原预览已过期”；正文保存、预览创建和时长调整的快速连续操作保持互斥。
8. 快速切换项目或卸载页面时取消旧轮询，旧响应不能覆盖当前项目。

### E2E-05：文生图闭环

1. 素材库输入提示词并选择画幅，提交后立即看到持久化 queued/running 状态，刷新页面任务不丢。
2. 成功后预览草稿；未点击入库前素材总数不变，页面不展示第三方临时 URL。
3. 点击“加入素材库”，看到同一图片 Asset 和标签处理中状态；重复点击不产生重复素材。
4. 标签完成后能按 Gemini 标签搜索到素材；若视觉服务关闭，页面明确显示降级来源。
5. 在工作台对当前字幕执行“AI 生成图片”，预览后“入库并应用”；刷新后 Selection 仍指向生成素材。
6. 生成期间编辑该字幕，再尝试按旧版本应用；页面提示版本冲突且不覆盖新字幕，允许只入库或重新生成。
7. 创建第二张草稿并丢弃；content URL 不再可读，素材库没有新增项。
8. 快速双击创建、accept、cancel 或 discard，服务端保持幂等/互斥，不出现空卡片或孤儿文件。

## 9. 响应式与可访问性

| ID | 视口/检查 | 通过标准 |
| --- | --- | --- |
| UX-01 | 1440×900 工作台 | 三栏可见，各自滚动，无主操作遮挡 |
| UX-02 | 1280×720 录屏 | 页首、主操作、候选和保存状态可读 |
| UX-03 | 768×1024 | 两栏/标签切换，当前 Segment 不丢 |
| UX-04 | 390×844 | “字幕 / 编辑 / 候选”三面板可切换，无全页水平滚动，创建/重试/保存/替换可用 |
| A11Y-01 | Tab 键遍历 | 所有主操作可聚焦，焦点环可见，顺序合理 |
| A11Y-02 | 纯键盘核心流 | 可创建、选 Segment、编辑、采用素材；排序有非拖放备选 |
| A11Y-03 | 状态表达 | 成功/警告/失败不只依赖颜色，有文字/图标 |
| A11Y-04 | 表单/图片 | label 与输入关联，错误可定位，内容图有合理 alt |

## 10. 安全与配置检查

| ID | 检查 | 通过标准 |
| --- | --- | --- |
| SEC-01 | 前端源码/dist 密钥搜索 | 无 `OPENAI_API_KEY`、Bearer token 或真实 Key |
| SEC-02 | git 跟踪文件 | `.env`、DB、uploads、node_modules 不被跟踪 |
| SEC-03 | 上传路径 | 包含 `../`、绝对路径或特殊字符的原文件名不决定磁盘目标 |
| SEC-04 | 错误体/日志 | 无堆栈、数据库绝对路径、Key 或请求 Authorization Header |
| SEC-05 | CORS | 本地只允许明确 origin；同源公网不使用带凭据的 `*` |
| SEC-06 | 故障入口 | 页面明确标记 Demo；公网使用访问控制；不声称为真实 Provider 事件 |
| SEC-07 | 图像密钥隔离 | `IMAGE_API_KEY` 不继承其他 Key，不进入前端、日志、AIRun、错误体或 Git |
| SEC-08 | 图像响应安全 | 仅接收受限 Base64；拒绝模型 URL、畸形图片、超大响应/字节/像素并移除元数据 |
| SEC-09 | 付费操作防滥用 | 每日额度、pending 上限和通用写限流同时生效；auto-import/auto-select 必须由本次请求显式授权 |
| SEC-10 | staging 最小化 | manifest 仅含请求哈希、模型、画幅、尺寸和 usage；不含 Key、Authorization、提示词原文或 Base64 |

PowerShell 仓库静态搜索可使用：

```powershell
Get-ChildItem -Recurse -File -Exclude package-lock.json |
  Where-Object { $_.FullName -notmatch 'node_modules|\.git|\.venv|dist' } |
  Select-String -Pattern 'sk-[A-Za-z0-9_-]{20,}|OPENAI_API_KEY\s*=\s*\S+'
```

期望只命中 `.env.example` 的空占位配置/文档，不应命中真实密钥。

## 11. ASR 真实验证

基础 CI 不下载模型也不需要外部 Key。只有在以下条件全部满足后，才可将“真实 ASR 已验证”写入提交说明：

- 明确 Provider（本地 faster-whisper、外部 OpenAI-compatible 或 DashScope）和模型版本。
- `demo/sample-zh.wav` 经真实 Provider 返回非空字幕，项目最终 ready。
- AIRun/任务事件如实保存 Provider/模型名，不显示 rules 冒充 ASR。
- 无效媒体、空音频、Provider 超时/无 Key 至少各测一条失败路径。
- 外部 Provider 测试不在不受控的每次 PR CI 执行；使用人工/定时环境和最小权限 secret。

本地 optional dependency 缺失时，期望错误应明确告知安装方式，不退回假文本。

## 12. 性能与容量基线

这些是 Demo 发现性基线，不是生产 SLA：

| ID | 场景 | 目标 |
| --- | --- | --- |
| PERF-01 | 1,000 字中文 + 12 素材，本地规则 | 普通开发机上在 10 秒内达到终态，无内存异常增长 |
| PERF-02 | 单 Worker 下连续创建 5 个文本任务 | API 均快速返回 202，任务有界排队并依次完成，无重复领取、任务丢失或结果串项 |
| PERF-03 | 100 次轮询 GET Job | 无 SQLite locked 错误，进度单调、终态稳定 |
| PERF-04 | 100 MB 上限附近文件 | 不一次性读入前端 JS 内存；超限被及时拒绝 |
| PERF-05 | 公网真实音频与语义增强 | 记录各阶段耗时而非只记录总时长；当前一次 71 秒热机样本为 ASR 约 20.5 秒、Gemini 增强约 3.1 秒、完整流程约 26 秒，不作为 SLA |
| PERF-06 | Mock 文生图并发与排队 | 同时创建至 `IMAGE_MAX_PENDING`，API 快速返回且单 Worker 有界领取；超过上限明确 429/409，不拖垮 ASR 健康检查 |
| PERF-07 | 真实文生图人工样本 | 受控环境记录至少 20 次成功率、p50/p95、响应/输出大小、单张费用和 ASR 排队影响，不作为 SLA |

若环境冷启需下载 ASR 模型，该时间必须单独记录，不与本地规则 PERF-01 混在一起。

## 13. 公网发布验收

1. 在公网地址运行 acceptance 脚本，保存输出和时间。
2. 走 E2E-01 与 E2E-02，录制强制刷新前后画面。
3. 重启容器/服务（不删除持久卷），再打开同一 Project ID；数据不丢。
4. 确认 API 与媒体 URL 使用 HTTPS，页面无 mixed content/CORS 错误。
5. 无登录时确认平台访问口令/配额；有登录时使用提交的测试账号完整重跑。
6. 从全新无痕窗口打开，证明不依赖开发者本地会话/缓存。
7. 检查公网故障实验结束后 mode 恢复 `none`，避免下一位验收者意外失败。
8. 文生图启用时，使用轮换后的专用 Key 人工生成一张非敏感图片，完成预览、入库、Gemini 标签、片段应用、删除与容器重启复读；保存耗时和运行记录，不保存 Key/Authorization。
9. 执行 `docker compose --env-file deploy/.env config`，确认 `stop_grace_period >= IMAGE_API_TIMEOUT + 30s`；默认应为 240 秒。

## 14. 发布准入/准出

### 准入

- 测试使用的 commit 已固定，不在执行期间改代码。
- 测试数据和所需环境变量已准备，不使用个人敏感数据。
- 本地/公网环境时间和磁盘空间正常。

### 必须准出条件

- 后端全部必须测试通过，无随机重跑才过的 flaky case。
- 前端 lint 和 build 通过。
- acceptance 脚本在本地与公网地址各通过一次。
- E2E-01/E2E-02 通过，手工选择刷新不丢。
- 公网服务重启后数据仍存在。
- 无真实密钥或本地数据被提交。
- 文生图默认测试全部使用 MockTransport，真实付费验收的请求数、费用、Provider/model 和降级状态有人工记录。
- `KNOWN_ISSUES.md`、`AI_USAGE.md` 与当前实现一致。
- 主演示脚本完整彩排至少 1 次，故障注入在彩排后清零。

## 15. 执行记录模板

### 2026-07-15 文生图与节奏控制融合门禁

- 后端 `python -m pytest`：PASS，`222 passed, 1 deselected`。
- 文生图专项（Provider、状态机、崩溃恢复、项目删除）：PASS，`54 passed`。
- 前端 `npm run lint`、`npx tsc --noEmit`、`npm run build`：PASS。
- 前端 `npm run test:browser`：PASS，Chromium `48 passed`；文生图、素材打标、目标/单段时长、保存互斥和失败调用筛选在同一工作台回归通过。
- 启动器契约：PASS，7 个场景；缺少任一时长或文生图核心端点时不会误复用旧后端。
- 本地融合服务 `/health/ready` 为 `ready`，数据库正常，2 个 Worker 在线；本轮未执行服务器部署或公网 smoke。

### 2026-07-15 历史记录：节奏控制阶段

- 后端 `python -m pytest tests`：PASS，`164 passed, 1 deselected`。
- 前端 `npm run lint`、`npx tsc --noEmit`、`npm run build`：PASS。
- 前端 `npm run test:browser`：PASS，Chromium `38 passed`；新增覆盖目标/单段时长、40ms 真实值回显、恢复自动、旧预览失效、保存互斥、快速双击和 390px 布局。
- 本轮只完成本地第一阶段，未执行服务器部署或公网 smoke。

### 2026-07-14 历史记录：上线前阶段

- 后端 `python -m pytest`：PASS，`89 passed, 1 deselected`。
- 前端 `npm run lint`：PASS。
- 前端 `npm run build`：PASS。
- 前端 `npm run test:browser`：PASS，Chromium `28 passed`，覆盖登录/首次初始化、素材删除、完整闭环、拖动排序、快速替换、失败回滚、保存竞态、时间线过期、预览异常和移动端布局。
- 启动器契约：PASS，3 个场景，覆盖旧服务能力探测、认证路由和启动恢复边界。
- 真实 LLM 生产验证：PASS，通过未提交的服务端配置调用 OpenAI-compatible Gemini 3.1 Flash Lite Preview，运行记录为 `degraded=false`；当前一次热机样本约 3.1 秒，文档和输出均不含密钥或真实网关。
- 真实 ASR 生产验证：PASS，公网单 Worker使用 `faster-whisper small/int8`，容器分配 3.5 CPU / 4 GB；当前一次 71 秒热机样本的 ASR 阶段约 20.5 秒、完整流程约 26 秒，运行记录保存真实 Provider/模型。
- 本地向量评测：混合排序(向量) Hit@3 `0.9412`、MRR `0.7966`、nDCG@3 `0.8288`。
- ffmpeg 冒烟：图片 + 视频 2 片段、3 秒、1280×720，输出 577,972 bytes；本机编码器 `libopenh264`。本机 ffmpeg 缺少字幕能力，因此该次 `subtitles_burned=false`；Docker 镜像安装 Debian ffmpeg 与 Noto CJK 字体，仍需 Docker daemon 可用后复验。
- `docker compose --env-file deploy/.env.example config --quiet`：PASS；当前开发机 Docker daemon 未运行，因此本地镜像构建、容器内 Caddy validate 和字幕镜像复验未执行，改由新增 GitHub CI delivery job 覆盖。
- 本地只读 acceptance：PASS，live/ready/seed/projects/runs/audit 全部 HTTP 200，活动素材 39 个；使用 `-SkipCreate`，未写入新项目。
- 公网部署与关键路径：PASS，已验证 HTTPS、应用登录、ready 与真实音视频模型链路；后续每个发布 commit 仍需重新保存 acceptance/smoke 输出，不能复用本次记录。

最终提交前仍需在所有代码合并后复跑敏感信息、大文件和 Git 暂存区检查；最终结果以提交汇报为准。

每次正式提交前复制一份记录，填写真实结果；不应预先填“全部通过”。

```text
测试日期：
Commit SHA：
执行人：
操作系统：
Python / Node / 浏览器版本：
数据目录（不含敏感内容）：

后端 pytest：PASS / FAIL（通过数/总数）
前端 lint：PASS / FAIL
前端 build：PASS / FAIL
本地 acceptance：PASS / FAIL
公网 acceptance：PASS / FAIL / NOT RUN
E2E-01：PASS / FAIL
E2E-02：PASS / FAIL
重启持久化：PASS / FAIL
移动视口：PASS / FAIL
真实 ASR：PASS / FAIL / NOT CONFIGURED

失败用例与 request_id/job_id：
未解决问题：
对 KNOWN_ISSUES 的更新：
最终发布决定：GO / NO-GO
```
