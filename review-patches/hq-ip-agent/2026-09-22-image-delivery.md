# IP12 图片任务交付修复

## 审查范围

- 来源仓库：`hq-ip-agent`
- 来源基线：`1f95c6339e169e2e3b7f894f5e878c3e7c158ee2`
- 来源提交：`ca766cd`（`fix(ip12): deliver image task products`）
- 审查补丁：`2026-09-22-image-delivery.patch`
- 补丁 SHA-256：`B66CB8E7133CFC1519BAC5499BB92A5CBF86062A8E0B703C5BF6BF14460A1029`
- 应用后 Git tree：`5e12e0ba802c5b6c6b9c119725c61781348d2294`
- 本 PR 仅提交审查材料，不合并、不部署。

## 事实结论

任务 9807 的图片生成本身成功，结果载荷包含有效图片 URL；故障发生在 IP12 交付适配层：纯图片结果虽然被分类器识别，却没有写入历史消息的 `images`，实时 `delivery` 事件也固定发送空图片数组。任务面板兜底又只提取视频，因此 Agent 随后的“图片卡片已发出”与真实界面不一致。

## 修复内容

1. 按业务域选择真正的成品媒体，同时兼容 `image/video/audio` 短域名和 `hq-*` Agent id，避免副产物混发。
2. 图片任务完成时，把图片写入历史消息 `images` 和 SSE `delivery.images`；刷新后仍可恢复。
3. 任务面板按业务域提取产物，不再只兜底视频。
4. 对旧版已经写入任务号但漏掉图片的历史消息，从持久化任务台账自动补回图片；已有任务无需重新生成或再次扣点。
5. completed 状态但没有提取到可展示产物时，明确禁止 Agent 声称图片或卡片已经发送。

## 验证

- `python tests/hq-job-watcher-test.py`：通过，覆盖历史消息、实时 SSE、任务面板、断线恢复、旧会话自愈和无产物事实约束。
- `python tests/hq-delivery-fallback-test.py`：通过。
- `python tests/hq-p0c-test.py`：通过。
- `python ../ip12-image-delivery-diagnosis/repro_image_delivery.py`：通过。
- `python -m py_compile agent/v4/delivery.py app.py tests/hq-job-watcher-test.py`：通过。
- `git diff --check`：通过。
- 零上下文补丁在来源基线的干净临时 worktree 中通过 `git apply --unidiff-zero --check`，应用后 tree 与来源提交完全一致。

本机完整回归另有三类与本补丁无关的既有环境限制：Windows C:/E: 跨盘 `relpath`、缺少 `fakeredis`、缺少 wqy 中文字体。Node Playwright 包在本机未安装，浏览器套件交由主站 CI 执行。

## 上线边界

审核通过后再由用户部署。此 PR 不修改线上会话、不补发任务、不执行合并；部署后状态恢复会自动让旧任务 9807 的图片重新出现在对话中。
