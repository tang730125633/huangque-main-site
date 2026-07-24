# PR #19 P2 审核问题修复设计

## 目标

关闭 PR #19 剩余的两个 P2 问题：禁止已绑定的配音扣费尝试改绑另一任务，并让关键帧交接阻塞项的 OpenAPI schema 完全兼容 OpenAPI 3.0.3。

## 范围

仅修改配音账本触发器、对应数据库测试、OpenAPI 文档及其契约测试。不新增 Phase 3-B 写接口、TTS、扣点流程、时间线修改、部署或合并动作。

## 方案选择

### 扣费尝试任务绑定

采用“允许首次绑定，绑定后不可替换”的状态约束：

- `OLD.job_id IS NULL` 时允许绑定一个身份一致的任务；
- `OLD.job_id IS NOT NULL` 时，`NEW.job_id` 必须与旧值相同；
- quote 已有 `consumed_job_id` 时，attempt 的任务必须与其相同；
- quote 尚未消费时允许 attempt 先绑定，现有 quote 更新触发器随后只允许消费同一任务；
- 插入和更新路径都校验 quote/job 一致性，避免通过新建记录绕过约束。

该方案保留未来写流程需要的两阶段绑定顺序，同时消除同一台词存在多个任务时的账本身份分叉。

### OpenAPI 3.0.3

`handoff_blockers[].shot_id` 不在 `required` 中，服务端无镜头时会省略字段。因此将其 schema 从 OpenAPI 3.1 联合类型改为单一 `type: string`，不使用 `nullable`。

## 测试设计

1. attempt 首次从 `NULL` 绑定任务成功；
2. 重复写入同一任务保持幂等；
3. 已绑定任务后改绑第二个任务失败；
4. quote 已消费后，attempt 插入或首次绑定到其他任务失败；
5. OpenAPI 版本仍为 3.0.3，`shot_id.type` 严格等于字符串 `string` 且可省略；
6. 相关测试、完整测试集、静态门禁和 GitHub CI 全部通过。

## 交付方式

在现有分支 `codex/short-drama-phase3-voice-spec` 上提交并更新 PR #19。不得新建 PR、合并、部署或重启服务。
