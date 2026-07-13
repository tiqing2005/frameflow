# FrameFlow AI 测试执行记录

## 基本信息

- Commit SHA：
- 工作树状态：clean / dirty（附 git status --short）
- 执行时间：
- 时区：
- 操作系统：
- 浏览器与版本：
- Python：
- Node.js：
- npm：
- 测试环境：本地 / Docker / 公网
- 数据目录或 volume：
- ASR Provider / Model：
- LLM Provider / Model：

## 自动化结果

| 项目 | 命令 | 结果 | 输出/链接 |
| --- | --- | --- | --- |
| 后端 | python -m pytest | PASS / FAIL | |
| 前端 lint | npm run lint | PASS / FAIL | |
| 前端 build | npm run build | PASS / FAIL | |
| Playwright | npm run test:browser | PASS / FAIL | |
| Contract smoke | scripts/acceptance.ps1 | PASS / FAIL | |
| 排序评测 | python evaluation/evaluate.py | PASS / FAIL | |

## 真实浏览器验收

| 用例 | 结果 | Project / Job / request_id | 证据 |
| --- | --- | --- | --- |
| 文本创建与异步进度 | | | |
| 每段至少 3 个候选 | | | |
| 匹配依据与命中词 | | | |
| 编辑自动保存 | | | |
| 排序保存 | | | |
| 搜索替换 | | | |
| 手选保护 | | | |
| Ctrl+R 持久化 | | | |
| ai_degrade | | | |
| job_fail → retry | | | |
| 移动端 390×844 | | | |
| 服务/容器重启 | | | |

## 失败与问题

- 失败用例：
- 错误码：
- Job ID：
- request_id：
- 是否可重试：
- 实际原因：
- 临时绕过：
- 是否阻塞发布：

## 最终结论

- [ ] GO
- [ ] CONDITIONAL GO
- [ ] NO-GO

结论说明：
