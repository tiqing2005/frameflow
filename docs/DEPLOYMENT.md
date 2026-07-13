# FrameFlow 单机部署手册

本文描述随仓库交付的 Linux VPS 方案：一个 FrameFlow 容器运行 FastAPI API 与一个持久化 Worker，一个 Caddy 容器负责反向代理和自动 HTTPS。SQLite、上传文件、种子素材和可选模型统一保存在 Docker 具名卷的 `/data`。

> 适用边界：作品演示、招聘作业验收和低并发单机服务。它不是多租户生产集群方案，不应横向扩容多个 SQLite 写实例。

## 1. 前置条件

- 64 位 Linux VPS，建议 Ubuntu 22.04/24.04 或 Debian 12。
- 最低 2 核 CPU、3 GB 内存、20 GB 磁盘；同时启用本地 ASR 与 Embedding 建议 4–8 GB 内存。
- Docker Engine 24+ 与 Docker Compose v2。
- 域名 A 记录（以及存在时的 AAAA 记录）已指向服务器。
- 云安全组和防火墙放行 TCP 22、80、443；HTTP/3 另需 UDP 443。
- 80/443 未被 Nginx、Apache 等其他服务占用。

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

脚本会创建 `deploy/.env`、写入域名与同源 CORS，并在终端中要求设置两次 Demo 访问密码。密码只用于生成 bcrypt 哈希，不会以明文写入配置。随后脚本启用整站 Basic Auth、校验 Compose、构建镜像、启动服务并等待就绪。首次构建通常需要数分钟。Caddy 在 DNS 与端口正确时自动申请和续期证书。

```bash
docker compose --env-file deploy/.env ps
curl -fsS -u frameflow https://app.example.com/health/live
curl -fsS -u frameflow https://app.example.com/health/ready
```

`live` 仅表示 API 存活；`ready` 还检查 SQLite、种子素材与 Worker 心跳，是发布验收标准。

## 3. 环境变量

真实配置位于 `deploy/.env`，模板见 `deploy/.env.example`。该文件已被 Git 忽略，应限制权限：

```bash
chmod 600 deploy/.env
```

| 变量 | 作用 | 默认/建议 |
| --- | --- | --- |
| `DOMAIN` | Caddy 站点域名，不含协议 | 必填 |
| `ACME_EMAIL` | HTTPS 证书账户联系邮箱 | 必填 |
| `ENABLE_BASIC_AUTH` | 首次部署是否强制配置整站鉴权 | `true`，受控 Demo 推荐保持开启 |
| `BASIC_AUTH_USER` | Caddy 整站鉴权用户名 | `frameflow` |
| `BASIC_AUTH_HASH` | Caddy bcrypt 哈希；脚本生成并用单引号保存 | 首次部署时生成 |
| `CADDY_MAX_REQUEST_BODY` | 入口层请求体硬上限，应略大于应用上传上限 | `110MB` |
| `FRAMEFLOW_CORS_ORIGINS` | 允许的浏览器来源，逗号分隔 | `https://你的域名` |
| `FRAMEFLOW_MAX_UPLOAD_MB` | 单文件上传上限 | `100` |
| `DEMO_MODE` | 是否注册故障注入接口 | `false`，部署环境保持关闭 |
| `DATA_VOLUME_NAME` | 持久化 `/data` 的具名卷 | `frameflow_data` |
| `LLM_PROVIDER` | 语义增强 provider：`rules`、`openai-compatible`、`openai` 或 `deepseek` | `rules` |
| `LLM_BASE_URL` | OpenAI-compatible API 基址，代码会追加 `/chat/completions` | DeepSeek 示例见下文 |
| `LLM_API_KEY` | 语义增强密钥，仅注入 FrameFlow 容器 | 空 |
| `LLM_MODEL` | 语义增强模型 ID | DeepSeek 示例 `deepseek-v4-pro` |
| `LLM_TIMEOUT` | 单次语义增强请求超时秒数 | `20` |
| `INSTALL_LOCAL_ASR` | 构建时安装 `faster-whisper` | `false` |
| `FRAMEFLOW_ASR_PROVIDER` | `auto`、`openai` 或 `local` | `auto` |
| `OPENAI_API_KEY` | OpenAI 兼容语音转写密钥，仅服务端读取 | 空 |
| `FRAMEFLOW_OPENAI_BASE_URL` | OpenAI 兼容接口基址 | 官方地址 |
| `HF_HOME` | Hugging Face 与本地 ASR 模型缓存目录 | `/data/models/huggingface` |
| `INSTALL_LOCAL_EMBEDDINGS` | 构建时安装 `sentence-transformers` 等本地向量依赖 | `false` |
| `EMBEDDING_PROVIDER` | `auto`、`local`、`openai-compatible` 或 `none` | `auto` |
| `FRAMEFLOW_MEMORY_LIMIT` | 应用容器内存上限 | `3g`，本地模型按容量上调 |
| `IMAGE_API_*` | 图像生成服务预留配置 | 当前版本尚未接入业务链路 |

修改构建参数后运行 `bash deploy/upgrade.sh`；只修改运行时变量可执行：

```bash
docker compose --env-file deploy/.env up -d --force-recreate
```

不要使用 `VITE_` 前缀保存密钥：Vite 构建变量会进入浏览器静态资源。

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

关闭命令会给出风险警告并把 `ENABLE_BASIC_AUTH=false` 写回本地配置。`10-basic-auth.caddy` 被 `.gitignore` 排除，不会把站点启用状态误提交到公开仓库。Basic Auth 必须配合 HTTPS 使用；本方案由 Caddy 自动提供 HTTPS。它是受控 Demo 的入口保护，不替代正式产品的用户、租户和资源级授权。

