from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import pathlib
import sqlite3
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from creator_agent.model_usage import ModelUsageError, ModelUsageGuard


class Clock:
    def __init__(self, value=2_000_000_000):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class CreatorModelUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.temp.name) / "usage.db"
        self.clock = Clock()

    def tearDown(self):
        self.temp.cleanup()

    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def guard(self, **changes):
        values = {
            "window_seconds": 60, "user_window_requests": 20,
            "ip_window_requests": 30, "user_concurrency": 2,
            "global_concurrency": 8, "user_daily_requests": 500,
            "global_daily_requests": 10000, "user_daily_tokens": 2_000_000,
            "global_daily_tokens": 40_000_000,
            "user_daily_cost_micro_usd": 1_000_000,
            "global_daily_cost_micro_usd": 20_000_000,
            "lease_seconds": 60, "circuit_failures": 8,
            "circuit_seconds": 60, "clock": self.clock,
        }
        values.update(changes)
        return ModelUsageGuard(self.db, **values)

    def test_account_and_ip_windows_reject_before_new_lease(self):
        guard = self.guard(user_window_requests=2, ip_window_requests=2)
        for _ in range(2):
            guard.acquire("alice", "1.2.3.4", "chat", 100).finish(True)
        with self.assertRaises(ModelUsageError) as raised:
            guard.acquire("alice", "1.2.3.4", "chat", 100)
        self.assertIn(raised.exception.code, {"model_user_rate_limited", "model_ip_rate_limited"})
        with closing(self.db()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM creator_model_calls").fetchone()[0], 2)

    def test_different_accounts_share_the_same_ip_window(self):
        guard = self.guard(user_window_requests=20, ip_window_requests=2)
        guard.acquire("alice", "8.8.8.8", "chat", 10).finish(True)
        guard.acquire("bob", "8.8.8.8", "chat", 10).finish(True)
        with self.assertRaises(ModelUsageError) as raised:
            guard.acquire("carol", "8.8.8.8", "chat", 10)
        self.assertEqual(raised.exception.code, "model_ip_rate_limited")

    def test_daily_request_token_and_cost_budget_survives_restart(self):
        first = self.guard(user_daily_requests=1)
        first.acquire("alice", "1.1.1.1", "chat", 10).finish(True)
        restarted = self.guard(user_daily_requests=1)
        with self.assertRaises(ModelUsageError) as raised:
            restarted.acquire("alice", "1.1.1.1", "chat", 10)
        self.assertEqual(raised.exception.code, "model_user_daily_requests")

        other = self.guard(
            user_daily_requests=500, user_daily_tokens=100,
            user_daily_cost_micro_usd=20,
        )
        with self.assertRaises(ModelUsageError) as budget:
            other.acquire("bob", "2.2.2.2", "chat", 100, 100)
        self.assertIn(budget.exception.code, {
            "model_user_daily_tokens", "model_user_daily_budget",
        })

    def test_user_and_global_concurrency_are_shared_across_instances(self):
        first = self.guard(user_concurrency=1, global_concurrency=2)
        second = self.guard(user_concurrency=1, global_concurrency=2)
        alice = first.acquire("alice", "1.1.1.1", "chat", 10)
        with self.assertRaises(ModelUsageError) as user_limit:
            second.acquire("alice", "1.1.1.2", "chat", 10)
        self.assertEqual(user_limit.exception.code, "model_user_concurrency")
        bob = second.acquire("bob", "2.2.2.2", "chat", 10)
        with self.assertRaises(ModelUsageError) as global_limit:
            second.acquire("carol", "3.3.3.3", "chat", 10)
        self.assertEqual(global_limit.exception.code, "model_global_concurrency")
        alice.finish(True); bob.finish(True)

    def test_multithread_global_limit_rejects_without_extra_rows(self):
        guard = self.guard(user_concurrency=1, global_concurrency=2)

        def acquire(index):
            try:
                return guard.acquire(
                    "user%d" % index, "10.0.0.%d" % index, "chat", 10,
                )
            except ModelUsageError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(acquire, range(1, 6)))
        leases = [item for item in results if not isinstance(item, Exception)]
        errors = [item for item in results if isinstance(item, ModelUsageError)]
        self.assertEqual(len(leases), 2)
        self.assertEqual(len(errors), 3)
        self.assertTrue(all(item.code == "model_global_concurrency" for item in errors))
        with closing(self.db()) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM creator_model_calls WHERE state='active'"
            ).fetchone()[0], 2)
        for lease in leases:
            lease.finish(True)

    def test_expired_lease_is_reclaimed_and_failures_open_circuit(self):
        guard = self.guard(
            user_concurrency=1, global_concurrency=1,
            circuit_failures=2, circuit_seconds=60, lease_seconds=30,
        )
        guard.acquire("alice", "1.1.1.1", "chat", 10)
        self.clock.advance(31)
        guard.acquire("alice", "1.1.1.1", "chat", 10).finish(False)
        self.clock.advance(61)
        guard.acquire("alice", "1.1.1.1", "chat", 10).finish(False)
        guard.acquire("bob", "2.2.2.2", "chat", 10).finish(False)
        with self.assertRaises(ModelUsageError) as raised:
            guard.acquire("carol", "3.3.3.3", "chat", 10)
        self.assertEqual(raised.exception.code, "model_circuit_open")


if __name__ == "__main__":
    unittest.main()
