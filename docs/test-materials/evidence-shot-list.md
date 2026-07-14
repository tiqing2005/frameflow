# FrameFlow AI 证据截图与录屏清单

## 必拍画面

1. health/live 与 health/ready 成功响应。
2. 橙黑暖白新版“项目”页、侧栏导航、服务端指标和全新项目标题。
3. 处理页至少三个不同阶段与实时事件。
4. 原始字幕和三个不同语义片段。
5. 一个候选的总分、TF-IDF、关键词、主题分项与命中词。
6. 编辑后的“保存中 / 已保存”状态。
7. 片段排序前后。
8. 全库搜索和人工选择标记。
9. Ctrl+R 前后相同文本、顺序和选择。
10. 素材库图片/视频筛选和测试素材详情。
11. 时间线、预览任务进度和最终可播放的真实 MP4。
12. “运行记录”页面中的实际 Provider、Model、Token / 无 Token 状态。
13. ai_degrade 的 degraded 记录。
14. job_fail 的错误码、失败阶段、attempt 和 request_id。
15. retry 后 attempt 增加、历史保留、最终成功。
16. 390×844 下“字幕 / 编辑 / 候选”移动面板。
17. 服务或容器重启后同一 Project ID 仍可访问。

## 文件命名建议

- 01-health-ready.png
- 02-new-project.png
- 03-processing-events.png
- 04-workbench-candidates.png
- 05-match-explanation.png
- 06-edit-saved.png
- 07-reorder.png
- 08-manual-selection.png
- 09-after-refresh.png
- 10-assets.png
- 11-preview-mp4.png
- 12-runs-provider.png
- 13-ai-degrade.png
- 14-job-fail.png
- 15-retry-success.png
- 16-mobile-workbench.png
- 17-restart-persistence.png

## 录屏安全

- 不显示 .env、API Key、Authorization Header。
- 不显示私人浏览器标签、通知、邮箱或账号头像。
- 不显示无关本地路径和终端历史。
- 故障注入必须明确说“演练”，不要包装成真实 Provider 宕机。
