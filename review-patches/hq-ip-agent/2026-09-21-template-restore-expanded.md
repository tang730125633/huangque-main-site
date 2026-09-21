# IP12 恢复会话时模板目录保持展开修复审阅单

## 背景与根因

IP12 v4 由独立 `hq-ip-agent` 仓库维护，主站只反向代理 `/workbench/ip12/`，不保存该服务的运行副本。独立仓库当前只有服务器 bare 远端，因此本 PR 以完整 Git patch 提交主站审核。

用户刷新或重新进入正在选择模板的会话时，恢复链路会给所有 widget 统一传入 `collapsed: true`。标准 `template_catalog` 因而被折叠成一行；后续相同 id/fingerprint 的组件又会被渲染去重直接跳过，无法自行恢复展开。模板数据没有丢失，但用户看不到横向模板卡片和下方详情。

## 修复内容

- 恢复渲染支持按 widget id 设置“保持展开”的窄范围例外，其他恢复组件仍默认收起。
- 仅当 compose 仍为 `needs_user_input`、标准模板目录仍存在、且后端已选项没有命中该目录中的模板 id 时，恢复后展开 `template_catalog`。
- “是否已选模板”按目录真实 item id 判断，避免把同一会话此前选择的普通文案误当成模板选择。
- 用户确认目录中的模板后，刷新或重新进入会话仍保持收起，并继续还原已选模板标记和详情定位。

## 用户可见结果

- 还在等待用户选择模板的会话，刷新或重新进入后直接显示 22 张横向模板卡和下方详情。
- 已经选择模板的会话仍以单行收起状态恢复，不会重新占满页面。
- 普通待办卡、动作卡及其他素材选择组件的恢复行为不变。

## Patch 边界

完整基线提交：`hq-ip-agent@9ee51f7315abc62e2d1e6aa62470c0a14f1fc2ec`

补丁 SHA-256：`98d8fca33859a52b4a6fd378dbda5797578a376893078e465551e961521e4e4c`

预期结果树：`b352e28051f547833b1e13dc913ad7c9cb43f6f7`

涉及文件：

- `static/v4.js`
- `tests/hq-ip12-test.mjs`

## 验证结果

- 红灯验证：旧实现下“待选择模板的恢复会话保持模板目录展开”失败；“已选择模板的恢复会话保持模板目录收起”通过。
- 修复后，待选模板恢复展开、已选模板在 compose 仍等待输入时恢复收起、历史重复目录顺序兼容、后端权威选择还原、详情定位、22 张横向卡、选择/重开及 390px 移动端布局断言全部通过。
- `node --check static/v4.js`、`node --check tests/hq-ip12-test.mjs`：通过。
- `node tests/hq-widget-lifecycle-test.mjs`：通过，覆盖桌面/移动端 task → chat → reload → resume 生命周期。
- `git diff --check`：通过。
- 在基线提交的临时干净 worktree 中执行 `git apply --check` 和 `git apply --index`：通过；`git write-tree` 与预期结果树一致。
- 完整 `hq-ip12-test.mjs` 仍有 1 条既有失败：Emoji 扫描命中旧文案“全屏预览 ↗”；与本 patch 无关，本次未扩大范围修改。

## 审核通过后的应用方式

在干净、基于完整基线 `9ee51f7315abc62e2d1e6aa62470c0a14f1fc2ec` 的 `hq-ip-agent` 工作区执行：

```bash
git apply --index /path/to/huangque-main-site/review-patches/hq-ip-agent/2026-09-21-template-restore-expanded.patch
test "$(git write-tree)" = "b352e28051f547833b1e13dc913ad7c9cb43f6f7"
```

应用前先核对补丁 SHA-256，应用后核对结果树并重新运行上述测试；确认无误后创建独立仓库提交并推送其权威 origin，再按独立服务发布流程部署。本 PR 自身不部署任何文件；审核通过前不得推送到生产 bare 远端，也不得上线。
