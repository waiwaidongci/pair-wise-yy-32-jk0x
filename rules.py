"""批次放行复核规则：纯函数模块，不依赖数据库与 HTTP。

输入为记录（sqlite3.Row 或 dict），输出每批一张的待办清单与复核结论。
规则、记录、页面分离：本模块只放规则，存储见 app.py，展示见 static/index.html。
"""
from __future__ import annotations

from datetime import datetime, timezone

CATEGORIES = ("检验", "偏差", "稳定性", "供应商变更", "返工")

OUTCOME_LABELS = {
    "releasable": "可放行",
    "conditional": "可有条件放行",
    "pending": "待补齐",
    "blocked": "卡住",
}

DISPOSITION_LABELS = {"no_impact": "无影响", "acceptable": "可接受", "unacceptable": "不可接受"}


def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def exception_valid(deviation, now: datetime | None = None) -> bool:
    """一般偏差的例外当前是否有效：有批准理由且截止时间在将来。"""
    now = now or datetime.now(timezone.utc)
    if not deviation["exception_reason"]:
        return False
    until = _parse_ts(deviation["exception_until"])
    return bool(until and until > now)


def latest_tests_by_type(tests) -> dict:
    """每个检验项目只取最新一轮结果。"""
    latest: dict[str, object] = {}
    for row in sorted(tests, key=lambda r: r["id"]):
        latest[row["test_type"]] = row
    return latest


def _item(key: str, category: str, status: str, label: str, detail: str = "") -> dict:
    return {"key": key, "category": category, "status": status, "label": label, "detail": detail}


def build_checklist(batch, deviations, tests, rework, supplier_changes, stability, now: datetime | None = None) -> list[dict]:
    """每批一张待办：未关闭偏差、最新检验、稳定性、供应商变更、返工。

    状态取值：ok（已满足）、pending（待补齐）、blocker（阻塞放行）、
    conditional（一般偏差有有效例外，仅可有条件放行）。
    """
    now = now or datetime.now(timezone.utc)
    items: list[dict] = []

    latest = latest_tests_by_type(tests)
    if not latest:
        items.append(_item("tests.none", "检验", "pending", "尚无检验记录", "放行前至少需要一项检验结果"))
    else:
        failed = [t for t in latest.values() if not t["passed"]]
        if failed:
            for t in failed:
                items.append(_item(f"tests.failed.{t['test_type']}", "检验", "blocker",
                                   f"最新检验不合格：{t['test_type']}（第{t['round']}轮）",
                                   f"结果 {t['result']} 超出 {t['spec_min']}~{t['spec_max']}，需合格复测"))
        else:
            items.append(_item("tests.ok", "检验", "ok", f"最新检验全部合格（{len(latest)} 项）", "、".join(sorted(latest))))

    open_deviations = [d for d in deviations if d["status"] == "open"]
    for d in open_deviations:
        if d["severity"] == "critical":
            items.append(_item(f"deviation.{d['id']}", "偏差", "blocker",
                               f"关键偏差未关闭：{d['title']}", "关键偏差始终阻止放行"))
        elif exception_valid(d, now):
            items.append(_item(f"deviation.{d['id']}", "偏差", "conditional",
                               f"一般偏差未关闭但例外有效：{d['title']}",
                               f"例外至 {d['exception_until']}，仅可有条件放行"))
        else:
            reason = "无例外批准" if not d["exception_reason"] else "例外已过期或未生效"
            items.append(_item(f"deviation.{d['id']}", "偏差", "pending", f"一般偏差未关闭：{d['title']}", reason))
    if not open_deviations:
        items.append(_item("deviations.clear", "偏差", "ok", "无未关闭偏差"))

    if not stability:
        items.append(_item("stability.none", "稳定性", "pending", "缺少稳定性考察数据"))
    else:
        bad = [s for s in stability if not s["passed"]]
        if bad:
            for s in bad:
                items.append(_item(f"stability.{s['id']}", "稳定性", "blocker",
                                   f"稳定性考察不合格：{s['condition']} {s['timepoint']}",
                                   f"结果 {s['result']} 超限 {s['spec_limit']}"))
        else:
            items.append(_item("stability.ok", "稳定性", "ok", f"稳定性考察合格（{len(stability)} 条）"))

    for c in supplier_changes:
        disposition = c["disposition"]
        if not disposition:
            items.append(_item(f"supplier.{c['id']}", "供应商变更", "pending",
                               f"供应商变更未做影响处置：{c['supplier']}（{c['change_type']}）", c["description"]))
        elif disposition == "unacceptable":
            items.append(_item(f"supplier.{c['id']}", "供应商变更", "blocker",
                               f"供应商变更影响处置为不可接受：{c['supplier']}", c["disposition_note"] or ""))
        else:
            items.append(_item(f"supplier.{c['id']}", "供应商变更", "ok",
                               f"供应商变更已处置：{c['supplier']}（{DISPOSITION_LABELS.get(disposition, disposition)}）",
                               c["disposition_note"] or ""))
    if not supplier_changes:
        items.append(_item("supplier.none", "供应商变更", "ok", "无供应商变更"))

    planned = [r for r in rework if r["status"] == "planned"]
    for r in planned:
        items.append(_item(f"rework.{r['id']}", "返工", "pending", f"返工未完成：{r['description']}", "已安排返工，需等待返工完成"))
    if not planned:
        done = len([r for r in rework if r["status"] == "completed"])
        items.append(_item("rework.clear", "返工", "ok", "无未完成返工" + (f"（已完成 {done} 项）" if done else "")))

    return items


def summarize(items: list[dict]) -> dict:
    """把待办清单汇总成复核结论：卡住 / 待补齐 / 可有条件放行 / 可放行。"""
    blockers = [i for i in items if i["status"] == "blocker"]
    pendings = [i for i in items if i["status"] == "pending"]
    conditionals = [i for i in items if i["status"] == "conditional"]
    if blockers:
        outcome = "blocked"
    elif pendings:
        outcome = "pending"
    elif conditionals:
        outcome = "conditional"
    else:
        outcome = "releasable"
    return {
        "outcome": outcome,
        "can_release": outcome == "releasable",
        "can_conditional": outcome == "conditional",
        "counts": {"blocker": len(blockers), "pending": len(pendings), "conditional": len(conditionals)},
        "missing": [i["label"] for i in blockers + pendings + conditionals],
    }


def deviation_close_blockers(tests, rework) -> list[str]:
    """关闭偏差的前置条件：失败检验要有合格复测，已安排返工要等返工完成。"""
    messages: list[str] = []
    for t in latest_tests_by_type(tests).values():
        if not t["passed"]:
            messages.append(f"检验「{t['test_type']}」第{t['round']}轮仍不合格，需失败后的合格复测")
    for r in rework:
        if r["status"] == "planned":
            messages.append(f"返工「{r['description']}」未完成，需等待返工完成")
    return messages
