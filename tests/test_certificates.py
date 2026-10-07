"""凭证业务标识去重、同标识冲突进入隐私复核及复核处理。"""
import unittest

from tests.helpers import make_service, seed_base


class 凭证复核测试(unittest.TestCase):
    def setUp(self):
        self.service = make_service(clock="2026-09-02")
        seed_base(self.service, size=5)
        self.payload = {
            "business_key": "BK-C1-2026", "child_id": "c1", "dose_seq": 1,
            "vaccinated_on": "2026-09-01", "vaccine": "influenza",
        }

    def test_相同凭证重复提交幂等(self):
        first = self.service.submit_certificate(dict(self.payload))
        again = self.service.submit_certificate(dict(self.payload))
        self.assertEqual(first.cert_id, again.cert_id)
        self.assertEqual(again.status, "accepted")
        doses = self.service.store.list_doses("c1")
        self.assertEqual(len(doses), 1)
        self.assertEqual(self.service.pending_reviews(), [])

    def test_同标识内容冲突进入隐私复核且事实不变(self):
        self.service.submit_certificate(dict(self.payload))
        conflicting = dict(self.payload, vaccinated_on="2026-09-08")
        result = self.service.submit_certificate(conflicting)
        self.assertEqual(result.status, "conflict_pending")
        # 原接种事实保持 09-01
        doses = self.service.store.list_doses("c1")
        self.assertEqual(len(doses), 1)
        self.assertEqual(doses[0].vaccinated_on, "2026-09-01")
        reviews = self.service.pending_reviews()
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["reason_code"], "conflicting_content")
        self.assertEqual(reviews[0]["business_key"], "BK-C1-2026")

    def test_复核驳回保持原事实(self):
        self.service.submit_certificate(dict(self.payload))
        conflict = self.service.submit_certificate(
            dict(self.payload, vaccinated_on="2026-09-08"))
        review = self.service.pending_reviews()[0]
        outcome = self.service.resolve_review(review["review_id"], approve=False)
        self.assertEqual(outcome["resolution"], "rejected")
        self.assertEqual(conflict.cert_id and
                         self.service.store.get_certificate(conflict.cert_id).status,
                         "rejected")
        doses = self.service.store.list_doses("c1")
        self.assertEqual(doses[0].vaccinated_on, "2026-09-01")
        self.assertEqual(self.service.pending_reviews(), [])

    def test_复核通过才修正接种事实并重算(self):
        self.service.submit_certificate(dict(self.payload))
        self.service.submit_certificate(
            dict(self.payload, vaccinated_on="2026-09-08"))
        review = self.service.pending_reviews()[0]
        self.service.resolve_review(review["review_id"], approve=True)
        doses = self.service.store.list_doses("c1")
        self.assertEqual(len(doses), 1)
        self.assertEqual(doses[0].vaccinated_on, "2026-09-08")
        # 重算日志覆盖该儿童与其班级
        entries = self.service.recompute_log()
        self.assertTrue(
            any(e["entity_type"] == "child" and e["entity_id"] == "c1"
                and e["reason"] == "review_approved" for e in entries)
        )
        self.assertTrue(
            any(e["entity_type"] == "class" and e["entity_id"] == "class-a"
                for e in entries)
        )

    def test_已处理复核不可重复处理(self):
        self.service.submit_certificate(dict(self.payload))
        self.service.submit_certificate(
            dict(self.payload, vaccinated_on="2026-09-08"))
        review_id = self.service.pending_reviews()[0]["review_id"]
        self.service.resolve_review(review_id, approve=True)
        with self.assertRaises(ValueError):
            self.service.resolve_review(review_id, approve=False)


if __name__ == "__main__":
    unittest.main()
