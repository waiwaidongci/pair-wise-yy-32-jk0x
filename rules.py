"""批次放行复核规则引擎。

只做纯计算：输入一个批次的全部记录，输出复核结论与缺项清单。
不接触数据库、HTTP 或全局时钟（``at`` 可注入，便于测试）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

# 结论：ready=可正式放行 / conditional=仅可有条件放行 / blocked=卡住
READY = "ready"
CONDITIONAL = "conditional"
BLOCKED = "blocked"

CRITICAL = "critical"
MINOR = "minor"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def exception_valid(deviation: dict[str, Any], at: datetime | None = None) -> bool:
    """一般偏差的例外是否有效：已批准、有理由、有效期尚未届满。关键偏差永远无效。"""
    if deviation.get("severity") == CRITICAL:
        return False
    until = parse_ts(deviation.get("exception_until"))
    return bool(deviation.get("exception_reason")) and until is not None and until > (at or utcnow())


# ---------------- 关闭偏差的前置条件 ----------------

def retest_evidence(tests: Iterable[dict[str, Any]]) -> bool:
    """是否存在“先失败、后复测合格”的证据：某检验项目早期轮次不合格，且最新轮次合格。"""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(tests, key=lambda r: (int(r["round"]), int(r["id"]))):
        by_type.setdefault(row["test_type"], []).append(row)
    for rounds in by_type.values():
        if len(rounds) >= 2 and any(not bool(r["passed"]) for r in rounds[:-1]) and bool(rounds[-1]["passed"]):
            return True
    return False


def closure_readiness(tests: Iterable[dict[str, Any]], reworks: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """偏差关闭前置：失败后的合格复测；已安排返工的还要等返工完成。"""
    reasons: list[str] = []
    has_retest = retest_evidence(tests)
    if not has_retest:
        reasons.append("缺少失败后的合格复测（需先有不合格轮次，其后复测合格）")
    pending = [r for r in reworks if r["status"] == "planned"]
    if pending:
        reasons.append("已安排返工但尚未完成，需等返工完成")
    return {"ready": not reasons, "reasons": reasons, "retest_ready": has_retest, "pending_rework": len(pending)}


# ---------------- 主评估 ----------------

def _latest_by(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: int(r["id"])):
        latest[row[key]] = row
    return latest


def evaluate(*, batch: dict[str, Any], deviations: list[dict[str, Any]], tests: list[dict[str, Any]],
             rework: list[dict[str, Any]], supplier_changes: list[dict[str, Any]],
             stability: list[dict[str, Any]], at: datetime | None = None) -> dict[str, Any]:
    """计算一张批次待办：分类列出资料项、缺项与总体放行结论。"""
    at = at or utcnow()
    items: list[dict[str, Any]] = []
    open_devs = [d for d in deviations if d["status"] == "open"]
    close = closure_readiness(tests, rework)

    # 1) 未关闭偏差：关键偏差始终挡住；一般偏差凭有效例外才允许有条件放行
    for d in open_devs:
        valid = exception_valid(d, at)
        if d["severity"] == CRITICAL:
            items.append({
                "key": f"deviation:{d['id']}", "category": "deviation", "critical": True,
                "title": d["title"], "detail": "关键偏差未关闭，始终阻止任何放行",
                "blocking": True, "conditional_ok": False, "exception": None,
                "close_ready": close["ready"], "close_reasons": close["reasons"],
            })
        else:
            items.append({
                "key": f"deviation:{d['id']}", "category": "deviation", "critical": False,
                "title": d["title"],
                "detail": (f"一般偏差未关闭；有效例外至 {d['exception_until']}，允许有条件放行"
                           if valid else "一般偏差未关闭且无有效例外，不能有条件放行"),
                "blocking": True, "conditional_ok": valid,
                "exception": {"until": d["exception_until"], "reason": d["exception_reason"],
                              "approved_by": d["exception_approved_by"]} if valid else None,
                "close_ready": close["ready"], "close_reasons": close["reasons"],
            })

    # 2) 最新检验：每个项目只看最新一轮，任一不合格或缺记录均为缺项
    if not tests:
        items.append({"key": "tests:none", "category": "test", "critical": False,
                      "title": "缺少检验记录", "detail": "放行前至少需要一项合格检验结果",
                      "blocking": True, "conditional_ok": False})
    else:
        for name, t in _latest_by(tests, "test_type").items():
            if not bool(t["passed"]):
                items.append({
                    "key": f"test:{name}", "category": "test", "critical": False,
                    "title": f"检验 {name} 第 {t['round']} 轮不合格",
                    "detail": f"最新结果 {t['result']} 超出限度 [{t['spec_min']}, {t['spec_max']}]，需合格复测",
                    "blocking": True, "conditional_ok": False})

    # 3) 返工：已安排未完成即缺项
    for r in rework:
        if r["status"] == "planned":
            items.append({"key": f"rework:{r['id']}", "category": "rework", "critical": False,
                          "title": f"返工未完成：{r['description']}", "detail": "返工已安排，需完成后才能放行/关闭偏差",
                          "blocking": True, "conditional_ok": False})

    # 4) 供应商变更：未做影响处置留在待办
    for sc in supplier_changes:
        if sc.get("disposition", "pending") != "assessed":
            items.append({"key": f"supplier:{sc['id']}", "category": "supplier", "critical": False,
                          "title": f"供应商变更未做影响处置：{sc['supplier']}（{sc['change_type']}）",
                          "detail": sc["description"], "blocking": True, "conditional_ok": False})

    # 5) 稳定性：每一考察条件只看最新时间点；无数据给提示但不硬挡
    if not stability:
        items.append({"key": "stability:none", "category": "stability", "critical": False,
                      "title": "暂无稳定性数据", "detail": "建议补充在效期稳定性考察结果",
                      "blocking": False, "conditional_ok": False, "warning": True})
    else:
        for cond, s in _latest_by(stability, "condition").items():
            if not bool(s["passed"]):
                items.append({"key": f"stability:{cond}", "category": "stability", "critical": False,
                              "title": f"稳定性 {cond} {s['timepoint']} 不合格",
                              "detail": f"结果 {s['result']} 超出限度 {s['spec_limit']}",
                              "blocking": True, "conditional_ok": False})

    hard = [i for i in items if i["blocking"] and not i["conditional_ok"]]
    soft = [i for i in items if i["blocking"] and i["conditional_ok"]]
    if hard:
        verdict = BLOCKED
    elif soft:
        verdict = CONDITIONAL
    else:
        verdict = READY

    return {
        "batch_id": batch["id"], "revision": batch["revision"], "verdict": verdict,
        "can_conditional": verdict == CONDITIONAL,
        "critical_blocked": any(i.get("critical") for i in hard),
        "blockers": [i["detail"] for i in items if i["blocking"]],
        "missing": items,  # 缺项/待办明细，页面按此展开
        "counts": {"open_deviations": len(open_devs), "blocking": len(hard) + len(soft),
                   "hard_blocking": len(hard), "warnings": len([i for i in items if i.get("warning")])},
        "closure": close,
    }
