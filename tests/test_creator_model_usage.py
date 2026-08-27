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

    def test_peak_price_reservation_blocks_request_over_remaining_budget(self):
        expected_cost = 7_213
        guard = self.guard(
            user_daily_cost_micro_usd=expected_cost * 2 - 1,
            input_price_micro_usd_per_million=440_000,
            output_price_micro_usd_per_million=1_320_000,
            price_version="deepseek-v4-flash-0731-peak-test",
        )
        tokens, cost = guard._estimate(1_000, 2_400)
        self.assertEqual(tokens, 11_592)
        self.assertEqual(guard._price_for_tokens(2_400, 1_320_000), 3_168)
        self.assertEqual(cost, expected_cost)

        guard.acquire("alice", "1.1.1.1", "chat", 1_000, 2_400).finish(True)
        with self.assertRaises(ModelUsageError) as raised:
            guard.acquire("alice", "1.1.1.1", "chat", 1_000, 2_400)
        self.assertEqual(raised.exception.code, "model_user_daily_budget")
        with closing(self.db()) as connection:
            rows = connection.execute(
                "SELECT estimated_cost_micro_usd,price_version,"
                "input_price_micro_usd_per_million,"
                "output_price_micro_usd_per_million "
                "FROM creator_model_calls WHERE username='alice'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["estimated_cost_micro_usd"], expected_cost)
        self.assertEqual(
            rows[0]["price_version"], "deepseek-v4-flash-0731-peak-test",
        )
        self.assertEqual(rows[0]["input_price_micro_usd_per_million"], 440_000)
        self.assertEqual(rows[0]["output_price_micro_usd_per_million"], 1_320_000)

    def test_price_configuration_cannot_lower_official_peak_defaults(self):
        guard = self.guard(
            input_price_micro_usd_per_million=1,
            output_price_micro_usd_per_million=1,
            input_token_overhead=1,
        )
        self.assertEqual(guard._estimate(1_000, 2_400)[1], 7_213)

    def test_legacy_rows_are_migrated_and_repriced_conservatively(self):
        with closing(self.db()) as connection:
            connection.execute("""CREATE TABLE creator_model_calls(
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
                "INSERT INTO creator_model_calls VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy", "alice", "hash", "chat", "2033-05-18",
                    3_400, 673, "completed", self.clock.value, 0,
                    self.clock.value,
                ),
            )
            connection.commit()

        self.guard()
        with closing(self.db()) as connection:
            row = connection.execute(
                "SELECT estimated_cost_micro_usd,price_version,"
                "input_price_micro_usd_per_million,"
                "output_price_micro_usd_per_million "
                "FROM creator_model_calls WHERE id='legacy'"
            ).fetchone()
        self.assertEqual(row["estimated_cost_micro_usd"], 15_302)
        self.assertTrue(row["price_version"].startswith("legacy-repriced:"))
        self.assertEqual(row["input_price_micro_usd_per_million"], 440_000)
        self.assertEqual(row["output_price_micro_usd_per_million"], 1_320_000)

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
