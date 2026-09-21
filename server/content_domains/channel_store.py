"""PostgreSQL backend for the versioned channel routing tables (routing schema).

由 ``channel_manager`` / ``channel_lifecycle`` 在 ``HQ_CHANNEL_STORE=postgres`` 时调用；
默认（未配置）走这两模块内既有的 SQLite 路径（``channel_management.db``，11 张表），
行为与迁移前逐字节一致。

本模块只认 PostgreSQL：**绝不 import sqlite3**，任何连接或执行失败都抛异常，由上层
沿用既有语义：

* 保存/映射/生命周期/调度路径 fail-closed —— 存储不可用即报错，绝不「静默成功」；
* 恢复态查询（``task_recovery_state`` / ``mark_interrupted_task_unknown`` /
  ``search_task_ids``）与 SQLite 路径一样把存储错误当成「读不到证据」处理
  （``unavailable`` / 空集），绝不当成「任务失败」或「没有这类任务」。

与 SQLite 的语义映射（逐条对应，切换前后行为一致）：

===================  =========================================================
SQLite 源             PostgreSQL 目标
===================  =========================================================
``BEGIN IMMEDIATE``   写路径显式加锁：涉及行存在时用 ``SELECT ... FOR UPDATE``
                      （渠道行），行可能不存在时用事务级咨询锁
                      ``pg_advisory_xact_lock``（新建渠道、映射发布、队列检查等），
                      等价于「同一时刻只有一个写者」且锁随后续提交/回滚释放。
``INSERT OR REPLACE`` ``INSERT ... ON CONFLICT (主键) DO UPDATE SET 全列``。
``SUM(state='failed')`` ``SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END)``
                      （零行时同样返回 NULL，后台展示口径不变）。
``ORDER BY ...,rowid`` PostgreSQL 没有隐式 rowid：改用 ``(updated DESC, id)`` 作
                      确定性次序，见 ``_LATEST_*`` 注释。
0/1 的 ``enabled``      写入一律 ``bool()`` 归一化到 boolean 列。
===================  =========================================================

切换纪律：同一时刻只能有一个权威。切换时所有读方（content / imggen / admin）必须
同一版本、同一开关一起切，禁止双权威并存。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager, closing

_MODES = {"sqlite", "postgres"}
_pool = None
_pool_lock = threading.Lock()

# 咨询锁命名空间（固定常量，避免与其它域串锁）；第二个键用 hashtext(名称)。
_ADVISORY_NAMESPACE = 0x48514331  # 'HQC1'

_log = logging.getLogger("hq.channel_store")
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
        _log.warning(
            "%s not set, falling back to default %r (legacy storage)",
            env_name, value,
        )
    else:
        _log.info("%s authority announced: mode=%s", env_name, value)


def mode() -> str:
    value = (os.environ.get("HQ_CHANNEL_STORE") or "sqlite").strip().lower()
    if value not in _MODES:
        raise RuntimeError("HQ_CHANNEL_STORE must be sqlite or postgres")
    _announce_mode("HQ_CHANNEL_STORE", value)
    return value


def enabled() -> bool:
    """是否已切换 PostgreSQL 权威。"""
    return mode() == "postgres"


def _pool_instance():
    global _pool
    if _pool is not None:
        return _pool
    url = (os.environ.get("HQ_DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError("HQ_DATABASE_URL is not configured")
    with _pool_lock:
        if _pool is None:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            maximum = int(os.environ.get("HQ_CHANNEL_DB_POOL_MAX") or "4")
            if maximum < 1 or maximum > 20:
                raise RuntimeError("HQ_CHANNEL_DB_POOL_MAX must be between 1 and 20")
            candidate = ConnectionPool(
                conninfo=url,
                min_size=1,
                max_size=maximum,
                timeout=5,
                kwargs={"autocommit": False, "row_factory": dict_row},
                open=False,
            )
            candidate.open(wait=True, timeout=5)
            _pool = candidate
    return _pool


def close_pool() -> None:
    """测试与进程退出清理用；生产进程长驻不需要调用。"""
    global _pool
    with _pool_lock:
        current, _pool = _pool, None
    if current is not None:
        current.close()


def _mgr():
    """惰性取 ``channel_manager``：复用其纯逻辑与常量（避免模块级循环导入）。"""
    from . import channel_manager
    return channel_manager


def _storage_errors():
    """与 SQLite 路径 ``(OSError, sqlite3.Error)`` 对应的 PostgreSQL 错误集合。

    只用于「读不到证据不等于失败」的三处恢复态查询；其它路径一律不吞错。
    """
    errors = [OSError]
    try:
        import psycopg
    except ImportError:  # pragma: no cover - 未装 psycopg 时该模式本来就用不了
        return tuple(errors)
    errors.append(psycopg.Error)
    try:
        from psycopg_pool import PoolTimeout
    except ImportError:  # pragma: no cover
        PoolTimeout = None
    if PoolTimeout is not None and PoolTimeout not in errors:
        errors.append(PoolTimeout)
    return tuple(errors)


def _lock(conn, name: str) -> None:
    """取事务级咨询锁，等价于 SQLite 的单写者语义（锁随事务结束释放）。"""
    conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_ADVISORY_NAMESPACE, name))


def _audit(conn, action, target, actor) -> None:
    conn.execute(
        "INSERT INTO routing.events(id,action,target,actor,created) VALUES(%s,%s,%s,%s,%s)",
        (uuid.uuid4().hex, action, target, actor, time.time()),
    )


def _revision(value):
    """把版本号归一化为 int；不是精确整数时返回 None。

    SQLite 里 ``WHERE version=2.5`` 只是查不到行（随后报「渠道版本不存在」），
    这里保持同样的结果，不引入新的类型错误。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number


# runs 表「最近一行」的次序：SQLite 是 ``ORDER BY started DESC,rowid DESC``（同一 started
# 取最后插入的那行）。PostgreSQL 没有隐式 rowid，改用 (updated DESC, id) 作确定性次序；
# started 为微秒级时间戳，需要靠 tiebreak 才分得出的情况实际不会出现（runbook 已记录）。
_LATEST_KIND = (
    "SELECT * FROM routing.runs WHERE channel=%s AND version=%s AND kind=%s "
    "ORDER BY started DESC,updated DESC,id LIMIT 1"
)
_LATEST_FULL = (
    "SELECT state,updated FROM routing.runs WHERE channel=%s AND version=%s AND kind='full' "
    "ORDER BY started DESC,updated DESC,id LIMIT 1"
)
_LATEST_CHECK = (
    "SELECT kind,state,updated,detail,version FROM routing.runs WHERE channel=%s AND version=%s AND kind=%s "
    "ORDER BY started DESC,updated DESC,id LIMIT 1"
)


# --------------------------------------------------------------------------- #
# 渠道与版本（routing.channels / routing.versions）
# --------------------------------------------------------------------------- #

def _fetch_version(conn, cid, rev, with_secret=False, missing_revision_message='渠道版本不存在'):
    """在给定连接上读渠道版本；``version()`` 与 ``overview()`` / ``reserve()`` 共用。"""
    mgr = _mgr()
    if rev is None:
        row = conn.execute('SELECT version FROM routing.channels WHERE id=%s', (cid,)).fetchone()
        if not row:
            raise ValueError('渠道不存在')
        rev = row['version']
    number = _revision(rev)
    row = None
    if number is not None:
        row = conn.execute(
            'SELECT * FROM routing.versions WHERE channel=%s AND version=%s', (cid, number),
        ).fetchone()
    if not row:
        raise ValueError(missing_revision_message)
    result = dict(json.loads(row['config']), id=cid, version=rev)
    if with_secret:
        result['secret'] = mgr._crypt(row['secret'], True)
    return result


def version(cid, rev=None, with_secret=False):
    with _pool_instance().connection() as conn:
        return _fetch_version(conn, cid, rev, with_secret)


