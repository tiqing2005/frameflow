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

核心字幕闭环不把任一外部网关作为单点依赖：文本输入可用确定性规则完成分段和匹配，媒体输入则需要本地 ASR 或已配置的真实 ASR Provider。当前公网部署的运行时策略是：

- 音视频转写：本机 CPU 上运行 `faster-whisper small/int8`，不把原始媒体交给外部 ASR。
- 字幕语义增强：以 `gemini` Provider 调用 `gemini-3.1-flash-lite-preview`，底层传输采用 OpenAI-compatible `/chat/completions`；超时、格式错误或不可用时回退规则。
- 字幕分段：确定性标点、换行、长度和短句合并规则。
- 关键词：本地中文分词/频率策略。
- 默认相似度：字符 n-gram TF-IDF 稀疏向量余弦相似度；安装本地 BGE 或配置远程 `/embeddings` 后，才会升级为神经 Embedding 余弦相似度。
- 混合匹配：`0.55 × 相似度 + 0.30 × 关键词重合 + 0.15 × 标签/主题重合`；Embedding 不可用时自动回退字符相似度。
- 素材自动标签：对图片发送一张规范化画面，对视频优先使用 poster（必要时抽取单帧）；视觉失败后依次回退纯文本 LLM 和确定性规则。
- 文生图：仅在用户主动提交时，由独立 Image Worker 调用独立的 OpenAI-compatible 图像 Provider；成功结果先作为私有草稿，用户确认后才进入素材库或当前字幕片段。
- 运行记录按实际路径标识 `faster-whisper`、`gemini`、Embedding Provider、`vision`、`text_llm`、`rules`、`ffmpeg` 或图像 Provider，不把规则回退记成模型成功。

其中语音识别、语义增强和已配置的单画面视觉标签都可产生真实模型调用；文生图则是用户显式触发的独立付费能力。公网当前素材匹配运行记录仍为 `char-ngram-tfidf`，所以不能宣称生产环境正在使用 BGE 或远程神经 Embedding。单画面标签也不能包装成对整段视频、多镜头或动作时序的完整多模态理解。

## 3. 为什么把规则作为主干

1. 面试现场不应因 API Key、余额、网络或 Provider 限流无法走完核心闭环。
2. 新字幕的输出必须可重现，方便单元测试和故障定位。
3. 混合公式的每个分量都可向面试官解释，且可保存命中词证据。
4. 当前素材库规模很小，使用本地 TF-IDF 比新增向量数据库更符合比例原则。

代价是默认匹配对抽象隐喻、跨语言同义改写的理解有限；视觉标签又只观察一张图片或视频 poster/单帧，不能覆盖完整时序。上述限制已在 `KNOWN_ISSUES.md` 中披露。

## 4. Provider 的接入边界

当前适配器通过下列稳定边界与业务模型解耦：

```text
Transcriber.transcribe(file) -> transcript + provider/model identity
SegmentEnhancer.enhance(text) -> schema-validated segments/topics/keywords
Embedder.embed(texts) -> versioned vectors
VisionTagger.suggest(jpeg_frame) -> schema-validated tags/keywords
ImageGenerator.generate(prompt, aspect_ratio) -> normalized PNG draft
```

接入时必须同时完成：

- 密钥只来自后端环境变量/密钥管理服务。
- 为外部请求设置连接、读取和总超时。
- 对结构化输出做 Schema 校验，失败有限重试后回退规则。
- 记录 provider、model、prompt/strategy version、input hash、latency、status 和 fallback_used。
- 对转写文本和 AI 输入设置保留期，不在通用日志中输出完整原文。
- 规则结果仍保留为降级路径，不让整条业务线与单一 Provider 绑定。
- ASR、文本 LLM、Embedding、Vision 和 Image 使用彼此独立的配置与密钥，禁止隐式继承或把密钥放入 `VITE_` 构建变量。
- 视觉输入只允许可信素材路径中的一张规范化 JPEG；文生图只接受受限 Base64 JSON 并在完整解码后统一为 PNG，不跟随任意结果 URL。

## 5. ASR 真实边界

音频/视频上传是真实的：后端接收文件、安全存储并创建异步任务。公网部署使用 `faster-whisper small/int8` 本地转写；当前专用服务器给容器分配 3.5 CPU / 4 GB，一次 71 秒测试音频的 ASR 阶段约 20.5 秒。该数据只描述当前样本与热机环境，不是性能 SLA，首次模型下载、长音频和并发排队会更久。

