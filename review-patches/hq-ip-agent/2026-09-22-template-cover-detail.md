# IP12 模板详情静态封面审核单

## 审核范围

- 来源仓库：`hq-ip-agent`
- 最新来源主线：`5991ddd45f89bcd9d779980188a81efdd17c5745`
- 已审核前置门禁修复：`6b74121b42afcb9fc3f6fd4276e16ecbf1f04787`
- 最终候选 HEAD：`f6fce31820d6de15b1052a4be7a0d33c7336a5f4`
- 最终结果树：`1ae8d0c3762fb8c5ab026206f58d2beaeaa63dde`
- 审核补丁：`2026-09-22-template-cover-detail.patch`
- 补丁 SHA-256：`37daf72c8d8015c7e7fd4f3d0d7e5dd154c982601e6d4da3a0c9b9bc89fa172d`
- 外部审核分支：`codex/pr1668-template-cover-final3-20260923`

前置门禁修复已由主站 PR 1672 版本化并合并。本 PR 只提交模板静态封面功能的可重放补丁与审核记录，不部署。

## 事实结论

模板封面数据没有缺失。上方横向卡片能够正确读取每款模板的 `image_url`；详情区此前优先把 `preview_url` 渲染为视频并显示播放状态，因此用户看到的不是模板静态封面。

## 实现内容

1. 详情区只读取当前模板的 `image_url`，显示对应静态封面。
2. 移除详情区视频、播放/暂停按钮及相关媒体状态逻辑。
3. 上方卡片与下方详情统一通过 `templateItemCover()` 规范化封面地址。
4. 缺少 `image_url`、但仍有 `preview_url` 时显示“暂无封面”，不回退为视频播放器。
5. “使用此模板”保留最新主线的 `submitWidgetChoice` / `widget_action` 协议，提交真实 widget id、代次和 item id；提交后旧模板卡失效。
6. 保留横向滚动、左右按钮、键盘方向键、恢复会话、桌面和移动端响应式行为。
7. 浏览器测试只精确 mock 只读 `GET /api/v4/assets`，不拦截写接口。

## 修改边界

- `static/v4.js`
- `static/style.css`
- `tests/hq-ip12-test.mjs`

不涉及后端、数据库 Schema、任务执行、模板数据、计费、生产会话写入或部署。

## 验证结果

- 定向 `hq-ip12-test.mjs`：全部通过。
- 完整浏览器回归：`329 PASS / 0 FAIL`。
- 完整 `tests/run_unit.sh`：通过。
- `node --check static/v4.js`、`node --check tests/hq-ip12-test.mjs`、`git diff --check`：通过。
- Standards：P0/P1/P2 = `0/0/0`，通过。
- Spec：P0/P1/P2 = `0/0/0`，通过。
- 覆盖默认封面、键盘/按钮联动、零视频/零播放入口、缺封面分支仍可结构化提交、提交后旧卡失效、恢复与响应式场景。
- 全程未连接生产数据、未调用真实 Provider、未部署。

## 审核通过后的应用方式

在干净、固定到已审核前置 HEAD `6b74121b42afcb9fc3f6fd4276e16ecbf1f04787` 的 `hq-ip-agent` 工作区执行：

```bash
sha256sum review-patches/hq-ip-agent/2026-09-22-template-cover-detail.patch
git apply --check --unidiff-zero --index review-patches/hq-ip-agent/2026-09-22-template-cover-detail.patch
git apply --unidiff-zero --index review-patches/hq-ip-agent/2026-09-22-template-cover-detail.patch
test "$(git write-tree)" = "1ae8d0c3762fb8c5ab026206f58d2beaeaa63dde"
```

应用后必须重新运行完整浏览器与单元门禁。部署必须由用户另行明确授权。
