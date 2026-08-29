# IP12 主 Agent 升级方案：通过 Responses + Function Calling 自由调用子 Agent

> 状态：方案草案 v1（2026-08-30）
> 读者：Tang、Grok（IP12 开发）、Pi（工具层开发）
> 目标：把 IP12 从"正则决策 + 旧接口"升级为"自主决策的主 Agent"，通过 function calling 调用生产内容子 Agent。

## 一、核心原则（Tang 的定义）

**Agent = 能根据任务和环境的变化，自主决定下一步要做什么。**

- 用户说取消 → 不调用工具
- 用户说生成 → 直接调用工具
- 用户不确定 → 追问

选择权在 Agent（模型），不在人为判断分支里。人为规则只保留安全底线：
付费先报价、确认才扣点、quote_token 一次性、删除验证归属（fail-closed）。

## 二、现状与差距（已调研核实）

| 层 | 现状 | 目标 |
|---|---|---|
| 模型接口 | Chat Completions（`model_router.py` 调 `/chat/completions`） | **Responses API**（带 state + function calling 循环 + reasoning） |
| 模型 | zelong GPT-5.4 / gptsapi GPT-4o 多供应商轮换 | 建议换 Luna（与工具层同构）；是否更换由 Tang 决定 |
| 主 Agent 决策 | `master_agent.py` 正则 decide()（shadow 模式） | 模型 function calling 自主决策 |
| 子 Agent 记录层 | `agent_runtime.py` AgentRun/ToolCall 合同 + 状态机 ✅ 已就位 | 复用（状态机与工具层报价确认流程一致） |
| 生产执行 | 窄生产桥（4 族能力，正则 production_intent） | 委派生产内容子 Agent（108 能力全量） |

## 三、目标架构

```
用户（唯一对话框，IP12 页面）
   │
   ▼
IP12 主 Agent（模型 + Responses API）
  ├─ 诊断：六 Skill 流程（现有 Reducer 不变）
  ├─ 决策：function calling 自主决定调哪个子 Agent
  │    tools = [delegate_production, delegate_search]
  ├─ 委派 → 生产内容子 Agent（工具层：Luna 自主选 108 能力、报价、执行）
  └─ 收到 function_call_output → 继续推理 → 呈现给用户
```

用户全程只面对一个对话框；主 Agent 负责"决定做什么"，子 Agent 负责"怎么做"。

## 四、升级路径（分三个阶段，每阶段独立可验收）

### 阶段 1：模型接口升级（基础设施）
- `model_router.py` 增加 Responses 通道（保留现有 provider 作为回退）
- 验收：一条多轮访谈走 Responses，state 正常

### 阶段 2：主 Agent 决策层换脑
- `master_agent.py` 的 `decide()` 从正则改为模型 function calling（见下方提示词与工具定义）
- 工具定义：`delegate_production`（委派生产子 Agent）、`delegate_search`（实时信息）
- 验收三话术：取消不调工具 / 生成直接调 / 不确定追问

### 阶段 3：子 Agent 接入
- `delegate_production` 执行时转发到生产内容子 Agent（工具层 `/agent`，携带画像上下文）
- 复用 `agent_runtime` 记录 AgentRun/ToolCall
- 验收黄金路径：诊断完成 → "用文案生成口播" → 主 Agent 调 delegate_production → 子 Agent 报价 → 用户确认 → 成品回到对话框

## 五、核心交付：主 Agent 系统提示词（草案）

```
你是黄雀 IP12 主 Agent，也是用户唯一的对话入口。

你的能力分两部分：
1. 诊断：通过六步访谈（基础资料→定位→人设→价值→故事→报告）帮用户理清
   行业、痛点、定位，最终产出 PDF 报告。诊断流程按你的内部 Skill 推进。
2. 生产：诊断完成或用户明确要求时，调用子 Agent 完成内容生产
   （数字人口播、图片、视频、配音、采集、获客、实时搜索等）。

可用工具（function calling）：
- delegate_production：把生产任务交给生产内容子 Agent。
  你只负责判断"现在该生产什么"，具体用哪个能力、怎么报价、怎么执行，
  由子 Agent 自己决定。
- delegate_search：查实时信息（新闻、行情、事实核查）。

决策原则（最高优先级）：
- 用户说取消/算了/不做了 → 停止，不调用任何工具。
- 用户明确要生成/制作/采集 → 直接调用对应子 Agent，不犹豫。
- 用户意图不明 → 先追问，不猜、不代劳。
- 客户画像和文案等已确认的信息，不得重复追问。

安全底线（不可违反）：
- 付费生产必须先报价，用户确认后才执行。
- 你（主 Agent）不直接接触报价、扣点、quote_token——
  这些全部由子 Agent 内部处理。
- 诊断事实以你的状态机为准；子 Agent 的产出你负责转述和呈现。

输出要求：
- 调用子 Agent 后，把结果用自然语言讲给用户（报价就呈现报价，
  完成就交付成品链接，失败就如实说明）。
- 全程中文。
```

## 六、工具定义草案（function calling）

```json
{
  "name": "delegate_production",
  "description": "把内容生产任务交给生产内容子 Agent（支持数字人口播、图片、视频、配音、文案成片、内容采集、获客等）。你只需要描述用户想要什么结果，子 Agent 会自己选择能力、报价并执行。",
  "parameters": {
    "type": "object",
    "properties": {
      "intent": {
        "type": "string",
        "description": "用户想要的生产结果的自然语言描述，例如：用已确认的文案生成数字人口播视频"
      },
      "context": {
        "type": "object",
        "description": "可选。传给子 Agent 的上下文：客户画像摘要、已确认文案、形象/音色编号等"
      }
    },
    "required": ["intent"]
  }
}
```

```json
{
  "name": "delegate_search",
  "description": "查询实时网络信息（最新新闻、行业动态、事实核查）。",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "搜索问题或关键词"}
    },
    "required": ["query"]
  }
}
```

## 七、与 Grok 的协作边界

- 本方案改的是：`model_router.py`（接口通道）、`master_agent.py`（决策层）。
- 不动：`ip12_harness.py`（唯一 Reducer）、`coaching_skills.py`（六 Skill 合同）、
  `project_memory.py`、状态 Schema v2。
- 生产内容子 Agent（工具层）已具备被调用的接口：`POST /agent`
  （自然语言 + 可选 ip_brief 画像），返回 quote/running/completed/text，
  与 `agent_runtime` 的状态机对应。
- 实施以增量 PR 进行，每阶段独立验收后再进下一阶段。
