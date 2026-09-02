# 黄雀·编导 Agent — 本地对话验收原型 yuelei-local-agent v1

> ⚠️ 本目录是**独立的本地验收原型**，用于验证"黄雀编导 Agent"从对话→意图→报价→确认→生产→对账的端到端闭环，**不是**可并入黄雀生产 `server` 的正式模块。

## 这是什么

一个跑在开发者本机的模拟服务，模拟"黄雀编导 Agent"如何与客户对话并驱动一次付费视频生产：

- 前台 `agent-lab.html`：客户对话台 + 素材上传。
  每轮 Agent 都会在右侧"本轮判断"区实时给出：识别到的**意图**、选中/建议的**能力**、**缺失参数**、建议**下一步**、以及发言**合规问题**。
  付费类需求不直接提交：先由系统**给报价** → 客户**点价格确认按钮** → 才提交任务 → 轮询任务状态 → 成功回显/失败标"已退 N 点或退款待核对"。

- 后端 `local_ui.py`：本地 HTTP 服务，提供 `/api/local-agent/*` 的能力：
  - 客户登录态与会话、**CSRF Token 校验**；
  - 素材上传 + **素材使用授权确认**（新增/更换素材后**强制重置授权**、需重新确认）；
  - DeepSeek 对话（`api_key` 读取自环境变量 `DEEPSEEK_API_KEY`，**无任何硬编码密钥**）；
  - 意图识别 → 能力选择 → 缺失参数 → 报价 → 确认扣点 → 视频生成任务 → **对账**（成功给结果视频；失败标注已退款/待核对）；
  - **明确不自动发布、不自动删除**；素材库连接仅用于素材取用。

  > 能力查询、报价、确认扣点与对账等真实动作**由黄雀 CLI（`hq_cli`）驱动**，并非纯本地假流程。

## 依赖（重要）

运行前需具备：
- Python 3.10+；
- 可访问 **DeepSeek API**（设 `DEEPSEEK_API_KEY`）；
- **已安装可导入的黄雀 CLI `hq_cli`**：`python -m hq_cli` 能被调用；
- **黄雀 CLI 已登录授权**：后端首次使用时通过 `hq_cli login` 完成授权（内部 `_ensure_cli_authorized` 会做校验）；
- 三个可配置的环境变量：
  - `DEEPSEEK_API_KEY`（必填，DeepSeek）
  - `DEEPSEEK_BASE_URL`（可选，默认 `https://api.deepseek.com`）
  - `HQ_CLI_CONFIG_DIR`（可选，黄雀 CLI 会话目录；**默认硬编码为当前机器路径 `E:\AI\data\Huangque\hq-cli-user`，换机需另设此变量**）

## 如何本地跑

```bash
# 1) 依赖&登录
pip install -r requirements.txt 2>/dev/null || true   # 视环境
python -m hq_cli login             # 确保 CLI 已授权
# 2) 环境变量
#    export DEEPSEEK_API_KEY=<你的 key>
#    export HQ_CLI_CONFIG_DIR=<你的黄雀CLI用户目录>   # 非开发机时必须设
# 3) 启动（站点目录为必填位置参数，缺参会友好报错退出）
export DEEPSEEK_API_KEY=<你的 key>
export HQ_CLI_CONFIG_DIR="E:/AI/data/Huangque/hq-cli-user"   # 或你环境
python local_ui.py <黄雀站点目录> [端口]      # 例: python local_ui.py E:/AI/data/Huangque/site 8765
# 浏览器打开 http://127.0.0.1:8765/agent-lab.html
# （页面 URL 带 ?admin=1 会显示"连接模型"管理面板）
```

界面含"登录测试账号"按钮，用于本机走通整个确认流程。

## 为什么放在独立目录，而非并入 server/？

- 它是一套**自洽的本地验证服务**：自带登录态/扣点/对账语义，但**没有接入唐生产 `server` 的鉴权、支付、数据库约定与部署体系**。
- 直接并入 `server/` 会与现有支付/权限/DB/部署 ABI 冲突。
- 因此放入 `agent-prototypes/yuelei-local-agent/` 供 review 与验证；若要从"验收原型"演进成黄雀正式 `agent` 功能，需另行按生产 ABI 做适配、过黄雀 PR 验收门禁后并入。

## 版本 / 边界（写给审者)

- 这是 **v1 原型**，用于本地走通"对话→意图→报价→确认→扣点任务→对账"的闭环；
- 提交不含本机运行态测试素材与缓存（agent-runs.json、uploads/），不含任何密钥；
- 合并与否、是否演进为正式 agent 模块、落入何种部署，均由生产 gate / 审者决定；本 PR 不主张自动接入或自动部署。
