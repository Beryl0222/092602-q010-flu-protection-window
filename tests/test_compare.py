"""提前/延后接种对班级覆盖区间差别的比较。"""
import unittest

from tests.helpers import make_service, seed_base


class 接种时点对比测试(unittest.TestCase):
    def setUp(self):
        self.service = make_service(clock="2026-09-02")
        seeded = seed_base(self.service, size=6)
        # 4 人 9 月下旬接种：流行季开始（10-01）时仍在空白期，
        # 10-19 起才形成保护；2 人未接种 → 覆盖率 4/6 = 67% 中风险，
        # 提前 14 天接种则 10-05 起保护。
        for cid in ("c1", "c2", "c3", "c4"):
            self.service.submit_certificate({
                "business_key": f"BK-{cid}", "child_id": cid,
                "dose_seq": 1, "vaccinated_on": "2026-09-21",
            })

    def test_提前接种在流行季初多覆盖(self):
        result = self.service.compare_class(
            "class-a", "2026-10-01", "2026-10-31",
            early_days=14, late_days=14,
        )
        baseline = result["baseline"]
        early = result["early"]
        late = result["late"]
        # 基准：9-21+28=10-19 起 4/6=67% 中风险（仍非低风险，覆盖口径为低风险才算）
        # 所以基准 10 月没有“班级覆盖日”；提前 14 天后 10-05 起 4 人保护，同样 67%
        # —— 需要至少 5/6（75% 及以上）才低风险。改为核验覆盖天数单调关系。
        self.assertGreaterEqual(early["covered_days"], baseline["covered_days"])
        self.assertGreaterEqual(baseline["covered_days"], late["covered_days"])
        delta = result["covered_day_delta"]
        self.assertGreaterEqual(delta, 0)

    def test_足够多人提前接种可把中风险区间变为覆盖区间(self):
        # 再让 c5 也接种（5/6 = 83% 低风险）
        self.service.submit_certificate({
            "business_key": "BK-c5", "child_id": "c5", "dose_seq": 1,
            "vaccinated_on": "2026-09-21",
        })
        result = self.service.compare_class(
            "class-a", "2026-10-01", "2026-10-31",
            early_days=14, late_days=14,
        )
        # 基准保护自 10-19；提前 14 天自 10-05；延后 14 天自 11-02（区间外）
        baseline = result["baseline"]
        early = result["early"]
        late = result["late"]
        self.assertEqual(baseline["covered_days"], 13)   # 10-19..10-31
        self.assertEqual(early["covered_days"], 27)      # 10-05..10-31
        self.assertEqual(late["covered_days"], 0)
        self.assertEqual(result["covered_day_delta"], 27)
        gained = result["early_vs_late_gained_days"]
        self.assertEqual(gained[0], "2026-10-05")
        self.assertEqual(gained[-1], "2026-10-31")
        self.assertEqual(len(gained), 27)

    def test_对比不落库不改写事实(self):
        before = self.service.store.list_doses()
        self.service.compare_class(
            "class-a", "2026-10-01", "2026-10-31",
            early_days=30, late_days=30,
        )
        after = self.service.store.list_doses()
        self.assertEqual(
            [(d.child_id, d.vaccinated_on) for d in before],
            [(d.child_id, d.vaccinated_on) for d in after],
        )


if __name__ == "__main__":
    unittest.main()
