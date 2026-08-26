---
name: ip12-value-proposition
description: Run IP12 module 3 when confirmed positioning and persona facts must become three concise, selectable value propositions.
metadata:
  module: "3"
  contract_version: "1.0.0"
  prompt_version: "module-03-value-v1"
  checkpoint_count: "2"
  executor: "ip12_harness"
---

# 价值主张提炼

复用已确认的定位、人设、优势、价值观、目标人群、领域和未来目标，凝练用户真正能够持续表达的价值主张。

## Harness 断点

1. 提炼 3–5 个价值关键词，不重复询问已经确认的内容。
2. 生成恰好三个价值主张候选。每项包含一句核心表达、价值摘要、推荐理由和局限提醒；最多推荐一项。

## 完成标准

用户明确选择一个候选后，Harness 保存选择快照并进入模块 4。

## 边界

- 价值必须能追溯到用户事实或明确目标。
- 不承诺收入、就业、客户结果、市场效果或尚未验证的能力。
- 每轮只处理当前断点；未经确认不得推进。
