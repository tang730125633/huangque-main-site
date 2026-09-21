# IP12 模板目录重复卡片修复审阅单

## 背景与根因

IP12 v4 由独立 `hq-ip-agent` 仓库维护，主站只反向代理 `/workbench/ip12/`，不保存该服务的运行副本。独立仓库当前只有服务器 bare 远端，因此本 PR 以完整 Git patch 提交主站审核。

模板能力查询会机械挂载标准 `template_catalog`，但提示词中的“不要再调用 attach_widgets”没有程序级约束。compose Agent 仍可能再挂一张自定义 ID 的 `option_pick`，造成同一批模板同时以新版横向目录和旧版纵向列表出现；后写入的通用卡还会把标准目录折叠。

## 修复内容

- 状态层按模板 ID 集合识别语义重复目录，不依赖模型可自由改写的标题。
- 标准目录先到时，后续重复 `attach_widgets` 会被拒绝；标准目录后到时，会清理此前已经存在的重复目录。
- 主 Agent 与子 Agent 的挂卡结果返回实际 `attached/suppressed` 数量，避免把被拦截卡片报告为已显示。
- 前端增加历史会话兜底：载荷中同时存在标准目录和重复通用卡时，只渲染标准目录。
- 多选卡及与模板 ID 不重叠的正常 `option_pick` 不受影响。

## 用户可见结果

- 页面始终只显示一张“模板成片 · 全部模板”卡。
- 22 个模板继续以横向卡片轨道展示，点击卡片联动下方详情。
- 每张卡不再重复出现“选这版”按钮；仅详情区保留统一确认操作。
- 已经保存过重复卡片的历史会话，刷新后也只显示标准横向目录。
- 桌面端和 390px 移动端布局保持上一版设计不变。

## Patch 边界

基线提交：`hq-ip-agent@ea14e97`

目标提交：`hq-ip-agent@34aaf22`

涉及文件：

- `agent/v4/main_agent.py`
- `agent/v4/state.py`
- `agent/v4/subagent.py`
- `static/v4.js`
- `tests/hq-ip12-test.mjs`
- `tests/hq-upload-test.py`

## 验证结果

- `python -m py_compile agent/v4/state.py agent/v4/subagent.py agent/v4/main_agent.py`：通过。
- `node --check static/v4.js`、`node --check tests/hq-ip12-test.mjs`：通过。
- `python tests/hq-upload-test.py`：通过；新增“标准目录先到”和“标准目录后到”两种回归场景。
- `python tests/hq-backend-pump-test.py`：通过。
- `python tests/hq-widget-reselect-test.py`：通过。
- `python tests/hq-voice-sample-test.py`：通过。
- 浏览器回归中的模板目录断言全部通过，包括 22 张横向卡、历史重复卡过滤、详情联动、键盘/按钮/滚轮/触控、确认状态、恢复状态和 390px 移动端布局。
- 浏览器全套仍有 1 条既有失败：Emoji 扫描命中旧文案“全屏预览 ↗”；与本 patch 无关。

## 审核通过后的应用方式

在干净、基于 `ea14e97` 的 `hq-ip-agent` 工作区执行：

```bash
git am /path/to/huangque-main-site/review-patches/hq-ip-agent/2026-09-21-template-catalog-dedupe.patch
```

应用后重新运行上述测试，再按独立服务发布流程部署。本 PR 自身不部署任何文件；审核通过前不得推送到生产 bare 远端，也不得上线。