def save(actor, body):
    mgr = _mgr()
    cid, config = mgr._channel_config(body)
    with _pool_instance().connection() as conn:
        with conn.transaction():
            # SQLite 用 BEGIN IMMEDIATE 串行化整个「读旧版本 → 写新版本」过程；
            # 渠道行可能尚不存在，所以先取咨询锁，再对已存在的行加 FOR UPDATE。
            _lock(conn, 'routing.channel:' + cid)
            old = conn.execute(
                'SELECT * FROM routing.channels WHERE id=%s FOR UPDATE', (cid,),
            ).fetchone()
            if old and int(body.get('version', -1)) != int(old['version']):
                raise ValueError('配置已被修改，请刷新后重试')
            if old:
                old_config = json.loads(conn.execute(
                    'SELECT config FROM routing.versions WHERE channel=%s AND version=%s',
                    (cid, old['version']),
                ).fetchone()['config'])
                if old_config.get('_lifecycle', {}).get('deleted'):
                    raise ValueError('渠道在回收站，请先恢复后编辑')
                if old_config.get('_lifecycle'):
                    config['_lifecycle'] = old_config['_lifecycle']
                if old_config.get('parameters'):
                    from .channel_parameters import validate
                    config['parameters'] = validate(config, old_config['parameters'])
                for mapping_row in conn.execute('SELECT config FROM routing.mappings').fetchall():
                    mapping = json.loads(mapping_row['config'])
                    if (mapping.get('enabled') and cid in mgr.mapping_channel_ids(mapping)
                            and mapping.get('kind') != mgr.ADAPTERS[config['adapter']]['kind']):
                        raise ValueError('该渠道仍被已启用映射使用，不能更改为不兼容协议；请先调整映射')
                for mapping_row in conn.execute(
                        'SELECT operation_id,state,config FROM routing.operation_mappings').fetchall():
                    mapping = json.loads(mapping_row['config'])
                    if (mapping_row['state'] in {'shadow', 'managed'}
                            and cid in mgr.mapping_channel_ids(mapping)):
                        from .function_registry import operation
                        try:
                            mgr._validate_operation_config(config, operation(mapping_row['operation_id']))
                        except ValueError as exc:
                            raise ValueError('该渠道仍被功能映射使用，不能保存不兼容配置；请先调整映射') from exc
            version_number = int(old['version']) + 1 if old else 1
            if old and 'reference_images' not in config['fixture']:
                previous = json.loads(conn.execute(
                    'SELECT config FROM routing.versions WHERE channel=%s AND version=%s',
                    (cid, old['version']),
                ).fetchone()['config'])
                if previous.get('fixture', {}).get('reference_images'):
                    config['fixture']['reference_images'] = previous['fixture']['reference_images']
            secret = mgr._crypt(str(body['secret'])) if body.get('secret') else ''
            if not secret and old:
                secret = conn.execute(
                    'SELECT secret FROM routing.versions WHERE channel=%s AND version=%s',
                    (cid, old['version']),
                ).fetchone()['secret']
            if not secret:
                raise ValueError('请填写 API 密钥；保险箱未配置时不能保存密钥')
            if config['daily_test']:
                from .channel_runtime import validate_payload
                validate_payload(config, config['fixture'])
            conn.execute(
                'INSERT INTO routing.versions(channel,version,config,secret,actor,created) '
                'VALUES(%s,%s,%s,%s,%s,%s)',
                (cid, version_number, json.dumps(config), secret, actor, time.time()),
            )
            conn.execute(
                'INSERT INTO routing.channels(id,version,enabled) VALUES(%s,%s,%s) '
                'ON CONFLICT (id) DO UPDATE SET version=EXCLUDED.version, enabled=EXCLUDED.enabled',
                (cid, version_number, bool(body.get('enabled') is True)),
            )
            conn.execute(
                'INSERT INTO routing.schedule(channel,light_due,full_due) VALUES(%s,%s,%s) '
                'ON CONFLICT (channel) DO UPDATE SET light_due=EXCLUDED.light_due, '
                'full_due=EXCLUDED.full_due',
                (cid, time.time() + 60, mgr._next_daily(config['daily_hour'])),
            )
            _audit(conn, 'channel.save', cid, actor)
    return {'id': cid, 'version': version_number}


# --------------------------------------------------------------------------- #
# 功能映射（routing.operation_mappings / _versions）
# --------------------------------------------------------------------------- #

def _fetch_operation_mapping(conn, operation_id, revision=None):
    """在给定连接上读映射；``operation_mapping()`` 与 ``overview()`` 共用。"""
    if revision is None:
        row = conn.execute(
            'SELECT operation_id,revision,state,config,actor,updated '
            'FROM routing.operation_mappings WHERE operation_id=%s',
            (operation_id,),
        ).fetchone()
    else:
        number = _revision(revision)
        row = None
        if number is not None:
            row = conn.execute(
                'SELECT operation_id,revision,state,config,actor,created AS updated '
                'FROM routing.operation_mapping_versions WHERE operation_id=%s AND revision=%s',
                (operation_id, number),
            ).fetchone()
    if not row:
        return None
    result = dict(json.loads(row['config']), operation_id=row['operation_id'],
                  revision=row['revision'], state=row['state'], actor=row['actor'],
                  updated=row['updated'])
    result['channels'] = _mgr().mapping_channel_ids(result)
    return result


def operation_mapping(operation_id, revision=None, connection=None):
    operation_id = str(operation_id or '').strip()
    if connection is None:
        with _pool_instance().connection() as conn:
            return _fetch_operation_mapping(conn, operation_id, revision)
    return _fetch_operation_mapping(connection, operation_id, revision)


def _mapping_channel(cid, contract_or_kind, require_enabled=False, connection=None):
    """解析渠道当前配置并校验能否接该功能（PG）。

    ``require_enabled=True`` 只额外要求渠道启用；完整生成测试的 24 小时时效不再是接单门槛，
    仅保留在 ``_ready_candidate()``（自动故障切换前的复核）。
    """
    mgr = _mgr()
    contract = contract_or_kind if isinstance(contract_or_kind, dict) else None
    kind = contract['channel_kind'] if contract else str(contract_or_kind)
    cid = str(cid or '').strip()
    if not cid:
        raise ValueError('请选择主渠道')
    owns_connection = connection is None
    conn = connection or _pool_instance().getconn()
    try:
        # 带 connection 时调用方已持有写事务（SQLite 的 BEGIN IMMEDIATE），
        # 因此这里加行锁；独立读取（接单捕获）与 SQLite 一样只是普通读。
        suffix = ' FOR UPDATE' if not owns_connection else ''
        current = conn.execute(
            'SELECT version,enabled FROM routing.channels WHERE id=%s' + suffix, (cid,),
        ).fetchone()
        if not current:
            raise ValueError('渠道不存在')
        version_row = conn.execute(
            'SELECT config FROM routing.versions WHERE channel=%s AND version=%s',
            (cid, current['version']),
        ).fetchone()
        if not version_row:
            raise ValueError('渠道版本不存在')
        cfg = dict(json.loads(version_row['config']), id=cid, version=current['version'])
        if cfg.get('_lifecycle', {}).get('deleted'):
            raise ValueError('回收站渠道不能配置映射')
        if contract:
            mgr._validate_operation_config(cfg, contract)
        elif mgr.ADAPTERS[cfg['adapter']]['kind'] != kind:
            raise ValueError('功能与渠道能力不兼容')
        if require_enabled and not current['enabled']:
            raise ValueError('渠道尚未启用，不能接单或发布为托管状态')
    finally:
        if owns_connection:
            _pool_instance().putconn(conn)
    return cfg


def _ready_candidate(cid, connection=None):
    """自动故障切换前的候选复核（PG）：存在、未回收、仍启用、且有最近 24 小时完整生成测试。

    刻意不跟着手动切换放宽：自动切换在无人值守下把任务交给另一个渠道，仍然要求
    最近 24 小时内有通过的完整生成测试。
    """
    mgr = _mgr()
    cid = str(cid or '').strip()
    if not cid:
        raise ValueError('候选渠道无效')
    owns_connection = connection is None
    conn = connection or _pool_instance().getconn()
    try:
        current = conn.execute('SELECT version,enabled FROM routing.channels WHERE id=%s', (cid,)
                               ).fetchone()
        if not current:
            raise ValueError('候选渠道不存在')
        row = conn.execute('SELECT config FROM routing.versions WHERE channel=%s AND version=%s',
                           (cid, current['version'])).fetchone()
        if not row:
            raise ValueError('候选渠道版本不存在')
        if json.loads(row['config']).get('_lifecycle', {}).get('deleted'):
            raise ValueError('候选渠道在回收站')
        if not current['enabled']:
            raise ValueError('候选渠道已停用')
        latest = conn.execute(_LATEST_FULL, (cid, current['version'])).fetchone()
        if not latest or latest['state'] != 'passed' or time.time() - latest['updated'] > 86400:
            raise ValueError('候选渠道缺少最近24小时通过的完整生成测试')
    finally:
        if owns_connection:
            _pool_instance().putconn(conn)
    return True


