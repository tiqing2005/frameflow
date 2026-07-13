# FrameFlow AI 测试计划

## 1. 目标

本计划验证的不是“页面看起来像完成”，而是原题最关键的可观察事实：

- 新输入能进入真实持久化异步任务，并通过多个服务端阶段。
- 处理后的每个片段有关键词/主题、至少 3 个唯一候选和可读理由。
- 人工文本编辑、顺序与素材选择经刷新/API 重读不丢。
- 幂等、版本冲突、失败、重试、取消与 Worker 恢复路径可重复。
- 素材、运行记录和审计记录是真实数据，不依赖前端写死结果。
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

1. 项目台无数据时显示真实空状态，有数据时指标与列表一致。
2. 新建文本项目，提交期间按钮禁用，无重复项目。
3. 处理页显示服务端阶段/进度/事件，任务成功后进工作台。
4. 原始字幕完整；每段有主题/关键词；每段至少 3 候选且理由可见。
5. 编辑一段文本/关键词，看到保存中/已保存。
6. 改变片段顺序，看到排序保存反馈。
7. 搜索“数据”或“健康”，替换一张不是当前选择的素材，看到 manual 标记。
8. Ctrl+R 强制刷新；文本、顺序与手工选择不变。

### E2E-02：故障与重试

1. 演示实验室明确声明故障注入性质。
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

## 9. 响应式与可访问性

| ID | 视口/检查 | 通过标准 |
| --- | --- | --- |
| UX-01 | 1440×900 工作台 | 三栏可见，各自滚动，无主操作遮挡 |
| UX-02 | 1280×720 录屏 | 页首、主操作、候选和保存状态可读 |
| UX-03 | 768×1024 | 两栏/标签切换，当前 Segment 不丢 |
| UX-04 | 390×844 | 无全页水平滚动，创建/重试/保存/替换可用 |
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

PowerShell 仓库静态搜索可使用：

```powershell
Get-ChildItem -Recurse -File -Exclude package-lock.json |
  Where-Object { $_.FullName -notmatch 'node_modules|\.git|\.venv|dist' } |
  Select-String -Pattern 'sk-[A-Za-z0-9_-]{20,}|OPENAI_API_KEY\s*=\s*\S+'
```

期望只命中 `.env.example` 的空占位配置/文档，不应命中真实密钥。

## 11. ASR 可选验证

基础 CI 不下载模型也不需要外部 Key。只有在以下条件全部满足后，才可将“真实 ASR 已验证”写入提交说明：

- 明确 Provider（本地 faster-whisper 或外部 OpenAI-compatible）和模型版本。
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
| PERF-02 | 连续创建 5 个文本任务 | API 均快速返回 202，单 Worker 顺序处理，无任务丢失/结果串项 |
| PERF-03 | 100 次轮询 GET Job | 无 SQLite locked 错误，进度单调、终态稳定 |
| PERF-04 | 100 MB 上限附近文件 | 不一次性读入前端 JS 内存；超限被及时拒绝 |

若环境冷启需下载 ASR 模型，该时间必须单独记录，不与本地规则 PERF-01 混在一起。

## 13. 公网发布验收

1. 在公网地址运行 acceptance 脚本，保存输出和时间。
2. 走 E2E-01 与 E2E-02，录制强制刷新前后画面。
3. 重启容器/服务（不删除持久卷），再打开同一 Project ID；数据不丢。
4. 确认 API 与媒体 URL 使用 HTTPS，页面无 mixed content/CORS 错误。
5. 无登录时确认平台访问口令/配额；有登录时使用提交的测试账号完整重跑。
6. 从全新无痕窗口打开，证明不依赖开发者本地会话/缓存。
7. 检查公网故障实验结束后 mode 恢复 `none`，避免下一位验收者意外失败。

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
- `KNOWN_ISSUES.md`、`AI_USAGE.md` 与当前实现一致。
- 主演示脚本完整彩排至少 1 次，故障注入在彩排后清零。

## 15. 执行记录模板

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
