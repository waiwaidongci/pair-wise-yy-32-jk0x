import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, BatchService
from records import Store
import rules


class BatchFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = BatchService(Store(Path(self.tmp.name) / "b.db"))
        self.f1 = self.s.register_factory("qa", "qa", "F1", "一厂", "CN")["id"]
        self.f2 = self.s.register_factory("qa", "qa", "F2", "二厂", "CN")["id"]
        self.future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat().replace("+00:00", "Z")

    def tearDown(self):
        self.s.store.close(); self.tmp.cleanup()

    def rev(self, bid: int) -> int:
        return self.s.batch_detail(bid)["batch"]["revision"]

    def _batch(self, no="B-1", product="药片"):
        return self.s.create_batch("operator", "operator", self.f1, no, product, "2026-01-01", "2028-01-01")["id"]

    # ---------- 端到端闭环 ----------
    def test_full_investigation_retest_rework_review_and_release(self):
        bid = self._batch()
        self.s.record_test("lab", "lab", self.f1, bid, "含量", 89, 95, 105, self.rev(bid))
        self.s.record_test("lab", "lab", self.f1, bid, "含量", 99, 95, 105, self.rev(bid))
        dev = self.s.add_deviation("operator", "operator", self.f1, bid, "minor", "装量轻微偏离", self.future, self.rev(bid))
        self.s.close_deviation("qa", "qa", dev["id"], "调整灌装参数并复测合格", self.rev(bid))
        rw = self.s.plan_rework("operator", "operator", self.f1, bid, "返工包装", self.rev(bid))
        self.s.complete_rework("operator", "operator", self.f1, rw["id"], self.rev(bid))
        self.s.record_stability("lab", "lab", self.f1, bid, "25C/60RH", "3m", 99, 105, self.rev(bid))
        self.s.record_review("qa", "qa", bid, "调查关闭，复测合格，建议放行")
        result = self.s.decide("qa", "qa", bid, "release", "复核通过", self.rev(bid))
        self.assertEqual("released", result["batch"]["state"])
        self.assertEqual(1, len(self.s.batch_detail(bid)["decisions"]))

    # ---------- 关键偏差 ----------
    def test_critical_deviation_always_blocks(self):
        bid = self._batch("B-2", "胶囊")
        self.s.record_test("lab", "lab", self.f1, bid, "含量", 100, 95, 105, self.rev(bid))
        crit = self.s.add_deviation("inspector", "inspector", self.f1, bid, "critical", "无菌数据异常", self.future, self.rev(bid))
        with self.assertRaises(ApiError) as blocked:
            self.s.decide("qa", "qa", bid, "release", "尝试放行", self.rev(bid))
        self.assertIn("关键偏差", blocked.exception.message)
        self.assertTrue(self.s.evaluate(bid)["critical_blocked"])
        # 关键偏差不允许例外
        with self.assertRaises(ApiError):
            self.s.approve_exception("qa", "qa", crit["id"], "暂时接受", self.future, self.rev(bid))
        # 即使有关闭所需的复测，关键偏差未关也挡住
        self.s.record_test("lab", "lab", self.f1, bid, "水分", 5, 0, 6, self.rev(bid))
        self.assertIn("关键偏差", self.s.evaluate(bid)["blockers"][0])

    def test_factory_conflict_and_stale_revision(self):
        bid = self._batch("B-2b")
        self.s.record_test("lab", "lab", self.f1, bid, "含量", 100, 95, 105, self.rev(bid))
        with self.assertRaises(ApiError):
            self.s.record_test("lab", "lab", self.f2, bid, "水分", 1, 0, 2, self.rev(bid))
        with self.assertRaises(ApiError) as stale:
            self.s.record_test("lab", "lab", self.f1, bid, "水分", 1, 0, 2, 1)
        self.assertEqual(409, stale.exception.status)

    # ---------- 关闭偏差前置 ----------
    def test_close_requires_passing_retest_after_failure(self):
        bid = self._batch("B-3")
        dev = self.s.add_deviation("operator", "operator", self.f1, bid, "minor", "含量偏低", self.future, self.rev(bid))
        # 从未失败过：不能关闭（缺少失败-复测证据）
        self.s.record_test("lab", "lab", self.f1, bid, "含量", 100, 95, 105, self.rev(bid))
        with self.assertRaises(ApiError) as e:
            self.s.close_deviation("qa", "qa", dev["id"], "已纠正", self.rev(bid))
        self.assertIn("合格复测", e.exception.message)
        # 失败后直接关闭仍不行
        self.s.record_test("lab", "lab", self.f1, bid, "含量", 90, 95, 105, self.rev(bid))
        with self.assertRaises(ApiError):
            self.s.close_deviation("qa", "qa", dev["id"], "已纠正", self.rev(bid))
        # 再测合格后可以关闭
        self.s.record_test("lab", "lab", self.f1, bid, "含量", 101, 95, 105, self.rev(bid))
        closed = self.s.close_deviation("qa", "qa", dev["id"], "已纠正并复测合格", self.rev(bid))
        self.assertEqual("closed", closed["status"])

    def test_close_waits_for_rework_completion(self):
        bid = self._batch("B-4")
        self.s.record_test("lab", "lab", self.f1, bid, "含量", 90, 95, 105, self.rev(bid))
        self.s.record_test("lab", "lab", self.f1, bid, "含量", 100, 95, 105, self.rev(bid))
        dev = self.s.add_deviation("operator", "operator", self.f1, bid, "minor", "混装", self.future, self.rev(bid))
        rw = self.s.plan_rework("operator", "operator", self.f1, bid, "重新分装", self.rev(bid))
        with self.assertRaises(ApiError) as e:
            self.s.close_deviation("qa", "qa", dev["id"], "复测合格", self.rev(bid))
        self.assertIn("返工", e.exception.message)
        self.s.complete_rework("operator", "operator", self.f1, rw["id"], self.rev(bid))
        self.assertEqual("closed", self.s.close_deviation("qa", "qa", dev["id"], "返工后关闭", self.rev(bid))["status"])

    # ---------- 供应商变更影响处置 ----------
    def test_supplier_change_blocks_until_disposed(self):
        bid = self._batch("B-5")
        self.s.record_test("lab", "lab", self.f1, bid, "水分", 1, 0, 3, self.rev(bid))
        sc = self.s.record_supplier_change("operator", "operator", self.f1, bid, "新包材厂",
                                           "内包材变更", "换铝箔供应商", self.rev(bid))
        self.assertEqual(rules.BLOCKED, self.s.evaluate(bid)["verdict"])
        with self.assertRaises(ApiError):
            self.s.dispose_supplier_change("qa", "qa", self.f1, sc["id"], "   ", self.rev(bid))
        self.s.dispose_supplier_change("qa", "qa", self.f1, sc["id"], "桥接批合格，质量协议已签", self.rev(bid))
        self.assertEqual(rules.READY, self.s.evaluate(bid)["verdict"])

    # ---------- 一般偏差 + 例外 = 有条件放行 ----------
    def test_minor_with_valid_exception_allows_conditional_only(self):
        bid = self._batch("B-6")
        self.s.record_test("lab", "lab", self.f1, bid, "含量", 100, 95, 105, self.rev(bid))
        dev = self.s.add_deviation("inspector", "inspector", self.f1, bid, "minor", "标签偏移", self.future, self.rev(bid))
        ev = self.s.evaluate(bid)
        self.assertEqual(rules.BLOCKED, ev["verdict"])  # 无例外：卡住
        self.s.approve_exception("qa", "qa", dev["id"], "不影响追溯", self.future, self.rev(bid))
        self.assertEqual(rules.CONDITIONAL, self.s.evaluate(bid)["verdict"])
        # 有条件放行必须带例外编号
        self.s.record_review("qa", "qa", bid, "凭例外有条件放行")
        with self.assertRaises(ApiError):
            self.s.decide("qa", "qa", bid, "conditional", "例外放行", self.rev(bid))
        out = self.s.decide("qa", "qa", bid, "conditional", "例外放行", self.rev(bid), exception_code="EX-1")
        self.assertEqual("conditional", out["batch"]["state"])
        # 例外仍未关闭，不能直接正式放行（按新版本重新复核后仍如此）
        self.s.record_review("qa", "qa", bid, "偏差仍挂起")
        with self.assertRaises(ApiError) as e:
            self.s.decide("qa", "qa", bid, "release", "想转正", self.rev(bid))
        self.assertIn("只能有条件放行", e.exception.message)

    # ---------- 复核失效与历史可查 ----------
    def test_review_invalidated_on_data_change_but_history_kept(self):
        bid = self._batch("B-7")
        self.s.record_test("lab", "lab", self.f1, bid, "含量", 100, 95, 105, self.rev(bid))
        r = self.s.record_review("qa", "qa", bid, "初版复核：可放行")
        # 再补一条合格稳定性：资料版本变化，结论 ready 不变，但原复核失效
        self.s.record_stability("lab", "lab", self.f1, bid, "25C/60RH", "0M", 99, 105, self.rev(bid))
        with self.assertRaises(ApiError) as e:
            self.s.decide("qa", "qa", bid, "release", "放行", self.rev(bid))
        self.assertIn("原复核已失效", e.exception.message)
        hist = self.s.review_history(bid)
        self.assertIsNone(hist["active"])
        self.assertEqual("superseded", hist["reviews"][0]["status"])
        self.assertEqual("初版复核：可放行", hist["reviews"][0]["conclusion"])
        self.assertEqual(r["id"], hist["reviews"][0]["id"])
        # 按新版本重新复核后放行
        self.s.record_review("qa", "qa", bid, "按新版本重算：可放行")
        self.assertEqual("released", self.s.decide("qa", "qa", bid, "release", "复核通过", self.rev(bid))["batch"]["state"])
        # 旧结论仍可查
        self.assertEqual(2, len(self.s.review_history(bid)["reviews"]))

    def test_failed_latest_stability_blocks(self):
        bid = self._batch("B-8")
        self.s.record_test("lab", "lab", self.f1, bid, "含量", 99, 95, 105, self.rev(bid))
        self.s.record_stability("lab", "lab", self.f1, bid, "40C/75RH", "1M", 100, 105, self.rev(bid))
        self.s.record_stability("lab", "lab", self.f1, bid, "40C/75RH", "3M", 108, 105, self.rev(bid))
        ev = self.s.evaluate(bid)
        self.assertEqual(rules.BLOCKED, ev["verdict"])
        self.assertIn("稳定性", ev["missing"][-1]["title"])

    def test_desk_filters_stuck_batches(self):
        # 卡住批
        b1 = self._batch("B-9a")
        self.s.add_deviation("operator", "operator", self.f1, b1, "critical", "严重问题", self.future, self.rev(b1))
        # 可放行批
        b2 = self._batch("B-9b")
        self.s.record_test("lab", "lab", self.f1, b2, "含量", 100, 95, 105, self.rev(b2))
        self.s.record_review("qa", "qa", b2, "ok")
        desk = self.s.desk()
        self.assertEqual(2, desk["stats"]["total"])
        stuck = self.s.desk(verdict=rules.BLOCKED)
        self.assertEqual(1, len(stuck["cards"]))
        self.assertEqual("B-9a", stuck["cards"][0]["batch"]["batch_no"])
        ready = self.s.desk(verdict=rules.READY)
        self.assertEqual("B-9b", ready["cards"][0]["batch"]["batch_no"])


