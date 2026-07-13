# FrameFlow AI Web

FrameFlow AI 的中文前端工作台，使用 React 19、TypeScript 与 Vite 构建。界面只消费真实 `/api/v1` 数据，不使用静态成功态模拟。

## 本地运行

要求 Node.js 20+。先启动端口 `8000` 的 API 与 Worker，再执行：

```bash
npm install
npm run dev
```

Vite 默认把 `/api` 与 `/media` 代理到 `http://127.0.0.1:8000`。可通过 `VITE_API_PROXY` 改写开发代理目标；部署为前后端不同源时，可用 `VITE_API_BASE_URL` 改写 API 根路径。

## 页面与真实交互

- 项目台：服务端统计、最近项目、删除与状态导航。
- 新建：文本粘贴以及真实音频/视频 multipart 上传。
- 处理页：轮询持久化任务阶段与事件，支持失败重试和运行中取消。
- 三栏工作台：原始字幕、分段编辑/排序、16:9 预览、候选解释、重新匹配、素材搜索替换及自动保存反馈。
- 素材库：真实上传、搜索筛选、详情与元数据编辑。
- AI 运行记录：模型/规则降级、耗时、Token 与错误明细。
- 演示实验室：下一任务的一次性 AI 降级/任务失败控制。

移动端工作台切换为“字幕 / 编辑 / 候选”三个面板；所有页面包含加载、空、错误、禁用和 Toast 状态。

## 校验

```bash
npm run lint
npm run build
```
