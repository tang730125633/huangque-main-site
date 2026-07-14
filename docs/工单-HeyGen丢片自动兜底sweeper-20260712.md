# 工单 · HeyGen 已计费丢片自动兜底 sweeper（#605 剩余项）

> 供其他 agent 独立执行。开工前先读团队 Git 协作规矩 + §0 公共上下文（仓库/分支PR/ship/DESIGN.md/漂移哨兵/**禁止服务器直改**）。
> 关联 issue #605。本工单只做「兜底 sweeper」——#605 的另外两块（轮询/下载抗网络抖动、失败任务卡「生成中」）已由 #607 / #608 上线，别重做。

## 目标与价值
HeyGen 的口播/电影化身**提交即扣费**（cinematic 每条约 $7）。提交成功后，若取片阶段（轮询/下载）超出死线或遇进程重启中断，任务会被判 `error` 并退点——但**成片其实已在 HeyGen 生成好**，钱花了、片没交付。

#607 已让 deadline 内的网络抖动能被吸收，但**跨 deadline** 的情况（HeyGen 排队慢、部署重启打断在飞任务）仍会丢片。2026-07-12 单日就有 **19 条**这类丢片，是**人工**跑脚本扫回来的（见下「已验证的手动做法」）。本工单把这个动作**产品化成自动 sweeper**，以后丢片自动补回，不再靠人工。

## 现状（已核实）
- 提交后失败会抛 `HeyGenBilledError`（`server/content_domains/video.py`），错误文案里带 `video_id=<32hex>`；同时 `video_assets` 表里该 job 的 `provider_video_id` 也存了这个 id。
- 失败任务：`jobs.status='error'`，`video_assets.provider_video_id` 非空、`video_file` 为空。
- HeyGen 侧查询：`GET /videos/{id}`（`video._heygen_request_json("GET", "/videos/"+id, direct=True)`）返回 `data.status`，completed 时带 `video_url`/`thumbnail_url`/`duration`。#607 的 `_heygen_poll_video` 已对网络错误重试。
- reaper 已在 `core.py` 里跑（僵尸任务清道夫）——sweeper 应作为**独立后台线程**或挂在 reaper 循环里，别和 reaper 的「判超时退点」逻辑纠缠。

### 已验证的手动做法（本会话跑过、成功挽回 19 条，直接产品化它）
对每条「`status=error` 且 `video_assets.provider_video_id` 是 32hex、`video_file` 空」的 video/cinematic 任务：
1. `GET /videos/{provider_video_id}` 查 HeyGen 状态。
2. `status=='completed'` 且有 `video_url` → 下载成片（`_download_video_file_direct`）+ 抽封面（`_extract_first_frame_cover`）。
3. 构造 result（必须含 `type/status/mode/video_file/video_url/text/duration` 等，形状对齐 `gen_cinematic`/`gen_video` 的成功返回——**漏 status 会让 video_assets 卡非终态**，见 #598）。
4. `jobs_store.set_terminal(jdb, jid, "done", result=result, from_states=("error",))` —— 从 error→done。
5. `video_domain.record_video_asset(jid, username, result)` 同步 video_assets 到 done。
6. **不补扣点数**（点已退，钱是我们烧的，别二次让用户买单）。
- `status=='failed'`（HeyGen 侧真失败，非网络丢）→ 跳过，无可挽回。

## 方案

### A. 后端 sweeper（`server/content_domains/core.py` + 复用 `video.py` 已有函数）
- 新增一个后台扫描线程（或并入 reaper 周期，间隔建议 5~10 分钟），扫描窗口建议「最近 24~36 小时」的 `status=error` video/cinematic 任务，条件：`video_assets.provider_video_id` 匹配 `^[0-9a-f]{32}$` 且 `video_file` 空。
- 对每条按上面「已验证的手动做法」1~6 步补回。**幂等**：已 done 的跳过；`set_terminal(from_states=("error",))` 抢不到就跳过（说明状态已变）。
- **只挽回、不补扣、不重发提交**——绝不调用任何 create/提交 POST（那会重复扣 $7）。只做 GET 查状态 + 下载。
- 每条挽回/跳过都 `print(..., flush=True)` 记一行，方便追账。
- 单条失败（网络/HeyGen 侧 failed）不阻断其它条。
- ⚠️ 别扫 xiaole_video（它的 provider id 是 UUID 不是 HeyGen 32hex，另一套引擎）；正则限定 32hex 天然排除。

### B.（可选）指标
- sweeper 每轮补回条数打日志；可选把「已计费但未交付」的积压数暴露到 `/api/gen/health`，供余额哨兵（scripts/balance_sentinel.py）盯。

## 边界与注意
- **绝不重发提交 POST**：sweeper 只 `GET /videos/{id}` + 下载成片，幂等、不计费。
- 挽回后 `jobs.status` 从 error→done、`video_assets` 同步 done，前端历史/资产页要能正常显示（result 形状对齐既有成功返回）。
- 死线/reaper 常量见 core.py（#609：VIDEO_GEN_DEADLINE=900、cinematic reaper grace=1200）——sweeper 是 reaper「误杀已出片任务」的补救，两者不冲突。
- 不改点数扣费逻辑、不改生成核心。

## 验收标准
1. 造一条「提交成功但取片失败」的 video/cinematic 任务（可 mock：HeyGen completed，但把本地 poll/download 打断使其判 error），sweeper 下一轮能把它补回 done + 出片 + 封面，且**不补扣点数**。
2. HeyGen 侧 `status=failed` 的任务，sweeper 跳过、不误判、不重发提交。
3. sweeper 幂等：对已 done 的任务不重复处理；连续跑两轮结果一致。
4. 单测：mock `_heygen_request_json` 返回 completed / failed / 网络错三种，断言只有 completed 被补回，且**全程无任何 POST/create 调用**（防重复扣费）。
5. 不影响 reaper 正常退点、不影响正常任务。

## 部署与验证
- 后端：`./ship "HeyGen 丢片自动兜底 sweeper (#605)" server/content_domains/core.py server/content_domains/video.py`
- 重启后 `/api/gen/health` 200；观察 sweeper 首轮日志有没有把历史积压的丢片补回。
- 漂移哨兵 `--verify-deploy` 校验线上 == origin/main。
