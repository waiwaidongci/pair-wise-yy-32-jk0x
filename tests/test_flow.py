import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, BatchService, Store


class BatchFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = BatchService(Store(Path(self.tmp.name) / "b.db"))
        self.f1 = self.s.register_factory("qa", "qa", "F1", "一厂", "CN")["id"]
        self.f2 = self.s.register_factory("qa", "qa", "F2", "二厂", "CN")["id"]
        self.future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat().replace("+00:00", "Z")

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def rev(self, batch_id): return self.s.batch_detail(batch_id)["batch"]["revision"]

    def make_batch(self, no="B-0", product="药片"):
        return self.s.create_batch("operator", "operator", self.f1, no, product, "2026-01-01", "2028-01-01")

    def pass_test_and_stability(self, batch_id):
        self.s.record_test("lab", "lab", self.f1, batch_id, "含量", 99, 95, 105, self.rev(batch_id))
        self.s.record_stability("lab", "lab", self.f1, batch_id, "25C/60RH", "3m", 99, 105, self.rev(batch_id))

    def test_full_investigation_retest_rework_and_release(self):
        batch = self.s.create_batch("operator", "operator", self.f1, "B-1", "药片", "2026-01-01", "2028-01-01")
        dev = self.s.add_deviation("operator", "operator", self.f1, batch["id"], "minor", "装量轻微偏离", self.future, batch["revision"])
        failed = self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 89, 95, 105, dev["batch_id"] and self.s.batch_detail(batch["id"])["batch"]["revision"])
        self.assertFalse(failed["passed"])
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        passed = self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, current)
        self.assertTrue(passed["passed"])
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        self.s.close_deviation("qa", "qa", dev["id"], "调整灌装参数", current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        rw = self.s.plan_rework("operator", "operator", self.f1, batch["id"], "返工包装", current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        self.s.complete_rework("operator", "operator", self.f1, rw["id"], current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        self.s.record_stability("lab", "lab", self.f1, batch["id"], "25C/60RH", "3m", 99, 105, current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        result = self.s.decide("qa", "qa", batch["id"], "release", "调查关闭，复测合格", current)
        self.assertEqual("released", result["batch"]["state"])
        self.assertEqual(1, len(result["batch"] and self.s.batch_detail(batch["id"])["decisions"]))

    def test_critical_block_conditional_exception_and_factory_conflict(self):
        batch = self.s.create_batch("operator", "operator", self.f1, "B-2", "胶囊", "2026-02-01", "2028-02-01")
        current = batch["revision"]
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 100, 95, 105, current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        crit = self.s.add_deviation("inspector", "inspector", self.f1, batch["id"], "critical", "无菌数据异常", self.future, current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        with self.assertRaises(ApiError) as blocked:
            self.s.decide("qa", "qa", batch["id"], "release", "尝试放行", current)
        self.assertIn("关键偏差", blocked.exception.message)
        with self.assertRaises(ApiError):
            self.s.approve_exception("qa", "qa", crit["id"], "暂时接受", self.future, current)
        with self.assertRaises(ApiError):
            self.s.record_test("lab", "lab", self.f2, batch["id"], "水分", 1, 0, 2, current)
        with self.assertRaises(ApiError) as stale:
            self.s.record_test("lab", "lab", self.f1, batch["id"], "水分", 1, 0, 2, 1)
        self.assertEqual(409, stale.exception.status)

    def test_close_deviation_needs_passing_retest_and_completed_rework(self):
        batch = self.make_batch("B-3")
        dev = self.s.add_deviation("operator", "operator", self.f1, batch["id"], "minor", "含量偏低", self.future, self.rev(batch["id"]))
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 88, 95, 105, self.rev(batch["id"]))
        with self.assertRaises(ApiError) as ctx:
            self.s.close_deviation("qa", "qa", dev["id"], "调整工艺", self.rev(batch["id"]))
        self.assertIn("复测", ctx.exception.message)
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, self.rev(batch["id"]))
        rw = self.s.plan_rework("operator", "operator", self.f1, batch["id"], "重新压片", self.rev(batch["id"]))
        with self.assertRaises(ApiError) as ctx:
            self.s.close_deviation("qa", "qa", dev["id"], "调整工艺", self.rev(batch["id"]))
        self.assertIn("返工", ctx.exception.message)
        self.s.complete_rework("operator", "operator", self.f1, rw["id"], self.rev(batch["id"]))
        closed = self.s.close_deviation("qa", "qa", dev["id"], "调整工艺", self.rev(batch["id"]))
        self.assertEqual("closed", closed["status"])

    def test_supplier_change_stays_on_todo_until_disposition(self):
        batch = self.make_batch("B-4")
        self.pass_test_and_stability(batch["id"])
        self.s.record_supplier_change("qa", "qa", self.f1, batch["id"], "供应商A", "产地变更", "原料产地变更", self.rev(batch["id"]))
        detail = self.s.batch_detail(batch["id"])
        self.assertEqual("pending", detail["review"]["outcome"])
        self.assertTrue(any("供应商变更" in m for m in detail["review"]["missing"]))
        with self.assertRaises(ApiError) as ctx:
            self.s.decide("qa", "qa", batch["id"], "release", "尝试放行", self.rev(batch["id"]))
        self.assertIn("供应商变更", ctx.exception.message)
        change = detail["supplier_changes"][0]
        with self.assertRaises(ApiError):
            self.s.disposition_supplier_change("qa", "qa", change["id"], "acceptable", "", self.rev(batch["id"]))
        self.s.disposition_supplier_change("qa", "qa", change["id"], "no_impact", "", self.rev(batch["id"]))
        detail = self.s.batch_detail(batch["id"])
        self.assertEqual("releasable", detail["review"]["outcome"])
        result = self.s.decide("qa", "qa", batch["id"], "release", "资料齐全", self.rev(batch["id"]))
        self.assertEqual("released", result["batch"]["state"])

    def test_unacceptable_supplier_change_blocks_release(self):
        batch = self.make_batch("B-5")
        self.pass_test_and_stability(batch["id"])
        self.s.record_supplier_change("qa", "qa", self.f1, batch["id"], "供应商B", "工艺变更", "供应商工艺变更", self.rev(batch["id"]))
        change = self.s.batch_detail(batch["id"])["supplier_changes"][0]
        self.s.disposition_supplier_change("qa", "qa", change["id"], "unacceptable", "变更影响不可接受", self.rev(batch["id"]))
        self.assertEqual("blocked", self.s.batch_detail(batch["id"])["review"]["outcome"])
        with self.assertRaises(ApiError) as ctx:
            self.s.decide("qa", "qa", batch["id"], "release", "尝试放行", self.rev(batch["id"]))
        self.assertIn("不可接受", ctx.exception.message)

    def test_conditional_release_requires_valid_exception(self):
        batch = self.make_batch("B-6", "胶囊")
        self.pass_test_and_stability(batch["id"])
        dev = self.s.add_deviation("operator", "operator", self.f1, batch["id"], "minor", "装量轻微偏离", self.future, self.rev(batch["id"]))
        with self.assertRaises(ApiError) as ctx:
            self.s.decide("qa", "qa", batch["id"], "conditional", "尝试有条件放行", self.rev(batch["id"]), "EX-1")
        self.assertIn("一般偏差", ctx.exception.message)
        self.s.approve_exception("qa", "qa", dev["id"], "影响可接受", self.future, self.rev(batch["id"]))
        with self.assertRaises(ApiError):
            self.s.decide("qa", "qa", batch["id"], "conditional", "缺少例外编号", self.rev(batch["id"]))
        with self.assertRaises(ApiError) as ctx:
            self.s.decide("qa", "qa", batch["id"], "release", "有未关闭一般偏差仍想正式放行", self.rev(batch["id"]))
        self.assertIn("一般偏差", ctx.exception.message)
        result = self.s.decide("qa", "qa", batch["id"], "conditional", "例外有效，有条件放行", self.rev(batch["id"]), "EX-1")
        self.assertEqual("conditional", result["batch"]["state"])
        # 资料一改，原决定失效但旧结论仍可查
        self.s.record_test("lab", "lab", self.f1, batch["id"], "水分", 1.0, 0, 2, self.rev(batch["id"]))
        detail = self.s.batch_detail(batch["id"])
        self.assertEqual(1, len(detail["decisions"]))
        self.assertNotEqual(detail["decisions"][0]["revision"] + 1, detail["batch"]["revision"])

    def test_review_snapshots_versioned_and_old_kept(self):
        batch = self.make_batch("B-7")
        detail = self.s.batch_detail(batch["id"])
        self.assertEqual(1, len(detail["reviews"]))
        self.assertEqual("pending", detail["reviews"][0]["outcome"])
        self.assertEqual(0, detail["reviews"][0]["superseded"])
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, self.rev(batch["id"]))
        self.s.record_stability("lab", "lab", self.f1, batch["id"], "25C/60RH", "3m", 99, 105, self.rev(batch["id"]))
        detail = self.s.batch_detail(batch["id"])
        self.assertEqual(3, len(detail["reviews"]))
        current = [r for r in detail["reviews"] if not r["superseded"]]
        self.assertEqual(1, len(current))
        self.assertEqual(detail["batch"]["revision"], current[0]["revision"])
        self.assertEqual("releasable", detail["review"]["outcome"])
        history = {r["revision"]: r["outcome"] for r in detail["reviews"]}
        self.assertEqual("pending", history[1])
        self.assertEqual("pending", history[2])
        self.assertEqual("releasable", history[3])

    def test_missing_stability_and_failed_test_block_release(self):
        batch = self.make_batch("B-8")
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, self.rev(batch["id"]))
        with self.assertRaises(ApiError) as ctx:
            self.s.decide("qa", "qa", batch["id"], "release", "缺稳定性", self.rev(batch["id"]))
        self.assertIn("稳定性", ctx.exception.message)
        self.s.record_stability("lab", "lab", self.f1, batch["id"], "25C/60RH", "3m", 99, 105, self.rev(batch["id"]))
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 88, 95, 105, self.rev(batch["id"]))
        with self.assertRaises(ApiError) as ctx:
            self.s.decide("qa", "qa", batch["id"], "release", "检验不合格", self.rev(batch["id"]))
        self.assertIn("检验", ctx.exception.message)


if __name__ == "__main__": unittest.main()
