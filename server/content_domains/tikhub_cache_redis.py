"""Redis TTL 后端：TikHub 采集缓存（M3F 域）。

源语义（``server/tikhub.py`` 的 SQLite 路径）：表
``cache(k TEXT PRIMARY KEY, v TEXT, exp INTEGER)``，``v`` 是 ``json.dumps(val)`` 后的
payload，``exp = int(time.time()) + ttl``；读时判 ``exp > time.time()``，写时顺手
``DELETE FROM cache WHERE exp < now`` 清过期行。线上该文件 0.9MB、SQLite 不回收空间
（实测 8 行仍占 925KB），且 content 与 leadgen 两个进程共享同一文件。

``HQ_TIKHUB_CACHE=redis`` 时由本模块接管这三个动作（get/set/del）；默认 sqlite 模式
完全走 ``tikhub.py`` 里的原路径，行为与迁移前逐字节一致。缓存可丢——**没有回填、
没有 Alembic 迁移、没有审计表**。

键规范
------
``<前缀><原 SQLite 主键>``，默认前缀 ``hq:tikhub:cache:``（``HQ_TIKHUB_CACHE_PREFIX``
只给测试隔离用，生产不要改）。原主键由 ``tikhub.py`` 生成：

* 搜索 ``srch:<平台>:<关键词>:<页>:<video_only 0|1>`` → TTL 1800s
* 详情 ``det:<平台>:<id 或分享链>`` → TTL 3600s（视频号 detail 读写都不缓存）
* 评论 ``cmt:<平台>:<id>:<cursor>:<count>`` → TTL 3600s

TTL 沿用源逻辑：调用方原样把 ttl 传进来，Redis 用 ``SET ... EX ttl`` 一一对应
（源的 ``exp`` 列不再需要；源靠扫描清过期，Redis 到期自动删除）。``ttl <= 0`` 在源里
等于「写一条立即过期、永不可读的行，并且覆盖掉旧值」——这里等价地删除该键，
绝不写 0/负 TTL（Redis 会直接报错）。

失败语义（必须与源 SQLite 路径一致）
------------------------------------
缓存永远不能炸主流程：源路径对任何异常 ``except Exception: pass``（读→None、写→丢弃）。
本模块运行时同样降级——读→None、写→no-op，只打一次告警，绝不把异常抛给调用方。

唯一例外是**配置错误**，与 M3A 的 ``flags_store`` 同一纪律：
``HQ_TIKHUB_CACHE`` 取值非法 → ``mode()`` 抛 ``RuntimeError``（进程侧立刻暴露，
不允许「以为切了其实没切」的静默双权威）；``HQ_REDIS_URL`` 未配置 → 显式的
``client()`` 抛 ``RuntimeError``（运维自检 / 测试用），业务路径 ``get/set/delete``
则记 error 日志后降级（线上宁可少缓存，也不能让采集任务挂掉）。切换后必须按
runbook 确认 journal 里没有 ``HQ_REDIS_URL is not configured``。
"""

from __future__ import annotations

import json
import logging
import os
import threading

log = logging.getLogger("hq.tikhub_cache_redis")

_MODES = {"sqlite", "redis"}
_DEFAULT_PREFIX = "hq:tikhub:cache:"

_client = None
_lock = threading.Lock()
_warned = set()  # 同类降级告警每次进程只打一次，避免刷爆 journal

_MODE_ANNOUNCED = False


def _announce_mode(env_name: str, value: str) -> None:
    """进程内一次性权威声明：第一次解析出模式时留一条日志。

    环境变量缺失/为空 = 静默退回默认旧存储，是切写后最危险的情形，
    用 WARNING 保证默认日志级别可见；显式配置用 INFO。
    """
    global _MODE_ANNOUNCED
    if _MODE_ANNOUNCED:
        return
    _MODE_ANNOUNCED = True
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        log.warning(
            "%s not set, falling back to default %r (legacy storage)",
            env_name, value,
        )
    else:
        log.info("%s authority announced: mode=%s", env_name, value)


def mode() -> str:
    """``sqlite``（默认）/ ``redis``；非法值直接抛错（配置错误必须立刻可见）。"""
    value = (os.environ.get("HQ_TIKHUB_CACHE") or "sqlite").strip().lower()
    if value not in _MODES:
        raise RuntimeError("HQ_TIKHUB_CACHE must be sqlite or redis")
    _announce_mode("HQ_TIKHUB_CACHE", value)
    return value