def save_operation_mapping(actor, body):
    """Publish one immutable operation mapping revision with optimistic locking."""
    from .function_registry import operation
    mgr = _mgr()
    operation_id = str(body.get('operation_id') or '').strip()
    contract = operation(operation_id)
    if not contract or not contract['channel_eligible']:
        raise ValueError('该功能不支持通用图片/视频渠道映射')
    state = str(body.get('state') or 'legacy').strip().lower()
    if state not in mgr.MAPPING_STATES:
        raise ValueError('映射状态必须为 legacy、shadow、managed 或 paused')
    channels = mgr._requested_mapping_channels(body)
    now = time.time()
    with _pool_instance().connection() as conn:
        with conn.transaction():
            _lock(conn, 'routing.operation_mapping:' + operation_id)
            if state in {'shadow', 'managed'}:
                if not channels:
                    raise ValueError('请选择主渠道')
                # 手动切换只看渠道自身能否接这个功能（存在 / 未回收 / 已启用 / 能力匹配），
                # 不再要求候补与主渠道同名同协议同参数；每条渠道保留自己的模型 ID。
                for target in channels:
                    _mapping_channel(target, contract, require_enabled=True, connection=conn)
            else:
                channels = []
            cid = channels[0] if channels else ''
            backup = channels[1] if len(channels) > 1 else ''
            config = {
                'kind': contract['channel_kind'], 'label': contract['name'],
                'channel': cid, 'backup': backup, 'channels': channels,
            }
            display_order = mgr._display_order(body, state, channels)
            if display_order is not None:
                config['display_order'] = display_order
            current = conn.execute(
                'SELECT revision FROM routing.operation_mappings WHERE operation_id=%s',
                (operation_id,),
            ).fetchone()
            expected = body.get('expected_revision')
            try:
                expected_number = None if expected in (None, '') else int(expected)
            except (TypeError, ValueError):
                raise ValueError('功能映射版本无效，请刷新后重试') from None
            if current and expected_number != int(current['revision']):
                raise ValueError('功能映射已被修改，请刷新后重试')
            if not current and expected_number not in (None, 0):
                raise ValueError('功能映射版本无效，请刷新后重试')
            revision = int(current['revision']) + 1 if current else 1
            encoded = json.dumps(config, ensure_ascii=False)
            conn.execute(
                'INSERT INTO routing.operation_mapping_versions'
                '(operation_id,revision,state,config,actor,created) VALUES(%s,%s,%s,%s,%s,%s)',
                (operation_id, revision, state, encoded, actor, now),
            )
            conn.execute(
                'INSERT INTO routing.operation_mappings'
                '(operation_id,revision,state,config,actor,updated) VALUES(%s,%s,%s,%s,%s,%s) '
                'ON CONFLICT (operation_id) DO UPDATE SET revision=EXCLUDED.revision, '
                'state=EXCLUDED.state, config=EXCLUDED.config, actor=EXCLUDED.actor, '
                'updated=EXCLUDED.updated',
                (operation_id, revision, state, encoded, actor, now),
            )
            _audit(conn, 'operation-mapping.publish', operation_id + ':v' + str(revision), actor)
    return operation_mapping(operation_id)


def rollback_operation_mapping(actor, body):
    """Restore a historical mapping by publishing it as a new revision."""
    operation_id = str(body.get('operation_id') or '').strip()
    try:
        target_revision = int(body.get('target_revision'))
    except (TypeError, ValueError):
        raise ValueError('请选择有效的历史映射版本') from None
    old = operation_mapping(operation_id, target_revision)
    if not old:
        raise ValueError('历史映射版本不存在')
    return save_operation_mapping(actor, {
        'operation_id': operation_id, 'state': old['state'],
        'channels': _mgr().mapping_channel_ids(old),
        'display_order': old.get('display_order'),
        'expected_revision': body.get('expected_revision'),
    })


# --------------------------------------------------------------------------- #
# 旧线路映射（routing.mappings）
# --------------------------------------------------------------------------- #

def save_mapping(actor, body):
    mgr = _mgr()
    kind, front = str(body.get('kind') or ''), str(body.get('front') or '').strip()
    cid, backup = str(body.get('channel') or ''), str(body.get('backup') or '')
    if kind == 'xiaole_video' and front not in {'grok', 'grok15', 'minimax', 'omni', 'micro'}:
        raise ValueError('请选择现有视频请求标识 grok、grok15、minimax、omni 或 micro；新增前台入口需另行接入价格与权限')
    cfg = version(cid)
    if kind != mgr.ADAPTERS[cfg['adapter']]['kind'] or not front or len(front) > 100:
        raise ValueError('功能类型与渠道能力不兼容，或前台标识未填写')
    if backup and (backup == cid or mgr.ADAPTERS[version(backup)['adapter']]['kind'] != kind):
        raise ValueError('备用渠道必须不同且能力兼容')
    config = dict(kind=kind, front=front, label=str(body.get('label') or front)[:100], channel=cid,
                  backup=backup, enabled=body.get('enabled') is True)
    selector = kind + ':' + front
    with _pool_instance().connection() as conn:
        with conn.transaction():
            _lock(conn, 'routing.mapping:' + selector)
            for target in filter(None, (cid, backup)):
                current = conn.execute(
                    'SELECT version FROM routing.channels WHERE id=%s FOR UPDATE', (target,),
                ).fetchone()
                if not current:
                    raise ValueError('渠道不存在')
                target_cfg = json.loads(conn.execute(
                    'SELECT config FROM routing.versions WHERE channel=%s AND version=%s',
                    (target, current['version']),
                ).fetchone()['config'])
                if target_cfg.get('_lifecycle', {}).get('deleted'):
                    raise ValueError('回收站渠道不能配置映射')
                if mgr.ADAPTERS[target_cfg['adapter']]['kind'] != kind:
                    raise ValueError('渠道能力已变化，请刷新后重试')
            conn.execute(
                'INSERT INTO routing.mappings(selector,config,actor,updated) VALUES(%s,%s,%s,%s) '
                'ON CONFLICT (selector) DO UPDATE SET config=EXCLUDED.config, actor=EXCLUDED.actor, '
                'updated=EXCLUDED.updated',
                (selector, json.dumps(config), actor, time.time()),
            )
            _audit(conn, 'mapping.save', selector, actor)
    return config


def unmap(actor, body):
    selector = str(body.get('selector') or '')
    with _pool_instance().connection() as conn:
        with conn.transaction():
            _lock(conn, 'routing.mapping:' + selector)
            row = conn.execute(
                'SELECT config FROM routing.mappings WHERE selector=%s', (selector,),
            ).fetchone()
            if not row or json.loads(row['config']) != body.get('expected'):
                raise ValueError('映射已变化，请刷新后重试')
            conn.execute('DELETE FROM routing.mappings WHERE selector=%s', (selector,))
            _audit(conn, 'mapping.delete', selector, actor)
    return {'ok': True}


# --------------------------------------------------------------------------- #
# 接单捕获与验收（读多写少；不改变公开语义）
# --------------------------------------------------------------------------- #

def _legacy_capture(kind, clean, preparation=False):
    """Compatibility route for operations not yet published to the new control plane."""
    mgr = _mgr()
    if preparation and clean.get('parameter_selection'):
        from .channel_parameters import historical, apply
        cfg = historical(clean)
        if mgr.ADAPTERS[cfg['adapter']]['kind'] != kind:
            raise ValueError('参数功能类型不匹配')
        clean, _ = apply(cfg, clean)
        from .channel_runtime import validate_payload
        validate_payload(cfg, clean)
        clean['_channel_binding'] = {'id': cfg['id'], 'version': cfg['version']}
        return clean
    if kind not in {'image', 'xiaole_video'}:
        from .channel_lifecycle import require_legacy
        require_legacy(kind, clean)
        return clean
    front = mgr._front(kind, clean)
    with _pool_instance().connection() as conn:
        row = conn.execute(
            'SELECT config FROM routing.mappings WHERE selector=%s', (kind + ':' + front,),
        ).fetchone()
        if not row or not json.loads(row['config']).get('enabled'):
            if clean.get('parameter_selection'):
                raise ValueError('功能映射已变化，请刷新参数后重新提交')
            from .channel_lifecycle import require_legacy
            require_legacy(kind, clean)
            return clean
        mapping = json.loads(row['config'])
        ch = conn.execute('SELECT * FROM routing.channels WHERE id=%s', (mapping['channel'],)).fetchone()
        if not ch or not ch['enabled']:
            raise ValueError('该功能的主渠道已停用，请管理员切换渠道')
    cfg = version(ch['id'], ch['version'])
    from .channel_parameters import apply
    clean, _ = apply(cfg, clean)
    from .channel_runtime import validate_payload
    validate_payload(cfg, clean)
    clean['_channel_binding'] = {'id': ch['id'], 'version': ch['version'], 'front': front}
    return clean


def capture(kind, payload, preparation=False, invocation_source='web'):
    mgr = _mgr()
    clean = dict(payload)
    clean.pop('_channel_binding', None)  # Never trust a client supplied private snapshot.
    clean.pop('_channel_shadow', None)
    source = str(invocation_source or 'web').strip().lower()
    if source not in {'web', 'agent', 'admin_e2e', 'internal'}:
        source = 'internal'
    operation_id, mapping = mgr.routing_for_payload(kind, clean)
    if not mapping or mapping['state'] == 'legacy':
        return _legacy_capture(kind, clean, preparation)
    if mapping['state'] == 'paused':
        raise ValueError('该功能已由管理员暂停，未执行旧线路降级')
    cid = mapping.get('channel') or ''
    if mapping['state'] == 'shadow':
        try:
            from .function_registry import operation
            cfg = _mapping_channel(cid, operation(operation_id) or mapping['kind'])
            clean['_channel_shadow'] = mgr._seal_shadow({
                'operation_id': operation_id, 'mapping_revision': mapping['revision'],
                'observation_id': uuid.uuid4().hex,
                'id': cfg['id'], 'version': cfg['version'], 'adapter': cfg['adapter'],
                'model': cfg['model'], 'invocation_source': source,
            })
        except ValueError as exc:
            clean['_channel_shadow'] = mgr._seal_shadow({
                'operation_id': operation_id, 'mapping_revision': mapping['revision'],
                'observation_id': uuid.uuid4().hex,
                'id': cid, 'state': 'invalid', 'detail': str(exc)[:120],
                'invocation_source': source,
            })
        return _legacy_capture(kind, clean, preparation)
    from .function_registry import operation
    contract = operation(operation_id) or mapping['kind']
    route_candidates, route_skipped, pricing = [], [], None
    if kind == 'image':
        pricing, route_candidates, route_skipped = mgr._managed_image_route(
            mapping, contract)
        cfg = version(route_candidates[0]['id'], route_candidates[0]['version'])
    else:
        cfg = _mapping_channel(cid, contract, require_enabled=True)
        pricing = cfg
    from .channel_parameters import apply
    clean, _ = apply(pricing, clean, required=not preparation)
    from .channel_runtime import validate_payload
    validate_payload(cfg, clean)
    clean['_channel_binding'] = {
        'operation_id': operation_id, 'mapping_revision': mapping['revision'],
        'id': cfg['id'], 'version': cfg['version'], 'adapter': cfg['adapter'],
        'model': cfg['model'], 'front': mgr._front(kind, clean),
        'invocation_source': source,
    }
    if route_candidates:
        clean['_channel_binding'].update({
            'pricing_id': pricing['id'], 'pricing_version': pricing['version'],
            'route_order': mgr.mapping_channel_ids(mapping),
            'route_candidates': route_candidates, 'route_skipped': route_skipped,
            'route_attempt': 1, 'attempts': [],
        })
    return clean


