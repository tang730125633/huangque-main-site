# 工单 · 影视级模仿(motion)「重新生成」跳转错误+回填失败修复（VID-REGEN-MOTION）

> 供其他 agent 独立执行。先读《工单-其他agent实施》§0 +《任务看板》+ `DESIGN.md`。
> **冲突组：C（`site/workbench/assets.html`）+ B（`site/workbench/video.html`）** —— 抢 `lock/C` + `lock/B`。
> 来源：Issue #415（yuelei-dev, bug）。关联已完成 VID-REGEN-CLONE（#416，口播重生成回填）——这是它的 **motion 版**。

## 背景
资产库里**影视级模仿(motion)**视频点「重新生成」跳转异常、参数未正确回填、内容读不出。口播的重生成回填已由 VID-REGEN-CLONE(#416) 修好，但 motion 分支还坏着。

## 根因（yuelei 定位，实施时以当前代码为准复核）
1. **mode 字段可能为空**：`assets.html` 的 `regenPayload()` 里 `mode = String(x.mode||'text')`。若 `video_assets` 表中 motion 视频的 mode 为 null → 回退成 `text`（数字人口播）→ **跳到错误模式**（跳去口播页而非 motion）。
2. **文件预填静默失败**：`video.html` 的 `fetchImagePrefill/fetchFilePrefill` 拉原形象图+参考视频，若原文件 URL 过期/网络异常，`Promise.allSettled` 不报错也无提示 → 用户看到空白、不知发生了什么。

## 改动
### `site/workbench/assets.html`
- `regenPayload()`：motion 视频的 mode 兜底判断——不能简单 `||'text'`。参照已存的 VID-REGEN-MODE(#333)/VID-REGEN-CLONE(#416) 逻辑：优先用记录的 mode，为空时按 motion 特征字段（有 `reference_video_file`/`reference_video_url` → motion）推断，避免误落口播。

### `site/workbench/video.html`
- `fetchImagePrefill`/`fetchFilePrefill` 失败时给**明确 toast 提示**（如"原素材已过期，请重新上传形象图/参考视频"），而不是静默空白。

### 数据修补（可选，运维侧）
- `video_assets` 表历史 motion 视频若 mode 为空，按 reference_video 特征回填 mode='motion'（一次性脚本，参照 VID-FRAME-COVER 的历史回填思路）。

## 验收标准
1. motion 视频点「重新生成」→ **跳到 motion(影视级模仿)页**、不再误落口播；文案/形象图/参考视频/比例/时长/motion 强度正确回填。
2. 原素材 URL 过期/拉取失败时，**有明确提示**，不再空白无声。
3. 口播重生成（VID-REGEN-CLONE #416）不回归。

## 部署
纯前端两页 → ship `site/`。改 workbench 页记得 `python scripts/stamp_assets.py`。（历史数据修补如做，走运维脚本、不入 git 主流程。）