def enabled() -> bool:
    """缓存是否已切到 Redis 权威。"""
    return mode() == "redis"


def prefix() -> str:
    value = (os.environ.get("HQ_TIKHUB_CACHE_PREFIX") or "").strip()
    return value or _DEFAULT_PREFIX


def key_for(key) -> str:
    """原 SQLite 主键 → Redis 键（含命名空间前缀）。"""
    return prefix() + str(key)


def _connect(url):
    import redis  # 延迟导入：默认 sqlite 模式不需要 redis 库

    conn = redis.Redis.from_url(
        url, socket_timeout=5, socket_connect_timeout=3, decode_responses=True
    )
    conn.ping()
    return conn


def client():
    """懒加载单例。``HQ_REDIS_URL`` 缺失或连不上都抛 ``RuntimeError``（绝不在此降级）。"""
    global _client
    if _client is not None:
        return _client
    url = (os.environ.get("HQ_REDIS_URL") or "").strip()
    if not url:
        raise RuntimeError("HQ_REDIS_URL is not configured")
    with _lock:
        if _client is None:
            _client = _connect(url)
    return _client


def _client_or_none():
    """业务路径用：任何失败都降级为 None（缓存失败不能炸主流程）。"""
    try:
        return client()
    except Exception as exc:
        _warn_once(
            "client",
            "Redis 缓存不可用（降级：读→未命中，写→丢弃）：%s: %s"
            % (exc.__class__.__name__, str(exc)[:160]),
        )
        return None


def available() -> bool:
    """自检用：Redis 是否真的可读写。"""
    try:
        return bool(client().ping())
    except Exception:
        return False


def describe() -> dict:
    """不含任何密钥的运行态摘要（runbook 切换后自检用）。"""
    return {
        "mode": mode(),
        "prefix": prefix(),
        "url_configured": bool((os.environ.get("HQ_REDIS_URL") or "").strip()),
        "available": available(),
    }


def _warn_once(tag, message):
    with _lock:
        if tag in _warned:
            return
        _warned.add(tag)
    log.warning(message)


def seconds(ttl):
    """``ttl`` → 秒（int）；无法转换返回 None（源里 int() 抛错被吞：不写、原条目保留）。"""
    try:
        return int(ttl)
    except (TypeError, ValueError):
        return None


def get(key):
    """命中且未过期→payload；未命中/过期/脏值/Redis 故障→None（与源路径同语义）。"""
    conn = _client_or_none()
    if conn is None:
        return None
    try:
        raw = conn.get(key_for(key))
    except Exception as exc:
        _warn_once("get", "Redis 缓存读失败（降级为未命中）：%s" % exc.__class__.__name__)
        return None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except Exception:
        return None  # 脏值当未命中，且不删键（与源一致：源也只是读不到）


def set(key, val, ttl):
    """写缓存并设置 TTL；任何失败静默丢弃（与源 SQLite 路径一致）。"""
    conn = _client_or_none()
    if conn is None:
        return
    lease = seconds(ttl)
    if lease is None:
        return  # 源里 int(time.time()) + ttl 抛 TypeError 被吞：不写，原条目保留
    try:
        target = key_for(key)
        if lease <= 0:
            conn.delete(target)  # 源的「写入即过期」= 旧值被覆盖且永不可读
            return
        conn.set(target, json.dumps(val, ensure_ascii=False), ex=lease)
    except Exception as exc:
        _warn_once("set", "Redis 缓存写失败（降级：丢弃本次写入）：%s" % exc.__class__.__name__)


def delete(key):
    """作废单个缓存键。源 SQLite 路径没有删除入口（只有过期），此函数供运维/测试用。"""
    conn = _client_or_none()
    if conn is None:
        return
    try:
        conn.delete(key_for(key))
    except Exception as exc:
        _warn_once("delete", "Redis 缓存删除失败：%s" % exc.__class__.__name__)


def close() -> None:
    """测试与进程退出清理用；生产进程长驻不需要调用。"""
    global _client
    with _lock:
        current, _client = _client, None
    if current is not None:
        try:
            current.close()
        except Exception:
            pass


def _set_client_for_testing(conn) -> None:
    """注入替身客户端（本地无 Redis 的自检用）；传 None 恢复为环境变量驱动。"""
    global _client
    with _lock:
        _client = conn
