# 工单 · 数字人口播重新生成参数回填（VID-REGEN-CLONE）

> 供其他 agent 独立执行。先读《工单-其他agent实施》§0 +《任务看板》+ `DESIGN.md`。
> **冲突组：B（`site/workbench/video.html`）+ C（`site/workbench/assets.html`）** —— 抢 `lock/B` + `lock/C`。
> 来源：Issue #395（yuelei-dev）。关联已完成 VID-REGEN-MODE（#325/#333）。

## 背景
资产库对**数字人口播**类型视频点「重新生成」，跳转到视频生成页后原始参数（文案、音色、形象图、比例、生成模式）**没有回填**，页面内容为空，用户需重新手输。

VID-REGEN-MODE（#325/#333）已为 motion/tryon 实现「记 mode + 按 mode 跳对应页回填」并声称「向下兼容口播」，但口播分支实测未回填——是该功能的遗漏/回归。

## 复现
1. 资产库 → 视频资产 → 找一条数字人口播视频 → 点「重新生成」
2. 跳转后视频生成页参数全空（期望自动回填）

## 改动（沿用 #325 既有方案，别另造轮子）
- `site/workbench/assets.html`：口播视频「重新生成」跳转时，比照 motion/tryon 分支，把原始参数（口播文案 / 音色 voice / 形象图 image_file / 比例 aspect_ratio / mode=口播）通过 URL query 或 localStorage 一并携带。
- `site/workbench/video.html`：页面初始化时检查 handoff 参数，若 mode=口播 则回填对应控件（文案框 / 音色选择 / 形象图 / 比例）。复用 motion/tryon 已有的回填读取逻辑，补齐口播字段映射。

## 验收标准
1. 口播视频点「重新生成」→ 视频页文案、音色、形象图、比例、生成模式全部正确回填。
2. motion / tryon 的既有回填不受影响（不回归 #325/#333）。
3. 形象图若为 COS 私有链，回填后仍能正常预览（走既有 token blob 加载）。

## 部署
纯前端两页 → ship `site/` 静态同步。改 cloud-shell.js 无关，不占 shell 组；但注意 stamp（改 workbench 页会 bump `?v=`，提交前跑 `python scripts/stamp_assets.py`）。