def _confirm_acceptance(connection, payload):
    mgr = _mgr()
    binding = payload.get('_channel_binding') if isinstance(payload, dict) else None
    if not isinstance(binding, dict) or not binding.get('operation_id'):
        return True
    operation_id = str(binding['operation_id'])
    from .function_registry import operation
    contract = operation(operation_id)
    mapping = operation_mapping(operation_id, connection=connection)
    route_order = [str(item) for item in binding.get('route_order', []) if str(item or '')]
    candidates = binding.get('route_candidates') or []
    if (not mapping or mapping.get('state') != 'managed'
            or int(mapping.get('revision') or 0) != int(binding.get('mapping_revision') or 0)
            or (route_order and mgr.mapping_channel_ids(mapping) != route_order)
            or (not route_order and mapping.get('channel') != binding.get('id'))):
        raise ValueError('功能映射已变化，请刷新后重新提交')
    snapshots = candidates or [mgr._route_candidate(binding)]
    if candidates:
        pricing = _mapping_channel(
            binding.get('pricing_id') or route_order[0], contract, connection=connection)
        if int(pricing['version']) != int(binding.get('pricing_version') or 0):
            raise ValueError('渠道版本已变化，请刷新后重新提交')
    for snapshot in snapshots:
        try:
            cfg = _mapping_channel(
                snapshot.get('id'), contract, require_enabled=True, connection=connection)
        except ValueError:
            raise ValueError('渠道版本已变化，请刷新后重新提交') from None
        if any(cfg.get(key) != snapshot.get(key) for key in mgr.ROUTE_CANDIDATE_FIELDS):
            raise ValueError('渠道版本已变化，请刷新后重新提交')
    return True


@contextmanager
def acceptance_guard(payloads):
    """Keep channel state immutable until the corresponding job commit finishes.

    SQLite 用「持有 BEGIN IMMEDIATE 写锁直到任务提交完成」实现线性化；这里用
    ``SELECT ... FOR UPDATE`` 锁定涉及的渠道行，直到调用方离开上下文后回滚释放，
    同样让任务提交成为受理的线性化点。
    """
    payloads = list(payloads)
    managed = [payload for payload in payloads
               if isinstance(payload, dict)
               and isinstance(payload.get('_channel_binding'), dict)
               and payload['_channel_binding'].get('operation_id')]
    if not managed:
        yield
        return
    conn = _pool_instance().getconn()
    try:
        # Serialize mapping publication with acceptance until the job commits.
        # Sorted acquisition also prevents inverted-order multi-job deadlocks.
        for operation_id in sorted({str(p['_channel_binding']['operation_id']) for p in managed}):
            _lock(conn, 'routing.operation_mapping:' + operation_id)
        for payload in managed:
            _confirm_acceptance(conn, payload)
        yield
    finally:
        try:
            conn.rollback()
        finally:
            _pool_instance().putconn(conn)


def record_shadow(job_id, snapshot):
    """Persist a server-owned shadow observation without making a provider call."""
    mgr = _mgr()
    if (not mgr._valid_shadow(snapshot) or not snapshot.get('operation_id')
            or not re.fullmatch(r'[0-9a-f]{32}', str(snapshot.get('observation_id') or ''))):
        return None
    public_snapshot = {key: snapshot.get(key) for key in (
        'operation_id', 'mapping_revision', 'observation_id', 'id', 'version', 'adapter',
        'model', 'invocation_source', 'state', 'detail',
    ) if snapshot.get(key) not in (None, '')}
    now = time.time()
    rid = public_snapshot['observation_id']
    with _pool_instance().connection() as conn:
        with conn.transaction():
            _lock(conn, 'routing.shadow:' + rid)
            existing = conn.execute(
                "SELECT r.job_id,s.snapshot FROM routing.runs r JOIN routing.run_snapshots s "
                "ON s.run_id=r.id WHERE r.id=%s AND r.kind='shadow'", (rid,),
            ).fetchone()
            if existing:
                if (str(existing['job_id']) != str(job_id)
                        or json.loads(existing['snapshot']) != public_snapshot):
                    raise ValueError('影子观察身份冲突，拒绝覆盖既有证据')
                return rid
            conn.execute(
                'INSERT INTO routing.runs'
                '(id,channel,version,kind,state,started,updated,duration,detail,job_id,provider_id,reservation) '
                'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (rid, str(public_snapshot.get('id') or ''),
                 int(public_snapshot.get('version') or 0), 'shadow', 'captured',
                 now, now, 0, '候选渠道快照（未调用 Provider）', str(job_id), '', 0),
            )
            conn.execute(
                'INSERT INTO routing.run_snapshots'
                '(run_id,operation_id,mapping_revision,invocation_source,snapshot) '
                'VALUES(%s,%s,%s,%s,%s)',
                (rid, public_snapshot['operation_id'], public_snapshot.get('mapping_revision'),
                 public_snapshot.get('invocation_source', 'web'),
                 json.dumps(public_snapshot, ensure_ascii=False)),
            )
    return rid


# --------------------------------------------------------------------------- #
# 生命周期（渠道启停/回收站、内置供应商启停）
# --------------------------------------------------------------------------- #

def rollback(actor, body):
    old = version(str(body.get('id')), int(body.get('target_version')))
    secret = version(old['id'], old['version'], True)['secret']
    current = version(old['id'])
    return save(actor, dict(old, version=current['version'], secret=secret,
                            enabled=body.get('enabled') is True))


def mutate(actor, body):
    """渠道启用/停用/移入回收站/恢复；与 channel_lifecycle.mutate 逐步对应。"""
    mgr = _mgr()
    cid = str(body.get('id') or '')
    action = body.get('action')
    reason = str(body.get('reason') or '').strip()
    if action not in {'enable', 'disable', 'delete', 'restore'} or not 2 <= len(reason) <= 200:
        raise ValueError('请选择有效操作并填写 2～200 字原因')
    from .channel_lifecycle import active_jobs
    with _pool_instance().connection() as conn:
        with conn.transaction():
            row = conn.execute(
                'SELECT * FROM routing.channels WHERE id=%s FOR UPDATE', (cid,),
            ).fetchone()
            if not row:
                raise ValueError('渠道不存在')
            if body.get('version') != row['version']:
                raise ValueError('渠道已被修改，请刷新后重新确认')
            previous = conn.execute(
                'SELECT config,secret FROM routing.versions WHERE channel=%s AND version=%s',
                (cid, row['version']),
            ).fetchone()
            config = json.loads(previous['config'])
            deleted = bool(config.get('_lifecycle', {}).get('deleted'))
            if deleted and action != 'restore':
                raise ValueError('渠道已在回收站，请先恢复')
            if not deleted and action == 'restore':
                raise ValueError('渠道不在回收站')
            mappings = [json.loads(r['config']) for r in conn.execute(
                'SELECT config FROM routing.mappings').fetchall()]
            mappings.extend(json.loads(r['config']) for r in conn.execute(
                "SELECT config FROM routing.operation_mappings WHERE state IN ('shadow','managed')")
                .fetchall())
            references = [m for m in mappings if cid in mgr.mapping_channel_ids(m)]
            if action == 'delete':
                if row['enabled']:
                    raise ValueError('请先停用渠道，再移入回收站')
                if references:
                    raise ValueError('渠道仍被渠道优先级映射引用，请先切换或删除关联映射')
                if conn.execute(
                        "SELECT 1 FROM routing.runs WHERE channel=%s AND state IN "
                        "('queued','running','unknown') LIMIT 1", (cid,)).fetchone():
                    raise ValueError('存在执行中或结果未知的调用，请先核对，暂不可删除')
                if active_jobs(cid):
                    raise ValueError('渠道仍有未结束任务，暂不可删除')
            if action == 'enable' and not all(config.get(k) for k in ('adapter', 'base_url', 'model')):
                raise ValueError('渠道配置不完整，不能启用')
            enabled = action == 'enable'
            revision = int(row['version']) + 1
            config['_lifecycle'] = {'deleted': action == 'delete', 'action': action, 'reason': reason,
                                    'actor': actor, 'at': time.time(), 'previous_version': row['version']}
            conn.execute(
                'INSERT INTO routing.versions(channel,version,config,secret,actor,created) '
                'VALUES(%s,%s,%s,%s,%s,%s)',
                (cid, revision, json.dumps(config, ensure_ascii=False), previous['secret'],
                 actor, time.time()),
            )
            conn.execute('UPDATE routing.channels SET version=%s,enabled=%s WHERE id=%s',
                         (revision, bool(enabled), cid))
            if enabled:
                conn.execute(
                    'INSERT INTO routing.schedule(channel,light_due,full_due) VALUES(%s,%s,%s) '
                    'ON CONFLICT (channel) DO UPDATE SET light_due=EXCLUDED.light_due, '
                    'full_due=EXCLUDED.full_due',
                    (cid, time.time() + 60, mgr._next_daily(config.get('daily_hour', 9))),
                )
            else:
                conn.execute('DELETE FROM routing.schedule WHERE channel=%s', (cid,))
            _audit(conn, 'channel.' + action, cid + ' · v' + str(revision) + ' · ' + reason, actor)
    return {'id': cid, 'version': revision, 'enabled': enabled, 'deleted': action == 'delete'}


