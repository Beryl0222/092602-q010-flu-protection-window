"""未来窗口变化触发器：重启后继续处理尚未触发的变化。"""
import tempfile
import unittest
from pathlib import Path

from flu_protection_window.clock import Clock
from flu_protection_window.service import Service
from flu_protection_window.store import Store
from tests.helpers import seed_base


class 触发器续跑测试(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "window.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _service(self, clock: str) -> Service:
        return Service(Store(self.db_path), Clock(clock))

    def test_接种登记后生成空白期结束等触发器(self):
        service = self._service("2026-09-02")
        seed_base(service, size=5)
        service.submit_certificate({
            "business_key": "BK-c1", "child_id": "c1", "dose_seq": 1,
            "vaccinated_on": "2026-09-01",
        })
        # gap_end 09-29、wane 2027-02-28、expire 2027-04-29 + 流行季开始
        self.assertGreaterEqual(service.pending_trigger_count(), 4)

    def test_重启后处理到期触发器并发出空白期结束通知(self):
        service = self._service("2026-09-02")
        seed_base(service, size=5)
        service.submit_certificate({
            "business_key": "BK-c1", "child_id": "c1", "dose_seq": 1,
            "vaccinated_on": "2026-09-01",
        })
        pending_before = service.pending_trigger_count()

        # 模拟服务重启：全新 Service/Store 指向同一数据库文件
        restarted = self._service("2026-09-29")
        processed = restarted.run_due("2026-09-29")
        kinds = {item["kind"] for item in processed}
        self.assertIn("gap_end", kinds)
        self.assertNotIn("season_start", kinds)  # 流行季 10-01 才开始
        gap_item = next(i for i in processed if i["kind"] == "gap_end")
        self.assertEqual(gap_item["child_id"], "c1")
        self.assertEqual(gap_item["affected_classes"], ["class-a"])
        self.assertLess(restarted.pending_trigger_count(), pending_before)

        # 再次重启推进到流行季开始日：season_start 触发；已处理的不重复
        at_season = self._service("2026-10-01")
        processed = at_season.run_due("2026-10-01")
        self.assertEqual({i["kind"] for i in processed}, {"season_start"})
        again = at_season.run_due("2026-10-02")
        self.assertEqual(again, [])

    def test_暂缓结束触发器在重启后生效(self):
        service = self._service("2026-09-02")
        seed_base(service, size=5)
        service.add_deferral({
            "deferral_id": "d1", "child_id": "c1",
            "start_date": "2026-09-10", "end_date": "2026-09-20",
            "reason": "免疫球蛋白",
        })
        restarted = self._service("2026-09-25")
        processed = restarted.run_due("2026-09-25")
        self.assertIn("deferral_end", {i["kind"] for i in processed})
        item = next(i for i in processed if i["kind"] == "deferral_end")
        self.assertEqual(item["child_id"], "c1")

    def test_推进到衰减与到期日仍可续跑(self):
        service = self._service("2026-09-02")
        seed_base(service, size=5)
        service.submit_certificate({
            "business_key": "BK-c1", "child_id": "c1", "dose_seq": 1,
            "vaccinated_on": "2026-09-01",
        })
        # 2026-09-01 + 180 = 2027-02-28 衰减；+60 = 2027-04-29 到期
        later = self._service("2027-02-28")
        processed = later.run_due("2027-02-28")
        self.assertIn("protection_wane", {i["kind"] for i in processed})
        expired = self._service("2027-04-29")
        processed = expired.run_due("2027-04-29")
        self.assertIn("protection_expire", {i["kind"] for i in processed})
        self.assertEqual(expired.pending_trigger_count(), 0)


if __name__ == "__main__":
    unittest.main()