### 请求体、资源和日志上限

Caddy 默认在入口层拒绝超过 `110MB` 的请求，应用仍按 `FRAMEFLOW_MAX_UPLOAD_MB=100` 校验单文件，两层限制不可调反。安全响应头包含 HSTS、CSP、禁止嵌入和 MIME 嗅探；CSP 只为 FastAPI `/docs`、`/redoc` 保留 jsDelivr 与官方 favicon 例外。Compose 同时限制 CPU、内存、PID、临时目录和 `json-file` 日志轮转；应用根文件系统只读，持久化写入仅允许 `/data`，临时写入仅允许 `/tmp`。可在 `deploy/.env` 调整 `FRAMEFLOW_*_LIMIT` 和 `CADDY_*_LIMIT`，但不建议取消上限。Compose 仅把应用需要的变量显式注入 FrameFlow，Caddy 的 Basic Auth 哈希和 ACME 配置不会进入应用容器。

### DeepSeek V4 Pro 示例

编辑 `deploy/.env`：

```dotenv
LLM_PROVIDER=deepseek
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=你的服务端密钥
LLM_MODEL=deepseek-v4-pro
LLM_TIMEOUT=20
```

然后重建运行容器以加载配置：

```bash
docker compose --env-file deploy/.env up -d --build --force-recreate
```

FrameFlow 调用 `${LLM_BASE_URL}/chat/completions`。模型输出必须通过严格 JSON Schema 校验，并完整保留原字幕；无密钥、超时、HTTP 错误、非法 JSON 或字幕缺失都会自动回退到确定性规则，任务仍可完成，运行记录会标为降级。`deepseek-v4-pro` 是否可用取决于你的 DeepSeek 账号或兼容网关，部署前应以供应商控制台为准。

## 4. 常用运维

```bash
make ps
make health
make logs
make restart
make config
```

不使用 `make` 时，等价命令均以 `docker compose --env-file deploy/.env` 开头。

## 5. 备份与恢复

为保证 SQLite WAL、数据库和上传素材一致，备份会短暂停止 FrameFlow；Caddy 继续运行并在此期间返回上游不可用。

```bash
bash deploy/backup.sh
```

归档默认写入 `backups/frameflow-UTC时间.tar.gz`，同时生成同名 `.sha256` 校验文件。脚本使用 `umask 077`，文件默认仅当前运维用户可读；创建后还会立即验证 tar 完整性。归档和校验文件必须一起同步到服务器之外，同机单份备份不能应对磁盘故障。备份本身未加密，若包含用户素材，应使用加密存储或在传输前加密。

恢复会清空当前数据卷，必须显式确认：

```bash
bash deploy/restore.sh backups/frameflow-20260713T120000Z.tar.gz --force
```

恢复脚本会先验证 SHA-256 和 tar 完整性，拒绝绝对路径、`..` 路径穿越、链接、设备和其他特殊文件，并在隔离临时卷完整解包。只有验证通过后才停止服务；若当前数据卷存在，脚本会自动创建一份带校验和的恢复前安全备份，再覆盖正式卷。验收恢复结果前不要删除这份安全备份。

## 6. 升级与回滚

```bash
bash deploy/upgrade.sh
```

流程为：`git pull --ff-only` → 构建新镜像 → 一致性备份 → 替换容器 → 就绪检查。旧镜像保留为 `frameflow:rollback-*`；若新版本未就绪，脚本自动回滚应用镜像。若未来发生不可逆数据迁移，仍应使用升级前备份恢复 `/data`。

固定源码、无需拉取时：

```bash
SKIP_GIT_PULL=1 bash deploy/upgrade.sh
```

## 7. 本地离线 ASR

在 `deploy/.env` 设置：

```dotenv
INSTALL_LOCAL_ASR=true
FRAMEFLOW_ASR_PROVIDER=local
FRAMEFLOW_WHISPER_MODEL=tiny
FRAMEFLOW_WHISPER_DEVICE=cpu
FRAMEFLOW_WHISPER_COMPUTE_TYPE=int8
```

然后运行升级脚本。模型首次使用时会下载到 `HF_HOME=/data/models/huggingface`，该目录随 `frameflow_data` 卷持久化。离线 ASR 显著增加镜像、下载时间、磁盘和内存占用；小 VPS 建议使用兼容的远程 ASR。

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

### 证书没有签发

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

### 数据卷权限

应用以 UID 10001 运行，默认具名卷会继承正确权限。若改为宿主机 bind mount，需要把目录授权给 UID 10001，且不得把数据库放在不可靠支持 SQLite 文件锁的存储上。

## 9. 安全与扩展边界

- Caddy 终止 TLS，应用端口 `8000` 只存在于 Compose 内部网络。
- 应用以非 root 用户运行；两个容器均启用只读根文件系统、`no-new-privileges`、能力裁剪、PID/CPU/内存限制和日志轮转。
- 当前演示版没有应用内登录、租户隔离和恶意内容扫描；已有整站 Basic Auth 与单进程读写限流，但不建议匿名长期暴露公网，也不能把它们当作多租户授权系统。
- 删除、上传和演示故障注入接口当前没有独立的资源级鉴权。公开面试 Demo 必须保持默认 Caddy Basic Auth，并建议叠加云防火墙白名单、VPN/Tailscale；不要把它当作匿名公共 SaaS。
- Caddy 安全响应头不能替代应用鉴权与上传内容治理。
- 多实例、高可用或大规模素材库应迁移到 PostgreSQL、对象存储、专用队列与独立 Worker；不要让多个容器并发写同一个 SQLite 文件。
