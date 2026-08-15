#!/usr/bin/env python3
"""信衡 CreditSentry —— 端到端 Demo 入口。

用法：
    python poc/run_demo.py --case CASE-001          # 链路 A：风险成立 → 分级处置
    python poc/run_demo.py --case CASE-002          # 链路 B：误报 → 被质疑官驳回
    python poc/run_demo.py --case CASE-001 --deny-approval   # 审批被拒的降级路径
    python poc/run_demo.py --all                    # 两条链路一起跑并汇总指标

产出全部落在 poc/out/<case>/：
    trace.json            全链路 Span（含 routing / adjudication Span）
    trace.txt             人可读的 Span 树
    evidence_ledger.json  证据账本
    case_state.json       共享状态终态
    logs.jsonl            结构化日志（trace_id 关联）
    mcp_audit.jsonl       MCP 调用审计日志
    处置意见书.md / 审计报告.md
    metrics.json          本次运行的指标

默认 --llm stub（确定性推理，可复现）；--llm live 按各 Agent 各自绑定的档位
调用真实端点，凭证从 config/providers.yaml 的 credential_ref 解析。

真机跑法（先自检再花钱）：

    python3 tools/preflight.py --preset dashscope-only
    python3 poc/run_demo.py --case CASE-001 --llm live --preset dashscope-only

详见 docs/真机部署与API方案.md。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.registry import MCPClient  # noqa: E402
from mcp_servers.world import World  # noqa: E402
from poc.creditsentry import modelconfig  # noqa: E402
from poc.creditsentry.agents import Orchestrator  # noqa: E402
from poc.creditsentry.llm import get_llm  # noqa: E402
from poc.creditsentry.tracing import Tracer  # noqa: E402

OUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

CASES = {
    "CASE-001": "case_001",
    "CASE-002": "case_002",
    "CASE-003": "case_003",
}


def run_case(case_key: str, llm_mode: str, auto_approve: bool,
             cfg=None) -> dict:
    fixture = CASES[case_key]
    world = World.load(fixture)
    tracer = Tracer()
    mcp = MCPClient(world, tracer)
    llm = get_llm(llm_mode, cfg=cfg)

    orch = Orchestrator(world, mcp, llm, auto_approve=auto_approve)
    state = orch.run()

    out_dir = os.path.join(OUT_ROOT, case_key)
    os.makedirs(out_dir, exist_ok=True)

    tracer.save(os.path.join(out_dir, "trace.json"))
    with open(os.path.join(out_dir, "trace.txt"), "w", encoding="utf-8") as f:
        f.write(tracer.render_tree())
    orch.ledger.save(os.path.join(out_dir, "evidence_ledger.json"))
    orch.store.save(state.case_id, os.path.join(out_dir, "case_state.json"))

    with open(os.path.join(out_dir, "logs.jsonl"), "w", encoding="utf-8") as f:
        for rec in orch.ctx.logs:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with open(os.path.join(out_dir, "mcp_audit.jsonl"), "w", encoding="utf-8") as f:
        for rec in mcp.audit_log:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    if state.gate and state.gate.get("opinion_report"):
        with open(os.path.join(out_dir, "处置意见书.md"), "w", encoding="utf-8") as f:
            f.write(state.gate["opinion_report"]["markdown"])
    if state.audit and state.audit.get("report"):
        with open(os.path.join(out_dir, "审计报告.md"), "w", encoding="utf-8") as f:
            f.write(state.audit["report"]["markdown"])

    metrics = _metrics(world, state, orch, tracer, llm_mode, llm)
    with open(os.path.join(out_dir, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    _print_summary(case_key, world, state, orch, tracer, metrics, out_dir)
    return metrics


def _metrics(world, state, orch, tracer, llm_mode, llm) -> dict:
    ev = state.risk_event or {}
    unsupported = [c for c in (state.assertion or {}).get("root_causes", [])
                   if not c.get("evidence_ids")]
    total_causes = len((state.assertion or {}).get("root_causes", []))
    gate = state.gate or {}
    needs_approval = bool(gate.get("needs_approval"))
    approved = bool((state.approval or {}).get("token"))

    return {
        "case_id": state.case_id,
        "label": world.data.get("label"),
        "llm_mode": llm_mode,
        "as_of_date": world.as_of,
        "model_usage": llm.usage(),
        # 提到顶层：真机排障时这三个数是最先要看的
        "llm_repair_rounds": llm.usage()["repair_rounds"],
        "llm_failed_calls": llm.usage()["failed_calls"],
        "llm_degradations": len(llm.usage()["degradations"]),
        "expected": world.data.get("expected_outcome"),
        "actual": {
            "adjudication": (state.adjudication or {}).get("verdict"),
            "final_grade": (state.adjudication or {}).get("final_grade"),
            "action_tier": gate.get("action_tier"),
        },
        "denoise_rate": ev.get("denoise_rate"),
        # 质疑覆盖率：未被质疑的主因不等于质疑通过，所以这个数必须是 1.0
        "rebuttal_coverage": (state.rebuttal_checklist.coverage()
                              if state.rebuttal_checklist else None),
        "evidence_checklist_coverage": (state.evidence_checklist.coverage()
                                        if state.evidence_checklist else None),
        "query_dimensions_used": sorted(
            {d for p in state.query_plans for d in p["dimensions_used"]}),
        "clarifications": [c for p in state.query_plans for c in p["clarifications"]],
        "evidence_count": len(orch.ledger.all()),
        "evidence_sufficiency": orch.ledger.sufficiency(),
        "evidence_completeness": 1.0 if total_causes and not unsupported else (
            1.0 if total_causes == 0 else 0.0),
        "unsupported_conclusions": len(unsupported),
        "approval_coverage": (1.0 if (not needs_approval or approved) else 0.0),
        "compliance_passed": (state.audit or {}).get("compliance", {}).get("passed"),
        "compliance_failed": (state.audit or {}).get("compliance", {}).get("failed"),
        "phase_transitions": len(state.history),
        **tracer.metrics(),
    }


def _print_summary(case_key, world, state, orch, tracer, metrics, out_dir) -> None:
    bar = "═" * 78
    print(f"\n{bar}")
    print(f"  {case_key}　{world.data.get('label')}")
    print(f"  {world.subject['name']}　敞口 {(state.exposure or {}).get('total_exposure', 0):,.0f} 元")
    print(bar)

    print("\n【全链路 Trace】")
    print(tracer.render_tree())

    adj = state.adjudication or {}
    gate = state.gate or {}
    print("\n【裁决】")
    print(f"  定性方：{(state.assertion or {}).get('conclusion')}"
          f"　建议分类 {(state.assertion or {}).get('suggested_grade') or '—'}")
    print(f"  质疑方：{(state.rebuttal or {}).get('verdict')}"
          f"　成立反驳 {len((state.rebuttal or {}).get('rebuttals', []))} 条"
          f"　尝试未成立 {len((state.rebuttal or {}).get('attempted_but_failed', []))} 条")
    print(f"  裁　决：{adj.get('verdict')}　—— {adj.get('basis')}")

    print("\n【处置】")
    if gate:
        for a in gate.get("actions", []):
            print(f"  · {a['label']}　层级 {a['action_tier']}"
                  f"　规则 {a['rule_id']}　{'需审批' if a['needs_approval'] else '无需审批'}")
        if state.approval:
            print(f"  审批：{state.approval['decision']}　({state.approval.get('approver')})")
        for r in (state.execution or {}).get("results", []):
            print(f"  执行：{r.get('label', r['action'])} → {r['status']}"
                  f"　{('审计流水 ' + r['audit_serial']) if r.get('audit_serial') else ''}")
    else:
        print("  未产生处置动作")

    print("\n【上下文工程】")
    print(f"  质疑清单　{metrics['rebuttal_coverage']:.0%} 覆盖"
          f"（{len(state.rebuttal_checklist.items) if state.rebuttal_checklist else 0} 项，"
          f"由系统从定性方断言派生，模型返回后逐项核对）")
    print(f"  取证清单　{metrics['evidence_checklist_coverage']:.0%} 覆盖"
          f"（{len(state.evidence_checklist.items) if state.evidence_checklist else 0} 项）")
    print(f"  查询改写　命中维度 {metrics['query_dimensions_used']}")
    for c in metrics["clarifications"]:
        print(f"    澄清 {c['clarification_id']} → {c['channel']}"
              f"{'（阻断）' if c['blocking'] else ''}　{c['question']}")

    comp = (state.audit or {}).get("compliance", {})
    print("\n【审计】")
    print(f"  合规项：{comp.get('passed', 0)} 通过 / {comp.get('failed', 0)} 未通过"
          f"　（共 {len(comp.get('items', []))} 项）")
    print(f"  证据：{metrics['evidence_count']} 条　充分度 {metrics['evidence_sufficiency']}"
          f"　无证据结论 {metrics['unsupported_conclusions']} 条")

    usage = metrics["model_usage"]
    print(f"\n【模型绑定】模式 {usage['mode']}"
          f"　（stub 模式仍按权限矩阵解析绑定，Trace 中实际模型与声明绑定分开上报）")
    for caller, u in usage["by_caller"].items():
        actual = "确定性推理器" if u["stubbed"] else u["actual_model"]
        print(f"  · {caller:<20} {u['profile']:<18}"
              f" 声明 {u['provider']}/{u['declared_model']}（族 {u['family']}）"
              f"　实际 {actual}")
    if usage["by_caller"]:
        fams = {u["family"] for u in usage["by_caller"].values()}
        print(f"  本次成本 ￥{usage['total_cost_cny']}"
              f"　涉及模型族 {sorted(fams)}"
              f"　修复轮 {usage['repair_rounds']} 次"
              f"　失败调用 {usage['failed_calls']} 次")
    # 降级必须显式打出来。一次悄悄降级的运行和一次正常运行长得一样，
    # 那么「这个结论当时是怎么来的」就永远说不清了
    for d in usage["degradations"]:
        print(f"  ⚠ 失败策略生效：{d['caller']} 的 {d['task']} —— {d['reason']}")
        print(f"    降级为：{d['fallback']}")

    exp = world.data.get("expected_outcome", {})
    act = metrics["actual"]
    ok = (act["adjudication"] == exp.get("adjudication")
          and act["action_tier"] == exp.get("action_tier"))
    print(f"\n【断言】期望 {exp.get('adjudication')} / {exp.get('action_tier')}"
          f"　实际 {act['adjudication']} / {act['action_tier']}"
          f"　{'✓ 通过' if ok else '✗ 不符'}")

    # 回溯案例：跑完之后才揭晓历史结局。它在 World 构造时就被摘出，
    # 任何 Agent 都够不到，因此这一行不构成前视信息。
    if world.retrospective:
        r = world.retrospective
        print(f"\n【历史结局 · 仅事后评分，未进入取证路径】")
        print(f"  as_of {world.as_of} → {r['outcome_date']}"
              f"　提前量 {r['lead_time_months']} 个月")
        print(f"  {r['detail']}")

    print(f"\n产出目录：{out_dir}")


def main() -> int:
    p = argparse.ArgumentParser(description="信衡 CreditSentry 端到端 Demo")
    p.add_argument("--case", choices=sorted(CASES), help="要运行的案件")
    p.add_argument("--all", action="store_true", help="运行全部案件并汇总")
    p.add_argument("--llm", choices=("stub", "live"), default="stub",
                   help="推理后端：stub=确定性可复现（默认），live=真实模型")
    p.add_argument("--deny-approval", action="store_true",
                   help="模拟人工审批被拒，演示降级为只读的路径")
    p.add_argument("--profile", action="append", metavar="AGENT=PROFILE",
                   help="覆盖某 Agent 的模型档位（可重复），如 "
                        "--profile devils-advocate=analyst-primary；覆盖后仍会校验不变量")
    p.add_argument("--preset", metavar="NAME",
                   help="套用 config/models.yaml 中的一组绑定预设（如 dashscope-only）；"
                        "--profile 优先级更高。用 --list-presets 查看可选值")
    p.add_argument("--list-presets", action="store_true", help="列出可用的绑定预设后退出")
    args = p.parse_args()

    if args.list_presets:
        for name, body in sorted(modelconfig.list_presets().items()):
            print(f"  {name:<26}{(body or {}).get('desc', '')}")
            for agent, prof in ((body or {}).get("bindings") or {}).items():
                print(f"{'':<28}· {agent:<22}→ {prof}")
        return 0

    if not args.case and not args.all:
        p.error("请指定 --case 或 --all")

    # 配置在此处加载一次并校验：对抗双方是否同族、PII 触点是否越出数据驻留白名单、
    # 唯一写触点是否被塞了模型入口——任一违反直接拒绝启动，不带病运行。
    try:
        cfg = modelconfig.resolve(args.preset, args.profile)
    except modelconfig.ConfigError as e:
        print(f"模型配置校验未通过，拒绝启动：\n{e}", file=sys.stderr)
        return 2

    cases = sorted(CASES) if args.all else [args.case]
    results = [run_case(c, args.llm, auto_approve=not args.deny_approval, cfg=cfg)
               for c in cases]

    if len(results) > 1:
        print("\n" + "═" * 78)
        print("  汇总")
        print("═" * 78)
        print(f"{'案件':<12}{'裁决':<22}{'层级':<8}{'降噪率':<10}{'证据':<8}{'断言'}")
        allok = True
        for m in results:
            exp = m["expected"]
            ok = (m["actual"]["adjudication"] == exp.get("adjudication")
                  and m["actual"]["action_tier"] == exp.get("action_tier"))
            allok &= ok
            print(f"{m['case_id'][-8:]:<12}{str(m['actual']['adjudication']):<22}"
                  f"{str(m['actual']['action_tier']):<8}"
                  f"{m['denoise_rate']:.1%}{'':<5}{m['evidence_count']:<8}"
                  f"{'✓' if ok else '✗'}")
        print(f"\n三条链路覆盖三种闸门结局：L2 审批后执行 · L0 维持原状 · L3 只出方案不执行")
        tok = sum(m["tokens_in"] + m["tokens_out"] for m in results)
        print(f"\n合计 Token（估算）：{tok:,}　"
              f"工具调用成功率：{results[0]['tool_success_rate']}")
        return 0 if allok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
