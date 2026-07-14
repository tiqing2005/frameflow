# FrameFlow 单机部署手册

本文描述随仓库交付的 Linux VPS 方案：一个 FrameFlow 容器运行 FastAPI API 与有界持久化 Worker 进程池。当前专用 4 核 / 8 GB 公网主机给应用容器分配 3.5 CPU / 4 GB，并使用单 Worker，让本地 ASR 独占主要计算资源。默认可由 Caddy 容器负责反向代理和自动 HTTPS；如果宿主机已经运行 Nginx（例如同机承载其他域名），使用 `external-nginx` 覆盖，让 FrameFlow 仅监听宿主机回环地址 `127.0.0.1:8080`，不抢占 80/443。SQLite、上传文件、种子素材和可选模型统一保存在 Docker 具名卷的 `/data`。

> 适用边界：作品演示、功能验收和低并发单机服务。`FRAMEFLOW_WORKER_CONCURRENCY` 可配置同一容器内的并发任务数（1–16）；CPU 本地运行 `faster-whisper` 时建议保持为 `1`，通过提高单 Worker 的 CPU 配额提速。它不是多租户生产集群方案，不应横向扩容多个 SQLite 写实例。

## 1. 前置条件

- 64 位 Linux VPS，建议 Ubuntu 22.04/24.04 或 Debian 12。
- 最低 2 核 CPU、3 GB 内存、20 GB 磁盘；同时启用本地 ASR 与 Embedding 建议 4–8 GB 内存。
- Docker Engine 24+ 与 Docker Compose v2。
- 域名 A 记录（以及存在时的 AAAA 记录）已指向服务器。
- 云安全组和防火墙放行 TCP 22、80、443；HTTP/3 另需 UDP 443。
- Caddy 模式要求 80/443 未被 Nginx、Apache 等其他服务占用；已有反向代理的服务器使用 `FRAMEFLOW_EDGE_MODE=external-nginx`。