class RulesPureTest(unittest.TestCase):
    def _batch(self, bid=1, rev=1):
        return {"id": bid, "revision": rev, "state": "investigation"}

    def test_retest_evidence_patterns(self):
        T = [{"id": 1, "test_type": "x", "round": 1, "passed": 0}]
        self.assertFalse(rules.retest_evidence(T))
        T.append({"id": 2, "test_type": "x", "round": 2, "passed": 1})
        self.assertTrue(rules.retest_evidence(T))
        # 先合格后失败不算
        T2 = [{"id": 1, "test_type": "y", "round": 1, "passed": 1}, {"id": 2, "test_type": "y", "round": 2, "passed": 0}]
        self.assertFalse(rules.retest_evidence(T2))

    def test_exception_expiry(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        d = {"severity": "minor", "exception_reason": "r", "exception_until": future}
        self.assertTrue(rules.exception_valid(d))
        d["exception_until"] = past
        self.assertFalse(rules.exception_valid(d))
        self.assertFalse(rules.exception_valid({"severity": "critical", "exception_reason": "r", "exception_until": future}))

    def test_no_tests_blocks_missing_stability_only_warns(self):
        ev = rules.evaluate(batch=self._batch(), deviations=[], tests=[], rework=[], supplier_changes=[], stability=[])
        self.assertEqual(rules.BLOCKED, ev["verdict"])
        ev = rules.evaluate(batch=self._batch(), deviations=[],
                            tests=[{"id": 1, "test_type": "x", "round": 1, "passed": 1, "result": 1, "spec_min": 0, "spec_max": 2}],
                            rework=[], supplier_changes=[], stability=[])
        self.assertEqual(rules.READY, ev["verdict"])
        self.assertTrue(ev["missing"][0]["warning"])

    def test_expired_exception_falls_back_to_blocked(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        dev = {"id": 3, "severity": "minor", "status": "open", "title": "t",
               "exception_reason": "r", "exception_until": past, "exception_approved_by": "qa"}
        test = {"id": 1, "test_type": "x", "round": 1, "passed": 1, "result": 1, "spec_min": 0, "spec_max": 2}
        ev = rules.evaluate(batch=self._batch(), deviations=[dev], tests=[test], rework=[], supplier_changes=[], stability=[])
        self.assertEqual(rules.BLOCKED, ev["verdict"])


if __name__ == "__main__":
    unittest.main()
