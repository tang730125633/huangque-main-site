"""Persistent resource guard for free Creator Agent model calls."""

from __future__ import annotations

from contextlib import closing
import hashlib
import math
import time
import uuid


class ModelUsageError(RuntimeError):
    def __init__(self, detail, code):
        super().__init__(detail)
        self.detail = str(detail)
        self.code = str(code)


class NullModelUsageGuard:
    class Lease:
        def finish(self, success=True):
            return None

    def acquire(self, *_args, **_kwargs):
        return self.Lease()

    def health(self):
        return True


class ModelUsageLease:
    def __init__(self, guard, call_id):
        self.guard = guard
        self.call_id = call_id
        self.finished = False

    def finish(self, success=True):
        if self.finished:
            return
        self.finished = True
        self.guard.finish(self.call_id, success)


class ModelUsageGuard:
    def __init__(self, db_factory, *, window_seconds=60, user_window_requests=20,
                 ip_window_requests=30, user_concurrency=2, global_concurrency=8,
                 user_daily_requests=500, global_daily_requests=10000,
                 user_daily_tokens=2_000_000, global_daily_tokens=40_000_000,
                 user_daily_cost_micro_usd=1_000_000,
                 global_daily_cost_micro_usd=20_000_000,
                 lease_seconds=210, circuit_failures=8, circuit_seconds=60,
                 clock=None):
        self.db_factory = db_factory
        self.window_seconds = max(10, int(window_seconds))
        self.user_window_requests = max(1, int(user_window_requests))
        self.ip_window_requests = max(1, int(ip_window_requests))
        self.user_concurrency = max(1, int(user_concurrency))
        self.global_concurrency = max(self.user_concurrency, int(global_concurrency))
        self.user_daily_requests = max(1, int(user_daily_requests))
        self.global_daily_requests = max(self.user_daily_requests, int(global_daily_requests))
        self.user_daily_tokens = max(1, int(user_daily_tokens))
        self.global_daily_tokens = max(self.user_daily_tokens, int(global_daily_tokens))
        self.user_daily_cost_micro_usd = max(1, int(user_daily_cost_micro_usd))
        self.global_daily_cost_micro_usd = max(
            self.user_daily_cost_micro_usd, int(global_daily_cost_micro_usd))
        self.lease_seconds = max(30, int(lease_seconds))
        self.circuit_failures = max(2, int(circuit_failures))
        self.circuit_seconds = max(10, int(circuit_seconds))
        self.clock = clock or time.time
        with closing(self.db_factory()) as connection:
            self.ensure_table(connection)
            connection.commit()

    @staticmethod
    def ensure_table(connection):
        connection.execute("""CREATE TABLE IF NOT EXISTS creator_model_calls(
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            ip_hash TEXT NOT NULL,
            kind TEXT NOT NULL,
            day TEXT NOT NULL,
            estimated_tokens INTEGER NOT NULL,
            estimated_cost_micro_usd INTEGER NOT NULL,
            state TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            lease_until INTEGER NOT NULL,
            finished_at INTEGER NOT NULL DEFAULT 0
        )""")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_creator_model_calls_user_time "
            "ON creator_model_calls(username,created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_creator_model_calls_ip_time "
            "ON creator_model_calls(ip_hash,created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_creator_model_calls_day "
            "ON creator_model_calls(day,state)"
        )

    @staticmethod
    def _ip_hash(ip):
        return hashlib.sha256(str(ip or "unknown").encode("utf-8")).hexdigest()

    @staticmethod
    def _estimate(input_chars, max_output_tokens):
        input_tokens = max(1, int(input_chars or 0))
        output_tokens = max(1, int(max_output_tokens or 0))
        total = input_tokens + output_tokens
        cost = int(math.ceil(input_tokens * 0.14 + output_tokens * 0.28))
        return total, max(1, cost)

    @staticmethod
    def _sum(connection, field, where, params):
        row = connection.execute(
            "SELECT COALESCE(SUM(%s),0) FROM creator_model_calls WHERE %s" % (field, where),
            params,
        ).fetchone()
        return int(row[0] or 0)

    def acquire(self, username, ip, kind, input_chars, max_output_tokens=2400):
        now = int(self.clock())
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        ip_hash = self._ip_hash(ip)
        tokens, cost = self._estimate(input_chars, max_output_tokens)
        call_id = "creator_model_" + uuid.uuid4().hex
        with closing(self.db_factory()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.ensure_table(connection)
            connection.execute(
                "UPDATE creator_model_calls SET state='expired',finished_at=? "
                "WHERE state='active' AND lease_until<=?",
                (now, now),
            )
            connection.execute(
                "DELETE FROM creator_model_calls WHERE created_at<? AND state<>'active'",
                (now - 8 * 86400,),
            )
            recent_failures = connection.execute(
                "SELECT COUNT(*) FROM creator_model_calls "
                "WHERE state='failed' AND finished_at>=?",
                (now - self.circuit_seconds,),
            ).fetchone()[0]
            if int(recent_failures) >= self.circuit_failures:
                connection.rollback()
                raise ModelUsageError("模型服务暂时熔断，请稍后重试", "model_circuit_open")
            window_start = now - self.window_seconds
            user_recent = connection.execute(
                "SELECT COUNT(*) FROM creator_model_calls WHERE username=? AND created_at>=?",
                (username, window_start),
            ).fetchone()[0]
            ip_recent = connection.execute(
                "SELECT COUNT(*) FROM creator_model_calls WHERE ip_hash=? AND created_at>=?",
                (ip_hash, window_start),
            ).fetchone()[0]
            if int(user_recent) >= self.user_window_requests:
                connection.rollback()
                raise ModelUsageError("操作过于频繁，请稍后再试", "model_user_rate_limited")
            if int(ip_recent) >= self.ip_window_requests:
                connection.rollback()
                raise ModelUsageError("当前网络请求过于频繁，请稍后再试", "model_ip_rate_limited")
            active_user = connection.execute(
                "SELECT COUNT(*) FROM creator_model_calls WHERE state='active' AND username=?",
                (username,),
            ).fetchone()[0]
            active_global = connection.execute(
                "SELECT COUNT(*) FROM creator_model_calls WHERE state='active'",
            ).fetchone()[0]
            if int(active_user) >= self.user_concurrency:
                connection.rollback()
                raise ModelUsageError("你已有模型请求处理中", "model_user_concurrency")
            if int(active_global) >= self.global_concurrency:
                connection.rollback()
                raise ModelUsageError("模型服务繁忙，请稍后重试", "model_global_concurrency")
            user_requests = connection.execute(
                "SELECT COUNT(*) FROM creator_model_calls WHERE username=? AND day=?",
                (username, day),
            ).fetchone()[0]
            global_requests = connection.execute(
                "SELECT COUNT(*) FROM creator_model_calls WHERE day=?", (day,),
            ).fetchone()[0]
            user_tokens = self._sum(
                connection, "estimated_tokens", "username=? AND day=?", (username, day))
            global_tokens = self._sum(
                connection, "estimated_tokens", "day=?", (day,))
            user_cost = self._sum(
                connection, "estimated_cost_micro_usd", "username=? AND day=?", (username, day))
            global_cost = self._sum(
                connection, "estimated_cost_micro_usd", "day=?", (day,))
            limits = (
                (int(user_requests) + 1, self.user_daily_requests, "今日模型请求次数已达上限", "model_user_daily_requests"),
                (int(global_requests) + 1, self.global_daily_requests, "今日全站模型请求已达上限", "model_global_daily_requests"),
                (user_tokens + tokens, self.user_daily_tokens, "今日个人模型 token 预算已用完", "model_user_daily_tokens"),
                (global_tokens + tokens, self.global_daily_tokens, "今日全站模型 token 预算已用完", "model_global_daily_tokens"),
                (user_cost + cost, self.user_daily_cost_micro_usd, "今日个人模型金额预算已用完", "model_user_daily_budget"),
                (global_cost + cost, self.global_daily_cost_micro_usd, "今日全站模型金额预算已用完", "model_global_daily_budget"),
            )
            for value, limit, detail, code in limits:
                if value > limit:
                    connection.rollback()
                    raise ModelUsageError(detail, code)
            connection.execute(
                """INSERT INTO creator_model_calls(
                   id,username,ip_hash,kind,day,estimated_tokens,
                   estimated_cost_micro_usd,state,created_at,lease_until)
                   VALUES(?,?,?,?,?,?,?,'active',?,?)""",
                (call_id, username, ip_hash, str(kind or "")[:80], day,
                 tokens, cost, now, now + self.lease_seconds),
            )
            connection.commit()
        return ModelUsageLease(self, call_id)

    def finish(self, call_id, success):
        now = int(self.clock())
        with closing(self.db_factory()) as connection:
            connection.execute(
                "UPDATE creator_model_calls SET state=?,finished_at=?,lease_until=0 "
                "WHERE id=? AND state='active'",
                ("completed" if success else "failed", now, call_id),
            )
            connection.commit()

    def health(self):
        try:
            with closing(self.db_factory()) as connection:
                self.ensure_table(connection)
                connection.execute("SELECT 1 FROM creator_model_calls LIMIT 1").fetchone()
            return True
        except Exception:
            return False

