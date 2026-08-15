#!/usr/bin/env python3
"""Skill 回归评估集执行器 —— Skill 发布的门禁。

    python tools/run_evals.py            # 跑全部可执行的评估集
    python tools/run_evals.py RiskGate   # 只跑指定 Skill

「eval/ 全绿才可发布」这句话必须能被执行，否则只是文档承诺。本脚本读取
``skills/<name>/eval/cases.jsonl``，对已接入执行器的 Skill 真实跑一遍并断言结果。

尚未接入执行器的 Skill 会被如实报告为 SKIPPED，不计入通过数——
我们不把「没跑」粉饰成「通过」。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.registry import MCPClient  # noqa: E402
from mcp_servers.world import World  # noqa: E402
from poc.creditsentry import gate, llm, querying, skills  # noqa: E402
from poc.creditsentry.ledger import EvidenceLedger  # noqa: E402
from poc.creditsentry.tracing import Tracer  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.join(ROOT, "skills")


def _ctx(caller: str):
    world = World.load("case_001")
    tracer = Tracer()
    mcp = MCPClient(world, tracer)
    return skills.Context(tracer, EvidenceLedger("EVAL"), mcp, world,
                          llm.get_llm("stub"), caller=caller)


# ---------------------------------------------------------------------------
# 各 Skill 的执行器：把评估用例喂给真实实现并断言
# ---------------------------------------------------------------------------

def run_riskgate(case: dict) -> tuple[bool, str]:
    d = gate.evaluate(**case["input"])
    want = case.get("expect_tier")
    if want is None:                       # 该组合不在不变量断言范围内
        return True, f"{d.action_tier}（{d.rule_id}，不变量外）"
    ok = d.action_tier == want
    return ok, f"期望 {want}，实际 {d.action_tier}（{d.rule_id}）"


def run_signalfusion(case: dict) -> tuple[bool, str]:
    ctx = _ctx("signal-hub")
    out = skills.signal_fusion(ctx, case["input"]["signals"])
    exp = case["expect"]
    checks = [
        (len(out["kept"]) == exp["kept_count"],
         f"kept {len(out['kept'])}≠{exp['kept_count']}"),
        (len(out["dropped"]) == exp["dropped_count"],
         f"dropped {len(out['dropped'])}≠{exp['dropped_count']}"),
        (all(d.get("drop_reason") for d in out["dropped"]) if exp["all_drops_have_reason"] else True,
         "存在无理由的丢弃（静默漏丢）"),
    ]
    bad = [m for ok, m in checks if not ok]
    return not bad, "；".join(bad) or "通过"


def run_evidenceledger(case: dict) -> tuple[bool, str]:
    led = EvidenceLedger("EVAL")
    i = case["input"]
    if i["extracted"].get("gap"):
        ev = led.record_gap(subject_id="S", fact_type="x", why=i["extracted"]["why"])
    else:
        ev = led.record(subject_id="S", source_system=i["source_system"],
                        fact_type="x", raw_content="raw", extracted=i["extracted"])
    want = case["expect"]["level"]
    return ev.level == want, f"期望 {want}，实际 {ev.level}（{ev.level_reason}）"


def run_litigationprobe(case: dict) -> tuple[bool, str]:
    got = llm.is_material_case(case["input"])
    want = case["expect"]["material"]
    return got == want, f"期望 material={want}，实际 {got}"


def run_queryrewrite(case: dict) -> tuple[bool, str]:
    """校验六维改写的触发/不触发条件与澄清渠道选路。改写是纯函数，可脱离案件独立回归。"""
    i = case["input"]
    facts: dict = {}
    if i.get("ambiguous") or i.get("partial"):
        facts["litigation"] = {"ambiguous": i.get("ambiguous"), "partial": i.get("partial")}
    if i.get("unbased_distress"):
        facts["guarantee"] = {"distressed_parties": [{"party": "X", "status_basis": ""}]}
    if i.get("undersampled"):
        facts["transaction"] = {"undersampled": True}
    if i.get("facts_blob"):
        facts["blob"] = i["facts_blob"]

    plan = querying.rewrite(caller="eval", base_query=i["base_query"], stance=i["stance"],
                            signal_types=i.get("signal_types", []), facts=facts,
                            as_of=i.get("as_of"))
    exp = case["expect"]
    dims = set(plan.dimensions_used)
    bad: list[str] = []

    for d in exp.get("has_dimensions", []):
        if d not in dims:
            bad.append(f"缺少维度 {d}（实际 {sorted(dims)}）")
    for d in exp.get("lacks_dimensions", []):
        if d in dims:
            bad.append(f"不应出现维度 {d}")
    if (want := exp.get("text_contains")):
        if not any(want in q.text for q in plan.subqueries):
            bad.append(f"无子查询包含 {want!r}")
    if exp.get("exact_filter") and not any(q.filters.get("exact") for q in plan.subqueries):
        bad.append("条款直查维未设置 exact 过滤")
    if (before := exp.get("all_filtered_before")):
        off = [q.dimension for q in plan.subqueries
               if q.filters.get("effective_before") != before]
        if off:
            bad.append(f"子查询 {off} 未带 effective_before={before}")
    if (ch := exp.get("clarification_channel")):
        got = [c for c in plan.clarifications if c.channel == ch]
        if not got:
            bad.append(f"未产生 {ch} 渠道的澄清"
                       f"（实际 {[c.channel for c in plan.clarifications]}）")
        else:
            c = got[0]
            if "blocking" in exp and c.blocking != exp["blocking"]:
                bad.append(f"blocking 期望 {exp['blocking']}，实际 {c.blocking}")
            if exp.get("has_options") and not c.options:
                bad.append("选项式提问却没有给选项——开放问题的回答无法结构化")
    return not bad, "；".join(bad) or "通过"


def run_policyrag(case: dict) -> tuple[bool, str]:
    """核心断言：**生效日晚于案件时点的条款一律不得被召回**。

    这是知识维度的前视污染——与把后来才披露的证据放进决策时点是同一类错误，
    只不过更隐蔽，因为条款看起来「一直都在」。
    """
    i = case["input"]
    ctx = _ctx("risk-analyst")
    plan = querying.rewrite(caller="risk-analyst", base_query=i["query"],
                            stance=i.get("stance", querying.PROVE),
                            signal_types=i.get("signal_types", []),
                            as_of=i.get("as_of"))
    hits = skills.policy_rag(ctx, plan, top_k=i.get("top_k", 5))
    exp = case["expect"]
    bad: list[str] = []

    real = [h for h in hits if h.get("doc_id")]
    if (as_of := i.get("as_of")):
        late = [f"{h['doc_id']}@{h['effective_date']}" for h in real
                if h["effective_date"] > as_of]
        if late:
            bad.append(f"召回了案件时点后才生效的条款：{late}")
    if "min_hits" in exp and len(real) < exp["min_hits"]:
        bad.append(f"命中 {len(real)} 条，少于期望的 {exp['min_hits']}")
    if exp.get("empty_with_reason"):
        if real or not hits[0].get("why"):
            bad.append("空集时应返回 no_policy_matched 并说明原因")
    if (doc := exp.get("must_include")) and doc not in {h.get("doc_id") for h in real}:
        bad.append(f"未召回应命中的 {doc}")
    if (doc := exp.get("must_exclude")) and doc in {h.get("doc_id") for h in real}:
        bad.append(f"召回了不应命中的 {doc}")
    return not bad, "；".join(bad) or f"通过（{len(real)} 条命中）"


def run_guaranteeprobe(case: dict) -> tuple[bool, str]:
    """校验担保台账 → 净未覆盖代偿敞口 → 置信度这条链的算术与边界。

    被测对象是两个纯函数（``summarize_guarantee`` 与 ``_contagion_confidence``），
    不依赖 MCP 与账本，因此可以脱离案件独立回归。
    """
    i = case["input"]
    entries = [] if i.get("empty") else [{
        "guaranteed_party": "被担保方",
        "outstanding": i["outstanding"],
        "party_status": i["party_status"],
        "mitigations": [{"type": "缓释合计", "amount": i["mitigations"]}],
    }]
    got = skills.summarize_guarantee(entries, i.get("direct_exposure"))
    got["has_guarantee"] = bool(entries)
    got["confidence"] = llm._contagion_confidence(got)

    bad: list[str] = []
    for key, want in case["expect"].items():
        actual = got.get(key)
        ok = (abs(actual - want) < 1e-6) if isinstance(want, float) and isinstance(
            actual, (int, float)) else actual == want
        if not ok:
            bad.append(f"{key}: 期望 {want}，实际 {actual}")
    return not bad, "；".join(bad) or "通过"


def run_safedisposition(case: dict) -> tuple[bool, str]:
    ctx = _ctx("disposition-executor")
    i = case["input"]
    order = {
        "action": i["action"],
        "params": {"subject_id": "SUB-330100-88217", "new_limit": 4_000_000,
                   "guarantee_type": "实控人连带责任担保"},
        "action_tier": i["action_tier"],
        "idempotency_key": "eval-key-1",
        "rollback_point_id": None,
        "approval_token": i.get("approval_token"),
    }
    out = skills.safe_disposition(ctx, order)
    if i.get("replay"):
        order["approval_token"] = "apv-eval"
        skills.safe_disposition(ctx, order)
        out = skills.safe_disposition(ctx, order)
        ok = out.get("idempotent_replay") is True
        return ok, f"幂等重放标记 = {out.get('idempotent_replay')}"

    exp = case["expect"]
    if "status" in exp:
        ok = out["status"] == exp["status"]
        msg = f"期望 {exp['status']}，实际 {out['status']}"
        if ok and "error_code" in exp:
            ok = out.get("error_code") == exp["error_code"]
            msg += f"；期望错误码 {exp['error_code']}，实际 {out.get('error_code')}"
        return ok, msg
    return True, "无断言"


RUNNERS = {
    "RiskGate": run_riskgate,
    "SignalFusion": run_signalfusion,
    "EvidenceLedger": run_evidenceledger,
    "LitigationProbe": run_litigationprobe,
    "QueryRewrite": run_queryrewrite,
    "PolicyRag": run_policyrag,
    "GuaranteeProbe": run_guaranteeprobe,
    "SafeDisposition": run_safedisposition,
}


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    print("信衡 CreditSentry · Skill 回归评估集")
    print("=" * 76)

    total_run = total_pass = 0
    skipped: list[str] = []
    failures: list[str] = []

    for name, meta in skills.REGISTRY.items():
        if only and name != only:
            continue
        path = os.path.join(SKILLS_DIR, name, "eval", "cases.jsonl")
        cases = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                cases = [json.loads(line) for line in f if line.strip()]

        runner = RUNNERS.get(name)
        if runner is None or not cases:
            reason = "无执行器" if runner is None else "评估集为空"
            skipped.append(f"{name}（{reason}）")
            print(f"  跳过    {name:20} v{meta.version:<8} {reason}")
            continue

        passed = 0
        first_fail = ""
        for c in cases:
            ok, msg = runner(c)
            if ok:
                passed += 1
            elif not first_fail:
                first_fail = f"{c.get('desc') or c.get('invariant') or ''} → {msg}"
        total_run += len(cases)
        total_pass += passed
        status = "通过" if passed == len(cases) else "失败"
        print(f"  {status}    {name:20} v{meta.version:<8} {passed}/{len(cases)}")
        if first_fail:
            failures.append(f"{name}: {first_fail}")
            print(f"          → 首个失败：{first_fail}")

    print("=" * 76)
    print(f"已执行 {total_run} 组用例：{total_pass} 通过，{total_run - total_pass} 失败")
    if skipped:
        print(f"未执行 {len(skipped)} 个 Skill 的评估集（如实计入未覆盖，不算通过）：")
        for s in skipped:
            print(f"  · {s}")
    print("\n发布门禁：已接入执行器的 Skill 全绿才允许经 Nacos 灰度发布。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
