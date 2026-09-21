# IP12 模板横向卡片改版审阅单

## 背景

IP12 v4 由独立 `hq-ip-agent` 仓库维护，主站只反向代理 `/workbench/ip12/`，不保存该服务的运行副本。独立仓库当前只有服务器 bare 远端，无法直接创建 GitHub PR。

本目录中的 `2026-09-21-template-carousel.patch` 是相对下述固定基线生成的完整 Git patch，供主站 PR 审阅。审核通过前不得推送到生产 bare 远端，也不得部署。邮件头中的本地提交号只用于生成补丁，不作为跨仓库身份；交付身份以“基线提交 + 补丁 SHA-256 + 结果树 SHA”共同确定。

## 用户可见变化

- 22 个模板由纵向长列表改为横向卡片轨道，桌面端一屏约 4 张。
- 点击卡片只切换当前浏览项，不会提前提交选择。
- 当前模板的大预览、名称、排版、时长、说明与操作统一放在下方详情区。
- 支持左右按钮、键盘方向键、触控横滑与鼠标滚轮横向浏览。
- 移动端一屏显示约 1.1 张卡片，并把详情区切成单列。
- 模板目录展开时隐藏“返回最新对话”，避免遮挡操作区。

## Patch 边界

基线提交：`hq-ip-agent@4a46560955c84127253896382c1433e51569443b`

补丁 SHA-256：`086bca728fb351a738c1e82a1bf2c7781c3c414f3045f33ab54228cb4691d860`

预期结果树：`72f0b2d92b343940c8dcd4b71cd748e8613894c6`

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
- 浏览器回归中，本次新增的模板目录断言全部通过，覆盖 22 张卡、双向按钮、键盘方向键、触控轨道、鼠标滚轮及边界放行、详情联动、播放拒绝/结束、切换和收起时停止媒体、浏览态与已选态的无障碍区分、确认使用、会话恢复、重新展开、遮挡处理和 390px 移动端布局。
- 浏览器全套仍有 1 条既有失败：Emoji 扫描会命中旧文案“全屏预览 ↗”；与本 patch 无关。
- 固定基线 `4a46560955c84127253896382c1433e51569443b` 的同一浏览器套件也只有上述 Emoji 失败，证明本补丁没有新增浏览器回归。
- 桌面 1440×1000 与移动端 390×844 截图人工检查通过。

## 审核通过后的应用方式

在干净、基于固定基线的 `hq-ip-agent` 工作区执行：

```bash
git checkout 4a46560955c84127253896382c1433e51569443b
sha256sum /path/to/huangque-main-site/review-patches/hq-ip-agent/2026-09-21-template-carousel.patch
git apply --index /path/to/huangque-main-site/review-patches/hq-ip-agent/2026-09-21-template-carousel.patch
git write-tree
```

SHA-256 必须等于 `086bca728fb351a738c1e82a1bf2c7781c3c414f3045f33ab54228cb4691d860`，`git write-tree` 必须等于 `72f0b2d92b343940c8dcd4b71cd748e8613894c6`。验证后在独立仓库的审阅/发布分支提交，并先推送到其权威 bare origin；后续部署只能固定该已推送提交并再次核对结果树。本 PR 自身不推送独立仓库，也不部署任何文件。
