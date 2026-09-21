# IP12 模板横向卡片改版审阅单

## 背景

IP12 v4 由独立 `hq-ip-agent` 仓库维护，主站只反向代理 `/workbench/ip12/`，不保存该服务的运行副本。独立仓库当前只有服务器 bare 远端，无法直接创建 GitHub PR。

本目录中的 `2026-09-21-template-carousel.patch` 是从独立仓库本地提交 `2d5a957` 生成的完整 Git patch，供主站 PR 审阅。审核通过前不得推送到生产 bare 远端，也不得部署。

## 用户可见变化

- 22 个模板由纵向长列表改为横向卡片轨道，桌面端一屏约 4 张。
- 点击卡片只切换当前浏览项，不会提前提交选择。
- 当前模板的大预览、名称、排版、时长、说明与操作统一放在下方详情区。
- 支持左右按钮、键盘方向键、触控横滑与鼠标滚轮横向浏览。
- 移动端一屏显示约 1.1 张卡片，并把详情区切成单列。
- 模板目录展开时隐藏“返回最新对话”，避免遮挡操作区。

## Patch 边界

基线提交：`hq-ip-agent@4a46560`

目标提交：`hq-ip-agent@2d5a957`

涉及文件：

- `agent/v4/subagent.py`
- `static/style.css`
- `static/v4.html`
- `static/v4.js`
- `tests/hq-ip12-test.mjs`
- `tests/hq-upload-test.py`

## 验证结果

- `node --check static/v4.js`：通过。
- `node --check tests/hq-ip12-test.mjs`：通过。
- `python -m py_compile agent/v4/subagent.py`：通过。
- `python tests/hq-upload-test.py`：通过。
- `python tests/hq-backend-pump-test.py`：通过。
- 浏览器回归中，本次新增的 13 项模板目录断言全部通过，覆盖 22 张卡、横向滚动、详情联动、确认使用、遮挡处理和 390px 移动端布局。
- 浏览器全套仍有 1 条既有失败：Emoji 扫描会命中旧文案“全屏预览 ↗”；与本 patch 无关。
- 桌面 1440×1000 与移动端 390×844 截图人工检查通过。

## 审核通过后的应用方式

在干净、基于 `4a46560` 的 `hq-ip-agent` 工作区执行：

```bash
git am /path/to/huangque-main-site/review-patches/hq-ip-agent/2026-09-21-template-carousel.patch
```

应用后重新运行上述测试，再按独立服务发布流程部署。本 PR 自身不部署任何文件。
