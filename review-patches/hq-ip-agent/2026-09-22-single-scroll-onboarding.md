# IP12 模板目录完整展示修复审核卡

## 背景与根因

IP12 v4 由独立 `hq-ip-agent` 仓库维护，主站只反向代理 `/workbench/ip12/`，不保存该服务的运行副本，因此本 PR 以可校验的 Git patch 提交主站审核。

恢复待选择模板的会话时，模板目录是消息区的纵向 Flex 子项。它此前保持默认 `flex-shrink: 1`，当五条历史消息与 22 款模板同时出现时，目录外框被压缩；外框的 `overflow: hidden` 随后裁掉横向卡片下方的模板详情。后端模板数据没有丢失，问题属于前端布局截断。

## 修复内容

- 将 `.template-catalog-box` 固定为按内容自然占高、不可参与 Flex 收缩。
- 保持主消息区为唯一纵向滚动容器，不给模板目录增加嵌套滚动条。
- 将恢复测试扩展为五条真实历史消息和 22 款模板场景。
- 在 1280×900 与 1969×1080 视口验证模板卡与详情均被外框包含、消息区存在滚动范围，并且滚到底后详情完整位于输入框上方。
- 保留 PR 1656 已有的新会话、短屏、移动端及输入框布局行为。

## 用户可见结果

- 恢复已有模板会话时，22 款横向模板卡及其下方详情不再被截断。
- 用户只需滚动聊天主区域即可查看完整模板详情，不会出现模板内部滚动条。
- 常用 900p、1080p 视口下，模板详情可完整滚动到输入框上方。

## Patch 边界

完整上游基线提交：`hq-ip-agent@0a70bd64d52e1018e665cb1fec2252d496688604`

修复提交：`edfff2887889a7073549d89237498760bd4bf6d8`

补丁 SHA-256：`df20b0daeb211b154e498b0ff86a76280897134bd919a3c59c253ecc0d16e4b4`

预期结果树：`ca47ac2b42adfc3535ce48909ed077278399816e`

涉及文件：

- `static/style.css`
- `tests/hq-ip12-test.mjs`

## 验证结果

- 红灯验证：仅加入新断言、未加入 CSS 修复时，1280×900 与 1969×1080 的 6 项布局断言全部失败。
- 绿灯验证：加入 CSS 修复后，同一组 6 项布局断言全部通过。
- 完整浏览器回归：当前上游基线为 `163 PASS / 1 FAIL`，候选为 `169 PASS / 1 FAIL`；失败项及失败套件完全相同，均为既有 Emoji 文案断言 `全屏预览 ↗`，候选未新增失败。
- `hq-widget-lifecycle-test.mjs`：通过。
- 后端专项单测逐项对照：候选与当前上游基线均为 `29 PASS / 4 FAIL`；4 个失败均由 Windows 路径/进程环境差异引起，失败测试完全相同（handover、upload、template-render、turn-recovery），候选未新增失败。
- `hq-p0c-test.py` 在候选与基线均输出 `P0C_UNIT: PASS`；Windows 下测试线程调用 shell 内建 `echo` 的 8 条噪音两侧一致。
- `node --check static/v4.js`、`node --check tests/hq-ip12-test.mjs`、`git diff --check`：通过。
- 补丁已在精确上游基线用 `git apply --unidiff-zero --index` 校验；应用后的树与上述预期结果树一致。

## 审核通过后的应用方式

在干净、基于完整上游基线 `0a70bd64d52e1018e665cb1fec2252d496688604` 的 `hq-ip-agent` 工作区执行：

```bash
git apply --unidiff-zero --index /path/to/huangque-main-site/review-patches/hq-ip-agent/2026-09-22-single-scroll-onboarding.patch
test "$(git write-tree)" = "ca47ac2b42adfc3535ce48909ed077278399816e"
```

应用前核对补丁 SHA-256，应用后核对结果树并重新运行上述测试；确认无误后在独立仓库创建提交并推送其权威 origin，再按独立服务发布流程部署。本 PR 自身不部署任何文件；审核通过前不得推送到生产 bare 远端，也不得上线。