def legacy_states(connection=None):
    """内置供应商启停状态（settings id=2 的单行 JSON）。"""
    if connection is None:
        with _pool_instance().connection() as conn:
            return legacy_states(conn)
    row = connection.execute('SELECT value FROM routing.settings WHERE id=2').fetchone()
    return json.loads(row['value']) if row else {}


def mutate_legacy(actor, body):
    from .channel_lifecycle import LEGACY_SCOPES
    key = str(body.get('id') or '')
    action = body.get('action')
    reason = str(body.get('reason') or '').strip()
    if key not in LEGACY_SCOPES or action not in {'enable', 'disable'}:
        raise ValueError('内置供应商有固定功能引用；仅已接入的任务线路支持启停，不支持删除')
    if not 2 <= len(reason) <= 200:
        raise ValueError('请填写 2～200 字原因')
    with _pool_instance().connection() as conn:
        with conn.transaction():
            # settings 的行可能尚不存在，行锁无效；用咨询锁串行化「读改写」。
            _lock(conn, 'routing.settings:2')
            states = legacy_states(conn)
            old = states.get(key, {'revision': 0, 'enabled': True})
            if body.get('version') != old['revision']:
                raise ValueError('供应商状态已变化，请刷新后重新确认')
            current = {'revision': old['revision'] + 1, 'enabled': action == 'enable', 'actor': actor,
                       'reason': reason, 'at': time.time(), 'scope': LEGACY_SCOPES[key]}
            states[key] = current
            conn.execute(
                'INSERT INTO routing.settings(id,value) VALUES(2,%s) '
                'ON CONFLICT (id) DO UPDATE SET value=EXCLUDED.value',
                (json.dumps(states, ensure_ascii=False),),
            )
            _audit(conn, 'legacy.' + action, key + ' · v' + str(current['revision']) + ' · ' + reason, actor)
    return current


# --------------------------------------------------------------------------- #
# 后台总览与调度
# --------------------------------------------------------------------------- #

def overview():
    mgr = _mgr()
    from .channel_lifecycle import legacy_states as _legacy_states, LEGACY_SCOPES
    now = time.time()
    with _pool_instance().connection() as conn:
        channels = [dict(r) for r in conn.execute('SELECT * FROM routing.channels').fetchall()]
        mappings = [json.loads(r['config']) for r in conn.execute('SELECT config FROM routing.mappings').fetchall()]
        operation_mappings = [_fetch_operation_mapping(conn, r['operation_id']) for r in conn.execute(
            'SELECT operation_id FROM routing.operation_mappings ORDER BY operation_id').fetchall()]
        for mapping in operation_mappings:
            mapping['history'] = [dict(row) for row in conn.execute(
                'SELECT revision,state,actor,created FROM routing.operation_mapping_versions '
                'WHERE operation_id=%s ORDER BY revision DESC LIMIT 20',
                (mapping['operation_id'],)).fetchall()]
        runs = [dict(r) for r in conn.execute(
            'SELECT r.*,s.operation_id,s.mapping_revision,s.invocation_source,s.snapshot '
            'FROM routing.runs r LEFT JOIN routing.run_snapshots s ON s.run_id=r.id '
            'ORDER BY r.started DESC LIMIT 100').fetchall()]
        for run in runs:
            raw_snapshot = run.pop('snapshot', None)
            if raw_snapshot:
                run['execution_snapshot'] = json.loads(raw_snapshot)
        events = [dict(r) for r in conn.execute(
            'SELECT * FROM routing.events ORDER BY created DESC LIMIT 30').fetchall()]
        for channel in channels:
            cfg = _fetch_version(conn, channel['id'], channel['version'])
            channel.update(cfg)
            channel['configured'] = True
            channel['fixture'] = {k: v for k, v in cfg['fixture'].items() if k != 'reference_images'}
            channel['material_count'] = len(cfg['fixture'].get('reference_images') or [])
            channel['history'] = [dict(r) for r in conn.execute(
                'SELECT version,actor,created FROM routing.versions WHERE channel=%s '
                'ORDER BY version DESC LIMIT 20', (channel['id'],)).fetchall()]
            schedule_row = conn.execute(
                'SELECT light_due,full_due FROM routing.schedule WHERE channel=%s',
                (channel['id'],)).fetchone()
            channel['schedule'] = dict(schedule_row) if schedule_row else {}
            latest = conn.execute(_LATEST_KIND, (channel['id'], channel['version'], 'full')).fetchone()
            channel['health'] = ('未验证' if not latest else '验证已过期' if now - latest['updated'] > 86400
                                 else {'passed': '成品核验通过', 'failed': '异常', 'unknown': '结果未知',
                                       'running': '检测中', 'queued': '待检测'}.get(latest['state'], '未验证'))
            problem = conn.execute(
                "SELECT state FROM routing.runs WHERE channel=%s AND version=%s AND "
                "state IN ('failed','unknown') AND updated>%s ORDER BY updated DESC LIMIT 1",
                (channel['id'], channel['version'],
                 max(now - 86400, latest['updated'] if latest else 0))).fetchone()
            if problem:
                channel['health'] = '异常' if problem['state'] == 'failed' else '结果未知'
            # 与 SQLite 的 SUM(state='failed') 口径一致：零行时仍是 NULL（不是 0）。
            stats = conn.execute(
                "SELECT COUNT(*) AS total,"
                "SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed,"
                "SUM(CASE WHEN state='unknown' THEN 1 ELSE 0 END) AS unknown,"
                "AVG(CASE WHEN state IN ('passed','failed','unknown') THEN duration END) AS avg_duration "
                "FROM routing.runs WHERE channel=%s AND kind='task' AND started>%s",
                (channel['id'], now - 86400)).fetchone()
            channel['stats'] = dict(stats)
            channel['checks'] = []
            for check_kind in ('connection', 'auth', 'full'):
                check_row = conn.execute(
                    _LATEST_CHECK, (channel['id'], channel['version'], check_kind)).fetchone()
                if check_row:
                    channel['checks'].append(dict(check_row))
    from .function_registry import operation_catalog
    operations = operation_catalog(channel_eligible=True)
    # 与 SQLite 路径一致：全量目录（含不可切换的）用于后台展示不可切换原因。
    all_operations = operation_catalog()
    current = {item['operation_id']: item for item in operation_mappings}
    for item in operations:
        item['mapping'] = current.get(item['operation_id'])
    return {'items': channels, 'mappings': mappings, 'operation_mappings': operation_mappings,
            'operations': operations, 'all_operations': all_operations,
            'runs': runs, 'events': events, 'adapters': mgr.ADAPTERS,
            'legacy_controls': _legacy_states(), 'legacy_scopes': LEGACY_SCOPES,
            'notifications': notification_settings(), 'timezone': 'Asia/Shanghai',
            'stats_window': '最近24小时'}