安装 Docker 请使用 [Docker 官方文档](https://docs.docker.com/engine/install/)。部署前检查：

```bash
docker --version
docker compose version
docker info
```

## 2. 首次部署

```bash
git clone https://github.com/tiqing2005/frameflow.git
cd frameflow
bash deploy/first-deploy.sh app.example.com ops@example.com
```

脚本会创建 `deploy/.env`、写入域名与同源 CORS，并要求设置应用管理员密码。Caddy 模式还会要求设置 Demo 入口密码；external-nginx 模式不启动 Caddy，也不改动宿主机已有 Nginx。应用密码生成 PBKDF2-SHA256 哈希，入口密码生成 bcrypt 哈希；两者都不会以明文写入配置。公网部署会关闭浏览器远程首次认领，只接受预置管理员凭据。随后脚本验证 Compose、应用 ready，以及公网 HTTPS smoke；任一必需检查失败都不会报告部署成功。

```bash
docker compose --env-file deploy/.env ps
curl -fsS -u frameflow https://app.example.com/health/live
curl -fsS -u frameflow https://app.example.com/health/ready
```

`live` 仅表示 API 存活；`ready` 还检查 SQLite、种子素材与 Worker 心跳，是发布验收标准。

发布脚本还会等待 Caddy healthcheck、执行 Caddyfile validate，并按 `PUBLIC_SMOKE_*` 配置执行真实 HTTPS/Basic Auth smoke。首次部署默认要求公网 smoke 成功；仅在隔离内网演练时才显式设置 `PUBLIC_SMOKE_REQUIRED=false`。

### 已有 Nginx 的服务器

如果 80/443 已由宿主机 Nginx 使用，不要启动 Caddy。先在 `deploy/.env` 设置：

```dotenv
FRAMEFLOW_EDGE_MODE=external-nginx
FRAMEFLOW_HOST_PORT=8080
FRAMEFLOW_AUTH_ENABLED=true
FRAMEFLOW_AUTH_LOCAL_SETUP_ENABLED=false
FRAMEFLOW_AUTH_PASSWORD_HASH='由 first-deploy.sh 交互生成的 PBKDF2 哈希'
```

然后执行 `bash deploy/first-deploy.sh frameflow.example.com ops@example.com`。脚本会通过
`deploy/docker-compose.external-nginx.yml` 只启动 `frameflow`，把应用发布到
`127.0.0.1:8080`；宿主机 Nginx 应将该域名反向代理到这个回环地址，并保留
`X-Forwarded-Proto`、上传体积限制和长任务超时。升级、回滚、备份恢复和 smoke 脚本会根据
`FRAMEFLOW_EDGE_MODE` 自动避免拉起 Caddy。

宿主机 Nginx 至少应设置 `client_max_body_size 110m`、`proxy_read_timeout 600s`，并添加
`X-Content-Type-Options nosniff`、`Referrer-Policy strict-origin-when-cross-origin`、
`X-Frame-Options DENY`；HTTPS 站点应启用 HSTS。应用登录仍是唯一业务会话，外层 Basic Auth
可选，不能用空密码哈希部署。

## 3. 环境变量

真实配置位于 `deploy/.env`，模板见 `deploy/.env.example`。该文件已被 Git 忽略，应限制权限：

```bash
chmod 600 deploy/.env
```

| 变量 | 作用 | 默认/建议 |
| --- | --- | --- |
| `DOMAIN` | 站点域名，不含协议 | 必填 |
| `FRAMEFLOW_EDGE_MODE` | `caddy` 或已有 Nginx 的 `external-nginx` | `caddy` |
| `FRAMEFLOW_HOST_PORT` | external-nginx 模式的宿主机回环端口 | `8080` |
| `ACME_EMAIL` | Caddy 模式的 HTTPS 证书账户联系邮箱 | Caddy 模式必填 |
| `ENABLE_BASIC_AUTH` | 首次部署是否强制配置整站鉴权 | `true`，受控 Demo 推荐保持开启 |
| `BASIC_AUTH_USER` | Caddy 整站鉴权用户名 | `frameflow` |
| `BASIC_AUTH_HASH` | Caddy bcrypt 哈希；脚本生成并用单引号保存 | 首次部署时生成 |
| `CADDY_MAX_REQUEST_BODY` | 入口层请求体硬上限，应略大于应用上传上限 | `110MB` |
| `PUBLIC_SMOKE_ENABLED` / `PUBLIC_SMOKE_REQUIRED` | 是否执行公网 HTTPS smoke，以及失败是否阻断发布 | 均为 `true` |
| `PUBLIC_SMOKE_URL` | 发布 smoke 的 HTTPS 基址 | `https://你的域名` |
| `FRAMEFLOW_CORS_ORIGINS` | 允许的浏览器来源，逗号分隔 | `https://你的域名` |
| `FRAMEFLOW_MAX_UPLOAD_MB` | 单文件上传上限 | `100` |
| `FRAMEFLOW_WORKER_CONCURRENCY` | 项目、预览和素材画面识别共享的核心 Worker 进程数（1–16）；文生图另有固定 1 个轻量进程 | CPU 本地 ASR 建议 `1` |
| `DEMO_MODE` | 是否注册故障注入接口 | `false`，部署环境保持关闭 |
| `DATA_VOLUME_NAME` | 持久化 `/data` 的具名卷 | `frameflow_data` |
| `FRAMEFLOW_AUTH_ENABLED` | 启用应用内管理员登录、会话与 CSRF 防护 | `true` |
| `FRAMEFLOW_AUTH_LOCAL_SETUP_ENABLED` | 是否允许从服务本机首次创建管理员 | 公网 `false`，本地开发可为 `true` |
| `FRAMEFLOW_AUTH_USERNAME` / `FRAMEFLOW_AUTH_DISPLAY_NAME` | 应用管理员账号与显示名称 | `admin` / `FrameFlow 管理员` |
| `FRAMEFLOW_AUTH_PASSWORD_HASH` | PBKDF2-SHA256 管理员密码哈希 | 首次部署脚本生成，必填 |
| `FRAMEFLOW_AUTH_SESSION_HOURS` | 应用会话有效期 | `12` |
| `FRAMEFLOW_AUTH_COOKIE_SECURE` | 只通过 HTTPS 发送会话 Cookie | 公网 `true` |
| `LLM_PROVIDER` | 语义增强 provider：`rules`、`gemini`、`openai-compatible`、`openai` 或 `deepseek` | 公网 `gemini`；零配置 `rules` |
| `LLM_BASE_URL` | OpenAI-compatible API 基址，代码会追加 `/chat/completions` | 使用供应商服务端基址，不提交真实网关 |
| `LLM_API_KEY` | 语义增强密钥，仅注入 FrameFlow 容器 | 空 |
| `LLM_MODEL` | 语义增强模型 ID | 公网 `gemini-3.1-flash-lite-preview` |
| `LLM_TIMEOUT` | 单次语义增强请求超时秒数 | `20` |
| `VISION_PROVIDER` | 素材画面识别：`none` 或 `openai-compatible` | `none`，默认不向外发送画面 |
| `VISION_BASE_URL` | 视觉兼容网关基址，代码会追加 `/chat/completions` | 官方地址或部署者选择的网关 |
| `VISION_API_KEY` | 视觉网关独立密钥，仅注入 FrameFlow 容器 | 空，不继承 LLM/ASR 密钥 |
| `VISION_MODEL` / `VISION_TIMEOUT` | 视觉模型 ID / 单次请求超时秒数 | `gpt-4o-mini` / `30` |
| `INSTALL_LOCAL_ASR` | 构建时安装 `faster-whisper` | 公网 `true` |
| `FRAMEFLOW_ASR_PROVIDER` | `auto`、`openai`、`local` 或 `dashscope` | 公网 `local` |
| `OPENAI_API_KEY` | OpenAI 兼容语音转写密钥，仅服务端读取 | 空 |
| `FRAMEFLOW_OPENAI_BASE_URL` | OpenAI 兼容接口基址 | 官方地址 |
| `HF_HOME` | Hugging Face 与本地 ASR 模型缓存目录 | `/data/models/huggingface` |
| `INSTALL_LOCAL_EMBEDDINGS` | 构建时安装 `sentence-transformers` 等本地向量依赖 | `false` |
| `EMBEDDING_PROVIDER` | `auto`、`local`、`openai-compatible` 或 `none` | `auto` |
| `FRAMEFLOW_CPUS` / `FRAMEFLOW_MEMORY_LIMIT` | 应用容器 CPU / 内存上限 | 当前专用机 `3.5` / `4g` |
| `FRAMEFLOW_STOP_GRACE_PERIOD` | Compose 停机宽限，应不小于 `IMAGE_API_TIMEOUT + 30s` | `240s` |
| `IMAGE_API_BASE_URL` | OpenAI-compatible 图像网关基址，代码追加 `/images/generations` | 空时功能不可用；不要提交真实网关 |
| `IMAGE_API_KEY` | 图像生成独立服务端密钥 | 空，不继承 LLM/ASR/Vision 密钥 |
| `IMAGE_MODEL` / `IMAGE_API_TIMEOUT` | 图像模型 ID / 单次 Provider 超时秒数 | `gpt-image-2` / `180` |
| `IMAGE_MAX_RESPONSE_MB` / `IMAGE_MAX_OUTPUT_MB` | Base64 JSON 响应 / 解码图片硬上限 | `25` / `15` |
| `IMAGE_MAX_PIXELS` | 完整解码后的最大总像素 | `24000000` |
| `IMAGE_DRAFT_RETENTION_HOURS` | 未接受草稿保留时间；已入库素材不受影响 | `72` |
| `IMAGE_DAILY_LIMIT` / `IMAGE_MAX_PENDING` | 每日生成数 / queued+running 上限；每日值 `0` 表示关闭配额 | 公网建议 `50` / `5` |
| `BACKUP_INCLUDE_MODEL_CACHE` | 是否备份可重新下载的模型与缓存 | `false` |
| `BACKUP_MIN_FREE_MB` | 快照卷与归档目录的额外空间余量 | `1024` |
| `BACKUP_RETENTION_DAYS` / `BACKUP_RETENTION_COUNT` | 旧备份保留策略；`0` 关闭对应规则 | `30` / `20` |

修改构建参数后运行 `bash deploy/upgrade.sh`；只修改运行时变量可执行：

```bash
bash deploy/upgrade.sh
```

不要使用 `VITE_` 前缀保存任何密钥：Vite 构建变量会进入浏览器静态资源。`VISION_*` 与 `IMAGE_API_KEY` 只能作为 FrameFlow 容器的运行时环境变量，不得作为 Docker 构建参数。

可随时复验完整发布链路：

```bash
bash deploy/smoke.sh
# 需要同时验证认证后公网 ready 时，仅临时传入，不要写入 .env：
FRAMEFLOW_SMOKE_PASSWORD='本次密码' bash deploy/smoke.sh
```

### 整站 Basic Auth（受控 Demo 默认启用）

`first-deploy.sh` 默认调用鉴权管理脚本，保护包括删除、上传和故障注入在内的全部页面/API。日常查看、启用或更换密码：

```bash
bash deploy/configure-auth.sh status

# 首次启用会交互输入两次密码并生成哈希；已有哈希则直接启用
bash deploy/configure-auth.sh enable
```

密码哈希保存在被 Git 忽略且权限为 `600` 的 `deploy/.env`。若需要换密码，先清空其中的 `BASIC_AUTH_HASH=`，再运行 `enable`。先在新的终端或无痕窗口确认账号可登录，再保留当前管理会话。

仅当站点位于可信内网、VPN 后方，或外层网关已有可靠身份认证时，才显式关闭：

```bash
bash deploy/configure-auth.sh disable
```

关闭命令会给出风险警告并把 `ENABLE_BASIC_AUTH=false` 写回本地配置。`10-basic-auth.caddy` 被 `.gitignore` 排除，不会把站点启用状态误提交到公开仓库。Basic Auth 必须配合 HTTPS 使用；本方案由 Caddy 自动提供 HTTPS。它是应用登录前的第二层 Demo 入口保护，不替代应用会话，也不等于用户、租户和资源级授权。

### 应用内管理员登录

应用认证默认启用。`first-deploy.sh` 会在服务器终端交互读取管理员密码，只把 PBKDF2-SHA256 哈希写入 `deploy/.env`，并设置 `FRAMEFLOW_AUTH_LOCAL_SETUP_ENABLED=false`，因此公网访客无法抢先创建管理员。登录成功后浏览器使用 HttpOnly、SameSite=Strict、Secure Cookie；写请求还必须携带会话返回的 CSRF Token。

本地全新数据目录可保留 `FRAMEFLOW_AUTH_LOCAL_SETUP_ENABLED=true`：首次打开登录页时，只能从 API 所在机器的回环地址创建一个管理员账号。账号创建后入口永久转为登录；若数据库已存在身份记录，修改该开关不会重新开放认领。

不要在仓库、截图或演示视频中公开管理员密码。向授权体验者提供测试账号时，应使用单独的临时密码，并在体验结束后更换或下线实例。

### 请求体、资源和日志上限

Caddy 默认在入口层拒绝超过 `110MB` 的请求，应用仍按 `FRAMEFLOW_MAX_UPLOAD_MB=100` 校验单文件；上传现在按 1 MiB 分块写入 `/data` 并边计数/哈希，避免把整个文件复制到 Python 堆内存。安全响应头包含 HSTS、CSP、禁止嵌入和 MIME 嗅探；CSP 只为 FastAPI `/docs`、`/redoc` 保留 jsDelivr 与官方 favicon 例外。Compose 同时限制 CPU、内存、PID、临时目录和 `json-file` 日志轮转；应用根文件系统只读，持久化写入仅允许 `/data`，临时写入仅允许 `/tmp`。可在 `deploy/.env` 调整 `FRAMEFLOW_*_LIMIT` 和 `CADDY_*_LIMIT`，但不建议取消上限。Compose 仅把应用需要的变量显式注入 FrameFlow，Caddy 的 Basic Auth 哈希和 ACME 配置不会进入应用容器。

### Gemini 语义增强（公网当前方案）

编辑 `deploy/.env`：

```dotenv
LLM_PROVIDER=gemini
LLM_BASE_URL=https://llm-gateway.example.com/v1
LLM_API_KEY=你的服务端密钥
LLM_MODEL=gemini-3.1-flash-lite-preview
LLM_TIMEOUT=20
```

然后重建运行容器以加载配置：

```bash
docker compose --env-file deploy/.env up -d --build --force-recreate
```

`gemini` 是运行记录中的供应商标识，传输仍调用 `${LLM_BASE_URL}/chat/completions`。模型输出必须通过严格 JSON Schema 校验，并完整保留原字幕；无密钥、超时、HTTP 错误、非法 JSON 或字幕缺失都会自动回退到确定性规则，任务仍可完成，运行记录会标为降级。当前一次生产样本的语义增强阶段约 3.1 秒，该数字不是 SLA。模型 ID、兼容程度和可用性以实际网关为准；DeepSeek 可通过 `LLM_PROVIDER=deepseek` 与对应 API 基址替换，不需要修改业务代码。

### 素材画面识别（可选）

视觉配置与文本 LLM、ASR、Embedding 完全独立，默认关闭。需要启用 OpenAI-compatible 视觉网关时，编辑权限为 `600` 的服务器 `deploy/.env`：

```dotenv
VISION_PROVIDER=openai-compatible
VISION_BASE_URL=https://vision-gateway.example.com/v1
VISION_API_KEY=
VISION_MODEL=gpt-4o-mini
VISION_TIMEOUT=30
```

只在服务器上填写 `VISION_API_KEY`，然后运行 `bash deploy/upgrade.sh` 让容器重新加载配置。任何曾经出现在聊天、截图、终端历史或公开日志里的旧 Key 都必须先在供应商侧撤销并轮换，不得继续部署或与 `LLM_API_KEY`、`OPENAI_API_KEY` 复用。

标签或关键词留空的新素材上传后会快速返回，持久化 Worker 随后处理；详情中的“AI 重新生成标签”也进入同一队列。项目处理、预览和素材画面识别共享 `FRAMEFLOW_WORKER_CONCURRENCY`，外部网关的并发与限流也应纳入压测。视觉调用只发送一张经 ffmpeg 归一化的图片画面，视频只发送一张 poster/抽取帧，不发送整段视频。启用第三方网关意味着这张画面会离开服务器；敏感素材应保持 `VISION_PROVIDER=none` 或不要上传，并事先确认供应商的数据保留、训练和合规政策。

视觉未配置、超时、HTTP 错误或结果不合格时，任务按“视觉模型 → 纯文本 LLM → 本地规则”降级，仍然完成且不会把底层网关错误暴露给用户；运行记录会如实标注 `degraded` 和最终产出来源。纯文本 LLM 与规则阶段不接收画面。

### 文生图与素材闭环（可选）

文生图密钥与 Gemini 语义增强、素材视觉标签、ASR 完全隔离。上线前先在供应商侧轮换任何曾出现在聊天、截图或终端历史中的旧 Key，再编辑服务器 `deploy/.env`：

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

FrameFlow 只从服务端调用 `${IMAGE_API_BASE_URL}/images/generations`，首版要求 Provider 返回 `b64_json`，不会把 Key 下发浏览器，也不会下载响应中的任意远程 URL。返回内容经过响应体、Base64、解码字节和像素上限校验，移除元数据并归一化为 PNG 后才保存。未接受草稿写入 `/data/private/image-generations`，因此容器升级与重启不会丢失；加入素材库后转入普通 Asset、备份和删除生命周期，并排队执行现有 Gemini 画面标签。

`app.serve` 会在核心 Worker 池之外固定拉起并监督 1 个 `app.image_worker` 进程，Compose 只需注入上述环境变量，不应再启动第二个会与同一 SQLite 文件争抢写锁的图像容器。`FRAMEFLOW_STOP_GRACE_PERIOD` 必须不小于 `IMAGE_API_TIMEOUT + 30s`；默认 `180 + 30 < 240`，使 SIGTERM 后有时间等待已提交的付费请求完成、落 staging 或被执行代次栅栏拒绝。当前 3.5 CPU / 4 GB 服务器继续保持：

```dotenv
FRAMEFLOW_WORKER_CONCURRENCY=1
FRAMEFLOW_CPUS=3.5
FRAMEFLOW_MEMORY_LIMIT=4g
FRAMEFLOW_STOP_GRACE_PERIOD=240s
```

图像 Provider 等待不会占用核心 ASR/ffmpeg 执行槽，但两个进程仍共享容器 CPU、内存、SQLite 和 `/data`。提交前应以服务器真实网络记录成功率和 p50/p95，同时检查生图并发时 ASR 延迟，不把单次结果写成 SLA。通用 HTTP 写限流不能替代付费额度；公网部署应保留有限的 `IMAGE_DAILY_LIMIT` 和 `IMAGE_MAX_PENDING`，并确认供应商单张价格、失败计费、商业授权、内容审核与数据保留条款。完整契约和验收项见 `IMAGE_GENERATION.md`。

修改后执行：

```bash
chmod 600 deploy/.env
bash deploy/upgrade.sh
docker compose --env-file deploy/.env logs --tail=200 frameflow
```

只在人工受控环境发送一张非敏感测试图。默认 pytest、CI、smoke 和验收脚本不得调用真实付费接口。

### 阿里百炼 Paraformer 文件转写

```dotenv
FRAMEFLOW_ASR_PROVIDER=dashscope
FRAMEFLOW_ASR_MODEL=paraformer-v2
DASHSCOPE_API_KEY=仅保存在服务器的新Key
FRAMEFLOW_DASHSCOPE_BASE_URL=https://你的专属Host/api/v1
FRAMEFLOW_ASR_PUBLIC_BASE_URL=https://你的FrameFlow域名
FRAMEFLOW_ASR_URL_SIGNING_SECRET=至少32字节的随机字符串
FRAMEFLOW_ASR_TIMEOUT=600
```

Paraformer 需要从公网 HTTPS 读取待转写文件。FrameFlow 会先用 ffmpeg 生成 8 kbps MP3，并按 75 秒切片，减少跨境回源单次传输量；每个切片只通过带 HMAC 签名和有效期的临时 URL 暴露，原始上传目录仍保持私有。Caddy Basic Auth 仅豁免 `/api/v1/asr/source/*`，其他页面和 API 继续受保护。该优化不能消除境外主机到国内云服务的链路抖动，因此 Paraformer 是可选云端方案，不是当前公网主路径。修改已有部署后需再次执行 `bash deploy/configure-auth.sh enable` 以刷新鉴权片段，然后重建应用。不要将签名密钥与 DashScope Key 提交到 Git。

## 4. 常用运维

```bash
make ps
make health
make logs
make restart
make config
```

不使用 `make` 时，Caddy 模式的等价命令以 `docker compose --env-file deploy/.env` 开头；
external-nginx 模式请优先使用 `deploy/upgrade.sh`、`deploy/smoke.sh`、`deploy/backup.sh`
和 `deploy/restore.sh`，它们会自动加载额外 Compose 覆盖并确保不启动 Caddy。

## 5. 备份与恢复

为保证 SQLite WAL、数据库和上传素材一致，备份会短暂停止 FrameFlow，把业务数据复制到临时卷并执行 SQLite `integrity_check` 与 `foreign_key_check`；普通备份随后立即恢复应用，再从临时卷压缩，从而缩短停机时间。

```bash
bash deploy/backup.sh
```

归档默认写入 `backups/frameflow-UTC时间.tar.gz`，同时生成同名 `.sha256` 校验文件。脚本会预检 Docker 数据盘和归档目录空间，默认排除 `/data/models`、`/data/cache` 与用户缓存，并按配置的天数和数量清理旧备份。归档和校验文件必须一起同步到服务器之外。

恢复会切换当前数据卷，必须显式确认：

```bash
bash deploy/restore.sh backups/frameflow-20260713T120000Z.tar.gz --force
```

恢复脚本先在全新隔离卷验证 SHA-256、路径安全、文件类型和 SQLite 完整性，再通过 `DATA_VOLUME_NAME` 切换新卷，不清空原卷。新数据未通过应用、边缘代理或公网 smoke 时会自动切回原卷；成功后也会保留原卷和恢复前安全备份，业务验收完成前不要删除。

## 6. 升级与回滚

```bash
bash deploy/upgrade.sh
```

流程为：`git pull --ff-only` → 构建新镜像 → 一致性备份 → 替换容器 → 应用/边缘代理/公网发布验收。旧镜像保留为 `frameflow:rollback-*`；任一必需检查失败时自动回滚应用镜像并再次执行完整验收。

固定源码、无需拉取时：

```bash
SKIP_GIT_PULL=1 bash deploy/upgrade.sh
```

## 7. 本地 ASR（公网主路径）

在 `deploy/.env` 设置：

```dotenv
INSTALL_LOCAL_ASR=true
FRAMEFLOW_ASR_PROVIDER=local
FRAMEFLOW_WHISPER_MODEL=small
FRAMEFLOW_WHISPER_DEVICE=cpu
FRAMEFLOW_WHISPER_COMPUTE_TYPE=int8
FRAMEFLOW_WORKER_CONCURRENCY=1
FRAMEFLOW_CPUS=3.5
FRAMEFLOW_MEMORY_LIMIT=4g
```

然后运行升级脚本。模型首次使用时会下载到 `HF_HOME=/data/models/huggingface`，该目录随 `frameflow_data` 卷持久化。当前专用 4 核 / 8 GB 公网机使用 `small/int8`、单 Worker，并给容器分配 3.5 CPU / 4 GB；一次 71 秒热机测试的 ASR 阶段约 20.5 秒、Gemini 语义增强约 3.1 秒、完整流程约 26 秒。该记录受音频、CPU 调度、模型热身和磁盘缓存影响，不应外推为 SLA。首次下载完成后模型无需每次重新获取；升级时不要删除 `/data` 模型缓存。

### 本地 Embedding

```dotenv
INSTALL_LOCAL_EMBEDDINGS=true
EMBEDDING_PROVIDER=local
FRAMEFLOW_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
FRAMEFLOW_EMBEDDING_DEVICE=cpu
FRAMEFLOW_MEMORY_LIMIT=4g
```

修改后运行 `bash deploy/upgrade.sh`。Compose 会把 `INSTALL_LOCAL_EMBEDDINGS` 传入 Docker 构建；模型缓存仍位于持久化的 `HF_HOME`。若依赖或模型不可用，业务会回退到字符 n-gram，但部署验收仍应检查运行记录中的实际算法。

## 8. 故障排查

### 证书没有签发（Caddy 模式）

检查 DNS、80/443、错误 AAAA 记录，再查看：

```bash
docker compose --env-file deploy/.env logs --tail=200 caddy
```

仅使用 IP 地址无法申请普通公网域名证书。

### FrameFlow 一直 unhealthy

```bash
docker compose --env-file deploy/.env logs --tail=200 frameflow
docker compose --env-file deploy/.env exec frameflow python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready').read().decode())"
```

就绪检查依赖 Worker 心跳。API 存活但 Worker 异常时，容器会保持 unhealthy，日志会给出原因。

### 边缘代理或公网 smoke 失败

```bash
docker compose --env-file deploy/.env logs --tail=200 caddy
docker compose --env-file deploy/.env exec caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
bash deploy/smoke.sh
```

Caddy 模式使用上述命令；external-nginx 模式改查宿主机 Nginx 配置和日志，并确认回环上游：

```bash
ss -ltnp | grep ':8080'
nginx -t
systemctl reload nginx
docker compose --env-file deploy/.env logs --tail=200 frameflow
bash deploy/smoke.sh
```

公网检查仍以真实域名 HTTPS 为准，重点核对 DNS A/AAAA、80/443、防火墙、Nginx
响应头和证书签发日志。

### 数据卷权限

应用以 UID 10001 运行，默认具名卷会继承正确权限。若改为宿主机 bind mount，需要把目录授权给 UID 10001，且不得把数据库放在不可靠支持 SQLite 文件锁的存储上。

## 9. 安全与扩展边界

- Caddy 模式由 Caddy 终止 TLS，应用端口 `8000` 只存在于 Compose 内部网络；external-nginx 模式由宿主机 Nginx 终止 TLS，应用只发布到 `127.0.0.1:8080`。
- 应用以非 root 用户运行；两个容器均启用只读根文件系统、`no-new-privileges`、能力裁剪、PID/CPU/内存限制和日志轮转。
- 当前演示版已有单管理员应用登录、CSRF 防护、整站 Basic Auth 与单进程读写限流，但没有租户隔离、RBAC、分布式配额或恶意内容扫描；不要把它当作多租户授权系统。
- 删除、上传和演示故障注入接口由管理员会话统一保护，但没有更细的资源归属权限。公网演示环境应保持默认 Caddy Basic Auth，并建议叠加云防火墙白名单、VPN/Tailscale；不要把它当作匿名公共 SaaS。
- Caddy/Nginx 安全响应头不能替代应用鉴权与上传内容治理。
- 多实例、高可用或大规模素材库应迁移到 PostgreSQL、对象存储、专用队列与独立 Worker；不要让多个容器并发写同一个 SQLite 文件。
- 当前公网容器保持 `FRAMEFLOW_WORKER_CONCURRENCY=1`；本地 ASR 和 ffmpeg 预览会争用 CPU/内存，应优先提高单 Worker 配额，提高并发前必须压测。
