"""v4 运行时：主 Agent（路由）+ 12 个业务子 Agent（独立 LLM 实例）的对话运行时。

模块：
- protocol:  SpecialistResult 六态协议
- state:     会话级状态（主会话 + 子 Agent 会话 + pending quote）
- skills:    业务 skill / AGENTS.md / use-huangque-cli 加载与摘要
- livecaps:  hq 实时能力目录与 describe 精简契约
- subagent:  子 Agent 运行时（hq 工具 + finish，先报价后确认强制门禁）
- main_agent:主 Agent 运行时（路由表 + delegate + 本地 IP 管线）
"""
from . import protocol, state, skills, livecaps, subagent, main_agent  # noqa: F401

__all__ = ["protocol", "state", "skills", "livecaps", "subagent", "main_agent"]
