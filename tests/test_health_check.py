import unittest

from scripts import health_check


class HealthCheckCpuTest(unittest.TestCase):
    def test_alerts_only_after_sustained_high_cpu(self):
        first, alert = health_check._next_cpu_state({}, 1_000_000_000, 100, 30)
        self.assertFalse(alert)

        high, alert = health_check._next_cpu_state(first, 20_000_000_000, 160, 30)
        self.assertFalse(alert)
        self.assertEqual(high["high_since"], 160)

        sustained, alert = health_check._next_cpu_state(high, 590_000_000_000, 1960, 30)
        self.assertTrue(alert)
        self.assertTrue(sustained["alerted"])


if __name__ == "__main__":
    unittest.main()