def reserve(cid, kind, job_id='', snapshot=None, execution_snapshot=None):
    mgr = _mgr()
    now, rid = time.time(), uuid.uuid4().hex
    with _pool_instance().connection() as conn:
        with conn.transaction():
            # 队列长度与预算都是「先数后写」，SQLite 靠 BEGIN IMMEDIATE 串行化；
            # 这里用同一把渠道级咨询锁保证同样的准入判定。
            _lock(conn, 'routing.reserve:' + str(cid))
            cfg = snapshot or _fetch_version(conn, cid, None)
            if kind != 'task':
                current = conn.execute(
                    'SELECT ch.enabled,v.config FROM routing.channels ch JOIN routing.versions v '
                    'ON v.channel=ch.id AND v.version=ch.version WHERE ch.id=%s', (cid,)).fetchone()
                if not current or not current['enabled'] or json.loads(current['config']).get(
                        '_lifecycle', {}).get('deleted'):
                    raise ValueError('渠道已停用或在回收站，不能发起新测试')
            if kind == 'full':
                start = int((now + 8 * 3600) // 86400) * 86400 - 8 * 3600
                used = conn.execute(
                    "SELECT COUNT(*) AS n,COALESCE(SUM(reservation),0) AS cost FROM routing.runs "
                    "WHERE channel=%s AND kind='full' AND started>=%s", (cid, start)).fetchone()
                if (not cfg['test_cost'] or used['n'] >= cfg['daily_limit']
                        or used['cost'] + cfg['test_cost'] > cfg['daily_budget']):
                    raise ValueError('完整测试预算或次数不足，请先配置；失败和未知结果同样占用预算')
            if kind == 'task':
                existing = conn.execute(
                    "SELECT id,channel,version,state FROM routing.runs WHERE job_id=%s AND kind='task' "
                    "ORDER BY started DESC LIMIT 1", (str(job_id),)).fetchone()
                if existing:
                    if (existing['state'] == 'queued' and existing['channel'] == cid
                            and int(existing['version']) == int(cfg['version'])):
                        return existing['id']
                    raise ValueError('该任务已有渠道执行记录，禁止重复提交，请按工单核查')
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM routing.runs WHERE channel=%s AND state='queued'",
                (cid,)).fetchone()['n']
            if pending >= max(1, cfg['queue_limit']):
                raise ValueError('渠道等待队列已满')
            conn.execute(
                'INSERT INTO routing.runs'
                '(id,channel,version,kind,state,started,updated,duration,detail,job_id,provider_id,reservation) '
                'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (rid, cid, cfg['version'], kind, 'queued', now, now, None, '等待执行',
                 str(job_id), '', cfg['test_cost'] if kind == 'full' else 0),
            )
            binding = execution_snapshot if isinstance(execution_snapshot, dict) else {}
            if binding:
                public_snapshot = mgr.public_execution_snapshot(binding)
                conn.execute(
                    'INSERT INTO routing.run_snapshots'
                    '(run_id,operation_id,mapping_revision,invocation_source,snapshot) '
                    'VALUES(%s,%s,%s,%s,%s)',
                    (rid, public_snapshot.get('operation_id', ''),
                     public_snapshot.get('mapping_revision'),
                     public_snapshot.get('invocation_source', 'web'),
                     json.dumps(public_snapshot, ensure_ascii=False)),
                )
    return rid


def finish(rid, state, detail, provider_id=''):
    with _pool_instance().connection() as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE routing.runs SET state=%s,detail=%s,updated=%s,duration=%s-started,"
                "provider_id=CASE WHEN %s!='' THEN %s ELSE provider_id END WHERE id=%s",
                (state, detail[:300], time.time(), time.time(), provider_id, provider_id, rid),
            )


def finish_task_failover_safe(rid, detail):
    now = time.time()
    with _pool_instance().connection() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT r.kind,r.state,r.provider_id,s.snapshot FROM routing.runs r "
                "JOIN routing.run_snapshots s ON s.run_id=r.id WHERE r.id=%s FOR UPDATE",
                (rid,),
            ).fetchone()
            if (not row or row['kind'] != 'task'
                    or row['state'] not in {'queued', 'running'} or row['provider_id']):
                # queued 也在允许范围内：并发/限流排队超时的任务从未提交供应商，同样是「未受理」。
                raise ValueError('当前任务状态不允许自动切换渠道')
            snapshot = json.loads(row['snapshot'])
            snapshot['failover_safe'] = True
            snapshot['failure_reason'] = str(detail or '')[:300]
            conn.execute(
                "UPDATE routing.runs SET state='failed',detail=%s,updated=%s,"
                "duration=%s-started WHERE id=%s",
                (str(detail or '')[:300], now, now, rid),
            )
            conn.execute(
                'UPDATE routing.run_snapshots SET snapshot=%s WHERE run_id=%s',
                (json.dumps(snapshot, ensure_ascii=False), rid),
            )


def prepare_task_failover(rid, candidate, reason=''):
    mgr = _mgr()
    candidate = mgr._route_candidate(candidate if isinstance(candidate, dict) else {})
    if not candidate.get('id') or not candidate.get('version'):
        raise ValueError('候选渠道快照无效')
    now = time.time()
    with _pool_instance().connection() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT r.*,s.snapshot FROM routing.runs r "
                "JOIN routing.run_snapshots s ON s.run_id=r.id WHERE r.id=%s FOR UPDATE",
                (rid,),
            ).fetchone()
            if (not row or row['kind'] != 'task' or row['state'] != 'failed'
                    or row['provider_id']):
                raise ValueError('当前任务状态不允许自动切换渠道')
            snapshot = json.loads(row['snapshot'])
            if not snapshot.get('failover_safe'):
                raise ValueError('上一渠道结果未确认，禁止自动切换')
            expected = next((item for item in snapshot.get('route_candidates', [])
                             if item.get('id') == candidate['id']), None)
            if not expected or any(expected.get(key) != candidate.get(key)
                                   for key in mgr.ROUTE_CANDIDATE_FIELDS):
                raise ValueError('候选渠道不在任务受理快照中')
            cfg = _fetch_version(conn, candidate['id'], candidate['version'],
                                 missing_revision_message='候选渠道版本不存在')
            _ready_candidate(candidate['id'], connection=conn)   # 采集后被停用/回收/证据过期的候选不再接单
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM routing.runs WHERE channel=%s "
                "AND state='queued' AND id!=%s", (candidate['id'], rid),
            ).fetchone()['n']
            if pending >= max(1, cfg['queue_limit']):
                raise ValueError('候选渠道等待队列已满')
            attempts = list(snapshot.get('attempts') or [])
            attempts.append({
                'attempt': int(snapshot.get('route_attempt') or len(attempts) + 1),
                'channel': row['channel'], 'version': row['version'],
                'state': 'failed', 'detail': str(row['detail'] or '')[:300],
            })
            snapshot.update(candidate)
            snapshot['route_attempt'] = len(attempts) + 1
            snapshot['switch_reason'] = str(
                reason or snapshot.get('failure_reason') or '')[:300]
            snapshot['attempts'] = attempts[:mgr.MAX_MAPPING_CHANNELS]
            snapshot.pop('failover_safe', None)
            snapshot.pop('failure_reason', None)
            conn.execute(
                "UPDATE routing.runs SET channel=%s,version=%s,state='queued',detail=%s,"
                "updated=%s,duration=NULL,provider_id='' WHERE id=%s",
                (candidate['id'], candidate['version'], '安全切换到下一渠道', now, rid),
            )
            conn.execute(
                'UPDATE routing.run_snapshots SET snapshot=%s WHERE run_id=%s',
                (json.dumps(snapshot, ensure_ascii=False), rid),
            )
    return snapshot


# --------------------------------------------------------------------------- #
# 任务证据与恢复态（读不到证据绝不等于失败）
# --------------------------------------------------------------------------- #

def task_evidence(job_id):
    with _pool_instance().connection() as conn:
        row = conn.execute(
            "SELECT r.id,r.kind,r.channel,r.version,r.state,r.provider_id,"
            "s.operation_id,s.mapping_revision,"
            "s.invocation_source,s.snapshot FROM routing.runs r "
            "LEFT JOIN routing.run_snapshots s ON s.run_id=r.id "
            "WHERE r.kind IN ('task','shadow') AND r.job_id=%s "
            "ORDER BY CASE WHEN r.kind='task' THEN 0 ELSE 1 END,r.started DESC LIMIT 1",
            (str(job_id),),
        ).fetchone()
    if not row:
        return {}
    result = dict(row)
    if result.get('snapshot'):
        result['execution_snapshot'] = json.loads(result.pop('snapshot'))
    else:
        result.pop('snapshot', None)
    return result


def task_recovery_state(job_id):
    try:
        evidence = task_evidence(job_id)
        return evidence.get('state') or 'absent'
    except _storage_errors():
        return 'unavailable'


def mark_interrupted_task_unknown(job_id, detail):
    try:
        with _pool_instance().connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT id,state FROM routing.runs WHERE kind='task' AND job_id=%s "
                    "ORDER BY started DESC LIMIT 1", (str(job_id),)).fetchone()
                if not row:
                    return 'absent'
                state = row['state']
                if state == 'running':
                    now = time.time()
                    changed = conn.execute(
                        "UPDATE routing.runs SET state='unknown',detail=%s,updated=%s,"
                        "duration=%s-started WHERE id=%s AND state='running'",
                        (str(detail or 'worker interrupted')[:300], now, now, row['id']),
                    ).rowcount
                    if changed:
                        state = 'unknown'
                return state
    except _storage_errors():
        return 'unavailable'


