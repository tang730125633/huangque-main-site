"""SpecialistResult 六态协议：子 Agent → 主 Agent 的唯一返回格式。

子 Agent 每轮只返回一个状态；参数细节（工具名、完整 inputs）不回流主 Agent，
只回流「成了什么 / 没成什么 / 要不要重试」的摘要与必要凭据。
"""
from __future__ import annotations

# 六态
COMPLETED = "completed"            # 完成，result 带成果（资产 id/URL/内容）
RUNNING = "running"                # 已提交，带 job_id（后续轮询）
NEEDS_USER_INPUT = "needs_user_input"  # 缺必填参数，question 向用户提问
NEEDS_APPROVAL = "needs_approval"  # 已报价，quote 带 quote_token/cost/points
FAILED = "failed"                  # 失败，带 error_code 与 retryable
CANCELLED = "cancelled"            # 用户取消

STATES = (COMPLETED, RUNNING, NEEDS_USER_INPUT, NEEDS_APPROVAL, FAILED, CANCELLED)

# 允许跨域交接的凭据字段（主 Agent 编排下一跳时原样传递）
CREDENTIAL_FIELDS = (
    "job_id", "task_id", "request_id", "run_id", "quote_token", "plan_digest",
    "consent_token", "audio_upload_id", "upload_id", "image_upload_id",
    "asset_id", "asset_key", "project_id", "workflow_id", "revision",
    "board_id", "node_id", "url", "file_path", "reference_version",
)


def make(
    state: str,
    summary: str,
    result: dict | None = None,
    question: str | None = None,
    missing_inputs: list | None = None,
    quote: dict | None = None,
    error_code: str | None = None,
    retryable: bool = False,
    trace: list | None = None,
) -> dict:
    """构造一个 SpecialistResult（dict 形态，便于 JSON 传输与存储）。"""
    if state not in STATES:
        raise ValueError(f"非法状态：{state}")
    return {
        "state": state,
        "summary": summary or "",
        "result": result or {},
        "question": question or "",
        "missing_inputs": missing_inputs or [],
        "quote": quote or {},
        "error_code": error_code or "",
        "retryable": bool(retryable),
        "trace": trace or [],
    }


def is_terminal(state: str) -> bool:
    """终态：completed / failed / cancelled（不再回派）。"""
    return state in (COMPLETED, FAILED, CANCELLED)


def strip_for_main(res: dict) -> dict:
    """回传给主 Agent 时的安全裁剪：保留六态与凭据，去掉工具级 trace 噪音。"""
    out = dict(res)
    out.pop("trace", None)
    return out
