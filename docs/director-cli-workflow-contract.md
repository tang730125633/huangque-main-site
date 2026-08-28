# 编导 CLI 补全契约

本文件描述编导工作台与“数字人一键生成”补全到 HQ CLI 时必须遵守的服务端契约。机器可读正本位于 `server/director_workflow_contract.py`。

## 当前阶段

- `director-capability` 的服务端只读契约已登记，可返回完整动作、状态、权限与安全约束。
- 其余动作均为 `planned`，在服务端编排器、鉴权、幂等和测试完成前不得由 CLI 宣称为可调用。
- HQ CLI 0.12.0 保持不变；最终动作完成后统一发新版 CLI、wheel、安装器与校验和。

## 核心原则

1. 浏览器与 CLI 共用同一服务端编排器，浏览器只负责收集输入和展示状态。
2. 付费动作必须先返回服务端报价，确认时绑定相同输入、`quote_token`、`plan_digest` 与唯一 `request_id`。
   文案生成与链接/本地素材拆解均按当前服务端 `points.cost_of` 计费，不得标记为免费写操作。
3. 网络结果不确定时，只能按原 `workflow_id`、`run_id` 或 `request_id` 查询和恢复，不能新建重复付费任务。
4. 账号所有权、分镜 revision、素材快照、扣点、任务绑定、退款和恢复都由服务端校验并持久化。
5. CLI 返回稳定的工作流状态，不直接暴露各上游供应商的不一致状态或私有地址。

## 串行实现顺序

1. 契约、权限、稳定状态、覆盖检查。
2. 脚本生成、链接/本地拆解、提示词反推、分镜读写。
3. 单镜头图片/视频/口播、完整生产与同款复刻。
4. 数字人一键生成的照片模式，以及真人视频 Precision 的服务端完整运行。Precision 公共 CLI 只提供计划、授权、启动、状态、恢复和放弃；源视频、音色复刻、完整配音、口型及自动剪辑接口仅是服务端持久子步骤，不得由客户端串联。
5. 发布新版 HQ CLI，并同步 wheel、Skill/MCP、根域与 Yuelei 子域安装器。

每一阶段必须从当时最新 `main` 开始，锁定 Head SHA，限制 diff，并在合并后才开始依赖它的下一阶段。生产部署、重启和真实扣点验收不属于 PR 授权范围。

Precision 完整运行必须以同一组 `run_id`、`plan_digest`、`quote_token` 和 `request_id` 贯穿所有子步骤。完整配音已扣点但响应超时、Precision 口型失败、剪辑失败或服务重启时，服务端只能从同一持久账本恢复；重复 `request_id` 必须返回原运行，不得再次扣点。

## 三仓发布顺序

- 主站服务端与用例正本：`tang730125633/huangque-main-site`
- HQ CLI 独立正本：`tang730125633/huangque-cli`
- Agent Skill 独立正本：`tang730125633/huangque-agent-skill`

所有主站用例和动作完成注册并可执行后，先在独立 CLI 仓库补齐客户端动作、类型、MCP 和测试；再在独立 Skill 仓库更新工作流、示例、最低 CLI 版本和安全约束。Skill 形成版本化 commit/tag 后，CLI 才能固定其版本、commit 与 manifest SHA-256。最后把已发布的 CLI 精确同步回主站下载镜像与安装器，禁止直接修改旧版本 wheel。