def search_task_ids(query):
    needle = '%' + str(query or '').lower() + '%'
    if needle == '%%':
        return set()
    try:
        with _pool_instance().connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT r.job_id FROM routing.runs r LEFT JOIN routing.versions v "
                "ON v.channel=r.channel AND v.version=r.version "
                "LEFT JOIN routing.run_snapshots s ON s.run_id=r.id "
                "WHERE r.kind IN ('task','shadow') AND (LOWER(r.provider_id) LIKE %s "
                "OR LOWER(r.channel) LIKE %s OR LOWER(CAST(r.version AS TEXT)) LIKE %s "
                "OR LOWER(COALESCE(v.config,'')) LIKE %s OR LOWER(COALESCE(s.operation_id,'')) LIKE %s "
                "OR LOWER(COALESCE(CAST(s.mapping_revision AS TEXT),'')) LIKE %s)",
                (needle, needle, needle, needle, needle, needle),
            ).fetchall()
        return {str(row['job_id']) for row in rows if row['job_id'] not in (None, '')}
    except _storage_errors():
        return set()


# --------------------------------------------------------------------------- #
# 通知设置（settings id=1）
# --------------------------------------------------------------------------- #

def _channel_delivery_counts():
    """渠道告警投递状态计数；过滤口径与 SQLite 语句一致（event_id LIKE 'channel.%'）。

    投递队列属 ops 域（runtime_observability）：该域已切换时读同一 PostgreSQL，
    否则读它当前的 SQLite 权威（本模块不 import sqlite3，走它的连接入口）。
    """
    from . import observability_store
    if observability_store.enabled():
        with _pool_instance().connection() as conn:
            rows = conn.execute(
                "SELECT state,COUNT(*) AS n FROM ops.alert_outbox WHERE event_id LIKE %s "
                "GROUP BY state", ('channel.%',),
            ).fetchall()
        return {row['state']: row['n'] for row in rows}
    from . import runtime_observability
    with closing(runtime_observability.database()) as c:
        return {row['state']: row['n'] for row in c.execute(
            "SELECT state,COUNT(*) n FROM alert_outbox WHERE event_id LIKE 'channel.%' GROUP BY state")}


def notification_settings(private=False):
    mgr = _mgr()
    with _pool_instance().connection() as conn:
        row = conn.execute('SELECT value FROM routing.settings WHERE id=1').fetchone()
    value = json.loads(row['value']) if row else {'enabled': False}
    if value.get('endpoint'):
        value['endpoint'] = mgr._crypt(value['endpoint'], True) if private else '已配置（隐藏）'
    value['delivery'] = _channel_delivery_counts()
    return value


def save_notifications(actor, body):
    mgr = _mgr()
    from .runtime_observability import valid_endpoint
    old = notification_settings(True)
    endpoint = body.get('endpoint') or old.get('endpoint', '')
    if endpoint and not valid_endpoint(endpoint):
        raise ValueError('通知地址必须为 HTTPS 或本机 HTTP')
    if body.get('enabled') and not endpoint:
        raise ValueError('请填写通知接收地址')
    value = json.dumps({'enabled': body.get('enabled') is True,
                        'endpoint': mgr._crypt(endpoint) if endpoint else ''})
    with _pool_instance().connection() as conn:
        with conn.transaction():
            conn.execute(
                'INSERT INTO routing.settings(id,value) VALUES(1,%s) '
                'ON CONFLICT (id) DO UPDATE SET value=EXCLUDED.value', (value,))
            _audit(conn, 'notification.save', 'webhook', actor)
    return notification_settings()


# --------------------------------------------------------------------------- #
# 执行器与调度器（channel_runtime 的行级读写）
#
# 这一组函数是 channel_runtime 里原先直连 `channel_manager.db()` 那几处事务的
# PostgreSQL 对等实现：同一个「读——判定——写」过程放进一个事务，锁语义与 SQLite 的
# `BEGIN IMMEDIATE`（整库单写者）对应——派发闸门与调度排期用全局咨询锁，
# 渠道事件用渠道级咨询锁，锁随提交/回滚释放。
# --------------------------------------------------------------------------- #

def run_record(rid):
    """按编号读一行 routing.runs（执行器入口）；行不存在返回 None。

    SQLite 路径 `dict(c.execute('SELECT * FROM runs WHERE id=?').fetchone())` 缺行时抛
    TypeError；这里返回 None，调用方随即取列同样抛 TypeError（文案不同，runbook 已记录），
    不会静默当作「没有这条运行」继续执行。
    """
    with _pool_instance().connection() as conn:
        row = conn.execute('SELECT * FROM routing.runs WHERE id=%s', (str(rid),)).fetchone()
    return dict(row) if row else None


def try_start_run(rid, cfg, stale_before):
    """执行闸门：清理陈旧 running、按并发与限流判定，通过则把本 run 置为 running。

    与 SQLite 路径的 `BEGIN IMMEDIATE` 事务逐步对应（同样的三步统计与三态返回）：

    * ``True``  —— 已获准并置 running，同时写一行 `runtime.dispatch` 限流流水；
    * ``False`` —— 本 run 已不在 queued（被终止或已被别的执行器认领），调用方应结束；
    * ``None``  —— 并发或限流未放行，调用方继续等待（判定结果不落库，与 SQLite 一致）。

    ``stale_before`` 是「执行进程中断」的判定线（调用方传 `time.time()-2400`）。
    行不存在时按「不再是 queued」处理（SQLite 在此会抛 TypeError），方向更安全。
    """
    now = time.time()
    with _pool_instance().connection() as conn:
        with conn.transaction():
            # SQLite 的 BEGIN IMMEDIATE 会串行化全部准入判定；这里用同一把全局咨询锁。
            _lock(conn, 'routing.runtime.dispatch')
            conn.execute(
                "UPDATE routing.runs SET state='unknown',detail=%s "
                "WHERE state='running' AND updated<%s",
                ('执行进程中断或超时，需人工核查', stale_before),
            )
            active = conn.execute(
                "SELECT COUNT(*) AS n FROM routing.runs WHERE channel=%s AND state='running'",
                (str(cfg['id']),),
            ).fetchone()['n']
            rate = conn.execute(
                "SELECT COUNT(*) AS n FROM routing.events WHERE target=%s "
                "AND action='runtime.dispatch' AND created>%s",
                (str(cfg['id']), now - 60),
            ).fetchone()['n']
            own = conn.execute('SELECT state FROM routing.runs WHERE id=%s', (str(rid),)).fetchone()
            if not own or own['state'] != 'queued':
                return False
            if active < cfg['concurrency'] and rate < cfg['rpm']:
                conn.execute("UPDATE routing.runs SET state='running',updated=%s WHERE id=%s",
                             (now, str(rid)))
                _audit(conn, 'runtime.dispatch', str(cfg['id']), 'runtime')
                return True
            return None


def execution_phase(rid):
    """读当前执行阶段说明（错误分支拼接阶段前缀用）；行不存在返回 None。"""
    with _pool_instance().connection() as conn:
        row = conn.execute('SELECT detail FROM routing.runs WHERE id=%s', (str(rid),)).fetchone()
    return row['detail'] if row else None


def mark_terminated(rid, detail):
    """管理员终止的独立终态；返回是否真的改动了行（等价 SQLite 的 rowcount>0）。

    与 SQLite 路径同口径：passed/failed/terminated 已是终态时不覆盖。
    """
    with _pool_instance().connection() as conn:
        with conn.transaction():
            changed = conn.execute(
                "UPDATE routing.runs SET state='terminated',detail=%s,updated=%s "
                "WHERE id=%s AND state NOT IN ('passed','failed','terminated')",
                (detail, time.time(), str(rid)),
            ).rowcount
    return changed > 0


def note_incident(channel, kind, state):
    """渠道事件去重入账；返回 ``(action, occurred)``，与 SQLite 路径逐步对应。

    同一 (渠道,检查类型) 的状态没变就只读不写，直接返回上次的 action/occurred
    （SQLite 的 `INSERT OR REPLACE` 在状态未变时本就不会被调用）。
    """
    with _pool_instance().connection() as conn:
        with conn.transaction():
            _lock(conn, 'routing.incident:' + str(channel) + ':' + str(kind))
            previous = conn.execute(
                'SELECT * FROM routing.channel_incidents WHERE channel=%s AND kind=%s',
                (str(channel), str(kind)),
            ).fetchone()
            if previous and previous['state'] == state:
                return previous['action'], previous['occurred']
            action = ('channel.recovered' if previous else '') if state == 'passed' else 'channel.' + state
            occurred = time.time()
            conn.execute(
                'INSERT INTO routing.channel_incidents(channel,kind,state,action,occurred) '
                'VALUES(%s,%s,%s,%s,%s) '
                'ON CONFLICT (channel,kind) DO UPDATE SET state=EXCLUDED.state,'
                'action=EXCLUDED.action,occurred=EXCLUDED.occurred',
                (str(channel), str(kind), state, action, occurred),
            )
            return action, occurred


def pending_incidents():
    """已入账但可能还没投递的渠道事件（action 非空），供调度器重投。

    SQLite 路径不排序（次序未定义）；这里按 (渠道,类型) 定序，内容与条数不变。
    """
    with _pool_instance().connection() as conn:
        rows = conn.execute(
            "SELECT channel,action,occurred FROM routing.channel_incidents "
            "WHERE action!='' ORDER BY channel,kind",
        ).fetchall()
    return [dict(row) for row in rows]


