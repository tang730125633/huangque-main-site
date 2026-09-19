# 同模型供应商优先级

## 范围

- 简洁视图只显示中文名称、英文模型和编辑入口；模型抽屉中排序真实托管渠道 ID。
- 已托管且候选集合未变时，拖拽/上下移动直接提交 `expected_revision`，读回修订与顺序后才提示生效。
- 首次接管、增删候选或更改控制状态仍需显式发布；不会把 shadow 自动改成 managed。
- 新发布的候选须有相同 model、adapter 和参数契约；历史跨模型候选在捕获新任务时被排除。
- 现有映射的模型命名不自动修正。模型别名与跨协议互换不在本次范围。
- 生图仅明确未受理时安全候补；未知、超时、429、5xx、已有供应商任务号不新增生成。视频不自动候补。
- 供应商编辑沿用现有密钥库和版本化 URL/Key 接口；未接通的内置线路不会伪装为可排序。

## 隔离验收

从仓库根目录启动仅绑定本机的静态服务器，打开
`/tests/fixtures/channel_priority_browser.html`。该页使用主站实际 JS/CSS，接口是明确标注的模拟实现，不使用账号、密钥或生产 API。

1. 编辑模型，第三条拖至第一条，观察其他行让位、发布反馈。
2. 刷新并重新打开，顺序保持（仅 fixture 使用 localStorage 模拟服务端持久化）。
3. “发布及刷新失败”模式下操作，必须显示结果未知且锁定后续写入；刷新重新获取状态后解锁。
4. 收起后优先级编辑器应清空，无控制台异常。

自动回归：`node --test tests/test_channel_priority_publish.js tests/test_channel_catalog.js tests/test_channel_provider_config.js`，以及 `node tests/channel_status_regression.cjs`。
后端回归：`tests.test_channel_manager`、`tests.test_channel_store`、`tests.test_channel_runtime_store`、`tests.test_managed_channel_recovery`、`tests.test_managed_channel_termination`。
PG 测试必须指向隔离库；不能在生产库运行夹具清理。`tests.pg_harness.postgres_url()` 可启动本地隔离 PG，再加载测试模块，避免 PG 用例被 skip。

## 发布边界

本次无数据库迁移、无生产配置修改、无付费验收。部署须从合并后的精确提交提取改动文件，先备份同名线上文件；后端代码需重启受影响服务。生产顺序与渠道配置保持不变，只有管理员实际发布才改变新任务路由。
回滚恢复本次代码文件；已发布映射须通过原有历史回滚入口另发修订，不能把代码回滚当作路由回滚。
