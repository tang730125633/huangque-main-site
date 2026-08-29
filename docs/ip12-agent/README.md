# IP12 Agent 接手说明

这份文档是 Huangque IP12 Agent 的统一入口，供 Codex、Claude、Pi、OpenClaw 或其他代码 Agent 阅读。仓库是公开仓库；这里只记录代码结构、运行合同和本地调试方法，不包含生产密钥、客户对话、数据库、PDF 或生成物。

## 先说当前真实状态

- 正式代码位于 `server/hermes_ip12/`，状态 Schema 为 v2。
- 当前 Agent Release 为 `ip12-a2-skills`，新 Project 的管线版本为 `ip12-skills-v1`。
- 唯一状态推进器是 `ip12_harness.py` 的 Reducer。不要新建第二套状态机或数据库。
- `foundation_pdf` 已经是确定性 Skill：冻结快照输入、零模型编译、严格 Artifact 校验。
- `intake` 和模块 1–4 虽然已有 Skill 注册、输入投影、Schema 和 trace，但仍复用旧 Harness Prompt 与断点流程，尚未完全拆成独立问题执行器。
- 模块 5–6 仍由现有 Reducer 工作流完成，不在六 Skill 注册表中。
- 本分支新增了基础访谈本地练习场，用来逐步调问题、回复风格和采集结果；它没有部署到正式站。

## 架构

```text
浏览器 / 本地练习场
        │
        ▼
server.py                    HTTP、Project、模型传输、Artifact、生产桥
        │
        ├── coaching_skills.py   六 Skill 注册与确定性 PDF 编译
        ├── ip12_harness.py      唯一 Reducer、Schema、确认、CAS、事实校验
        ├── project_memory.py    只读 Project 投影，不是第二事实库
        └── codex_local_transport.py
                                本地 Codex 订阅调用

事实正本：coach_state.ip_profile
未确认内容：coach_state.intake / coach_state.pending
并发合同：revision + expected_revision
幂等合同：request_id + receipt
```

## 代码地图

| 文件 | 职责 | 修改注意事项 |
|---|---|---|
| `server/hermes_ip12/server.py` | Flask API、Project 持久化、模型调用、PDF 与生产桥 | 不要在这里复制一套状态推进逻辑 |
| `server/hermes_ip12/ip12_harness.py` | Reducer、状态 Schema、intake、模块 1–6、确认动作 | 所有状态推进必须回到这里 |
| `server/hermes_ip12/coaching_skills.py` | 六 Skill 合同、输入投影、快照和 PDF 编译 | `foundation_pdf` 不得调用模型 |
| `server/hermes_ip12/project_memory.py` | 给模型的只读 Project 摘要 | 不得成为事实正本 |
| `server/hermes_ip12/codex_local_transport.py` | `codex exec` 本地订阅传输 | 子进程不得继承 Provider API Key |
| `server/hermes_ip12/prompt.md` | 主教练通用 Prompt | Prompt 不能替代 Reducer 校验 |
| `server/hermes_ip12/templates/index.html` | 正式 IP12 主界面 | UI 状态必须以服务端返回为准 |
| `server/hermes_ip12/templates/intake_lab.html` | 本地基础访谈练习场 | 只用于本地逐轮调试 |
| `tests/ip12_local_codex_preview.py` | 完整 IP12 本地预览 | 使用隔离数据目录 |
| `tests/ip12_intake_playground.py` | 只启动基础访谈练习场 | 不进入模块 1 |

## 六个注册 Skill

| Skill ID | 当前职责 | 模型 |
|---|---|---|
| `intake` | 基础资料采集与核对 | 是 |
| `module_1_positioning` | 定位关键词、三项候选和最终选择 | 是 |
| `module_2_persona` | 人格、人设候选和表达边界 | 是 |
| `module_3_value` | 价值主张候选、对象和证明方式 | 是 |
| `module_4_story` | 事实故事、传播建议和未来愿景 | 是 |
| `foundation_pdf` | 冻结快照到固定 PDF Markdown | 否 |

注册表只表示合同存在，不代表 Skill 已完全独立。判断是否真正拆分，必须检查该 Skill 是否自己拥有问题卡、进入条件、退出条件、允许读取的事实、允许写入的字段和独立回归集。

## 状态合同

| 字段 | 含义 |
|---|---|
| `schema_version` | 固定为 2；不得静默升级或降级 |
| `pipeline_version` | `legacy` 或 `ip12-skills-v1` |
| `revision` | Project CAS 版本；每次写入递增 |
| `intake` | 基础访谈候选、已问字段、拒答和核对稿 |
| `ip_profile` | 已确认事实、偏好、最终输出和选择快照 |
| `pending` | 当前模块尚未确认的断点 |
| `completed_modules` | 已确认完成的模块编号 |
| `foundation_report` | 冻结快照、PDF Artifact 与双确认状态 |

模型只能输出 proposal。只有 Reducer 可以确认事实、写入正式档案、推进模块或完成报告。

