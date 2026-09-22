# IP12 图片任务交付修复

## 审查范围

- 来源仓库：`hq-ip-agent`
- 当前来源基线：`4e9cc71aa38429e2cc4c9c7bb4ecb401d53aecd1`
- 最终候选提交：`c5fc586548b39d1f28c06fcf00f1cb9ab780aaeb`
- 审查补丁：`2026-09-22-image-delivery.patch`
- 补丁 SHA-256：`183E29033D6B095CAE219D32A9D1247AC768DFFF1CC93217752CE1F41FB43DDB`
- 应用后 Git tree：`27d47ba17117d01afde843756ada9a6dfb458f54`
- 外部审查分支：`codex/pr1662-image-delivery-review-v2-20260922`
- 本 PR 仅提交审查材料，不部署。

## 事实结论

图片生成任务本身可以成功并返回有效图片 URL；故障发生在 IP12 交付适配层：图片结果没有完整写入历史消息与实时 `delivery` 事件，旧会话恢复和任务面板关联也可能漏图或误绑媒体，导致 Agent 的交付话术与真实界面不一致。

## 修复内容

1. 按业务域选择真正的成品媒体，同时兼容 `image/video/audio` 短域名和 `hq-*` Agent id，避免副产物混发。
2. 图片任务完成时，把全部图片写入历史消息 `images` 和 SSE `delivery.images`，不再静默截断为前三张。
3. 任务面板只使用结构化 `task_job`/`media_job` 与 `task_domain` 关联消息；正文偶然出现相同任务号不能绑定无关媒体。
4. 从持久化任务台账补回旧版漏图消息；补图应用于完整恢复历史，不受最近五条状态投影限制，也不重新生成或扣点。
5. 跨业务域同号任务不会串图；`output` 仅含 `job_id` 的旧任务记录仍按权威结构解包。
6. completed 状态但没有提取到可展示产物时，明确禁止 Agent 声称图片或卡片已经发送。

## 验证

- 红灯复现：初版补丁存在最近五条之外无法恢复、多图第 4 张起丢失、正文任务号误关联三项阻塞。
- `python tests/hq-job-watcher-test.py`：通过；覆盖历史、实时 SSE、任务面板、完整多图、深历史恢复、结构化关联、跨域同号、旧任务结构和无产物事实约束。
- `python tests/hq-delivery-fallback-test.py`：通过。
- 精确最终候选在隔离 Linux 工作区执行 `tests/run_unit.sh`：全绿。
- Windows 全部 33 个后端命令对照：候选与干净当前基线均为 `29 PASS / 4 FAIL`，失败项完全相同，均为跨盘路径或缺少 `fakeredis` 的环境限制。
- 完整 17 套浏览器回归对照：候选与干净当前基线均为 `168 PASS / 2 FAIL`，失败套件和两条既有断言完全相同；候选未新增失败。最新基线相对该对照仅修改 `subagents/hq-compose/SKILL.md`，随后完整 Linux 后端门禁已在最终候选上重跑全绿。
- `python -m py_compile agent/v4/delivery.py app.py tests/hq-job-watcher-test.py tests/hq-delivery-fallback-test.py`：通过。
- `git diff --check`：通过。
- 零上下文补丁已在当前来源基线的干净工作区用 `git apply --unidiff-zero --index` 校验；应用后 tree 必须与最终候选完全一致。

## Patch 边界

涉及文件：

- `agent/v4/delivery.py`
- `app.py`
- `tests/hq-delivery-fallback-test.py`
- `tests/hq-job-watcher-test.py`

不涉及数据库 Schema、迁移、生产会话写入、真实 Provider 调用、任务补发、重新扣点或部署。

## 审核通过后的应用方式

在干净、基于 `hq-ip-agent@4e9cc71aa38429e2cc4c9c7bb4ecb401d53aecd1` 的工作区执行：

```bash
git apply --unidiff-zero --index /path/to/huangque-main-site/review-patches/hq-ip-agent/2026-09-22-image-delivery.patch
test "$(git write-tree)" = "27d47ba17117d01afde843756ada9a6dfb458f54"
```

应用前核对补丁 SHA-256，应用后核对结果树并重新运行完整测试。审核通过后方可将精确候选合入外部仓库；部署必须由用户另行明确授权。
