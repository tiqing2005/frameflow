# FrameFlow AI 配套测试素材包

本目录与 docs/FRAMEFLOW_TEST_GUIDE.html 配套使用。

## 立即可用

| 文件 | 用途 |
| --- | --- |
| main-flow-text.txt | 文本主流程：分段、关键词、匹配、编辑与刷新 |
| failure-retry-text.txt | job_fail、ai_degrade 故障演练 |
| segmentation-boundary.txt | 中英文标点、空行、长无标点文本的分段边界 |
| low-relevance-text.txt | 验证低相关补位是否被如实标记 |
| idempotency-cases.json | 幂等 A/B/C 三组请求体 |
| frameflow-api-tests.http | health、dashboard、创建和幂等冲突的 HTTP 请求模板 |
| asset-upload-metadata.csv | 上传图片/视频时可直接填写的名称、标签和关键词 |
| invalid-upload.svg | 验证用户 SVG 上传被拒绝 |
| fake-image.jpg | 文本伪装成 JPG，验证真实文件签名检查 |
| generate-oversize-file.ps1 | 在隔离目录生成 101 MB 超限测试文件 |
| execution-record.md | 测试执行与 GO / NO-GO 记录模板 |
| evidence-shot-list.md | 截图和录屏证据清单 |
| frameflow-video-test-pack.zip | 便于一次下载/传给验收人员的视频测试包；内容与 `videos/` 下 5 个样例一致 |

## 仓库中现成的二进制素材

这些文件不重复复制，避免让仓库产生无意义的大文件副本：

- 真实中文音频：../../demo/sample-zh.wav
- 城市图片：../../backend/seed_media/city.jpg
- 团队图片：../../backend/seed_media/teamwork.jpg
- 自然图片：../../backend/seed_media/nature.jpg
- 办公短视频：../../backend/seed_media/video-focus-work.mp4
- 智慧城市短视频：../../backend/seed_media/video-smart-city.mp4
- 森林短视频：../../backend/seed_media/video-forest-breath.mp4

## 推荐使用顺序

1. 使用 main-flow-text.txt 完成核心闭环。
2. 上传 city.jpg，元数据使用 asset-upload-metadata.csv 的第一行。
3. 上传 video-focus-work.mp4，元数据使用 CSV 的第二行。
4. 使用 invalid-upload.svg 和 fake-image.jpg 验证拒绝路径。
5. 在专用临时目录运行 generate-oversize-file.ps1，测试 100 MB 上限。
6. 使用 failure-retry-text.txt 依次演示 ai_degrade 与 job_fail。
7. 用 execution-record.md 和 evidence-shot-list.md 保存证据。

`frameflow-video-test-pack.zip` 是面向非 Git 验收人员的便携副本；仓库内自动化或开发调试应直接使用 `videos/`，避免重复解压。

## 安全说明

- 不要把真实 .env、API Key 或 Authorization Header 写入任何测试材料。
- job_fail、删除、取消、Worker 中断、超限上传和备份恢复只在隔离环境执行。
- frameflow-api-tests.http 中的幂等创建会写入一个真实测试项目。
- generate-oversize-file.ps1 默认只在本目录生成 oversize-101mb.bin，使用后请手动删除。
