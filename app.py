#!/usr/bin/env python3
"""批次放行复核台：动作与 HTTP 编排层。

规则见 rules.py，记录见 records.py；本文件只负责鉴权、事务、版本号与接口。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import rules
from records import DB_PATH, Store, now


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


TERMINAL_STATES = {"released", "rejected"}


class BatchService:
    def __init__(self, store: Store):
        self.store, self.conn = store, store.conn

    # ---------- 通用辅助 ----------
    @staticmethod
    def _actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
        if not actor:
            raise ApiError(401, "缺少身份")
        if role not in allowed:
            raise ApiError(403, "角色无权执行此操作")
        return actor

    def _row(self, table: str, identity: int):
        try:
            return self.store.row(table, identity)
        except KeyError as exc:
            raise ApiError(404, str(exc)) from exc

    def _factory_check(self, actor: str, factory_id: int, batch=None) -> None:
        factory = self.conn.execute("SELECT * FROM factories WHERE id=?", (factory_id,)).fetchone()
        if not factory:
            raise ApiError(404, "工厂不存在")
        if batch is not None and int(batch["factory_id"]) != int(factory_id):
            raise ApiError(403, "不能修改其他工厂的批次")

    def _advance_batch(self, batch_id: int, expected_revision: int, next_state: str) -> None:
        batch = self._row("batches", batch_id)
        if batch["state"] in TERMINAL_STATES:
            raise ApiError(409, "终态批次不可修改")
        if int(expected_revision) != int(batch["revision"]):
            raise ApiError(409, "批次版本冲突：资料已被他人修改，请刷新后重试")
        cur = self.conn.execute("UPDATE batches SET state=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                                (next_state, now(), batch_id, expected_revision))
        if cur.rowcount != 1:
            raise ApiError(409, "并发更新冲突")
        self._supersede_reviews(batch_id, batch["revision"])

    def _supersede_reviews(self, batch_id: int, revision: int) -> None:
        """资料一改（revision 失效），该版本上的有效复核结论全部失效，留痕可查。"""
        self.conn.execute("""UPDATE reviews SET status='superseded', superseded_at=?
                             WHERE batch_id=? AND revision=? AND status='active'""",
                          (now(), batch_id, revision))

    def _active_review(self, batch_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM reviews WHERE batch_id=? AND status='active' ORDER BY id DESC LIMIT 1",
                                (batch_id,)).fetchone()
        return self._review_dict(row) if row else None

    @staticmethod
    def _review_dict(row) -> dict:
        return {"id": row["id"], "batch_id": row["batch_id"], "revision": row["revision"], "status": row["status"],
                "verdict": row["verdict"], "conclusion": row["conclusion"],
                "reviewed_by": row["reviewed_by"], "created_at": row["created_at"],
                "superseded_at": row["superseded_at"], "snapshot": json.loads(row["snapshot_json"])}

    @staticmethod
    def _rule_inputs(recs: dict) -> dict:
        return {k: recs[k] for k in ("batch", "deviations", "tests", "rework", "supplier_changes", "stability")}

    def evaluate(self, batch_id: int) -> dict:
        return rules.evaluate(**self._rule_inputs(self.store.batch_records(batch_id)))

    # ---------- 基础登记 ----------
    def register_factory(self, actor: str | None, role: str | None, code: str, name: str, country: str) -> dict:
        actor = self._actor(actor, role, {"qa"})
        if not code or not name:
            raise ApiError(400, "工厂代号和名称不能为空")
        try:
            with self.conn:
                cur = self.conn.execute("INSERT INTO factories(code,name,country) VALUES(?,?,?)", (code, name, country))
                self.store.audit(actor, "factory.register", "factory", cur.lastrowid, {"code": code})
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "工厂代号已存在") from exc
        return {"id": cur.lastrowid, "code": code, "name": name, "country": country}

    def create_batch(self, actor: str | None, role: str | None, factory_id: int, batch_no: str, product: str,
                     mfg_date: str, expiry_date: str) -> dict:
        actor = self._actor(actor, role, {"operator"})
        self._factory_check(actor, factory_id)
        if not batch_no.strip() or not product.strip() or expiry_date <= mfg_date:
            raise ApiError(400, "批号、产品或有效期不合法")
        stamp = now()
        try:
            with self.conn:
                cur = self.conn.execute("""INSERT INTO batches(factory_id,batch_no,product,mfg_date,expiry_date,state,created_by,created_at,updated_at)
                                         VALUES(?,?,?,?,?, 'manufactured',?,?,?)""",
                                        (factory_id, batch_no, product, mfg_date, expiry_date, actor, stamp, stamp))
                self.store.audit(actor, "batch.create", "batch", cur.lastrowid, {"factory_id": factory_id, "batch_no": batch_no})
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "该工厂批号已存在") from exc
        return self._batch_dict(self._row("batches", cur.lastrowid))

    # ---------- 偏差 ----------
    def add_deviation(self, actor, role, factory_id, batch_id, severity, title, due_at, expected_revision) -> dict:
        actor = self._actor(actor, role, {"operator", "inspector"})
        batch = self._row("batches", batch_id)
        self._factory_check(actor, factory_id, batch)
        if severity not in {"critical", "minor"} or not title.strip():
            raise ApiError(400, "偏差等级或描述不合法")
        if batch["state"] in TERMINAL_STATES:
            raise ApiError(409, "已终态批次不能新增偏差")
        with self.conn:
            cur = self.conn.execute("""INSERT INTO deviations(batch_id,severity,title,due_at,status,created_by,created_at)
                                     VALUES(?,?,?,?,'open',?,?)""", (batch_id, severity, title, due_at, actor, now()))
            self._advance_batch(batch_id, expected_revision, "investigation")
            self.store.audit(actor, "deviation.open", "deviation", cur.lastrowid, {"batch_id": batch_id, "severity": severity})
        return self._deviation_dict(self._row("deviations", cur.lastrowid))

    def close_deviation(self, actor, role, deviation_id, corrective_action, expected_revision) -> dict:
        actor = self._actor(actor, role, {"qa"})
        deviation = self._row("deviations", deviation_id)
        batch = self._row("batches", deviation["batch_id"])
        if deviation["status"] != "open":
            raise ApiError(409, "偏差已经关闭")
        if not corrective_action.strip():
            raise ApiError(400, "必须填写纠正措施")
        # 规则：关闭前必须有失败后的合格复测；已安排返工的还要等返工完成
        recs = self.store.batch_records(batch["id"])
        ready = rules.closure_readiness(recs["tests"], recs["rework"])
        if not ready["ready"]:
            raise ApiError(409, "偏差关闭前置未满足：" + "；".join(ready["reasons"]))
        with self.conn:
            self.conn.execute("UPDATE deviations SET status='closed',corrective_action=?,closed_by=?,closed_at=? WHERE id=? AND status='open'",
                              (corrective_action, actor, now(), deviation_id))
            self._advance_batch(batch["id"], expected_revision, batch["state"])
            self.store.audit(actor, "deviation.close", "deviation", deviation_id, {"batch_id": batch["id"]})
        return self._deviation_dict(self._row("deviations", deviation_id))

    def approve_exception(self, actor, role, deviation_id, reason, until, expected_revision) -> dict:
        actor = self._actor(actor, role, {"qa"})
        deviation = self._row("deviations", deviation_id)
        batch = self._row("batches", deviation["batch_id"])
        if deviation["severity"] == "critical":
            raise ApiError(409, "关键偏差不允许例外批准")
        if deviation["status"] != "open" or not reason.strip() or not rules.parse_ts(until) \
                or rules.parse_ts(until) <= rules.utcnow():
            raise ApiError(400, "例外原因或有效期不合法")
        with self.conn:
            self.conn.execute("UPDATE deviations SET exception_reason=?,exception_until=?,exception_approved_by=? WHERE id=?",
                              (reason, until, actor, deviation_id))
            self._advance_batch(batch["id"], expected_revision, batch["state"])
            self.store.audit(actor, "deviation.exception", "deviation", deviation_id, {"batch_id": batch["id"], "until": until})
        return self._deviation_dict(self._row("deviations", deviation_id))

    # ---------- 检验 / 稳定性 ----------
    def record_test(self, actor, role, factory_id, batch_id, test_type, result, spec_min, spec_max, expected_revision) -> dict:
        actor = self._actor(actor, role, {"lab"})
        batch = self._row("batches", batch_id)
        self._factory_check(actor, factory_id, batch)
        if not test_type.strip() or spec_min > spec_max:
            raise ApiError(400, "检验项目或标准不合法")
        if batch["state"] in TERMINAL_STATES:
            raise ApiError(409, "终态批次不能补录检验")
        round_no = self.conn.execute("SELECT COALESCE(MAX(round),0)+1 FROM tests WHERE batch_id=? AND test_type=?",
                                     (batch_id, test_type)).fetchone()[0]
        passed = int(spec_min <= result <= spec_max)
        with self.conn:
            cur = self.conn.execute("""INSERT INTO tests(batch_id,test_type,result,spec_min,spec_max,passed,round,recorded_by,created_at)
                                     VALUES(?,?,?,?,?,?,?,?,?)""",
                                    (batch_id, test_type, result, spec_min, spec_max, passed, round_no, actor, now()))
            next_state = "investigation" if (batch["state"] == "awaiting_resample" or not passed) else batch["state"]
            self._advance_batch(batch_id, expected_revision, next_state)
            self.store.audit(actor, "test.record", "batch", batch_id, {"test_type": test_type, "round": round_no, "passed": bool(passed)})
        return self._test_dict(self._row("tests", cur.lastrowid))

    def record_stability(self, actor, role, factory_id, batch_id, condition, timepoint, result, spec_limit, expected_revision) -> dict:
        actor = self._actor(actor, role, {"lab"})
        batch = self._row("batches", batch_id)
        self._factory_check(actor, factory_id, batch)
        passed = int(result <= spec_limit)
        with self.conn:
            cur = self.conn.execute("""INSERT INTO stability(batch_id,condition,timepoint,result,spec_limit,passed,recorded_by,created_at)
                                     VALUES(?,?,?,?,?,?,?,?)""",
                                    (batch_id, condition, timepoint, result, spec_limit, passed, actor, now()))
            self._advance_batch(batch_id, expected_revision, batch["state"])
            self.store.audit(actor, "stability.record", "batch", batch_id, {"condition": condition, "timepoint": timepoint, "passed": bool(passed)})
        return dict(self._row("stability", cur.lastrowid))

    # ---------- 返工 ----------
    def plan_rework(self, actor, role, factory_id, batch_id, description, expected_revision) -> dict:
        actor = self._actor(actor, role, {"operator"})
        batch = self._row("batches", batch_id)
        self._factory_check(actor, factory_id, batch)
        if batch["state"] in TERMINAL_STATES:
            raise ApiError(409, "终态批次不能返工")
        with self.conn:
            cur = self.conn.execute("INSERT INTO rework(batch_id,description,status,created_by,created_at) VALUES(?,?,'planned',?,?)",
                                    (batch_id, description, actor, now()))
            self._advance_batch(batch_id, expected_revision, "investigation")
            self.store.audit(actor, "rework.plan", "rework", cur.lastrowid, {"batch_id": batch_id})
        return dict(self._row("rework", cur.lastrowid))

    def complete_rework(self, actor, role, factory_id, rework_id, expected_revision) -> dict:
        actor = self._actor(actor, role, {"operator"})
        row = self._row("rework", rework_id)
        batch = self._row("batches", row["batch_id"])
        self._factory_check(actor, factory_id, batch)
        if row["status"] != "planned":
            raise ApiError(409, "返工记录已经完成")
        with self.conn:
            self.conn.execute("UPDATE rework SET status='completed',completed_by=?,completed_at=? WHERE id=?", (actor, now(), rework_id))
            self._advance_batch(batch["id"], expected_revision, batch["state"])
            self.store.audit(actor, "rework.complete", "rework", rework_id, {"batch_id": batch["id"]})
        return dict(self._row("rework", rework_id))

    # ---------- 供应商变更 ----------
    def record_supplier_change(self, actor, role, factory_id, batch_id, supplier, change_type, description, expected_revision) -> dict:
        actor = self._actor(actor, role, {"operator", "qa"})
        batch = self._row("batches", batch_id)
        self._factory_check(actor, factory_id, batch)
        with self.conn:
            cur = self.conn.execute("""INSERT INTO supplier_changes(batch_id,supplier,change_type,description,recorded_by,created_at)
                                     VALUES(?,?,?,?,?,?)""", (batch_id, supplier, change_type, description, actor, now()))
            self._advance_batch(batch_id, expected_revision, "investigation")
            self.store.audit(actor, "supplier_change.record", "batch", batch_id, {"supplier": supplier, "change_type": change_type})
        return self._supplier_dict(self._row("supplier_changes", cur.lastrowid))

    def dispose_supplier_change(self, actor, role, factory_id, change_id, impact_assessment, expected_revision) -> dict:
        actor = self._actor(actor, role, {"operator", "qa"})
        row = self._row("supplier_changes", change_id)
        batch = self._row("batches", row["batch_id"])
        self._factory_check(actor, factory_id, batch)
        if row["disposition"] == "assessed":
            raise ApiError(409, "该供应商变更已完成影响处置")
        if not impact_assessment.strip():
            raise ApiError(400, "必须填写影响评估/处置结论")
        with self.conn:
            self.conn.execute("""UPDATE supplier_changes SET disposition='assessed',impact_assessment=?,disposed_by=?,disposed_at=? WHERE id=?""",
                              (impact_assessment, actor, now(), change_id))
            self._advance_batch(batch["id"], expected_revision, batch["state"])
            self.store.audit(actor, "supplier_change.dispose", "supplier_change", change_id, {"batch_id": batch["id"]})
        return self._supplier_dict(self._row("supplier_changes", change_id))

    # ---------- 复核留痕 ----------
    def record_review(self, actor, role, batch_id, conclusion) -> dict:
        actor = self._actor(actor, role, {"qa"})
        batch = self._row("batches", batch_id)
        if batch["state"] in TERMINAL_STATES:
            raise ApiError(409, "终态批次无需再复核")
        if not conclusion.strip():
            raise ApiError(400, "必须填写复核结论")
        evaluation = rules.evaluate(**self._rule_inputs(self.store.batch_records(batch_id)))
        with self.conn:
            self._supersede_reviews(batch_id, batch["revision"])
            cur = self.conn.execute("""INSERT INTO reviews(batch_id,revision,status,verdict,conclusion,snapshot_json,reviewed_by,created_at)
                                     VALUES(?,?,'active',?,?,?,?,?)""",
                                    (batch_id, batch["revision"], evaluation["verdict"], conclusion,
                                     json.dumps(evaluation, ensure_ascii=False, sort_keys=True), actor, now()))
            self.store.audit(actor, "review.record", "review", cur.lastrowid,
                             {"batch_id": batch_id, "revision": batch["revision"], "verdict": evaluation["verdict"]})
        return self._review_dict(self._row("reviews", cur.lastrowid))

    def review_history(self, batch_id: int) -> dict:
        self._row("batches", batch_id)
        rows = self.conn.execute("SELECT * FROM reviews WHERE batch_id=? ORDER BY id DESC", (batch_id,)).fetchall()
        return {"batch_id": batch_id, "active": self._active_review(batch_id), "reviews": [self._review_dict(r) for r in rows]}

    # ---------- 放行决定 ----------
    def decide(self, actor, role, batch_id, decision, rationale, expected_revision, exception_code="") -> dict:
        actor = self._actor(actor, role, {"qa"})
        batch = self._row("batches", batch_id)
        if decision not in {"release", "reject", "conditional", "resample"}:
            raise ApiError(400, "放行决定不合法")
        if batch["state"] in TERMINAL_STATES:
            raise ApiError(409, "批次已经是终态")
        if int(expected_revision) != int(batch["revision"]):
            raise ApiError(409, "批次已被其他工厂或质量人员修改，请刷新版本")
        if not rationale.strip():
            raise ApiError(400, "必须填写决定依据")

        evaluation = rules.evaluate(**self._rule_inputs(self.store.batch_records(batch_id)))
        active = self._active_review(batch_id)
        if decision in {"release", "conditional"}:
            if evaluation["verdict"] == rules.BLOCKED:
                detail = "；".join(evaluation["blockers"][:3]) or "仍有缺项"
                raise ApiError(409, f"复核台未通过（卡住）：{detail}")
            if not active or int(active["revision"]) != int(batch["revision"]):
                raise ApiError(409, "缺少当前资料版本的有效复核结论：资料已更新，原复核已失效，请按新版本重新复核")
        if decision == "resample":
            if batch["state"] == "conditional":
                raise ApiError(409, "有条件放行后不能直接改为再取样")
            new_state = "awaiting_resample"
        elif decision == "reject":
            new_state = "rejected"
        elif decision == "release":
            if evaluation["verdict"] != rules.READY:
                raise ApiError(409, "仍有挂起的一般偏差，只能有条件放行，或先关闭偏差")
            new_state = "released"
        else:  # conditional
            if evaluation["verdict"] != rules.CONDITIONAL:
                raise ApiError(409, "当前没有凭有效例外挂起的一般偏差，不应作有条件放行")
            if not exception_code.strip():
                raise ApiError(400, "有条件放行必须提供例外编号")
            new_state = "conditional"
        with self.conn:
            cur = self.conn.execute("""INSERT INTO decisions(batch_id,revision,decision,rationale,exception_code,decided_by,created_at)
                                     VALUES(?,?,?,?,?,?,?)""",
                                    (batch_id, batch["revision"], decision, rationale, exception_code or None, actor, now()))
            updated = self.conn.execute("UPDATE batches SET state=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                                        (new_state, now(), batch_id, expected_revision))
            if updated.rowcount != 1:
                raise ApiError(409, "并发放行冲突")
            self._supersede_reviews(batch_id, batch["revision"])
            self.store.audit(actor, "batch.decision", "batch", batch_id,
                             {"decision": decision, "revision": batch["revision"], "state": new_state})
        return {"decision": dict(self._row("decisions", cur.lastrowid)), "batch": self.batch_detail(batch_id)["batch"]}

    # ---------- 查询 ----------
    def batch_detail(self, batch_id: int) -> dict:
        recs = self.store.batch_records(batch_id)
        batch = recs["batch"]
        factory = self.conn.execute("SELECT code,name FROM factories WHERE id=?", (batch["factory_id"],)).fetchone()
        batch["factory_code"] = factory["code"] if factory else None
        evaluation = rules.evaluate(**self._rule_inputs(recs))
        active = self._active_review(batch_id)
        return {**recs, "batch": self._batch_dict(batch), "evaluation": evaluation,
                "active_review": None if not active else {k: active[k] for k in ("id", "revision", "verdict", "conclusion", "reviewed_by", "created_at", "superseded_at")}}

    def desk(self, verdict: str | None = None, status: str | None = None, product: str | None = None,
             factory_id: int | None = None) -> dict:
        """每批一张待办：附最新复核结论、按规则重算的缺项；支持筛出卡住批次。"""
        where, params = [], []
        if status:
            where.append("state=?"); params.append(status)
        if factory_id:
            where.append("factory_id=?"); params.append(factory_id)
        if product:
            where.append("product LIKE ?"); params.append(f"%{product}%")
        sql = "SELECT * FROM batches" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC"
        cards = []
        for b in self.conn.execute(sql, params):
            detail = self.batch_detail(b["id"])
            ev = detail["evaluation"]
            if verdict and ev["verdict"] != verdict:
                continue
            last_row = self.conn.execute("SELECT * FROM reviews WHERE batch_id=? ORDER BY id DESC LIMIT 1", (b["id"],)).fetchone()
            cards.append({"batch": detail["batch"], "evaluation": ev, "active_review": detail["active_review"],
                          "last_review": None if not last_row else self._review_brief(last_row),
                          "missing": ev["missing"], "latest_tests": self._latest_tests(detail["tests"]),
                          "latest_stability": self._latest_tests(detail["stability"], "condition")})
        stats = {"total": len(cards), "ready": 0, "conditional": 0, "blocked": 0,
                 "review_stale": 0, "terminal": 0}
        for c in cards:
            stats[c["evaluation"]["verdict"]] += 1
            b, lr = c["batch"], c["last_review"]
            if b["state"] in TERMINAL_STATES:
                stats["terminal"] += 1
            if lr and (lr["status"] != "active" or int(lr["revision"]) != int(b["revision"])):
                stats["review_stale"] += 1
        return {"generated_at": now(), "stats": stats, "cards": cards}

    @staticmethod
    def _review_brief(row) -> dict:
        return {"id": row["id"], "revision": row["revision"], "status": row["status"],
                "verdict": row["verdict"], "conclusion": row["conclusion"],
                "reviewed_by": row["reviewed_by"], "created_at": row["created_at"],
                "superseded_at": row["superseded_at"]}

    @staticmethod
    def _latest_tests(rows: list[dict], key: str = "test_type") -> list[dict]:
        latest: dict[str, dict] = {}
        for row in sorted(rows, key=lambda r: int(r["id"])):
            latest[row[key]] = row
        return list(latest.values())

    @staticmethod
    def _batch_dict(row) -> dict:
        out = {"id": row["id"], "factory_id": row["factory_id"], "batch_no": row["batch_no"], "product": row["product"],
               "mfg_date": row["mfg_date"], "expiry_date": row["expiry_date"], "state": row["state"], "revision": row["revision"]}
        if "factory_code" in row.keys():
            out["factory_code"] = row["factory_code"]
        return out

    @staticmethod
    def _deviation_dict(row) -> dict:
        return {"id": row["id"], "batch_id": row["batch_id"], "severity": row["severity"], "title": row["title"], "due_at": row["due_at"],
                "status": row["status"], "corrective_action": row["corrective_action"], "exception_reason": row["exception_reason"],
                "exception_until": row["exception_until"], "exception_approved_by": row["exception_approved_by"],
                "closed_by": row["closed_by"], "closed_at": row["closed_at"]}

    @staticmethod
    def _test_dict(row) -> dict:
        return {"id": row["id"], "batch_id": row["batch_id"], "test_type": row["test_type"], "result": row["result"],
                "spec_min": row["spec_min"], "spec_max": row["spec_max"], "passed": bool(row["passed"]), "round": row["round"]}

    @staticmethod
    def _supplier_dict(row) -> dict:
        return {"id": row["id"], "batch_id": row["batch_id"], "supplier": row["supplier"], "change_type": row["change_type"],
                "description": row["description"], "disposition": row["disposition"], "impact_assessment": row["impact_assessment"],
                "disposed_by": row["disposed_by"], "disposed_at": row["disposed_at"]}

    def state(self) -> dict:
        return {"factories": [dict(row) for row in self.conn.execute("SELECT * FROM factories ORDER BY id")],
                "batches": [self._batch_dict(row) for row in self.conn.execute("SELECT * FROM batches ORDER BY id DESC")],
                "audits": [dict(row) for row in self.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 30")]}

    # ---------- 演示数据 ----------
    def seed(self) -> None:
        if self.conn.execute("SELECT id FROM factories LIMIT 1").fetchone():
            return
        fid = self.register_factory("qa-demo", "qa", "F-DEMO", "演示工厂", "CN")["id"]
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds").replace("+00:00", "Z")

        def rev(bid: int) -> int:
            return self.batch_detail(bid)["batch"]["revision"]

        def make(no: str, product: str) -> int:
            return self.create_batch("operator", "operator", fid, no, product, "2026-06-01", "2028-06-01")["id"]

        # B1：完整闭环（失败→复测合格→关闭偏差→供应商处置→复核→放行），旧复核已被新版本取代可查
        bid = make("B2026-001", "片剂-A")
        self.record_test("lab", "lab", fid, bid, "含量", 88.4, 95, 105, rev(bid))
        self.record_test("lab", "lab", fid, bid, "含量", 99.2, 95, 105, rev(bid))
        dev = self.add_deviation("inspector", "inspector", fid, bid, "minor", "含量首轮低于限度", future, rev(bid))
        self.record_review("qa", "qa", bid, "首轮失败，等待复测与调查结论")
        self.close_deviation("qa", "qa", dev["id"], "复测合格，确认为混合时间不足，已再验证工艺", rev(bid))
        sc = self.record_supplier_change("operator", "operator", fid, bid, "鑫源辅料", "原料药供应商变更", "新产地供应商，批次首次使用", rev(bid))
        self.dispose_supplier_change("qa", "qa", fid, sc["id"], "已完成小试与稳定性桥接，质量协议齐备，评估可接受", rev(bid))
        self.record_stability("lab", "lab", fid, bid, "25C/60RH", "3M", 99.0, 105, rev(bid))
        self.record_review("qa", "qa", bid, "缺项关闭，复测合格，建议正式放行")
        self.decide("qa", "qa", bid, "release", "复核通过：偏差关闭、复测合格、供应商变更已处置、稳定性合格", rev(bid))

        # B2：关键偏差未关闭，始终挡住
        bid = make("B2026-002", "注射液-B")
        self.record_test("lab", "lab", fid, bid, "无菌", 0, 0, 0, rev(bid))
        self.add_deviation("inspector", "inspector", fid, bid, "critical", "无菌检查阳性", future, rev(bid))
        self.record_review("qa", "qa", bid, "关键偏差调查中，任何放行均被阻止")

        # B3：一般偏差 + 有效例外 → 可有条件放行
        bid = make("B2026-003", "胶囊-C")
        self.record_test("lab", "lab", fid, bid, "含量", 100.1, 95, 105, rev(bid))
        dev = self.add_deviation("inspector", "inspector", fid, bid, "minor", "标签打印轻微偏移", future, rev(bid))
        self.approve_exception("qa", "qa", dev["id"], "不影响可追溯性，限定下次印刷前整改", future, rev(bid))
        self.record_review("qa", "qa", bid, "凭有效例外 EX-2026-007，建议有条件放行")

        # B4：检验失败后缺合格复测，偏差无法关闭
        bid = make("B2026-004", "颗粒-D")
        self.record_test("lab", "lab", fid, bid, "溶出度", 72, 80, 120, rev(bid))
        self.add_deviation("operator", "operator", fid, bid, "minor", "溶出度首轮不合格", future, rev(bid))
        self.record_stability("lab", "lab", fid, bid, "25C/60RH", "0M", 98, 105, rev(bid))

        # B5：复测合格但返工未完成，偏差不能关
        bid = make("B2026-005", "软膏-E")
        self.record_test("lab", "lab", fid, bid, "含量", 90, 95, 105, rev(bid))
        self.record_test("lab", "lab", fid, bid, "含量", 100, 95, 105, rev(bid))
        dev = self.add_deviation("operator", "operator", fid, bid, "minor", "包装规格混装", future, rev(bid))
        self.plan_rework("operator", "operator", fid, bid, "拆包重新分装并复检", rev(bid))
        self.record_review("qa", "qa", bid, "返工已安排未完成，偏差暂不能关闭")

        # B6：供应商变更未做影响处置
        bid = make("B2026-006", "片剂-A")
        self.record_test("lab", "lab", fid, bid, "水分", 1.8, 0, 5, rev(bid))
        self.record_supplier_change("operator", "operator", fid, bid, "博润包材", "内包材供应商变更", "铝箔供应商由 A 改为 B，尚无评估", rev(bid))

        # B7：稳定性最新时间点不合格
        bid = make("B2026-007", "糖浆-F")
        self.record_test("lab", "lab", fid, bid, "含量", 98, 95, 105, rev(bid))
        self.record_stability("lab", "lab", fid, bid, "40C/75RH", "1M", 100, 105, rev(bid))
        self.record_stability("lab", "lab", fid, bid, "40C/75RH", "3M", 108, 105, rev(bid))


class Handler(BaseHTTPRequestHandler):
    service: BatchService

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        try:
            return json.loads(self.rfile.read(size)) if size else {}
        except json.JSONDecodeError as exc:
            raise ApiError(400, "JSON 请求体无效") from exc

    def _parts(self) -> list[str]:
        return [p for p in urlparse(self.path).path.strip("/").split("/") if p]

    def _static(self, name: str) -> None:
        path = (Path(__file__).parent / "static" / name)
        if not path.is_file():
            self._send(404, {"error": "页面资源不存在"}); return
        ctype = "text/html; charset=utf-8" if name.endswith(".html") else "text/javascript; charset=utf-8"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        try:
            parsed, p, q = urlparse(self.path), self._parts(), parse_qs(urlparse(self.path).query)
            if p in (["health"], ["api", "health"]):
                out = {"status": "ok"}
            elif p == ["api", "state"]:
                out = self.service.state()
            elif p == ["api", "desk"]:
                out = self.service.desk(q.get("verdict", [None])[0], q.get("status", [None])[0],
                                        q.get("product", [None])[0],
                                        int(q["factory_id"][0]) if q.get("factory_id") else None)
            elif len(p) == 3 and p[:2] == ["api", "batches"]:
                out = self.service.batch_detail(int(p[2]))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "reviews":
                out = self.service.review_history(int(p[2]))
            elif not p:
                self._static("index.html"); return
            elif p[:1] == ["static"] and len(p) == 2:
                self._static(p[1]); return
            else:
                raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message})
        except Exception as exc:  # 原型：直接回显便于排查
            self._send(500, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            p, b = self._parts(), self._body()
            actor, role = self.headers.get("X-Actor"), self.headers.get("X-Role")
            S = self.service
            if p == ["api", "factories"]:
                out = S.register_factory(actor, role, b.get("code", ""), b.get("name", ""), b.get("country", ""))
            elif p == ["api", "batches"]:
                out = S.create_batch(actor, role, int(b.get("factory_id", 0)), b.get("batch_no", ""), b.get("product", ""),
                                     b.get("mfg_date", ""), b.get("expiry_date", ""))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "deviations":
                out = S.add_deviation(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("severity", ""),
                                      b.get("title", ""), b.get("due_at"), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "deviations"] and p[3] == "close":
                out = S.close_deviation(actor, role, int(p[2]), b.get("corrective_action", ""), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "deviations"] and p[3] == "exception":
                out = S.approve_exception(actor, role, int(p[2]), b.get("reason", ""), b.get("until", ""),
                                          int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "tests":
                out = S.record_test(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("test_type", ""),
                                    float(b.get("result", 0)), float(b.get("spec_min", 0)), float(b.get("spec_max", 0)),
                                    int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "rework":
                out = S.plan_rework(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("description", ""),
                                    int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "rework"] and p[3] == "complete":
                out = S.complete_rework(actor, role, int(b.get("factory_id", 0)), int(p[2]), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "supplier-changes":
                out = S.record_supplier_change(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("supplier", ""),
                                               b.get("change_type", ""), b.get("description", ""), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "supplier-changes"] and p[2].isdigit() and p[3] == "dispose":
                out = S.dispose_supplier_change(actor, role, int(b.get("factory_id", 0)), int(p[2]),
                                                b.get("impact_assessment", ""), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "stability":
                out = S.record_stability(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("condition", ""),
                                         b.get("timepoint", ""), float(b.get("result", 0)), float(b.get("spec_limit", 0)),
                                         int(b.get("expected_revision", -1)))
            elif p == ["api", "reviews"]:
                out = S.record_review(actor, role, int(b.get("batch_id", 0)), b.get("conclusion", ""))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "decide":
                out = S.decide(actor, role, int(p[2]), b.get("decision", ""), b.get("rationale", ""),
                               int(b.get("expected_revision", -1)), b.get("exception_code", ""))
            else:
                raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message})
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            self._send(500, {"error": str(exc)})


def run(port: int, db_path: str, seed: bool) -> None:
    store = Store(db_path)
    service = BatchService(store)
    if seed:
        service.seed()
    Handler.service = service
    print(f"batch release review desk on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8214)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    if args.init:
        Store(args.db).close()
    if args.seed or not args.init:
        run(args.port, args.db, args.seed)


if __name__ == "__main__":
    main()
