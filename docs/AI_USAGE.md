# FrameFlow AI 中的 AI 使用说明

## 1. 开发过程中的 AI 辅助

本项目的需求拆解、架构备选方案、部分代码初稿、UI 初稿、测试用例和交付文档使用了 AI 编程助手 Codex 辅助生成。这一使用范围应在提交信息和面试中如实披露。

AI 辅助不代表：

- 代码、命令和文档无需人工复核。
- 候选人可以无法解释架构决策、数据模型或故障处理。
- 自动生成的测试名称可以代替真实执行记录。
- 可以把故障注入、样例数据或规则输出宣称为外部大模型的真实结果。

提交前，候选人应至少亲自完成：复跑安装/测试命令、在公网环境走完主流程与故障流程、逐项理解 `INTERVIEW_QA.md`，并修改任何与最终实现不一致的文档。

## 2. 运行时是否使用外部 AI

文本主流程仍不依赖外部 LLM、神经 Embedding 或 ASR 服务。当前公网部署的真实运行时策略是：

- 音视频转写：本机 CPU 上运行 `faster-whisper small/int8`，不把原始媒体交给外部 ASR。
- 字幕语义增强：以 `gemini` Provider 调用 `gemini-3.1-flash-lite-preview`，底层传输采用 OpenAI-compatible `/chat/completions`；超时、格式错误或不可用时回退规则。
- 字幕分段：确定性标点、换行、长度和短句合并规则。
- 关键词：本地中文分词/频率策略。
- “向量相似度”：字符 n-gram TF-IDF 稀疏向量余弦相似度。
- 混合匹配：TF-IDF 相似度 + 关键词重合 + 标签/主题重合。
- 运行记录按实际路径标识 `faster-whisper`、`openai-compatible`、`rules` 或 `deterministic-fallback`，不把规则回退记成模型成功。

其中语音识别与语义增强是真实模型调用；素材匹配仍是本地机器学习/信息检索方法，不应被宣称为神经语义 Embedding 或多模态视觉理解。

## 3. 为什么把规则作为主干

1. 面试现场不应因 API Key、余额、网络或 Provider 限流无法走完核心闭环。
2. 新字幕的输出必须可重现，方便单元测试和故障定位。
3. 混合公式的每个分量都可向面试官解释，且可保存命中词证据。
4. 当前素材库规模很小，使用本地 TF-IDF 比新增向量数据库更符合比例原则。

代价是对抽象隐喻、跨语言语义和素材画面本身的理解能力有限，该限制已在 `KNOWN_ISSUES.md` 中披露。

## 4. Provider 的接入边界

当前适配器通过下列稳定边界与业务模型解耦：

```text
Transcriber.transcribe(file) -> transcript + optional timestamps
SegmentEnhancer.enhance(text) -> schema-validated segments/topics/keywords
Embedder.embed(texts) -> versioned vectors
```

接入时必须同时完成：

- 密钥只来自后端环境变量/密钥管理服务。
- 为外部请求设置连接、读取和总超时。
- 对结构化输出做 Schema 校验，失败有限重试后回退规则。
- 记录 provider、model、prompt/strategy version、input hash、latency、status 和 fallback_used。
- 对转写文本和 AI 输入设置保留期，不在通用日志中输出完整原文。
- 规则结果仍保留为降级路径，不让整条业务线与单一 Provider 绑定。

## 5. ASR 真实边界

音频/视频上传是真实的：后端接收文件、安全存储并创建异步任务。公网部署使用 `faster-whisper small/int8` 本地转写；当前专用服务器给容器分配 3.5 CPU / 4 GB，一次 71 秒测试音频的 ASR 阶段约 20.5 秒。该数据只描述当前样本与热机环境，不是性能 SLA，首次模型下载、长音频和并发排队会更久。

DashScope Paraformer-v2 保留为可选云端方案。为降低新加坡服务器到国内服务的跨境回源风险，适配器会先转为 8 kbps MP3，并按 75 秒切片，再通过 HMAC 临时 URL 让 Provider 拉取；网络路径仍由双方链路决定，因此它不是公网默认方案。未安装本地模型且未配置其他可用 Provider 时，任务会以 `ASR_NOT_CONFIGURED` 失败，不使用文件名、预置文本或假延时伪装转写。

语义增强的同一轮生产样本约耗时 3.4 秒，完整异步流程约 26 秒；实际记录应以运行页面中的 provider、model、latency 和 degraded 标志为准。Gemini 是当前配置而非业务硬依赖；DeepSeek 可按同一 Provider 边界替换。

## 6. 演示故障注入的性质

`ai_degrade` 和 `job_fail` 是有意设计的一次性故障注入，用于在无法稳定依赖外部 Provider 的演示环境中，可重复检验错误反馈、降级标记与重试。

它们不证明某个真实外部 Provider 已被调用，也不应在演示解说中说成“刚才 OpenAI/某模型真的宕机”。正确说法是：

> 这是可重复的故障演练入口，用来证明状态、降级和重试路径真实存在；它不代表当前配置的 Gemini、DeepSeek 或 ASR Provider 真的发生了故障。

## 7. 面试中的简短披露模板

> 我使用 Codex 辅助了需求拆解、代码初稿、测试和文档，交付前对关键流程和故障路径进行复跑。公网音视频由本地 faster-whisper 转写，字幕语义增强以 `gemini` Provider 经 OpenAI-compatible 协议调用；分段校验和素材匹配仍有确定性规则与 TF-IDF 主干。运行记录如实保存实际 Provider 与降级状态，外部 AI 不是文本核心闭环的单点依赖。
