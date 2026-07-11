# -*- coding: utf-8 -*-
"""余额哨兵：HeyGen 有两个额度池，单价差 420 倍，只看 remaining_quota 会盯错池子。

2026-07-11 事故复盘。用「生成前后读余额」实测（这是查上游单价唯一可信的方法）：

    plan_credit  一条 cinematic_avatar 扣 1        ← 优先扣，是真正在供片的池
    api 钱包     同一条扣 420 quota = $7.00        ← plan_credit 归零后【静默】落到这里

而顶层 remaining_quota **等于 api，完全不含 plan_credit**：

    {"remaining_quota": 69, "details": {"api": 69, "plan_credit": 390}}

哨兵原来读 remaining_quota，等于在为「$7 的应急钱包快没钱了」报警，却对
「1 credit 的主力池快见底了」一无所知。当天真实发生的事：压测把 plan_credit 打到 0
→ HeyGen 无提示地落到 API 钱包 → 包括真实用户在内每条按 $7 计费 → $15 两条烧光
→ 之后所有人 402。哨兵全程没吭一声（它只看到 api 还有钱）。

关键的反直觉结论：**API 钱包里有钱，本身就是危险。** 钱包为空时 plan_credit 耗尽会
直接 402、任务判死退点 —— 响亮地坏掉，立刻能发现；钱包有钱时则是静默烧钱。
"""
import unittest

from scripts import balance_sentinel as bs


class HeygenQuotaParseTests(unittest.TestCase):
    def test_reads_both_pools_not_the_top_level_field(self):
        # 线上真实响应：顶层 remaining_quota 等于 api，plan_credit 藏在 details 里
        resp = {"error": None, "data": {"remaining_quota": 69,
                                        "details": {"api": 69, "plan_credit": 390}}}
        plan, api = bs.heygen_quota(resp)
        self.assertEqual(plan, 390)
        self.assertEqual(api, 69)
        self.assertNotEqual(plan, resp["data"]["remaining_quota"],
                            "plan_credit 不能等于顶层 remaining_quota，否则就是又盯错池子了")

    def test_missing_details_does_not_crash(self):
        self.assertEqual(bs.heygen_quota({}), (0.0, 0.0))
        self.assertEqual(bs.heygen_quota({"data": {}}), (0.0, 0.0))


class HeygenAlertTests(unittest.TestCase):
    def _names(self, items):
        return [i[0] for i in items]

    def test_healthy_plan_credit_no_alert(self):
        items = bs.heygen_alerts(plan=390, api=69)
        self.assertEqual(len(items), 1)
        name, bal, thresh, _, _ = items[0][:5]
        self.assertEqual(bal, 390)
        self.assertGreater(bal, thresh, "390 条套餐额度是健康的，不该告警")

    def test_low_plan_credit_alerts_even_when_api_wallet_is_full(self):
        """主力池见底 → 必须告警，哪怕 API 钱包还很满。

        原哨兵的行为正相反：api 满就当没事，而这恰恰是最危险的组合。
        """
        items = bs.heygen_alerts(plan=10, api=900)
        name, bal, thresh, _, _ = items[0][:5]
        self.assertLess(bal, thresh, "plan_credit=10 必须低于阈值 → 触发告警")

    def test_exhausted_plan_with_money_in_wallet_is_a_critical_alert(self):
        """plan_credit=0 且钱包有钱 = 正在按 420 倍价格静默烧钱。这是最严重的一条。"""
        items = bs.heygen_alerts(plan=0, api=900)
        self.assertIn("HeyGen 静默跳价", self._names(items))
        crit = [i for i in items if i[0] == "HeyGen 静默跳价"][0]
        self.assertLess(crit[1], crit[2], "必须无条件触发（bal=0 < thresh=1）")
        msg = crit[5]
        self.assertIn("$7", msg)
        self.assertIn("420", msg)
        self.assertIn("清空", msg, "要明确告诉运维：宁可让它响亮地失败，也别静默烧钱")

    def test_both_pools_empty_is_not_a_silent_burn(self):
        """两个池都空 = 会直接 402 失败，响亮但不烧钱 —— 不该报『静默跳价』。"""
        items = bs.heygen_alerts(plan=0, api=0)
        self.assertNotIn("HeyGen 静默跳价", self._names(items))
        self.assertLess(items[0][1], items[0][2], "但套餐额度为 0 仍要告警")


class CollectAlertsTests(unittest.TestCase):
    def test_check_returning_a_list_yields_multiple_alerts(self):
        # HeyGen 要同时报「套餐额度低」和「正在 420 倍烧钱」，一条不够
        def fake_heygen():
            return bs.heygen_alerts(plan=0, api=900)

        alerts, state = bs.collect_alerts([fake_heygen], {}, now=1_000_000, journal=[])
        self.assertEqual(len(alerts), 2)
        self.assertTrue(any("420" in a for a in alerts))

    def test_cooldown_suppresses_repeats(self):
        def fake_heygen():
            return bs.heygen_alerts(plan=0, api=900)

        now = 1_000_000
        alerts, state = bs.collect_alerts([fake_heygen], {}, now=now, journal=[])
        self.assertEqual(len(alerts), 2)
        again, _ = bs.collect_alerts([fake_heygen], state, now=now + 60, journal=[])
        self.assertEqual(again, [], "2 小时冷却内不该重复轰炸")
        later, _ = bs.collect_alerts([fake_heygen], state, now=now + bs.COOLDOWN + 1, journal=[])
        self.assertEqual(len(later), 2, "冷却过后要再提醒")

    def test_a_broken_check_does_not_kill_the_others(self):
        def boom():
            raise RuntimeError("HeyGen 接口挂了")

        def ok():
            return ("TikHub", 1.0, 3.0, "$1.00", "https://tikhub.io")

        alerts, _ = bs.collect_alerts([boom, ok], {}, now=1_000_000, journal=[])
        self.assertEqual(len(alerts), 1, "一个供应商查询失败，不能让其他供应商的告警一起哑掉")


if __name__ == "__main__":
    unittest.main()
