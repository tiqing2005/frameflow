# FrameFlow 公网双模型链路实测

## 环境

- 公网部署：新加坡 Linux VPS，专用于 FrameFlow
- 宿主机：4 核 CPU / 8 GB 内存
- 应用容器：3.5 CPU / 4 GB
- Worker：1
- ASR：`faster-whisper small/int8`，CPU `int8`
- 语义增强：`gemini-3.1-flash-lite-preview`
- Gemini 通过 OpenAI-compatible `/chat/completions` 网关传输，运行记录 Provider 为 `gemini`

真实 API Key、网关地址和服务器地址均不写入本文件。

## 真实样本

- 输入：约 71 秒中文 M4A 音频
- 项目：`d4e8bc70-091e-4705-9d01-bd19065d51e6`
- 任务：`191d2a71-ceb5-4457-aa00-1ffe705ed7b8`
- 执行时间：2026-07-14

## 结果

| 阶段 | Provider | Model | 耗时 | 状态 |
| --- | --- | --- | ---: | --- |
| 语音转写 | `faster-whisper` | `small` | 20,492 ms | succeeded，未降级 |
| 字幕语义增强 | `gemini` | `gemini-3.1-flash-lite-preview` | 3,403 ms | succeeded，未降级 |
| 素材匹配 | `hybrid-fallback` | `char-ngram-tfidf` | 23 ms | succeeded |

- 从上传创建任务到任务完成：约 26 秒。
- 任务状态真实经过 `queued → running/transcribing → segmenting → keywording → succeeded`。
- 该结果证明音频转写和 Gemini 语义增强均被真实调用，不是文件名推断、预置字幕或规则伪装。

## 结论与边界

此前中国区 DashScope 从新加坡服务器反向拉取媒体时，71 秒样本曾需要约 8 分钟。当前主链路改为服务器本地 ASR，只把转写文本发送给 Gemini，避免跨境媒体回源，并把同一样本完整流程降低到约 26 秒。

本记录是单次热机样本，不是性能 SLA。首次模型下载、冷启动、长音频、噪声、并发排队和供应商网络波动都会影响耗时。生产配置保持单 Worker，避免多个本地 Whisper 进程争抢 CPU 和内存。
