# 工单 · FB5 音色复刻接口校验加固（clone-vip）（Issue #60 qilin 审计）

> 供其他 agent 独立执行。先读《工单-其他agent实施》§0 + 《任务看板》领取流程。
> **冲突组：A（`server/content_domains/**`）** —— 抢 `lock/A`。
> ⚠️ **本工单含严重数据污染 bug，优先级高。**

## 严重问题（先讲）
`POST /api/gen/audio/clone-vip`：传错参数（如 `audio_format=exe`、有槽位但不传 `audio`）时**返回 200 并把槽位当成开始复刻处理**，**污染已有音色数据**——实测 `status` 从 `ready` 被改成 `failed`、`reclone_count` 从 9 变 10、`preview_url` 变 null、`voice_name` 被改成"测试"。**用户只是传错参数，已有音色就被损坏。**

## 根因
参数校验在"修改槽位状态之后"或缺失。**必须：先完成全部参数校验，通过后才碰槽位数据。**

## 要修（均在 clone-vip 处理逻辑，`content_domains/core.py` 或对应 audio 域）
按此**顺序**校验，任一失败**立即返回、绝不修改** `status/reclone_count/voice_name/preview_url`：
1. 方法错误（GET/PUT 等）→ `405 Method Not Allowed` + 头 `Allow: POST`。
2. 非法 JSON → `400 请求体不是合法 JSON`。
3. 缺 `slot_id` → `400 缺少音色槽位 ID`。
4. 缺 `audio` → `400 请先上传样音`。
5. 不支持的 `audio_format` → `400 audio_format 仅支持 mp3/wav/m4a/aac/ogg`。
6. 槽位不存在 / 不属于当前账号 → `404 音色槽位不存在或不属于当前账号`（当前是 400，改 404）。
7. 已达当前 20 次重新复刻上限 → 明确提示（如 400/409「该槽位已达复刻上限」）。
8. **以上全过，才开始改槽位 / 起复刻任务。**
- 顺带：未登录提示与其它配音接口统一（如都用 `未登录`）。

## 修复受污染槽位（部署后由审核方处理）
审计中被污染的槽位需人工/脚本修回：`S_xaUB8OR62`、`S_p0lB8OR62`（被误改为 failed、reclone_count 虚增）。**审核方部署后确认并修数据。**

## 边界
- 只改 clone-vip 校验顺序 + 返回码，不改正常复刻逻辑。基于 T8 后 content_domains 结构。

## 验收标准
1. 8 类异常请求都在**碰槽位前**返回正确 400/404/405，**不修改任何槽位字段**。
2. 正常复刻流程不变。
3. `py_compile` 通过、health 200。

## 部署与验证
- `./ship "FB5 clone-vip 校验加固" server/content_domains/core.py`
- 实测 8 类异常不再污染槽位；正常复刻正常。完成后 Issue #60 回复 qilin。

> 本工单只做规划，未改动任何文件。