DashScope Paraformer-v2 保留为可选云端方案。为降低新加坡服务器到国内服务的跨境回源风险，适配器会先转为 8 kbps MP3，并按 75 秒切片，再通过 HMAC 临时 URL 让 Provider 拉取；网络路径仍由双方链路决定，因此它不是公网默认方案。未安装本地模型且未配置其他可用 Provider 时，任务会以 `ASR_NOT_CONFIGURED` 失败，不使用文件名、预置文本或假延时伪装转写。

语义增强的同一轮生产样本约耗时 3.1 秒，完整异步流程约 26 秒；实际记录应以运行页面中的 provider、model、latency 和 degraded 标志为准。Gemini 是当前配置而非业务硬依赖；DeepSeek 可按同一 Provider 边界替换。

## 6. 视觉标签与文生图边界

### 6.1 单画面视觉标签

- `VISION_PROVIDER` 默认是 `none`，不会把素材画面发送到外部服务；只有显式设置支持的视觉 Provider 并配置独立 `VISION_API_KEY` 才启用调用，`VISION_BASE_URL` 与 `VISION_MODEL` 也应按实际网关核对。
- 图片只发送一张规范化 JPEG；视频优先发送已经生成的 poster，不可用时才尝试抽取单帧。系统不会上传整段视频，也不声称理解镜头间关系或动作时序。
- 视觉成功时记录 `source=vision`；失败后依次调用纯文本 LLM 和本地规则，后两级会如实标记为降级。素材详情允许重新生成和人工修改，模型输出不是不可覆盖的事实。
- 画面会经过部署者选择的第三方网关。敏感素材应关闭视觉 Provider 或由用户同时填写标签与关键词，第三方的数据保留、训练及合规政策需另行审核。

### 6.2 独立文生图 Provider

- 文生图使用独立的 `IMAGE_API_BASE_URL`、`IMAGE_API_KEY`、`IMAGE_MODEL` 和单并发 Image Worker，不复用 LLM、ASR、Embedding 或 Vision 密钥。提示词及返回图片会经过第三方图像网关。
- 系统不会因为候选分数低而静默调用付费模型。用户必须主动提交；结果先进入私有草稿区，确认后才原子加入素材库、排队标签，并可选择写入当前字幕片段。
- Provider 只允许返回一个 `b64_json` 结果。后端限制 HTTP 响应、Base64 解码字节、最终文件大小和像素数，完整解码、移除元数据并归一化为 PNG 后才可展示或入库。
- 上游通常不返回可信费用明细和连续进度。读超时、断流或 5xx 可能意味着请求已接单并计费，因此系统不会自动盲重试；界面会提示“结果未知/可能已计费”，只有用户确认才再次调用。若 Provider 不支持幂等键，人工重试仍可能产生第二次费用。
- 当前使用每日生成数、待处理任务数、固定单并发、最多尝试次数、租约与持久化阶段屏障控制风险；这不是完整的企业预算、内容审核或计费系统。

## 7. 演示故障注入的性质

`ai_degrade` 和 `job_fail` 是有意设计的一次性故障注入，用于在无法稳定依赖外部 Provider 的演示环境中，可重复检验错误反馈、降级标记与重试。

它们不证明某个真实外部 Provider 已被调用，也不应在演示解说中说成“刚才 OpenAI/某模型真的宕机”。正确说法是：

> 这是可重复的故障演练入口，用来证明状态、降级和重试路径真实存在；它不代表当前配置的 Gemini、DeepSeek 或 ASR Provider 真的发生了故障。

## 8. 面试中的简短披露模板

> 我使用 Codex 辅助了需求拆解、代码初稿、测试和文档，交付前对关键流程和故障路径进行复跑。公网音视频由本地 faster-whisper 转写，字幕语义增强以 `gemini` Provider 经 OpenAI-compatible 协议调用；分段校验和素材匹配仍有确定性规则与字符 TF-IDF 主干，公网当前没有把匹配伪装成神经 Embedding。素材标签只对单张图片或视频 poster/单帧做视觉识别，失败会回退文本 LLM 和规则；文生图使用用户显式触发的独立 Provider，提示词、隐私和可能费用均有明确提示。运行记录如实保存实际 Provider 与降级状态，外部 AI 不是字幕核心闭环的单点依赖。
