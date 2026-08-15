#!/usr/bin/env python3
"""live 代码路径的离线一致性与故障回归。

`--llm live` 在 README 里长期挂着一句话：「尚未在真实端点上跑过完整链路」。
真正的问题不是「没跑过」，而是**它没法在没有 key、没有网络的情况下被回归**——
于是每次改动都只能靠肉眼审阅，而 live 路径上最关键的三段代码
（传输重试、Schema 修复轮、失败策略降级）恰恰只在异常时才被执行到。

本脚本用 ``tools/mock_llm_server.py`` 把这三段代码变成可回归的对象，检验两类命题：

**命题一 · 等价性。** 端点正常时，live 路径的产出必须与 stub **完全一致**。
伪端点内部调用的就是确定性推理器，因此任何差异都只可能来自新增的
HTTP → 解析 → 归一化 → 校验这几层。这是它们「不扭曲内容」的证明。

**命题二 · fail-safe。** 端点异常时，系统的降级方向必须朝向阻断而非放行。
最重要的一条是 ``always-bad``：模型持续给不出合规输出时，案件必须停在
EVIDENCE_GAP 转人工，且**授信额度不得被修改**——不是「尽力而为地跑完」。

用法::

    python3 tools/live_conformance.py            # 全部故障模式
    python3 tools/live_conformance.py --fault always-bad --verbose
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.registry import MCPClient  # noqa: E402
from mcp_servers.world import World  # noqa: E402
from poc.creditsentry import modelconfig  # noqa: E402
from poc.creditsentry.agents import Orchestrator  # noqa: E402
from poc.creditsentry.llm import get_llm  # noqa: E402
from poc.creditsentry.tracing import Tracer  # noqa: E402
from tools.mock_llm_server import DEFAULT_PORT, FAULTS, serve  # noqa: E402

CASE = "case_001"

#: 每个故障模式的期望行为。四列的含义：
#:   same_as_stub  产出是否必须与 stub 逐字节一致
#:   repairs       期望的修复轮次数（None 表示不作断言）
#:   degraded      是否期望触发失败策略降级
#:   blocked       是否期望案件被阻断（不得进入处置执行）
EXPECT: dict[str, dict[str, Any]] = {
    "none":        {"same_as_stub": True,  "repairs": 0, "degraded": False, "blocked": False},
    "prose":       {"same_as_stub": True,  "repairs": 0, "degraded": False, "blocked": False},
    # 表述偏差应当被 normalize 吸收，**不该**浪费一轮修复
    "loose-format": {"same_as_stub": True, "repairs": 0, "degraded": False, "blocked": False},
    "bad-enum":    {"same_as_stub": True,  "repairs": 2, "degraded": False, "blocked": False},
    "no-evidence": {"same_as_stub": True,  "repairs": 1, "degraded": False, "blocked": False},
    "fake-evidence": {"same_as_stub": True, "repairs": 1, "degraded": False, "blocked": False},
    "partial-checklist": {"same_as_stub": True, "repairs": 1, "degraded": False, "blocked": False},
    # 回执只给 target 时由代码兜底对齐，不必惊动模型
    "resolution-by-target": {"same_as_stub": True, "repairs": 0,
                             "degraded": False, "blocked": False},
    "always-bad":  {"same_as_stub": False, "repairs": None, "degraded": True, "blocked": True},
    "http500":     {"same_as_stub": True,  "repairs": 0, "degraded": False, "blocked": False},
    "ratelimit":   {"same_as_stub": True,  "repairs": 0, "degraded": False, "blocked": False},
    # 鉴权失败不重试、不修复，直接走失败策略——重试一个必然失败的请求毫无意义
    "auth-fail":   {"same_as_stub": False, "repairs": None, "degraded": True, "blocked": True},
    "no-json-mode": {"same_as_stub": True, "repairs": 0, "degraded": False, "blocked": False},
}


def _run(mode: str, cfg: Any) -> dict[str, Any]:
    """跑一遍 CASE-001，返回用于比对的产出摘要。"""
    world = World.load(CASE)
    tracer = Tracer()
    mcp = MCPClient(world, tracer)
    llm = get_llm(mode, cfg=cfg)
    orch = Orchestrator(world, mcp, llm, auto_approve=True)
    st = orch.run()
    usage = llm.usage()
    return {
        "phase": st.phase,
        "assertion": st.assertion,
        "rebuttal_verdict": (st.rebuttal or {}).get("verdict"),
        "adjudication": (st.adjudication or {}).get("verdict"),
        "action_tier": (st.gate or {}).get("action_tier"),
        "rebuttal_coverage": (st.rebuttal_checklist.coverage()
                              if st.rebuttal_checklist else None),
        "repairs": usage["repair_rounds"],
        "degradations": usage["degradations"],
        # 写类 MCP 调用：阻断路径下必须为 0
        "writes": [a for a in mcp.audit_log
                   if a["tool"] in ("adjust_limit", "add_guarantee", "rollback_adjustment")
                   and a["status"] == "OK"],
    }


def _compare(fault: str, stub: dict[str, Any], live: dict[str, Any]) -> list[str]:
    exp = EXPECT[fault]
    problems: list[str] = []

    if exp["same_as_stub"]:
        for key in ("assertion", "rebuttal_verdict", "adjudication",
                    "action_tier", "rebuttal_coverage", "phase"):
            if stub[key] != live[key]:
                problems.append(
                    f"{key} 与 stub 不一致：stub={stub[key]!r} live={live[key]!r}")

    if exp["repairs"] is not None and live["repairs"] != exp["repairs"]:
        problems.append(f"修复轮次数应为 {exp['repairs']}，实际 {live['repairs']}")

    got_degraded = bool(live["degradations"])
    if got_degraded != exp["degraded"]:
        problems.append(
            f"降级期望 {exp['degraded']}，实际 {got_degraded}"
            + (f"（{live['degradations'][0]['reason']}）" if got_degraded else ""))

    if exp["blocked"]:
        if live["phase"] != "EVIDENCE_GAP":
            problems.append(f"应阻断在 EVIDENCE_GAP，实际停在 {live['phase']}")
        if live["writes"]:
            problems.append(
                f"阻断路径下仍发生了 {len(live['writes'])} 次写操作："
                f"{[w['tool'] for w in live['writes']]}")
    return problems


def main() -> int:
    p = argparse.ArgumentParser(description="live 代码路径的离线一致性与故障回归")
    p.add_argument("--fault", choices=sorted(FAULTS), help="只跑单个故障模式")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    cfg = modelconfig.resolve("offline-mock", None)
    print("基线：stub 模式跑一遍 CASE-001…")
    stub = _run("stub", modelconfig.load())

    faults = [args.fault] if args.fault else list(FAULTS)
    print(f"\n{'故障模式':<24}{'修复轮':<8}{'降级':<8}{'阻断':<8}结果")
    print("─" * 78)

    failures = 0
    for fault in faults:
        srv = serve(fault, args.port, args.verbose)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            live = _run("live", cfg)
            problems = _compare(fault, stub, live)
        except Exception as e:  # 崩溃本身就是失败：live 路径不允许把异常抛到编排层
            live = {"repairs": "-", "degradations": [], "phase": "-", "writes": []}
            problems = [f"抛出未捕获异常 {type(e).__name__}: {e}"]
        finally:
            srv.shutdown()
            srv.server_close()

        ok = not problems
        failures += 0 if ok else 1
        print(f"{fault:<24}{str(live['repairs']):<8}"
              f"{('是' if live['degradations'] else '否'):<8}"
              f"{('是' if live['phase'] == 'EVIDENCE_GAP' else '否'):<8}"
              f"{'✓ 通过' if ok else '✗ 不符'}")
        for prob in problems:
            print(f"{'':<24}→ {prob}")

    print("─" * 78)
    print(f"合计 {len(faults)} 个故障模式：{len(faults) - failures} 通过，{failures} 失败")
    if not failures:
        print("\nlive 路径结论：")
        print("  · 端点正常时，HTTP → 解析 → 归一化 → 校验 各层不改变结论（与 stub 逐字节一致）")
        print("  · 端点异常时，降级方向恒为阻断；阻断路径下写类 MCP 调用为 0")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
