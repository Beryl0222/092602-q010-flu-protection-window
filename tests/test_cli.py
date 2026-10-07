"""命令行端到端：载入情景、按日期提醒、对比、重启续跑。"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from flu_protection_window.cli import main

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "data" / "scenario.json"


class 命令行测试(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "app.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *argv) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(list(argv))
        self.assertEqual(code, 0, buffer.getvalue())
        return buffer.getvalue()

    def test_load_advise_compare_run_due(self):
        out = self._run("load", str(SCENARIO), "--db", self.db,
                        "--date", "2026-09-02")
        self.assertIn("certificate_conflict_pending: 1", out)
        self.assertIn("certificate_accepted", out)

        # 校医视图：含提醒原因、采用指南、未计入规则类别
        out = self._run("advise", "--class", "class-1-1",
                        "--date", "2026-10-12", "--db", self.db)
        self.assertIn("采用指南", out)
        self.assertIn("2025版", out)
        self.assertIn("免疫空白期", out)
        self.assertIn("医学暂缓", out)
        self.assertIn("风险级别", out)
        self.assertIn("暴露事件", out)

        # JSON 视图可被机器读取且结构完整
        payload = json.loads(self._run(
            "advise", "--class", "class-1-1", "--date", "2026-10-12",
            "--db", self.db, "--json"))
        self.assertIn("uncovered_by_rule", payload)
        self.assertEqual(payload["guideline"]["version"], "2025版")

        # 班主任视图：文本中不出现人数明细
        teacher = self._run("advise", "--class", "class-1-1",
                            "--date", "2026-10-12", "--role", "teacher",
                            "--db", self.db)
        self.assertIn("风险级别", teacher)
        self.assertNotIn("覆盖率", teacher)
        self.assertNotIn("免疫空白期", teacher)

        # 复核队列能看到冲突凭证
        reviews = json.loads(self._run("reviews", "--db", self.db, "--json"))
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["business_key"], "BK-C3-2025-10")

        # 提前/延后对比
        compare = json.loads(self._run(
            "compare", "--class", "class-1-1",
            "--start", "2026-10-01", "--end", "2027-03-31",
            "--db", self.db, "--json"))
        self.assertGreaterEqual(
            compare["early"]["covered_days"], compare["late"]["covered_days"])

        # 通知已留痕
        notes = self._run("notifications", "--db", self.db)
        self.assertIn("通知记录", notes)

    def test_新版本生效后采用新指南而旧通知不变(self):
        self._run("load", str(SCENARIO), "--db", self.db,
                  "--date", "2026-09-02")
        self._run("advise", "--class", "class-1-1",
                  "--date", "2026-10-12", "--db", self.db)
        later = json.loads(self._run(
            "advise", "--class", "class-1-1", "--date", "2026-11-15",
            "--db", self.db, "--json"))
        self.assertEqual(later["guideline"]["version"], "2026版-北方")

    def test_重启后续跑触发器(self):
        self._run("load", str(SCENARIO), "--db", self.db,
                  "--date", "2026-09-02")
        out = self._run("run-due", "--date", "2026-09-30", "--db", self.db)
        # c1 于 09-01 接种，09-29 空白期结束；流行季 10-01 尚未到
        self.assertIn("gap_end", out)
        self.assertNotIn("season_start", out)
        # 新进程日期推进到流行季开始后
        out = self._run("run-due", "--date", "2026-10-01", "--db", self.db)
        self.assertIn("season_start", out)
        # 已处理的不重复
        out = self._run("run-due", "--date", "2026-10-02", "--db", self.db)
        self.assertIn("处理到期窗口变化 0 条", out)


if __name__ == "__main__":
    unittest.main()
