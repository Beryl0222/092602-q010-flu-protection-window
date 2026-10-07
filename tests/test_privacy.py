"""最小群体人数、角色授权与通知抑制。"""
import unittest

from flu_protection_window.service import PermissionError_
from tests.helpers import make_service, seed_base


class 隐私披露测试(unittest.TestCase):
    def setUp(self):
        self.service = make_service(clock="2026-09-02")
        seeded = seed_base(self.service, size=6, small_class=True)
        self.ids = seeded["children"]

    def test_班主任只看到风险级别(self):
        view = self.service.advise_class("class-a", "2026-10-12",
                                         role="teacher")
        self.assertIn(view["risk_level"], ("high", "medium", "low"))
        self.assertIsNone(view["denominator"])
        self.assertIsNone(view["covered"])
        self.assertIsNone(view["uncovered_by_rule"])
        self.assertIsNone(view["individuals"])
        self.assertIsNone(view["season"])

    def test_不足最小群体人数时班主任得不到风险级别(self):
        view = self.service.advise_class("class-small", "2026-10-12",
                                         role="teacher")
        self.assertEqual(view["risk_level"], "insufficient")
        self.assertIsNone(view["denominator"])
        self.assertIsNone(view["individuals"])
        # 校医视图可以看到原因，但仍然没有个体数据外发
        staff = self.service.advise_class("class-small", "2026-10-12",
                                          role="school_doctor")
        self.assertEqual(staff["risk_level"], "insufficient")
        self.assertTrue(any("最小群体" in r for r in staff["reasons"]))

    def test_班主任不能查看个体健康细节(self):
        with self.assertRaises(PermissionError_):
            self.service.individual_detail("c1", "2026-10-12",
                                           role="teacher")

    def test_监护人需要有效授权(self):
        with self.assertRaises(PermissionError_):
            self.service.individual_detail(
                "c1", "2026-10-12", role="guardian", guardian_id="g1")
        ok = self.service.individual_detail(
            "c1", "2026-10-12", role="guardian", guardian_id="g-c1")
        self.assertEqual(ok["child_id"], "c1")

    def test_未授权角色被拒(self):
        with self.assertRaises(PermissionError_):
            self.service.advise_class("class-a", "2026-10-12",
                                      role="parent_volunteer")

    def test_无家长授权时个体通知被抑制但留痕(self):
        # c5 撤销授权且未接种；c1 有授权且处于空白期
        from flu_protection_window.domain import Consent
        self.service.store.upsert_consent(Consent(
            child_id="c5", guardian_id="g-c5", granted=False,
            scope="individual-health", valid_from="2026-01-01"))
        self.service.submit_certificate({
            "business_key": "BK-c1", "child_id": "c1", "dose_seq": 1,
            "vaccinated_on": "2026-10-01",
        })
        self.service.advise_class("class-a", "2026-10-12",
                                  role="school_doctor")
        notes = {n.target_id: n for n in self.service.store.list_notifications()}
        c1_note = next(n for n in notes.values()
                       if n.scope == "child" and n.target_id == "c1")
        c5_note = next(n for n in notes.values()
                       if n.scope == "child" and n.target_id == "c5")
        self.assertEqual(c1_note.status, "sent")
        self.assertEqual(c5_note.status, "suppressed")
        self.assertEqual(c5_note.content["suppress_reason"],
                         "guardian_consent_missing")
        # 被抑制的通知同样可审计
        self.assertEqual(c5_note.guideline_version, "2025版")

    def test_人数不足时教师通知被抑制并留痕(self):
        self.service.advise_class("class-small", "2026-10-12",
                                  role="school_doctor")
        suppressed = [
            n for n in self.service.store.list_notifications("class-small")
            if n.audience == "teacher"
        ]
        self.assertEqual(len(suppressed), 1)
        self.assertEqual(suppressed[0].status, "suppressed")
        self.assertEqual(suppressed[0].reason_code, "below_min_group")


if __name__ == "__main__":
    unittest.main()
