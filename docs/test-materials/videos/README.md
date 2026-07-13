# FrameFlow 视频测试素材

这些文件均可直接拖入“上传音视频”区域，且小于 100 MB。

| 文件 | 用途 | 预期结果 |
| --- | --- | --- |
| `01_standard_mandarin.mp4` | 标准普通话、横屏 MP4 | 正常识别日期、远程办公、人工智能、数据分析 |
| `02_numbers_and_terms.mp4` | 数字、英文缩写和专有名词 | 重点观察 FrameFlow、RTX 4060、24 FPS、100 MB、API、GPU、HTTP |
| `03_noisy_office.webm` | 背景噪声、WebM/Opus | 应识别主要人声，允许少量标点或同音词差异 |
| `04_portrait_interview.mov` | 低音量、竖屏 MOV | 应完成音轨提取，并生成关于时间管理的简体字幕 |
| `05_silent_video_negative.mp4` | 无音轨负向测试 | 应得到“无可用语音”一类的明确错误，不能生成虚假字幕 |

语音由 Windows 中文语音引擎生成，仅用于功能验收，不代表真实人物录音效果。
