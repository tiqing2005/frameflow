# 检索策略离线评测

评测集：34 条中文字幕（24 条 easy + 10 条 hard）。easy 与演示样例同源；hard 含同义改写、比喻和跨主题干扰项（negatives），用于区分纯字面匹配与语义匹配。

语义通道：向量（bge-small-zh）。

| 策略 | Hit@3 | MRR | nDCG@3 | Hard Hit@3 | 负面精度 | Top 3 未命中 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 关键词基线 | 0.853 | 0.773 | 0.780 | 0.600 | 0.800 | nature-2、hard-synonym-2、hard-synonym-3、hard-metaphor-1、hard-distractor-1 |
| 字符 TF-IDF | 0.882 | 0.672 | 0.719 | 0.600 | 0.700 | hard-synonym-2、hard-synonym-3、hard-metaphor-1、hard-distractor-1 |
| 混合排序(字符) | 0.882 | 0.757 | 0.781 | 0.600 | 0.700 | hard-synonym-3、hard-metaphor-1、hard-metaphor-2、hard-distractor-1 |
| 混合排序(向量) | 0.941 | 0.797 | 0.829 | 0.800 | 0.600 | hard-metaphor-2、hard-distractor-1 |

> 这是一组小规模、可复现的工程决策证据，不代表线上泛化指标。Hard case 的价值在于让三个策略产生区分度：字面匹配在同义/比喻上明显下滑，混合排序保留三项分数与命中词，便于人工判断。启用 embedding provider（本地 BGE 或远程 /embeddings）后，'混合排序(向量)' 行会自动出现。

运行：`python evaluation/evaluate.py`。脚本会覆盖本文件。
