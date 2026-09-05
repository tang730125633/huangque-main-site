# 编导 Agent：本地自然对话规则迁入主站

## 目的与本次边界

顾客在编导聊天框说目标、给内容，助手先交付可用文字，再按必要信息推进。
迁移来源是 2026-09-05 本地验证的 `concise-deliver-first-v2-20260905`，
借鉴 12IP v4 的自然语言 + 可选工具调用形式，不复制个人 IP 记忆或自动付费委派。

本 PR 不部署生产，不导入本地聊天记录、调试账号、Windows 路径或密钥。
不把 localhost 预览服务器作为生产服务，也不新建另一套任务、点数或会话数据库。

| 项目 | 主站适配 |
| --- | --- |
| 自然回复 | 模型直接给文字，服务端包装原有 `type/content/plan` 返回结构；不强制模型输出 JSON |
| 工具循环 | 普通回复可零工具；核实能力才调用原 `director_cli.page_guide`，最多 4 轮、每轮 2 次工具、总预算 150 秒 |
| 产品工具 | 主站使用原有固定页面 CLI capabilities/describe 桥，不携带账号/模型密钥；不搬入本地任意目录搜索器 |
| 对话历史 | 现有账号隔离的前端历史，按 user/assistant 角色输入；丢弃开头孤立 assistant；每轮注入最新规则 |
| 恢复 | 复用原有聊天 job、submission_idempotency、前端 pending request 与生产恢复，不复制原型 SQLite |
| 页面建议 | 结构化参数仅由工具提出，继续经过原动作白名单、页面范围和 revision 校验 |
| 正式生产 | `prepare_script_plan` 仅准备分镜脚本待确认方案，原服务端精确口令 + 价格卡点击才能受理 |
| 附件 | 保留主站原上传逻辑。本地 IndexedDB 附件元数据原型不作为新生产上传方案发布 |

当前真实能力边界：普通口播/改稿/创意/分镜草稿在对话内给出；正式分镜脚本仍走
现有确认生产通道。数字人、反推、独立图片/视频/配音的 CLI 目录即使存在，也不等于
已接入这个聊天执行桥。本 PR 不声称实现这些新的付费执行链。

## 对话规则

- 普通交流默认 1–2 句、20–60 字，尽量不超过 80 字；成稿不套这个长度限制。
- 有主题、品牌、促销信息就直接写；不追问一串平台/时长/风格，不问“要不要我写”。
- 顾客委托自行选题就直接给一版，不再让其从菜单选择。
- 一次最多问一个真正阻断的问题；不复述已给信息，不默认加教程、总结或邀约。
- “买三送一”可直接使用，不要求普通活动先举证，不自加瓶/箱、日期、价格或功效。
- 自己旧稿中的商业事实不是用户已确认信息；旧长回复也不是新轮次的风格模板。
- 模型身份来自服务端配置；无法实际生产就一句话讲清，不假装成功，也不把切页面教程当结果。

这些是模型行为约束，不是机械截断或逐字模板。5000 字的原接口内容上限仍保留；
超限或模型 `length/incomplete` 会显式失败，不悄悄剪掉成稿。

## 框架与安全

`gen_director_agent` 的登录、功能开关、零点对话额度和持久化 job 不变。
非确认轮调用 `director_conversation.converse`：

1. 服务端选择 endpoint/key/model/protocol，客户不能覆盖；自定义 Base 缺专用 Key 不回退全局 Key。
2. 新规则 + 历史角色消息 + 当前问题/页面数据发给模型。
3. 自然文字直接返回；工具必须为 `hq_cli_page_guide`、`prepare_script_plan` 或 `propose_page_actions`。
4. CLI 查询的实际回执回送模型；重复同页面查询本轮复用结果。工具不能任意 shell/run/upload/login。
5. 页面建议只是暂存提案，必须经过原 `normalize_model_result` 再交前端；失败轮没有部分动作落地。
6. 精确当前消息“确认生成”仍优先走原服务端待确认方案检查，不交给模型猜意图。

工具输出、历史和页面不是系统指令；其中出现价格或授权文字不构成扣点授权。
报价签发、账号归属、过期、参数摘要、原单恢复、扣点均不迁入模型层。
拒绝、空回答、非法协议、工具超限直接报错，不再伪装成那段重复功能介绍。

