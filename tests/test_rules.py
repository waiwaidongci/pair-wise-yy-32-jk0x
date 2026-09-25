import sys, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rules

FUTURE = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat().replace("+00:00", "Z")
PAST = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat().replace("+00:00", "Z")


def deviation(i, severity="minor", status="open", reason=None, until=None):
    return {"id": i, "severity": severity, "status": status, "title": f"偏差{i}",
            "exception_reason": reason, "exception_until": until}


def test_row(i, test_type="含量", passed=1, rnd=1):
    return {"id": i, "test_type": test_type, "passed": passed, "round": rnd,
            "result": 99, "spec_min": 95, "spec_max": 105}


STABILITY_OK = [{"id": 1, "passed": 1, "condition": "25C/60RH", "timepoint": "3m", "result": 99, "spec_limit": 105}]


class ChecklistTest(unittest.TestCase):
    def checklist(self, **kw):
        base = dict(deviations=[], tests=[test_row(1)], rework=[], supplier_changes=[], stability=list(STABILITY_OK))
        base.update(kw)
        return rules.build_checklist({"id": 1, "revision": 1}, base["deviations"], base["tests"],
                                     base["rework"], base["supplier_changes"], base["stability"])

    def outcome(self, **kw):
        return rules.summarize(self.checklist(**kw))

    def test_clean_batch_is_releasable(self):
        s = self.outcome()
        self.assertEqual("releasable", s["outcome"])
        self.assertTrue(s["can_release"])
        self.assertFalse(s["can_conditional"])
        self.assertEqual([], s["missing"])

    def test_critical_deviation_always_blocks(self):
        s = self.outcome(deviations=[deviation(1, "critical", reason="有例外也不行", until=FUTURE)])
        self.assertEqual("blocked", s["outcome"])
        self.assertTrue(any("关键偏差" in m for m in s["missing"]))

    def test_minor_deviation_exception_window(self):
        s = self.outcome(deviations=[deviation(1, reason="影响可接受", until=FUTURE)])
        self.assertEqual("conditional", s["outcome"])
        self.assertTrue(s["can_conditional"])
        self.assertFalse(s["can_release"])
        s = self.outcome(deviations=[deviation(1, reason="影响可接受", until=PAST)])
        self.assertEqual("pending", s["outcome"])
        s = self.outcome(deviations=[deviation(1)])
        self.assertEqual("pending", s["outcome"])

    def test_failed_latest_test_blocks_until_retest_passes(self):
        self.assertEqual("blocked", self.outcome(tests=[test_row(1, passed=0)])["outcome"])
        s = self.outcome(tests=[test_row(1, passed=0), test_row(2, passed=1, rnd=2)])
        self.assertEqual("releasable", s["outcome"])

    def test_missing_tests_and_stability_are_pending(self):
        self.assertEqual("pending", self.outcome(tests=[])["outcome"])
        self.assertEqual("pending", self.outcome(stability=[])["outcome"])
        bad = [{"id": 1, "passed": 0, "condition": "40C/75RH", "timepoint": "6m", "result": 110, "spec_limit": 105}]
        self.assertEqual("blocked", self.outcome(stability=bad)["outcome"])

    def test_supplier_change_disposition(self):
        change = {"id": 1, "supplier": "供应商A", "change_type": "产地变更", "description": "d",
                  "disposition": None, "disposition_note": None}
        self.assertEqual("pending", self.outcome(supplier_changes=[change])["outcome"])
        self.assertEqual("blocked", self.outcome(supplier_changes=[dict(change, disposition="unacceptable")])["outcome"])
        self.assertEqual("releasable", self.outcome(supplier_changes=[dict(change, disposition="no_impact")])["outcome"])
        self.assertEqual("releasable", self.outcome(supplier_changes=[dict(change, disposition="acceptable", disposition_note="已评估")])["outcome"])

    def test_rework_pending_until_completed(self):
        rw = {"id": 1, "status": "planned", "description": "重新压片"}
        self.assertEqual("pending", self.outcome(rework=[rw])["outcome"])
        self.assertEqual("releasable", self.outcome(rework=[dict(rw, status="completed")])["outcome"])

    def test_deviation_close_blockers(self):
        msgs = rules.deviation_close_blockers([test_row(1, passed=0)], [])
        self.assertTrue(any("复测" in m for m in msgs))
        msgs = rules.deviation_close_blockers(
            [test_row(1, passed=0), test_row(2, passed=1, rnd=2)],
            [{"id": 1, "status": "planned", "description": "重新压片"}])
        self.assertEqual(1, len(msgs))
        self.assertIn("返工", msgs[0])
        self.assertEqual([], rules.deviation_close_blockers([test_row(1)], []))


if __name__ == "__main__": unittest.main()
