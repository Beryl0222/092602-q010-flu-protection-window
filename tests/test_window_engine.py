"""免疫空白期、剂次规则、衰减/到期的个体与班级推演。"""
import unittest

from flu_protection_window.domain import RULE_CATEGORIES
from tests.helpers import make_service, seed_base


class 保护窗口推演测试(unittest.TestCase):
    def setUp(self):
        self.service = make_service(clock="2026-09-02")
        seeded = seed_base(self.service, size=6)
        self.ids = seeded["children"]

    def _cert(self, cid, day, dose_seq=1):
        self.service.submit_certificate({
            "business_key": f"BK-{cid}-{dose_seq}",
            "child_id": cid, "dose_seq": dose_seq,
            "vaccinated_on": day, "vaccine": "influenza",
        })

    def test_接种后两到四周为免疫空白期(self):
        self._cert("c1", "2026-09-01")
        detail = self.service.individual_detail("c1", "2026-09-15",
                                                role="school_doctor")
        self.assertEqual(detail["category"], "gap")
        self.assertFalse(detail["protected"] if "protected" in detail else detail["covered"])
        self.assertEqual(detail["protection_starts_on"], "2026-09-29")
        # 满 28 天后进入完整保护
        after = self.service.individual_detail("c1", "2026-09-29",
                                               role="school_doctor")
        self.assertEqual(after["category"], "full")
        self.assertTrue(after["covered"])

    def test_未接种与空白期计入未覆盖规则类别(self):
        self._cert("c1", "2026-09-20")  # 9-20+28 = 10-18 才保护
        self._cert("c2", "2026-09-20")
        self._cert("c3", "2026-09-20")
        # c4 c5 c6 未接种
        view = self.service.advise_class("class-a", "2026-10-12",
                                         role="school_doctor")
        by_rule = view["uncovered_by_rule"]
        self.assertEqual(by_rule["gap"]["count"], 3)
        self.assertEqual(by_rule["unvaccinated"]["count"], 3)
        self.assertEqual(by_rule["gap"]["label"],
                         RULE_CATEGORIES["gap"])
        self.assertEqual(view["covered"], 0)
        self.assertEqual(view["denominator"], 6)
        self.assertEqual(view["risk_level"], "high")

    def test_低龄儿童两剂间隔不足则程序不完整(self):
        # 4 岁儿童（首剂时不足 108 月龄）需要两剂
        service = make_service(clock="2026-09-02")
        service.load_scenario({
            "guidelines": [{
                "guideline_id": "g", "version": "v",
                "effective_from": "2025-01-01",
                "gap_max_days": 28, "protection_days": 180,
                "waning_days": 60, "two_dose_under_months": 108,
                "min_dose_interval_days": 28, "min_class_size": 1,
                "high_risk_below": 0.5, "medium_risk_below": 0.75,
                "exposure_window_days": 14, "region": None,
            }],
            "seasons": [], "classes": [
                {"class_id": "k", "name": "幼", "region": "north"}],
            "children": [
                {"child_id": "y1", "birth_date": "2022-01-01", "name": "y"}],
            "consents": [], "enrollments": [
                {"child_id": "y1", "class_id": "k",
                 "valid_from": "2026-09-01"}],
        })
        service.submit_certificate({"business_key": "BK-y1-1", "child_id": "y1",
                                    "dose_seq": 1, "vaccinated_on": "2026-09-01"})
        waiting = service.individual_detail("y1", "2026-10-15",
                                            role="child_health_office")
        self.assertEqual(waiting["category"], "incomplete_schedule")
        self.assertTrue(waiting["needs_second_dose"])
        # 间隔不足的第二剂不计入
        service.submit_certificate({"business_key": "BK-y1-2", "child_id": "y1",
                                    "dose_seq": 2, "vaccinated_on": "2026-09-15"})
        short = service.individual_detail("y1", "2026-10-15",
                                          role="child_health_office")
        self.assertEqual(short["category"], "incomplete_schedule")
        self.assertIn("间隔不足", short["detail"])
        # 合格第二剂后，空白期自第二剂起算
        service.submit_certificate({"business_key": "BK-y1-3", "child_id": "y1",
                                    "dose_seq": 3, "vaccinated_on": "2026-10-10"})
        gap = service.individual_detail("y1", "2026-10-20",
                                        role="child_health_office")
        self.assertEqual(gap["category"], "gap")
        protected = service.individual_detail("y1", "2026-11-10",
                                              role="child_health_office")
        self.assertEqual(protected["category"], "full")

    def test_衰减期仍计入覆盖_到期后不再计入(self):
        # 使用接种时已满 108 月龄的儿童，单剂即构成有效程序
        service = make_service(clock="2026-04-10")
        service.load_scenario({
            "guidelines": [{
                "guideline_id": "g", "version": "v",
                "effective_from": "2025-01-01",
                "gap_max_days": 28, "protection_days": 180,
                "waning_days": 60, "two_dose_under_months": 108,
                "min_dose_interval_days": 28, "min_class_size": 1,
                "high_risk_below": 0.5, "medium_risk_below": 0.75,
                "exposure_window_days": 14, "region": None,
            }],
            "seasons": [], "classes": [],
            "children": [
                {"child_id": "old1", "birth_date": "2015-01-01", "name": "o"}],
            "consents": [], "enrollments": [],
        })
        service.submit_certificate({"business_key": "BK-old1", "child_id": "old1",
                                    "dose_seq": 1, "vaccinated_on": "2025-10-01"})
        # +180 = 2026-03-30 进入衰减，再 +60 = 2026-05-29 到期
        waning = service.individual_detail("old1", "2026-04-10",
                                           role="school_doctor")
        self.assertEqual(waning["category"], "waning")
        self.assertTrue(waning["covered"])
        expired = service.individual_detail("old1", "2026-06-01",
                                            role="school_doctor")
        self.assertEqual(expired["category"], "expired")
        self.assertFalse(expired["covered"])

    def test_医学暂缓期间不计入保护(self):
        self._cert("c1", "2026-08-01")  # 9 月已在完整保护期
        self.service.add_deferral({
            "deferral_id": "d1", "child_id": "c1",
            "start_date": "2026-09-10", "end_date": "2026-09-20",
            "reason": "免疫球蛋白",
        })
        detail = self.service.individual_detail("c1", "2026-09-15",
                                                role="school_doctor")
        self.assertEqual(detail["category"], "deferred")
        self.assertFalse(detail["covered"])
        back = self.service.individual_detail("c1", "2026-09-21",
                                              role="school_doctor")
        self.assertEqual(back["category"], "full")

    def test_非流行季班级为off_season(self):
        view = self.service.advise_class("class-a", "2026-09-15",
                                         role="school_doctor")
        self.assertEqual(view["risk_level"], "off_season")
        self.assertIn("流行季", view["reasons"][0])


if __name__ == "__main__":
    unittest.main()
