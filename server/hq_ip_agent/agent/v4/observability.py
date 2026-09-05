"""v4 结构化日志：统一 logging，带 sid/seq 上下文字段（观测 #28）。

所有关键日志点从 print(..., flush=True) 迁到 logging，journalctl 按
sid= 直接过滤单会话链路、seq= 定位具体轮次；拿不到上下文的点
（启动、媒体下载等）自动落默认值 "-"，不会 KeyError。

用法：
    from agent.v4 import observability
    import logging
    log = logging.getLogger(__name__)
    log.info("交付完成", extra=observability.ctx(sid, seq))

ContextFilter 为每条 LogRecord 补默认 sid/seq，Formatter 直接引用。
setup_logging() 幂等（多次调用只装一次 handler），app.py __main__ 启动时调用。
"""
from __future__ import annotations

import logging

_DEFAULT = "-"

_FORMAT = ("%(asctime)s %(levelname)s %(name)s "
           "[sid=%(sid)s seq=%(seq)s] %(message)s")

_SETUP_DONE = False


class _ContextFilter(logging.Filter):
    """给缺上下文的记录补默认 sid/seq 占位。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "sid"):
            record.sid = _DEFAULT
        if not hasattr(record, "seq"):
            record.seq = _DEFAULT
        return True


def setup_logging(level: int = logging.INFO) -> None:
    """安装根 logger 的 StreamHandler（stderr，journalctl 统一捕获）。幂等。"""
    global _SETUP_DONE
    if _SETUP_DONE:
        return
    root = logging.getLogger()
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(_ContextFilter())
    root.addHandler(handler)
    root.setLevel(level)
    _SETUP_DONE = True


def ctx(sid: str | None = None, seq=None) -> dict:
    """构造 logging extra：None/空串自动落默认占位符。"""
    return {
        "sid": sid or _DEFAULT,
        "seq": seq if seq is not None else _DEFAULT,
    }
