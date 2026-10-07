"""转班增量重算：只重算相关儿童和班级。"""
import unittest

from tests.helpers import make_service, seed_base


class 转班增量测试(unittest.TestCase):
    def setUp(self):
        self.service = make_service(clock="2026-09-02")
        seeded = seed_base(self.service, size=6)
        # 再建第二个班并放入 5 名儿童
        from flu_protection_window.domain import (
            Child, Enrollment, SchoolClass,
        )
        self.service.store.add_class(
            SchoolClass(class_id="class-b", name="二班", region="north"))
        for cid in ("d1", "d2", "d3", "d4", "d5"):
            self.service.store.add_child(
                Child(child_id=cid, birth_date="2017-03-01", name=cid))
            self.service.store.add_enrollment(Enrollment(
                child_id=cid, class_id="class-b", valid_from="2026-09-01"))

    def test_转班关闭旧时段并开启新时段(self):
        result = self.service.transfer_child(
            "c1", "class-b", on="2026-10-15", reason="家庭迁居")
        self.assertTrue(result["changed"])
        self.assertEqual(result["affected_classes"],
                         ["class-a", "class-b"])
        enrollments = self.service.store.list_enrollments(child_id="c1")
        self.assertEqual(len(enrollments), 2)
        closed = [e for e in enrollments if e.valid_to is not None]
        opened = [e for e in enrollments if e.valid_to is None]
        self.assertEqual(closed[0].valid_to, "2026-10-15")
        self.assertEqual(opened[0].class_id, "class-b")

    def test_转班当天按新班级统计(self):
        # c1 在原班已完整保护；转到二班后二班覆盖人数 +1
        self.service.submit_certificate({
            "business_key": "BK-c1", "child_id": "c1",
            "dose_seq": 1, "vaccinated_on": "2026-09-01",
        })
        before_a = self.service.advise_class(
            "class-a", "2026-10-14", role="school_doctor")
        self.assertEqual(before_a["covered"], 1)
        before_b = self.service.advise_class(
            "class-b", "2026-10-14", role="school_doctor")
        self.assertEqual(before_b["covered"], 0)

        self.service.transfer_child("c1", "class-b", on="2026-10-15")
        after_a = self.service.advise_class(
            "class-a", "2026-10-15", role="school_doctor")
        after_b = self.service.advise_class(
            "class-b", "2026-10-15", role="school_doctor")
        self.assertEqual(after_a["denominator"], 5)
        self.assertEqual(after_a["covered"], 0)
        self.assertEqual(after_b["denominator"], 6)
        self.assertEqual(after_b["covered"], 1)

    def test_只重算相关班级(self):
        log_before = len(self.service.recompute_log())
        self.service.transfer_child("c1", "class-b", on="2026-10-15")
        new_entries = self.service.recompute_log()[log_before:]
        class_entries = {e["entity_id"] for e in new_entries
                         if e["entity_type"] == "class"}
        self.assertEqual(class_entries, {"class-a", "class-b"})
        # 历史推演结果不受影响：旧时段仍然保留
        old = self.service.advise_class(
            "class-a", "2026-10-14", role="school_doctor")
        self.assertEqual(old["denominator"], 6)

    def test_重复转入同一班级不产生变化(self):
        result = self.service.transfer_child(
            "c1", "class-a", on="2026-10-15")
        self.assertFalse(result["changed"])


if __name__ == "__main__":
    unittest.main()