## 模型参数

模型和密钥继续从原服务器环境读取，本 PR 不自动切换正在运行的模型。
参考 `deploy/director-agent-natural.env.example`，DeepSeek 专用配置必须整对提供。

| 协议 | 选择 | 请求 |
| --- | --- | --- |
| `auto` | `deepseek*` 型号 | Chat Completions，temperature 0.4，max_tokens 2200，thinking disabled |
| `auto` | 其他型号 | 原 Responses，reasoning 使用原配置，verbosity low，max_output_tokens 9000 |
| 显式 | `responses` / `chat_completions` | 支持已配置的兼容网关；其他值失败关闭 |

不强制所有模型接受同一套参数。旧请求中的 `provider=openai_responses` 为兼容性字段，
仍由服务端选择真实协议；浏览器不能借该字段选择上游地址或模型。

## 测试与发布交接

自动化命令与同 SHA GitHub CI 记录放在 PR 正文。新增测试覆盖自然文字、成稿长度、
角色历史、两种协议真实请求体、工具回执、非法参数、凭据不跨作用域、工具预算、失败无部分动作，
并在 Linux CI 中执行原确认/幂等/CLI/工作流回归。浏览器截图明确使用隔离本地业务 API 替身，
只证明实际页面脚本显示/交互，不当作 DeepSeek 在线生产或真实扣点证据。

主线 `36d2693` 的扩大回归已有失败：`digital-human-oneclick-material-resolve` 的
可用性元数据/断言不一致，旧前端确认单测试缺少 `page_revision`；Windows 另有 3 个
工作流测试的 SQLite 句柄清理错误。同环境 Base/Head 已复现相同问题，未改断言或加 skip。
`scripts/check_director_regression.py` 在独立进程中运行两份相同测试集，Head 加上新对话测试，
逐项比较测试 ID、计数、skip、失败详情；新增或改变失败、移除测试、新 skip 都阻断。
CI 上传完整 JSON 证据并明确 `all_tests_green=false`，比较门禁绿不等于这些主线债务已修复。

生产发布仍须独立审核和单独确认，从审核通过、已合并的实时 main 发布。
无数据库迁移，不更换登录，不新增端口/服务，不自动打开 `director_agent` 功能开关。

运行文件边界（不要整目录覆盖）：

- `server/content_domains/director_conversation.py` → `/home/ubuntu/content-api/content_domains/director_conversation.py`（新增）
- `server/content_domains/director_agent.py` → `/home/ubuntu/content-api/content_domains/director_agent.py`
- `site/workbench/script-agent.js` → `/var/www/huangquechuanmei/workbench/script-agent.js`
- `site/workbench/script.html` → `/var/www/huangquechuanmei/workbench/script.html`（资源戳）
- `site/workbench/digital-human-oneclick.html` → `/var/www/huangquechuanmei/workbench/digital-human-oneclick.html`（资源戳）

发布前记录审核 Head、合并 main、每个源 blob/SHA256、每个运行目标 preimage SHA256
及新增文件 present/absent。任一目标与预期不符停止，不覆盖未识别热改。统一备份上述五文件
及本次若更改的受保护模型配置（不打印密钥）。功能开关本次不改变，也不做开关数据库迁移。
发布使用仓库 `ship --exact-content-domains` 精确文件模式，新会话模块与调用模块必须同批部署；
其现有 import 检查必须成功，随后只重启 `huangque-content`，验证健康和正式登录页面。
本说明不是已执行清单；实际 preimage/合并 SHA 在获准发布时才采集，不伪造当前服务器摘要。

发布验证：问候一句、主题直接给稿、已有活动不重复问、CLI 查询回执可追踪；
近似确认不得开价格卡，原有效方案精确确认才开卡；未授权不得启动收费任务。
首次付费冒烟仍需专门授权，不能用本 PR 提交请求当成扣点许可。

回滚：停止新对话提交，保留在飞生产任务，恢复同一备份中的五个精确目标和受控模型配置；
新模块 preimage 为 absent 时只移除该新增模块。重启 `huangque-content` 并检查健康，
已受理任务继续由原恢复链路处理。禁止回滚/删除 jobs、点数账本、用户素材或聊天记录。