## 本地教程：只调基础访谈

### 前提

1. 已安装并登录 Codex CLI。
2. `codex login status` 显示使用 ChatGPT 登录。
3. 从本分支运行，不使用生产数据目录。

### 启动

```bash
python3 tests/ip12_intake_playground.py \
  --port 4323 \
  --data-dir /tmp/ip12-intake-playground-user
```

打开：

```text
http://127.0.0.1:4323/intake-lab
```

页面只显示基础访谈，并实时展示：

- 当前问题；
- 候选事实和对应原话；
- 未回答数量；
- 当前总结稿；
- 对每条回复的人工评价。

它使用 Codex 订阅额度，不会创建黄雀报价、Job 或点数流水。

## 本地教程：运行完整 IP12

```bash
python3 tests/ip12_local_codex_preview.py \
  --port 4318 \
  --data-dir /tmp/ip12-full-local-preview
```

打开 `http://127.0.0.1:4318/`。该入口会运行完整模块 1–6、冻结快照和 PDF 流程；仍然必须使用虚构人物，不连接正式客户数据。

## 常用 API

| 方法与路径 | 用途 |
|---|---|
| `GET /api/conversations` | 列出当前账号 Project |
| `POST /api/conversations` | 创建新 Project；写入默认管线版本 |
| `GET /api/conversations/<id>` | 读取 Project、状态和可用动作 |
| `POST /api/chat-complete` | 非流式对话入口，适合测试与 Agent 调用 |
| `POST /api/chat` | SSE 对话入口，正式网页使用 |
| `POST /api/foundation-report/generate` | 在快照确认后生成 PDF |
| `POST /api/foundation-report/confirm` | 严格校验后确认 PDF 并开放模块 5 |

写请求必须携带当前 `expected_revision`。重复请求必须复用原 `request_id`，不能为了得到成功结果改键重试。

## 测试

最小本地回归：

```bash
python3 -m unittest discover -s tests -p 'test_ip12_intake_playground.py' -q
python3 -m unittest discover -s tests -p 'test_ip12_harness.py' -q
python3 -m unittest discover -s tests -p 'test_ip12_persona_agent_v1.py' -q
python3 -m unittest discover -s tests -p 'test_hermes_ip12_routes.py' -q
```

PDF 依赖可用时再运行：

```bash
python3 -m unittest discover -s tests -p 'test_ip12_six_skill_pipeline.py' -q
```

提交前还要运行：

```bash
python3 scripts/ci_validate.py
git diff --check
```

## 当前已知缺口

1. `intake` 仍把完整字段目录前置，尚未拆成“核心必问 + 可选穿插”的渐进式采集。
2. 用户已经回答当前问题、但模型漏掉 `profile_updates` 时，控制层仍可能重复追问；不能用换句话说掩盖漏采集。
3. 问题风格、回答风格和总结质量目前共用同一模型决策，需要在本地练习场逐项固定验收标准。
4. 模块 1–4 仍主要复用通用 Harness Prompt；注册 Skill 不等于独立 Skill 执行器。
5. 本地人工评价当前只保存在浏览器会话中；需要 Tang 与开发 Agent 逐轮审阅，不可自动当成训练数据。

## 其他 Agent 的工作边界

开始前必须读取根目录 `AGENTS.md`，然后遵守：

- 不创建第二套 Project 状态机、事实库或数据库；
- 不把未确认候选写入 `ip_profile`；
- 不用 Prompt 替代 revision、CAS、幂等和确认门；
- 不上传真实客户对话、密钥、数据库、PDF 或生成物；
- 本地调试默认使用 Codex 订阅和虚构人物；
- 未经 Tang 明确授权，不 push、PR、部署或修改正式 Project；
- 修改一个 Skill 时，先跑该 Skill 独立回归，再跑完整黄金路径。

可以把下面这段直接交给下一个 Agent：

```text
先读取 AGENTS.md 和 docs/ip12-agent/README.md。当前任务只在本地调 IP12 Agent，禁止部署正式站。复用 server/hermes_ip12/ip12_harness.py 的唯一 Reducer、schema v2、revision/CAS 和 request_id 幂等合同，不新建状态机或数据库。先在 tests/ip12_intake_playground.py 启动基础访谈练习场；一次只修改一个明确问题，并补充能复现用户原话的回归测试。不要把“Skill 已注册”误报成“Skill 已独立”。
```

## 发布门槛

只有同时满足以下条件，才可以提议重新接入正式 IP12：

- 问题卡覆盖 Word 采集表，但敏感可选项不阻塞；
- 用户反问、拒答、纠正和要求解释时不跳题；
- 已回答内容不重复问；
- 每条候选事实都能回查到用户原话；
- 30 轮后姓名、身份、数字和故事不漂移；
- 10 个虚构人物完整跑通模块 1–6；
- PDF 生成阶段模型调用数为 0；
- 无报价、Job 或点数副作用；
- 完整 CI 和独立复审通过。
