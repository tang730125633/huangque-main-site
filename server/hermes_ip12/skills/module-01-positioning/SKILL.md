---
name: ip12-positioning-diagnosis
description: Run IP12 module 1 when a confirmed intake must become evidence-based positioning keywords and three selectable positioning directions.
metadata:
  module: "1"
  contract_version: "1.0.0"
  prompt_version: "module-01-positioning-v1"
  checkpoint_count: "2"
  executor: "ip12_harness"
---

# 定位诊断

把已确认的真实经历、至少两项核心技能、长期兴趣、价值观或帮助目标，以及目标人群，整理成可确认的定位结果。

## Harness 断点

1. 提炼 3–5 个核心关键词。信息不足时只追问一个尚未回答、最有价值的问题。
2. 生成恰好三个差异化定位候选。每项包含简短名称、定位摘要、推荐理由、风险提醒；最多推荐一项，但选择权属于用户。

## 完成标准

用户明确选择一个候选后，Harness 保存选择快照并进入模块 2。

## 边界

- 只使用用户原话和已确认资料，不编造行业成绩、市场案例或专家身份。
- 未来目标必须保持为未来目标，不能写成既有能力或成果。
- 每轮只处理当前断点；用户可随时修改，未经确认不得推进。
