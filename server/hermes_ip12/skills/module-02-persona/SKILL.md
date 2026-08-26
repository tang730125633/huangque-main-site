---
name: ip12-persona-design
description: Run IP12 module 2 when confirmed positioning must become three evidence-based persona directions without re-asking module 1 facts.
metadata:
  module: "2"
  contract_version: "1.0.0"
  prompt_version: "module-02-persona-v1"
  checkpoint_count: "2"
  executor: "ip12_harness"
---

# 人设塑造

复用模块 1 已确认的经历、行为、定位、价值观和目标人群，形成可传播且不夸大的个人表达方向。

## Harness 断点

1. 提炼人格关键词与核心价值观；允许用户修正，不重复采集已有资料。
2. 生成恰好三个人设候选。每项包含名称、核心特质、故事基调、传播标签、优势和风险；最多推荐一项。

## 完成标准

用户明确选择一个候选后，Harness 保存选择快照并进入模块 3。

## 边界

- 不使用未经用户确认的心理判断、天赋评价或身份包装。
- 不把愿景写成当前人设事实，不把目标人群写成已有客户。
- 每轮只处理当前断点；未经确认不得推进。
