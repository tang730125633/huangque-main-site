# hq-ip-agent 视频素材十秒切割门槛审核单

## 背景与目标

IP12 Agent 上传视频目前按九秒上限直接计算切片数量，导致 9 秒以上但不足 10 秒的原片也被等分成两个约 4.x 秒片段。本补丁把“是否切割”和“切片最大时长”拆成两个明确规则：

- 原片时长 `< 10.0 秒`：不切割，直接把原片作为可用镜头。
- 原片时长 `>= 10.0 秒`：按 `ceil(总时长 / 9.0)` 等分，保证每个派生片段不超过 9 秒。

只修改视频预处理模块及其回归测试；不修改历史素材索引、不触发旧素材重切、不涉及计费或上游生成。

## 修复内容

- 新增独立的 `SPLIT_TRIGGER_DURATION = 10.0`，保留 `MAX_DURATION = 9.0` 作为派生片段硬上限。
- 集中通过 `_clip_count(duration)` 决定是否切割及片段数。
- 状态输出同步允许 `< 10 秒` 的未切原片进入可用镜头列表，避免“后台不切但前端看不到”的半修复。
- 增加 `8.9 / 9 / 9.9 / 10 / 10.1` 秒边界回归，同时保留 `11 / 19 / 60` 秒、缩略图和重启恢复验证。

## 固定审核对象

- 上游基线：`hq-ip-agent@260c6246ac78a34f7dfc5f3e6967e35290bdab51`
- 修复 HEAD：`5dd9cd953fa73ced05f2f509153f0d9045b6d13f`
- 预期结果树：`5d83e2337c2f2f611249fcdc8461e4a49eebf987`
- 补丁 SHA-256：`d20fcf896808e30144f341f1f76e9ac3396c2f73459074d8cec7ba552167c52f`

涉及文件：

- `agent/visual_preprocess.py`
- `tests/hq-visual-preprocess-test.py`

## 验证结果

- 在生产机 `/tmp` 隔离克隆、精确基线 `260c624...` 上应用补丁成功。
- `python3 tests/hq-visual-preprocess-test.py`：`ALL PASS`。
- 边界行为：`8.9 / 9 / 9.9` 秒保留原片；`10 / 10.1` 秒各切为两个片段。
- 长视频行为：`11` 秒切 2 段、`19` 秒切 3 段、`60` 秒切 7 段，所有派生片段均不超过 9 秒。
- 原片字节保持、派生缩略图、账号隔离和中断恢复回归通过。
- `git diff --check` 通过；未调用任何付费服务。

## 审核通过后的应用方式

在干净、固定到上述完整上游基线的 `hq-ip-agent` 工作区执行：

```bash
sha256sum review-patches/hq-ip-agent/2026-09-23-sub10s-cut-trigger.patch
git apply --check --unidiff-zero --index review-patches/hq-ip-agent/2026-09-23-sub10s-cut-trigger.patch
git apply --unidiff-zero --index review-patches/hq-ip-agent/2026-09-23-sub10s-cut-trigger.patch
test "$(git write-tree)" = "5d83e2337c2f2f611249fcdc8461e4a49eebf987"
```

应用后必须重新运行定向测试，只提交这两个文件，然后仅重启 `hq-ip-agent.service`。部署不会扫描并重置已经处于 `ready` 的旧素材；新规则只用于新上传、尚未处理或用户明确重试的素材。