def poll_schedule(now):
    """调度器取「到期的自检」并推进下次到点时间；返回 ``[(channel, kind)]``。

    与 SQLite 路径的 `BEGIN IMMEDIATE` + 逐行处理逐步对应：只取已启用渠道，
    connection 看 `cfg['monitor']` 与 light_due，full 看 `cfg['daily_test']` 与 full_due；
    取到就把该列推进（connection=`now+poll_seconds`，full=当日 daily_hour）。
    渠道版本在同一事务内读（SQLite 用独立连接），只影响并发改渠道那一瞬的可见性。
    """
    mgr = _mgr()
    due = []
    with _pool_instance().connection() as conn:
        with conn.transaction():
            _lock(conn, 'routing.schedule')
            rows = conn.execute(
                'SELECT s.channel,s.light_due,s.full_due,ch.version FROM routing.schedule s '
                'JOIN routing.channels ch ON ch.id=s.channel WHERE ch.enabled ORDER BY s.channel',
            ).fetchall()
            for row in rows:
                cfg = _fetch_version(conn, row['channel'], row['version'])
                for kind, column, enabled in [('connection', 'light_due', cfg['monitor']),
                                              ('full', 'full_due', cfg['daily_test'])]:
                    if enabled and row[column] <= now:
                        due.append((row['channel'], kind))
                        next_at = (now + cfg['poll_seconds'] if kind == 'connection'
                                   else mgr._next_daily(cfg['daily_hour'], now))
                        conn.execute(
                            'UPDATE routing.schedule SET ' + column + '=%s WHERE channel=%s',
                            (next_at, row['channel']),
                        )
    return due


def queued_test_run_ids(limit=8):
    """重启后可以续跑的排队自检（kind 非 task），按入队时间取前 N 条。

    SQLite 是 `ORDER BY started LIMIT 8`，同一时间戳的次序本来未定义；这里补 `,id`
    作确定次序（runbook 已记录）。
    """
    with _pool_instance().connection() as conn:
        rows = conn.execute(
            "SELECT id FROM routing.runs WHERE state='queued' AND kind!='task' "
            "ORDER BY started,id LIMIT %s", (int(limit),),
        ).fetchall()
    return [row['id'] for row in rows]


def record_audit(action, target, actor):
    """单独写一条 routing.events 审计（start_test、scheduler.blocked 这些零散审计点）。"""
    with _pool_instance().connection() as conn:
        with conn.transaction():
            _audit(conn, action, target, actor)


# --------------------------------------------------------------------------- #
# 参数发布与工作台布局（channel_parameters 的设置行与版本发布）
# --------------------------------------------------------------------------- #

def _setting_json(conn, setting_id):
    """在给定连接上读单行设置（settings.value）；行不存在返回 {}。"""
    row = conn.execute('SELECT value FROM routing.settings WHERE id=%s',
                       (int(setting_id),)).fetchone()
    return json.loads(row['value']) if row else {}


def _save_setting(conn, setting_id, value, ensure_ascii=True):
    """单行设置写入口；`INSERT OR REPLACE` → `ON CONFLICT (id) DO UPDATE`。"""
    serialized = value if isinstance(value, str) else json.dumps(value, ensure_ascii=ensure_ascii)
    conn.execute(
        'INSERT INTO routing.settings(id,value) VALUES(%s,%s) '
        'ON CONFLICT (id) DO UPDATE SET value=EXCLUDED.value',
        (int(setting_id), serialized),
    )


def layout_setting():
    """工作台布局（settings id=4）；行不存在返回 {}（与 SQLite 路径一致）。"""
    with _pool_instance().connection() as conn:
        return _setting_json(conn, 4)


def draft_setting():
    """参数草稿（settings id=3）；行不存在返回 {}。"""
    with _pool_instance().connection() as conn:
        return _setting_json(conn, 3)


def save_layout(actor, value):
    """保存工作台布局并记审计（`layout.save`），与 SQLite 路径同一事务内完成。"""
    with _pool_instance().connection() as conn:
        with conn.transaction():
            _lock(conn, 'routing.settings:4')
            _save_setting(conn, 4, value, ensure_ascii=False)
            _audit(conn, 'layout.save', 'workbench', actor)


def mapping_by_selector(selector):
    """按 selector 读旧线路映射配置；不存在返回 {}（对应 SQLite 的 `json.loads(r[0]) if r else {}`）。"""
    with _pool_instance().connection() as conn:
        row = conn.execute('SELECT config FROM routing.mappings WHERE selector=%s',
                           (str(selector),)).fetchone()
    return json.loads(row['config']) if row else {}


def published_channel_configs():
    """已启用旧线路映射 → 对应渠道当前版本的配置；供参数目录展示。

    返回 ``[(mapping_dict, version, config_dict)]``。与 SQLite 路径逐条对应：映射
    enabled 且渠道 enabled 才算；渠道行缺失（不可能，无外键）或渠道停用时跳过。
    SQLite 路径不排序，这里按 selector 定序，内容不变。
    """
    result = []
    with _pool_instance().connection() as conn:
        for row in conn.execute('SELECT config FROM routing.mappings ORDER BY selector').fetchall():
            mapping = json.loads(row['config'])
            if not mapping.get('enabled'):
                continue
            current = conn.execute('SELECT version,enabled FROM routing.channels WHERE id=%s',
                                   (mapping['channel'],)).fetchone()
            if not current or not current['enabled']:
                continue
            version_row = conn.execute(
                'SELECT config FROM routing.versions WHERE channel=%s AND version=%s',
                (mapping['channel'], current['version']),
            ).fetchone()
            result.append((mapping, current['version'], json.loads(version_row['config'])))
    return result


def channel_versions(channel):
    """某渠道的全部版本记录（version/config/actor/created），按版本号倒序。

    只用于后台参数页的历史列表；`config` 里不含密钥列（secret 单独一列，不在本查询里）。
    """
    with _pool_instance().connection() as conn:
        rows = conn.execute(
            'SELECT version,config,actor,created FROM routing.versions WHERE channel=%s '
            'ORDER BY version DESC', (str(channel),),
        ).fetchall()
    return [dict(row) for row in rows]


def change_parameters(actor, body):
    """参数草稿 / 发布 / 回滚（channel_parameters.change 的写路径）。

    与 SQLite 路径的 `BEGIN IMMEDIATE` 事务逐步对应：读渠道当前版本（行锁）→ 校验
    草稿版本与生命周期 → 起草/发布/回滚 → 写回 settings id=3 → 审计。错误文案与
    SQLite 路径逐字相同；发布与回滚新增一版 routing.versions 并推进 channels.version。
    """
    from .channel_parameters import validate
    cid = str(body.get('id') or '')
    action = body.get('action')
    with _pool_instance().connection() as conn:
        with conn.transaction():
            _lock(conn, 'routing.parameters:' + cid)
            row = conn.execute(
                'SELECT ch.version,v.config,v.secret FROM routing.channels ch '
                'JOIN routing.versions v ON v.channel=ch.id AND v.version=ch.version '
                'WHERE ch.id=%s FOR UPDATE OF ch', (cid,),
            ).fetchone()
            if not row or row['version'] != body.get('version'):
                raise ValueError('渠道版本已变化，请刷新后重试')
            cfg = json.loads(row['config'])
            if cfg.get('_lifecycle', {}).get('deleted'):
                raise ValueError('请先恢复回收站渠道')
            all_drafts = _setting_json(conn, 3)
            old = all_drafts.get(cid, {})
            if body.get('draft_revision', 0) != old.get('revision', 0):
                raise ValueError('草稿已被其他管理员修改，请刷新')
            if action == 'rollback':
                previous = conn.execute(
                    'SELECT config FROM routing.versions WHERE channel=%s AND version=%s',
                    (cid, body.get('target_version')),
                ).fetchone()
                if not previous:
                    raise ValueError('历史版本不存在')
                spec = json.loads(previous['config']).get('parameters')
            elif action == 'publish':
                if old.get('base_version') != row['version']:
                    raise ValueError('请重新保存草稿后发布')
                spec = old.get('parameters')
            else:
                spec = body.get('parameters')
            spec = validate(cfg, spec)
            if action == 'draft':
                all_drafts[cid] = dict(revision=old.get('revision', 0) + 1,
                                       base_version=row['version'], parameters=spec,
                                       actor=actor, updated=time.time())
            else:
                if body.get('confirmed') is not True:
                    raise ValueError('请先确认前台参数、点数和影响范围')
                cfg['parameters'] = spec
                version = row['version'] + 1
                conn.execute(
                    'INSERT INTO routing.versions(channel,version,config,secret,actor,created) '
                    'VALUES(%s,%s,%s,%s,%s,%s)',
                    (cid, version, json.dumps(cfg), row['secret'], actor, time.time()),
                )
                conn.execute('UPDATE routing.channels SET version=%s WHERE id=%s', (version, cid))
                all_drafts[cid] = dict(revision=old.get('revision', 0) + 1,
                                       base_version=version, parameters=spec,
                                       actor=actor, updated=time.time())
            _save_setting(conn, 3, all_drafts)
            _audit(conn, 'parameters.' + action, cid, actor)
