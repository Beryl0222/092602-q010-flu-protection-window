"""指南版本化：新版本只改变生效日之后的预测；历史接种与旧通知保持原样。"""
import unittest

from flu_protection_window.domain import ExposureEvent
from tests.helpers import (
    guideline_2025, guideline_2026_north, make_service, season,
)


class 指南版本测试(unittest.TestCase):
    def _service_with_child(self):
        service = make_service(clock="2026-09-02")
        service.load_scenario({
            "guidelines": [guideline_2025(), guideline_2026_north()],
            "seasons": [season()],
            "classes": [
                {"class_id": "class-a", "name": "一班", "region": "north"},
                {"class_id": "class-south", "name": "南班", "region": "south"},
            ],
            "children": [
                {"child_id": "n1", "birth_date": "2017-01-01", "name": "北1"},
                {"child_id": "n2", "birth_date": "2017-01-01", "name": "北2"},
                {"child_id": "n3", "birth_date": "2017-01-01", "name": "北3"},
                {"child_id": "n4", "birth_date": "2017-01-01", "name": "北4"},
                {"child_id": "n5", "birth_date": "2017-01-01", "name": "北5"},
            ],
            "consents": [],
            "enrollments": [
                {"child_id": f"n{i}", "class_id": "class-a",
                 "valid_from": "2026-09-01"}
                for i in range(1, 6)
            ],
        })
        return service

    def test_生效日前后采用不同指南版本(self):
        service = self._service_with_child()
        service.submit_certificate({
            "business_key": "BK-n1", "child_id": "n1", "dose_seq": 1,
            "vaccinated_on": "2026-10-20",
        })
        # 2026-10-25：2025 版仍生效，空白期 28 天，11-17 才保护
        before = service.individual_detail("n1", "2026-10-25",
                                           role="school_doctor")
        self.assertEqual(before["guideline_version"], "2025版")
        self.assertEqual(before["category"], "gap")
        # 2026-11-05：2026 北方版生效，空白期 21 天，11-10 起保护
        after = service.individual_detail("n1", "2026-11-05",
                                          role="school_doctor")
        self.assertEqual(after["guideline_version"], "2026版-北方")
        self.assertEqual(after["category"], "gap")
        covered = service.individual_detail("n1", "2026-11-10",
                                            role="school_doctor")
        self.assertEqual(covered["guideline_version"], "2026版-北方")
        self.assertEqual(covered["category"], "full")

    def test_历史接种事实不被指南更新改写(self):
        service = self._service_with_child()
        service.submit_certificate({
            "business_key": "BK-n1", "child_id": "n1", "dose_seq": 1,
            "vaccinated_on": "2026-09-05",
        })
        doses = service.store.list_doses("n1")
        self.assertEqual(len(doses), 1)
        self.assertEqual(doses[0].vaccinated_on, "2026-09-05")
        # 再次推演后剂次事实依旧
        service.advise_class("class-a", "2026-12-01", role="school_doctor")
        doses = service.store.list_doses("n1")
        self.assertEqual(len(doses), 1)
        self.assertEqual(doses[0].vaccinated_on, "2026-09-05")

    def test_旧通知保持原样_重算只幂等去重(self):
        service = self._service_with_child()
        # 5 人均未接种：流行季内高风险，10-12 已产生教师通知（2025 版）
        first = service.advise_class("class-a", "2026-10-12",
                                     role="school_doctor")
        self.assertEqual(first["guideline"]["version"], "2025版")
        notes_after_first = service.store.list_notifications("class-a")
        teacher_notes = [n for n in notes_after_first
                         if n.audience == "teacher"]
        self.assertEqual(len(teacher_notes), 1)
        first_id = teacher_notes[0].notification_id
        first_created = teacher_notes[0].created_at
        first_content = teacher_notes[0].content

        # 2026 版生效后重算同一天结果：通知不被改写、不重复
        service.advise_class("class-a", "2026-10-12", role="school_doctor")
        notes = [n for n in service.store.list_notifications("class-a")
                 if n.audience == "teacher"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].notification_id, first_id)
        self.assertEqual(notes[0].created_at, first_created)
        self.assertEqual(notes[0].content, first_content)

    def test_地区专用版本不影响其他地区(self):
        service = self._service_with_child()
        # 南方班级没有成员、只验证版本选择：补一个通用版本查询场景
        from flu_protection_window import engine
        from datetime import date
        guidelines = service.store.list_guidelines()
        north = engine.select_guideline(guidelines, "north",
                                        date(2026, 11, 5))
        south = engine.select_guideline(guidelines, "south",
                                        date(2026, 11, 5))
        self.assertEqual(north.version, "2026版-北方")
        self.assertEqual(south.version, "2025版")

    def test_暴露事件观察窗内风险上调(self):
        service = self._service_with_child()
        # 4/5 覆盖 = 80%，原本低风险
        for i in range(1, 5):
            service.submit_certificate({
                "business_key": f"BK-n{i}", "child_id": f"n{i}",
                "dose_seq": 1, "vaccinated_on": "2026-09-01",
            })
        service.store.add_exposure(ExposureEvent(
            event_id="e1", class_id="class-a",
            occurred_on="2026-10-10", description="病例"))
        view = service.advise_class("class-a", "2026-10-12",
                                    role="school_doctor")
        self.assertEqual(view["covered"], 4)
        self.assertEqual(view["risk_level"], "medium")
        self.assertTrue(view["exposure_window"])
        # 观察窗外恢复低风险
        later = service.advise_class("class-a", "2026-10-25",
                                     role="school_doctor")
        self.assertEqual(later["risk_level"], "low")
        self.assertFalse(later["exposure_window"])


if __name__ == "__main__":
    unittest.main()
