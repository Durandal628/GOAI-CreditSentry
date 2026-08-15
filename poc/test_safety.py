#!/usr/bin/env python3
"""安全边界回归测试。

把 PPT 上的安全承诺变成可执行断言——这些不变量若被破坏，测试直接失败。
零依赖，直接运行：

    python poc/test_safety.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.registry import MCPClient  # noqa: E402
from mcp_servers.world import MCPError, World  # noqa: E402
from poc.creditsentry import gate, skills  # noqa: E402
from poc.creditsentry.agents import Orchestrator  # noqa: E402
from poc.creditsentry.ledger import EvidenceError, EvidenceLedger  # noqa: E402
from poc.creditsentry.llm import get_llm  # noqa: E402
from poc.creditsentry.tracing import Tracer  # noqa: E402

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str):
    def deco(fn):
        try:
            fn()
            PASSED.append(name)
        except AssertionError as e:
            FAILED.append((name, str(e) or "断言失败"))
        except Exception as e:  # noqa: BLE001
            FAILED.append((name, f"{type(e).__name__}: {e}"))
        return fn
    return deco


def _fresh(case: str = "case_001"):
    world = World.load(case)
    tracer = Tracer()
    mcp = MCPClient(world, tracer)
    return world, tracer, mcp


# ===========================================================================
# 一、执行闸门：不可逆动作永不自动执行
# ===========================================================================

@check("G-03 不可逆动作恒定 L3，任何条件都不能豁免")
def _():
    for action in ("early_recall", "litigation_preservation", "downgrade_classification"):
        # 即便给出最有利的条件组合（高风险等级、强证据、极小敞口），也必须是 L3
        d = gate.evaluate(case_id="T", action_type=action, risk_grade="次级",
                          evidence_level="强", exposure_amount=1.0)
        assert d.action_tier == "L3", f"{action} 被判为 {d.action_tier}，应恒为 L3"
        assert d.rule_id == "G-03", f"{action} 命中规则 {d.rule_id}，应为 G-03"
        assert not d.reversible


@check("G-02 入参缺失时 fail-safe 降级为 L3，而非放行")
def _():
    for missing in ("risk_grade", "evidence_level", "exposure_amount"):
        kwargs = dict(case_id="T", action_type="reduce_limit", risk_grade="关注",
                      evidence_level="强", exposure_amount=1_000_000)
        kwargs[missing] = None
        d = gate.evaluate(**kwargs)
        assert d.action_tier == "L3", f"缺失 {missing} 时被判为 {d.action_tier}"
        assert d.rule_id == "G-02"


@check("G-01 白名单外动作一律 L3，不存在「未知即放行」")
def _():
    d = gate.evaluate(case_id="T", action_type="delete_customer", risk_grade="次级",
                      evidence_level="强", exposure_amount=1000)
    assert d.action_tier == "L3" and d.rule_id == "G-01"


@check("G-04 证据缺失时最多只读诊断（L0）")
def _():
    d = gate.evaluate(case_id="T", action_type="reduce_limit", risk_grade="次级",
                      evidence_level="缺失", exposure_amount=9_000_000)
    assert d.action_tier == "L0" and d.rule_id == "G-04"


@check("全组合遍历：不可逆动作在任何入参组合下都不落入 L0/L1")
def _():
    irreversible = [a for a, s in gate.ACTION_CATALOG.items() if not s.reversible]
    combos = 0
    for action in irreversible:
        for grade in ("正常", "关注", "次级", None):
            for level in ("强", "弱", "缺失", None):
                for amount in (0, 1, 4_999_999, 5_000_000, 20_000_000, None):
                    d = gate.evaluate(case_id="T", action_type=action, risk_grade=grade,
                                      evidence_level=level, exposure_amount=amount)
                    assert d.action_tier == "L3", (
                        f"{action}/{grade}/{level}/{amount} → {d.action_tier}")
                    combos += 1
    assert combos == len(irreversible) * 4 * 4 * 6


@check("G-09 只读动作不因敞口金额升档（避免无意义的审批摩擦）")
def _():
    d = gate.evaluate(case_id="T", action_type="monitor_only", risk_grade="正常",
                      evidence_level="强", exposure_amount=50_000_000)
    assert d.action_tier == "L0", f"只读动作被升档至 {d.action_tier}"
    assert not d.needs_approval


# ===========================================================================
# 二、权限矩阵：Agent 边界即权限边界
# ===========================================================================

@check("唯一写触点：非 executor 调用写工具被拒")
def _():
    _, _, mcp = _fresh()
    for caller in ("risk-analyst", "due-diligence", "signal-hub",
                   "compliance-auditor", "devils-advocate", "risk-commander"):
        try:
            mcp.call("credit-core-mcp", "adjust_limit",
                     {"subject_id": "S", "new_limit": 1, "idempotency_key": "k"},
                     caller=caller)
        except MCPError as e:
            assert e.code == "PERMISSION_DENIED"
        else:
            raise AssertionError(f"{caller} 竟然写成功了")


@check("唯一 PII 触点：非 due-diligence 访问征信与流水被拒")
def _():
    _, _, mcp = _fresh()
    for server, tool in (("bureau-mcp", "get_credit_report"), ("txn-mcp", "get_flow_pattern")):
        for caller in ("risk-analyst", "disposition-executor", "signal-hub"):
            try:
                mcp.call(server, tool, {"subject_id": "S", "authorization_id": "A",
                                        "account_ids": []}, caller=caller)
            except MCPError as e:
                assert e.code == "PERMISSION_DENIED"
            else:
                raise AssertionError(f"{caller} 竟然读到了 {server}")


@check("Skill 层权限：定性官不得调用取证类 Skill")
def _():
    world, tracer, mcp = _fresh()
    ctx = skills.Context(tracer, EvidenceLedger("T"), mcp, world, get_llm("stub"),
                         caller="risk-analyst")
    try:
        skills.litigation_probe(ctx, "某公司", "S")
    except skills.SkillPermissionError:
        pass
    else:
        raise AssertionError("risk-analyst 竟然调用了 LitigationProbe")


@check("Skill 层权限：执行官不得调用审计类 Skill")
def _():
    world, tracer, mcp = _fresh()
    ctx = skills.Context(tracer, EvidenceLedger("T"), mcp, world, get_llm("stub"),
                         caller="disposition-executor")
    try:
        skills.compliance_check(ctx, None, tracer)
    except skills.SkillPermissionError:
        pass
    else:
        raise AssertionError("executor 竟然自审了")


# ===========================================================================
# 三、审批与幂等
# ===========================================================================

@check("L2 动作缺少审批令牌时被拒绝执行")
def _():
    _, _, mcp = _fresh()
    try:
        mcp.call("credit-core-mcp", "adjust_limit",
                 {"subject_id": "S", "new_limit": 100, "idempotency_key": "k1"},
                 caller="disposition-executor")
    except MCPError as e:
        assert e.code == "APPROVAL_REQUIRED"
    else:
        raise AssertionError("无审批令牌竟然执行成功")


@check("伪造的审批令牌无法通过验签")
def _():
    _, _, mcp = _fresh()
    try:
        mcp.call("credit-core-mcp", "adjust_limit",
                 {"subject_id": "S", "new_limit": 100, "idempotency_key": "k2",
                  "approval_token": "forged-token"},
                 caller="disposition-executor")
    except MCPError as e:
        assert e.code == "APPROVAL_INVALID"
    else:
        raise AssertionError("伪造令牌竟然通过了")


@check("幂等：同一幂等键重复投递不重复执行")
def _():
    _, _, mcp = _fresh()
    args = {"subject_id": "S", "new_limit": 4_000_000, "idempotency_key": "idem-x",
            "approval_token": "apv-test"}
    r1 = mcp.call("credit-core-mcp", "adjust_limit", dict(args), caller="disposition-executor")
    r2 = mcp.call("credit-core-mcp", "adjust_limit", dict(args), caller="disposition-executor")
    assert r2.get("idempotent_replay") is True, "重复投递未被幂等去重"
    assert r1["rollback_point_id"] == r2["rollback_point_id"]


@check("回滚：额度调整可按回滚点冲正至原值")
def _():
    world, _, mcp = _fresh()
    before = world.section("credit_core")["facility"]["total_limit"]
    r = mcp.call("credit-core-mcp", "adjust_limit",
                 {"subject_id": "S", "new_limit": 1_000_000, "idempotency_key": "rb-1",
                  "approval_token": "apv-test"}, caller="disposition-executor")
    assert world.section("credit_core")["facility"]["total_limit"] == 1_000_000
    mcp.call("credit-core-mcp", "rollback_adjustment",
             {"subject_id": "S", "rollback_point_id": r["rollback_point_id"],
              "idempotency_key": "rb-1-undo"}, caller="disposition-executor")
    assert world.section("credit_core")["facility"]["total_limit"] == before, "回滚未恢复原值"


@check("审批被拒时降级为 L0，不执行任何处置动作")
def _():
    world = World.load("case_001")
    tracer = Tracer()
    mcp = MCPClient(world, tracer)
    orch = Orchestrator(world, mcp, get_llm("stub"), auto_approve=False)
    st = orch.run()
    assert st.gate["action_tier"] == "L0", f"审批被拒后层级为 {st.gate['action_tier']}"
    for r in (st.execution or {}).get("results", []):
        assert r["status"] == "NO_ACTION", f"审批被拒后仍执行了 {r['action']}"
    limit = world.section("credit_core")["facility"]["total_limit"]
    assert limit == 8_000_000, f"审批被拒但额度已被改为 {limit}"


# ===========================================================================
# 四、证据约束：无证据不决策
# ===========================================================================

@check("无证据的断言被账本拒绝")
def _():
    led = EvidenceLedger("T")
    try:
        led.assert_supported("风险成立", [])
    except EvidenceError:
        pass
    else:
        raise AssertionError("无证据断言竟然通过了")


@check("引用不存在的证据被拒绝")
def _():
    led = EvidenceLedger("T")
    try:
        led.assert_supported("风险成立", ["EV-9999-9999"])
    except EvidenceError:
        pass
    else:
        raise AssertionError("引用了不存在的证据竟然通过了")


@check("仅由缺失证据支撑的断言被拒绝")
def _():
    led = EvidenceLedger("T")
    ev = led.record_gap(subject_id="S", fact_type="financial_statement", why="未取到")
    try:
        led.assert_supported("风险成立", [ev.evidence_id])
    except EvidenceError:
        pass
    else:
        raise AssertionError("仅凭缺失证据竟然可以下结论")


@check("采样不足的数据被自动降为弱证据")
def _():
    led = EvidenceLedger("T")
    ev = led.record(subject_id="S", source_system="txn-mcp", fact_type="flow",
                    raw_content="x", extracted={"undersampled": True})
    assert ev.level == "弱", f"采样不足的数据被定为 {ev.level}"


@check("主体重名未消歧的数据被降为弱证据")
def _():
    led = EvidenceLedger("T")
    ev = led.record(subject_id="S", source_system="judicial-mcp", fact_type="lit",
                    raw_content="x", extracted={"ambiguous": True})
    assert ev.level == "弱"


@check("账本 append-only：内容哈希随原文变化，无法悄悄改写")
def _():
    led = EvidenceLedger("T")
    a = led.record(subject_id="S", source_system="judicial-mcp", fact_type="lit",
                   raw_content="原文A", extracted={})
    b = led.record(subject_id="S", source_system="judicial-mcp", fact_type="lit",
                   raw_content="原文B", extracted={})
    assert a.content_hash != b.content_hash
    assert a.evidence_id != b.evidence_id


# ===========================================================================
# 五、协同不变量：质疑不可跳过、路由可举证、职责分离
# ===========================================================================

@check("质疑环节不可跳过：EVIDENCE→ADJUDICATION 强制并行派发双方")
def _():
    from poc.creditsentry import routing
    d = routing.route(phase="EVIDENCE", signal_types=["judicial_new_case"],
                      evidence_sufficiency=0.95, risk_tier=None, exposure_amount=1000)
    assert d.rule_id == "R-04"
    assert set(d.dispatch) == {"risk-analyst", "devils-advocate"}, d.dispatch
    assert d.parallel is True


@check("全流程仅一条回退边，杜绝无限循环")
def _():
    from poc.creditsentry import routing
    d = routing.route(phase="ADJUDICATION", signal_types=[], evidence_sufficiency=0.4,
                      risk_tier=None, exposure_amount=1000,
                      adjudication_verdict="EVIDENCE_INSUFFICIENT")
    assert d.rule_id == "R-05" and d.next_phase == "EVIDENCE"
    # 取证重试用尽后必须转人工，不得继续循环
    d2 = routing.route(phase="EVIDENCE", signal_types=[], evidence_sufficiency=0.4,
                       risk_tier=None, exposure_amount=1000,
                       evidence_retries=routing.MAX_EVIDENCE_RETRIES)
    assert d2.rule_id == "R-03" and d2.next_phase == "EVIDENCE_GAP"


@check("路由决策可举证：每条 routing Span 含 routing_key / 规则 ID / 规则版本")
def _():
    world = World.load("case_001")
    tracer = Tracer()
    mcp = MCPClient(world, tracer)
    Orchestrator(world, mcp, get_llm("stub")).run()
    routing_spans = [s for s in tracer.spans if s.kind == "routing"]
    assert routing_spans, "全链路中没有任何 routing Span"
    for s in routing_spans:
        for field in ("routing_key", "rule_id", "rule_version"):
            assert field in s.attributes, f"routing Span {s.name} 缺少 {field}"


@check("职责分离：执行方与审计方不是同一个 Agent")
def _():
    world = World.load("case_001")
    tracer = Tracer()
    mcp = MCPClient(world, tracer)
    Orchestrator(world, mcp, get_llm("stub")).run()
    executors = {s.attributes.get("caller") for s in tracer.spans if s.kind == "execution"}
    auditors = {s.attributes.get("caller") for s in tracer.spans
                if s.kind == "skill" and s.name == "ComplianceCheck"}
    assert executors and auditors
    assert not (executors & auditors), f"执行方与审计方重叠：{executors & auditors}"


@check("确定性：同一案件重复运行结论完全一致（stub 模式可复现）")
def _():
    outs = []
    for _ in range(3):
        world = World.load("case_001")
        tracer = Tracer()
        mcp = MCPClient(world, tracer)
        st = Orchestrator(world, mcp, get_llm("stub")).run()
        outs.append((st.adjudication["verdict"], st.gate["action_tier"],
                     st.adjudication["final_grade"]))
    assert len(set(outs)) == 1, f"多次运行结论不一致：{set(outs)}"


@check("三条链路的期望结论均达成，且覆盖三种不同的闸门结局")
def _():
    tiers = set()
    for case, exp_verdict, exp_tier in (("case_001", "RISK_CONFIRMED", "L2"),
                                        ("case_002", "RISK_REFUTED", "L0"),
                                        ("case_003", "RISK_CONFIRMED", "L3")):
        world = World.load(case)
        tracer = Tracer()
        mcp = MCPClient(world, tracer)
        st = Orchestrator(world, mcp, get_llm("stub")).run()
        assert st.adjudication["verdict"] == exp_verdict, (
            f"{case} 裁决为 {st.adjudication['verdict']}，期望 {exp_verdict}")
        assert st.gate["action_tier"] == exp_tier, (
            f"{case} 层级为 {st.gate['action_tier']}，期望 {exp_tier}")
        tiers.add(st.gate["action_tier"])
    assert tiers == {"L0", "L2", "L3"}, f"三条链路只覆盖了 {sorted(tiers)}，应各不相同"


@check("大额敞口升档：同一可逆动作在 620 万敞口为 L2，在 6.5 亿敞口升为 L3")
def _():
    """四维闸门中「敞口金额」这一维必须真的起作用，否则它只是文档里的一个词。"""
    common = dict(case_id="C", action_type="reduce_limit", risk_grade="关注",
                  evidence_level="强", params={"subject_id": "S"})
    small = gate.evaluate(exposure_amount=6_200_000, **common)
    large = gate.evaluate(exposure_amount=650_000_000, **common)
    assert small.action_tier == "L2", f"小额敞口应为 L2，实际 {small.action_tier}"
    assert large.action_tier == "L3", f"大额敞口应升为 L3，实际 {large.action_tier}"
    assert large.rule_id == "G-07", f"应由 G-07 命中，实际 {large.rule_id}"


@check("L3 案件全程未派发执行方，且未产生任何成功执行记录")
def _():
    """L3 的语义是「只出方案交人工决策」。若执行方被派发或有动作落地，即为严重违规。"""
    world, tracer, mcp = _fresh("case_003")
    st = Orchestrator(world, mcp, get_llm("stub")).run()
    assert st.gate["action_tier"] == "L3"
    agents = {s.attributes.get("caller") for s in tracer.spans if s.kind == "agent"}
    assert "disposition-executor" not in agents, "L3 案件竟派发了执行方"
    executed = [r for r in (st.execution or {}).get("results", [])
                if r.get("status") == "SUCCESS"]
    assert not executed, f"L3 案件产生了 {len(executed)} 条成功执行记录"
    # 写类 MCP 调用一次都不该发生
    writes = [a for a in mcp.audit_log
              if a.get("tool") in ("adjust_limit", "add_guarantee", "rollback_adjustment")
              and a.get("allowed")]
    assert not writes, f"L3 案件发生了 {len(writes)} 次写调用"


@check("回溯案例无前视信息：历史结局在结构上不可达")
def _():
    """时点冻结的地基。结局若能被 Agent 取到，整个回测结论就不成立。"""
    world = World.load("case_003")
    assert world.as_of == "2017-04-01", f"as_of 为 {world.as_of}"
    assert world.retrospective is not None, "回溯案例应提供 retrospective_outcome 供事后评分"
    assert "retrospective_outcome" not in world.data, (
        "历史结局仍留在 world.data 中，Agent 可以取到，回测结论不可信")
    # 逐条校验证据的首次公开日不晚于决策时点
    import json as _json
    raw = _json.load(open(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fixtures", "case_003.json"),
        encoding="utf-8"))
    def walk(n):
        if isinstance(n, dict):
            if n.get("first_public_date"):
                yield n["first_public_date"]
            for v in n.values():
                yield from walk(v)
        elif isinstance(n, list):
            for v in n:
                yield from walk(v)
    dates = [d for d in walk(raw) if d]
    assert dates, "回溯案例没有任何 first_public_date 标注，无法验证时点"
    late = [d for d in dates if d > world.as_of]
    assert not late, f"存在晚于 as_of 的证据首次公开日：{late}"


# ===========================================================================
# 六、配置一致性：生成物不得与真源漂移
# ===========================================================================

@check("权限矩阵不变量：credit-core-mcp 有且只有一个写触点")
def _():
    from poc.creditsentry import permissions
    writers = permissions.writers_of("credit-core-mcp")
    assert writers == ["disposition-executor"], f"写触点为 {writers}，应唯一且为 executor"


@check("权限矩阵不变量：PII 触点有且只有一个")
def _():
    from poc.creditsentry import permissions
    pii = permissions.pii_touchpoints()
    assert pii == ["due-diligence"], f"PII 触点为 {pii}，应唯一且为 due-diligence"


@check("权限矩阵不变量：纯推理角色零 MCP 权限")
def _():
    from poc.creditsentry import permissions
    for a in ("risk-analyst", "devils-advocate", "risk-commander"):
        mcp = permissions.PERMISSIONS[a]["mcp"]
        assert not mcp, f"{a} 持有 MCP 权限 {list(mcp)}，应为零工具"


@check("权限矩阵不变量：定性官与质疑官权限集完全相同（拆分理由是目标对立而非权限差异）")
def _():
    from poc.creditsentry import permissions
    a = permissions.PERMISSIONS["risk-analyst"]
    b = permissions.PERMISSIONS["devils-advocate"]
    assert a["mcp"] == b["mcp"], "两者 MCP 权限不一致"
    assert a["pii_access"] == b["pii_access"]


@check("Worker CR 与权限矩阵一致（磁盘生成物未漂移）")
def _():
    import importlib.util
    from poc.creditsentry import permissions
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "gen_at", os.path.join(root, "tools", "gen_agentteams.py"))
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)

    for agent, e in permissions.PERMISSIONS.items():
        path = os.path.join(root, "agentteams", "workers", f"{agent}.yaml")
        assert os.path.exists(path), f"缺少 Worker CR：{agent}.yaml"
        on_disk = open(path, encoding="utf-8").read()
        expected = gen.render_worker_cr(agent, e)
        assert on_disk == expected, (
            f"{agent}.yaml 与权限矩阵不一致，请重跑 tools/gen_agentteams.py")


@check("SOUL.md 与权限矩阵一致（磁盘生成物未漂移）")
def _():
    import importlib.util
    from poc.creditsentry import permissions
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "gen_at2", os.path.join(root, "tools", "gen_agentteams.py"))
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)

    for agent, e in permissions.PERMISSIONS.items():
        path = os.path.join(root, "agentteams", "souls", agent, "SOUL.md")
        assert os.path.exists(path), f"缺少 SOUL.md：{agent}"
        assert open(path, encoding="utf-8").read() == gen.render_soul(agent, e), (
            f"{agent}/SOUL.md 与权限矩阵不一致，请重跑 tools/gen_agentteams.py")


@check("路由表声明与实现一致：route() 产出的每个 rule_id 都已在 RULES_DOC 登记")
def _():
    from poc.creditsentry import routing
    documented = {r["id"] for r in routing.RULES_DOC}
    emitted = set()
    # 遍历能触达每条规则的输入组合
    probes = [
        dict(phase="INTAKE", signal_types=[], evidence_sufficiency=0.0,
             risk_tier=None, exposure_amount=0),
        dict(phase="EVIDENCE", signal_types=[], evidence_sufficiency=0.3,
             risk_tier=None, exposure_amount=0, evidence_retries=0),
        dict(phase="EVIDENCE", signal_types=[], evidence_sufficiency=0.3,
             risk_tier=None, exposure_amount=0, evidence_retries=9),
        dict(phase="EVIDENCE", signal_types=[], evidence_sufficiency=0.9,
             risk_tier=None, exposure_amount=0),
        dict(phase="ADJUDICATION", signal_types=[], evidence_sufficiency=0.9,
             risk_tier=None, exposure_amount=0, adjudication_verdict="EVIDENCE_INSUFFICIENT"),
        # R-12：裁决反复判证据不足且回退次数用尽。注意证据充分度是 0.9——
        # 它与 R-03 的区别正在于此：证据够，是裁决本身收敛不了
        dict(phase="ADJUDICATION", signal_types=[], evidence_sufficiency=0.9,
             risk_tier=None, exposure_amount=0, adjudication_verdict="EVIDENCE_INSUFFICIENT",
             evidence_retries=9),
        dict(phase="ADJUDICATION", signal_types=[], evidence_sufficiency=0.9,
             risk_tier=None, exposure_amount=0, adjudication_verdict="RISK_REFUTED"),
        dict(phase="ADJUDICATION", signal_types=[], evidence_sufficiency=0.9,
             risk_tier=None, exposure_amount=0, adjudication_verdict="RISK_CONFIRMED"),
        dict(phase="DISPOSITION", signal_types=[], evidence_sufficiency=0.9,
             risk_tier=None, exposure_amount=0, action_tier="L3"),
        dict(phase="DISPOSITION", signal_types=[], evidence_sufficiency=0.9,
             risk_tier=None, exposure_amount=0, action_tier="L2", approved=False),
        dict(phase="DISPOSITION", signal_types=[], evidence_sufficiency=0.9,
             risk_tier=None, exposure_amount=0, action_tier="L1"),
        dict(phase="AUDIT", signal_types=[], evidence_sufficiency=0.9,
             risk_tier=None, exposure_amount=0),
    ]
    for p in probes:
        emitted.add(routing.route(**p).rule_id)
    missing = emitted - documented
    assert not missing, f"以下规则未在 RULES_DOC 登记：{sorted(missing)}"
    unreached = documented - emitted
    assert not unreached, f"以下已登记规则无法被触达（死规则）：{sorted(unreached)}"


@check("路由表 YAML 的规则集与 RULES_DOC 一致")
def _():
    from poc.creditsentry import routing
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "agentteams", "routing-table.yaml"), encoding="utf-8").read()
    for r in routing.RULES_DOC:
        assert f"id: {r['id']}" in text, f"routing-table.yaml 缺少规则 {r['id']}"
    assert routing.ROUTING_TABLE_VERSION in text, "routing-table.yaml 版本号与代码不一致"


@check("每个 Skill 都在权限矩阵中被至少一个 Agent 授权")
def _():
    from poc.creditsentry import permissions
    for name, meta in skills.REGISTRY.items():
        assert meta.callers, f"Skill {name} 无任何授权 Agent"
        for c in meta.callers:
            assert name in permissions.PERMISSIONS[c]["skills"]


# ===========================================================================
# 七、模型配置：绑定与工具权限受同一套审计
# ===========================================================================

@check("模型配置四层分离：磁盘配置加载后全部不变量通过")
def _():
    from poc.creditsentry import modelconfig
    cfg = modelconfig.load()
    problems = cfg.check_invariants()
    assert not problems, "；".join(problems)


@check("异构对抗：定性官与质疑官必须绑定不同模型族")
def _():
    """拆成两个 Agent 是为了避免自洽坍缩。若共用同一批权重，
    坍缩只是从上下文层面搬到了权重层面——同族的两个实例不是两个独立观点。"""
    from poc.creditsentry import modelconfig
    cfg = modelconfig.load()
    a = cfg.profile_for("risk-analyst")
    b = cfg.profile_for("devils-advocate")
    assert a.family != b.family, f"两者同属模型族 {a.family}，对抗在权重层面坍缩"

    # 反向验证：刻意配成同族时必须被拒绝，否则这条约束形同虚设
    try:
        modelconfig.ModelConfig(cfg.providers, cfg.profiles, cfg.pii_allowed_residency,
                                cfg.adversarial_pairs,
                                {"devils-advocate": "analyst-primary"})
    except modelconfig.ConfigError:
        pass
    else:
        raise AssertionError("对抗双方被配成同族却未被拒绝")


@check("PII 围栏：唯一 PII 触点的模型不得越出数据驻留白名单")
def _():
    from dataclasses import replace
    from poc.creditsentry import modelconfig, permissions
    cfg = modelconfig.load()
    for agent in permissions.pii_touchpoints():
        prof = cfg.profile_for(agent)
        assert prof.provider.data_residency in cfg.pii_allowed_residency, (
            f"{agent} 的模型落在 {prof.provider.data_residency}")

    # 反向验证：把该 provider 挪到白名单外，必须被拒绝
    prov = dict(cfg.providers)
    target = cfg.profile_for(permissions.pii_touchpoints()[0]).provider.name
    prov[target] = replace(prov[target], data_residency="__outside__")
    profs = {k: replace(v, provider=prov[v.provider.name]) for k, v in cfg.profiles.items()}
    try:
        modelconfig.ModelConfig(prov, profs, cfg.pii_allowed_residency, cfg.adversarial_pairs)
    except modelconfig.ConfigError:
        pass
    else:
        raise AssertionError("PII 触点的模型越出数据驻留白名单却未被拒绝")


@check("唯一写触点不得持有模型入口（llm=False 即不得绑定 profile）")
def _():
    from poc.creditsentry import modelconfig, permissions
    cfg = modelconfig.load()
    for agent, e in permissions.PERMISSIONS.items():
        if not e["llm"]:
            assert e.get("model_profile") is None, f"{agent} 声明 llm=False 却绑定了模型"
    try:
        modelconfig.ModelConfig(cfg.providers, cfg.profiles, cfg.pii_allowed_residency,
                                cfg.adversarial_pairs,
                                {"disposition-executor": "analyst-primary"})
    except modelconfig.ConfigError:
        pass
    else:
        raise AssertionError("为纯规则驱动的执行方绑定模型却未被拒绝")


@check("凭证只以引用形式出现：配置文件中不含任何明文密钥")
def _():
    """配置可以进版本库，密钥不能。凭证一律是 env: / k8s-secret: / higress-consumer: 引用。"""
    from poc.creditsentry import modelconfig
    cfg = modelconfig.load()
    allowed = ("env:", "k8s-secret:", "higress-consumer:", "none:")
    for name, p in cfg.providers.items():
        assert p.credential_ref.startswith(allowed), (
            f"provider {name} 的 credential_ref={p.credential_ref!r} 不是受支持的引用形式")
    # 按密钥的**形态**匹配，而不是朴素子串——"risk-analyst" 里就含有 "sk-"，
    # 用子串会一直误报，误报多了这条断言迟早被人删掉。
    import re as _re
    SECRET_SHAPES = (
        _re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),      # OpenAI / DashScope 风格
        _re.compile(r"\bAKIA[0-9A-Z]{16}\b"),          # AK 形态
        _re.compile(r"\b[A-Fa-f0-9]{32,}\b"),          # 裸十六进制串
    )
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for fname in ("providers.yaml", "models.yaml"):
        text = open(os.path.join(root, "config", fname), encoding="utf-8").read()
        for pat in SECRET_SHAPES:
            hit = pat.search(text)
            assert not hit, f"{fname} 中疑似出现明文密钥：{hit.group()[:12]}…"


@check("Worker CR 中的模型绑定与权限矩阵一致")
def _():
    from poc.creditsentry import permissions
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for agent, e in permissions.PERMISSIONS.items():
        if agent == permissions.TEAM_LEADER:
            continue
        path = os.path.join(root, "agentteams", "workers", f"{agent}.yaml")
        text = open(path, encoding="utf-8").read()
        want = f"profile: {e['model_profile']}" if e["llm"] else "profile: null"
        assert want in text, f"{agent}.yaml 缺少或不匹配 {want!r}"


@check("YAML 子集解析器：往返一致且拒绝不支持的语法")
def _():
    """零依赖的代价是自带解析器，那它就必须自己被测——
    配置解析静默出错比直接失败危险得多。"""
    from poc.creditsentry import _miniyaml
    got = _miniyaml.parse(
        "a: 1\n"
        "b: hello  # 行尾注释\n"
        "c: [x, y, z]\n"
        "d:\n"
        "  e: true\n"
        "  f: null\n"
        "g:\n"
        "  - p: 1\n"
        "    q: two\n"
        "  - p: 2\n"
        "    q: three\n"
        "url: http://h/v1#frag\n"
    )
    assert got == {"a": 1, "b": "hello", "c": ["x", "y", "z"],
                   "d": {"e": True, "f": None},
                   "g": [{"p": 1, "q": "two"}, {"p": 2, "q": "three"}],
                   "url": "http://h/v1#frag"}, f"解析结果不符：{got}"
    for bad, why in (("a: 1\n\tb: 2\n", "制表符缩进"),
                     ("a: {x: 1}\n", "流式映射"),
                     ("a: 1\na: 2\n", "重复键")):
        try:
            _miniyaml.parse(bad)
        except _miniyaml.YamlError:
            continue
        raise AssertionError(f"未拒绝不支持的语法：{why}")


# ===========================================================================
# 八、上下文工程：清单、提示词、上下文装配
# ===========================================================================

@check("质疑清单由系统从断言派生，不由模型自报")
def _():
    """模型若能自己决定清单内容，就能通过少报主因来减轻自己的工作量。"""
    from poc.creditsentry import checklist
    assertion = {"root_causes": [
        {"type": "偿债能力恶化", "confidence": 0.85, "evidence_ids": ["EV-1"]},
        {"type": "资金用途异常", "confidence": 0.62, "evidence_ids": ["EV-2"]},
        {"type": "噪声项", "confidence": 0.20, "evidence_ids": ["EV-3"]},
    ]}
    cl = checklist.rebuttal_checklist(assertion)
    targets = [i.target for i in cl.items]
    assert targets == ["偿债能力恶化", "资金用途异常"], f"清单为 {targets}"
    assert cl.coverage() == 0.0 and not cl.complete(), "新建清单不应已完成"


@check("质疑覆盖不足即阻断：未被质疑的主因不等于质疑通过")
def _():
    """这是本轮补上的一个真实漏洞——此前质疑方可以对某条主因保持沉默，
    系统无法区分「反驳失败」与「根本没看」。"""
    from poc.creditsentry import checklist, routing
    assertion = {"conclusion": "RISK_CONFIRMED", "suggested_grade": "关注",
                 "root_causes": [
                     {"type": "A", "confidence": 0.8, "evidence_ids": ["EV-1"]},
                     {"type": "B", "confidence": 0.7, "evidence_ids": ["EV-2"]}]}
    rebuttal = {"verdict": "SUPPORTED", "rebuttals": [], "surviving_causes": ["A", "B"]}

    cl = checklist.rebuttal_checklist(assertion)
    cl.mark_by_target("A", checklist.ATTEMPTED_FAILED, "试过了，反驳不成立")
    # B 刻意不标记
    assert cl.coverage() == 0.5, f"覆盖率应为 0.5，实际 {cl.coverage()}"

    full = routing.adjudicate(assertion, rebuttal)
    assert full["verdict"] == "RISK_CONFIRMED", "无清单时应按原逻辑采信"

    partial = routing.adjudicate(assertion, rebuttal, rebuttal_checklist=cl.to_dict())
    assert partial["verdict"] == "EVIDENCE_INSUFFICIENT", (
        f"覆盖不足却放行了：{partial['verdict']}")
    assert "B" in partial["basis"], f"未指明是哪条主因没被质疑：{partial['basis']}"

    cl.mark_by_target("B", checklist.REFUTED, "找到反证")
    done = routing.adjudicate(assertion, rebuttal, rebuttal_checklist=cl.to_dict())
    assert done["verdict"] == "RISK_CONFIRMED", "补齐覆盖后应恢复正常裁决"


@check("三条链路的质疑清单覆盖率均为 100%")
def _():
    for case in ("case_001", "case_002", "case_003"):
        world, tracer, mcp = _fresh(case)
        st = Orchestrator(world, mcp, get_llm("stub")).run()
        cl = st.rebuttal_checklist
        assert cl is not None, f"{case} 未生成质疑清单"
        assert cl.complete(), (
            f"{case} 质疑清单未覆盖完全：{[i.target for i in cl.unaddressed()]}")


@check("取证清单：应取而未取的事实被补登记为显式缺口，不会静悄悄消失")
def _():
    """没有这一步，一条本该取而未取的事实会从证据链里消失得无声无息——
    「输出取证清单交人工」这句话也就无从落地。"""
    from poc.creditsentry import checklist
    ecl = checklist.evidence_checklist(["judicial_new_case", "guarantee_contagion"])
    targets = {i.target for i in ecl.items}
    assert {"litigation_case", "registration_change", "guarantee_entry"} <= targets, (
        f"取证清单未覆盖必需事实：{sorted(targets)}")
    assert not ecl.complete(), "新建清单不应已完成"

    for case in ("case_001", "case_003"):
        world, tracer, mcp = _fresh(case)
        st = Orchestrator(world, mcp, get_llm("stub")).run()
        assert st.evidence_checklist is not None, f"{case} 未生成取证清单"
        assert st.evidence_checklist.complete(), (
            f"{case} 存在既未取到、也未登记为缺口的事实："
            f"{[i.target for i in st.evidence_checklist.unaddressed()]}")
        # 每条标记为 GAP 的事实都必须在证据缺口里有对应登记
        gaps = {i.target for i in st.evidence_checklist.items
                if i.status == checklist.GAP}
        assert gaps <= {g["fact_type"] for g in st.evidence_gaps}, (
            f"{case} 清单标记为 GAP 的事实未登记进证据缺口："
            f"{gaps - {g['fact_type'] for g in st.evidence_gaps}}")


@check("提示词模板带版本号，且缺槽位 / 多余槽位一律抛错")
def _():
    from poc.creditsentry import prompts
    for name in ("risk_root_cause", "devils_advocate"):
        t = prompts.get(name)
        assert t.version and t.objective, f"{name} 缺版本号或目标函数"
    # 对抗双方的目标函数必须真的不同，否则拆成两个角色没有意义
    a = prompts.get("risk_root_cause").objective
    b = prompts.get("devils_advocate").objective
    assert a != b, "对抗双方的目标函数相同"

    for bad, why in (({}, "缺槽位"), ({"policy_note": "x", "nope": 1}, "多余槽位")):
        try:
            prompts.render("risk_root_cause", **bad)
        except prompts.PromptError:
            continue
        raise AssertionError(f"未拒绝：{why}")

    # 渲染后不得残留未填槽位。注意正文里合法存在输出示例的花括号
    # （如 `{conclusion, root_causes[...]}`），所以只能按槽位模式匹配，不能查裸花括号。
    import re as _re
    for name in ("risk_root_cause", "devils_advocate"):
        t = prompts.get(name)
        slots = {s: "测试" for s in t.slots if s != "objective"}
        text, ver = prompts.render(name, **slots)
        left = _re.findall(r"\{[a-z_]+\}", text)
        assert not left, f"{name} 渲染后仍残留槽位 {left}"
        assert ver == t.version


@check("Trace 中记录了提示词版本与上下文装配账单")
def _():
    world, tracer, mcp = _fresh("case_001")
    Orchestrator(world, mcp, get_llm("stub")).run()
    llm_spans = [s for s in tracer.spans if s.kind == "llm"]
    assert llm_spans, "没有 llm Span"
    for s in llm_spans:
        assert s.attributes.get("prompt.version"), f"{s.name} 缺 prompt.version"
        man = s.attributes.get("context.manifest")
        assert man and man.get("included"), f"{s.name} 缺上下文装配账单"


@check("上下文装配：原文灌入被拒、必需块不被裁剪、裁剪留痕")
def _():
    from poc.creditsentry import context
    # 原文灌入：证据引用里出现长文本即抛错
    try:
        context.ContextAssembler().add_evidence_refs(
            "facts", [{"note": "很长" * 500}])
    except context.ContextError:
        pass
    else:
        raise AssertionError("把原文灌进上下文却未被拒绝")

    # 裁剪：低优先级非必需块被丢，且留痕；必需块保留
    a = context.ContextAssembler(budget_chars=300)
    a.add("must", {"k": "x" * 100}, priority=10, required=True)
    a.add("optional", {"k": "y" * 500}, priority=90)
    payload, man = a.build()
    assert "must" in payload and "optional" not in payload
    assert man.dropped and man.dropped[0]["key"] == "optional", "裁剪未留痕"
    assert man.dropped[0].get("why"), "裁剪未说明原因（静默截断）"

    # 必需块本身超预算 → 抛错，而不是硬塞或悄悄裁掉
    b = context.ContextAssembler(budget_chars=50)
    b.add("must", {"k": "x" * 500}, required=True)
    try:
        b.build()
    except context.ContextError:
        return
    raise AssertionError("必需块超预算却未抛错")


# ===========================================================================
# 九、查询改写：六维与澄清选路
# ===========================================================================

@check("查询改写：求证方不得产生否定式维，证伪方必须产生")
def _():
    """两个角色若产生相同的子查询集合，说明对抗只停留在提示词措辞上。"""
    from poc.creditsentry import querying
    prove = querying.rewrite(caller="risk-analyst", base_query="涉诉 偿债能力",
                             stance=querying.PROVE)
    refute = querying.rewrite(caller="devils-advocate", base_query="涉诉 偿债能力",
                              stance=querying.REFUTE)
    assert "negation" not in prove.dimensions_used, "求证方产生了否定式子查询"
    assert "negation" in refute.dimensions_used, "证伪方缺少否定式子查询"
    assert {q.text for q in prove.subqueries} != {q.text for q in refute.subqueries}, (
        "对抗双方的检索集合完全相同，检索层没有体现立场差异")


@check("查询改写：给定案件时点后，全部子查询都带生效日过滤")
def _():
    from poc.creditsentry import querying
    plan = querying.rewrite(caller="risk-analyst", base_query="担保圈 代偿",
                            stance=querying.REFUTE, signal_types=["guarantee_contagion"],
                            as_of="2017-04-01")
    off = [q.dimension for q in plan.subqueries
           if q.filters.get("effective_before") != "2017-04-01"]
    assert not off, f"子查询 {off} 未带时点过滤，存在知识维前视污染风险"


@check("澄清选路：能自动解决的不问人，能派任务的不问开放问题")
def _():
    """银行场景里补充信息的成本不在用户打字，在于去哪个系统查、有没有授权。"""
    from poc.creditsentry import querying
    cs = {c.clarification_id: c for c in querying.detect_clarifications({
        "litigation": {"ambiguous": True, "partial": True},
        "guarantee": {"distressed_parties": [{"party": "X", "status_basis": ""}]},
        "transaction": {"undersampled": True},
    })}
    assert cs["CLR-1"].channel == querying.SYSTEM_TASK and cs["CLR-1"].task
    assert cs["CLR-2"].channel == querying.SYSTEM_TASK
    assert cs["CLR-4"].channel == querying.AUTO, "已有明确规则的仍去打扰人"
    human = cs["CLR-3"]
    assert human.channel == querying.HUMAN_CHOICE and human.blocking
    assert human.options, "问人却没给选项——开放问题的回答无法结构化、无法进证据账本"


@check("知识维无前视：回溯案件召回的条款生效日均不晚于案件时点")
def _():
    """与证据层的时点冻结是同一条纪律。用 2025 年的行内制度评价 2017 年的案子，
    只是更隐蔽——条款看起来「一直都在」。"""
    world, tracer, mcp = _fresh("case_003")
    orch = Orchestrator(world, mcp, get_llm("stub"))
    orch.run()
    as_of = world.as_of
    late = [(e.extracted.get("source"), e.extracted.get("effective_date"))
            for e in orch.ledger.all()
            if e.source_system == "policy-kb"
            and (e.extracted.get("effective_date") or "") > as_of]
    assert not late, f"召回了案件时点 {as_of} 之后才生效的条款：{late}"
    assert any(e.source_system == "policy-kb" for e in orch.ledger.all()), (
        "一条条款都没召回，过滤过度")


# ===========================================================================
# live 接口层：输出契约、修复轮与失败策略
# ===========================================================================
# 这一组断言存在的理由：stub 模式下模型输出由代码构造，字段必然齐全；
# 接真实端点后它由模型生成，没有任何东西保证它齐全。下面每一条对应的
# 都是「只有在模型表现不好时才会被执行到」的代码路径。

@check("输出契约：两种推理模式产出同一套 Schema（stub 输出也必须过校验）")
def _():
    """README 声称「两种模式产出同一套 JSON Schema」。这条断言把它变成事实：
    确定性推理器的输出如果过不了给 live 用的校验器，那句话就是假的。"""
    from poc.creditsentry import schemas
    from poc.creditsentry.llm import _STUB_REASONERS
    world = World.load("case_001")
    tracer = Tracer()
    mcp = MCPClient(world, tracer)
    llm = get_llm("stub")
    orch = Orchestrator(world, mcp, llm)
    st = orch.run()
    for task, obj in (("risk_root_cause", st.assertion),
                      ("devils_advocate", st.rebuttal)):
        assert not schemas.validate(task, obj), \
            f"{task} 的 stub 输出过不了 live 的 Schema 校验：{schemas.validate(task, obj)}"
    assert set(_STUB_REASONERS) == set(schemas.SCHEMAS), \
        "推理器与输出契约的 task 集合不一致，两边会漂移"


@check("输出契约：非法枚举、无证据根因、置信度越界一律被拒（含反向验证）")
def _():
    from poc.creditsentry import schemas
    good = {"conclusion": "RISK_CONFIRMED", "suggested_grade": "关注", "summary": "x",
            "root_causes": [{"type": "偿债能力恶化", "confidence": 0.8,
                             "evidence_ids": ["EV-001-0001"]}]}
    assert not schemas.validate("risk_root_cause", good)

    bad_cases = {
        "自造枚举值": {**good, "conclusion": "HIGH_RISK"},
        "无证据根因": {**good, "root_causes": [{"type": "x", "confidence": 0.8,
                                                "evidence_ids": []}]},
        "置信度越界": {**good, "root_causes": [{"type": "x", "confidence": 1.7,
                                                "evidence_ids": ["EV-001-0001"]}]},
        "缺必填字段": {k: v for k, v in good.items() if k != "conclusion"},
        "非法五级分类": {**good, "suggested_grade": "高危"},
    }
    for name, obj in bad_cases.items():
        assert schemas.validate("risk_root_cause", obj), f"{name} 未被拒绝"

    # 质疑侧：verdict 越界会让 routing.adjudicate 的 verdict_map 直接 KeyError，
    # 因此必须在契约层拦住而不是等它崩
    from poc.creditsentry import routing
    for v in schemas.VERDICTS:
        assert v in routing.adjudicate({"conclusion": "RISK_CONFIRMED"},
                                       {"verdict": v})["verdict"] or True
    assert schemas.validate("devils_advocate",
                            {"verdict": "MAYBE", "checklist_resolutions": []})


@check("归一化只吸收表述差异，绝不伪造内容")
def _():
    """把 "62%" 改成 0.62 是纠正表述；给没有证据的结论编一个 ID 是伪造证据。
    这条断言守的就是这条线。"""
    from poc.creditsentry import schemas
    loose = {"result": {"conclusion": "risk_confirmed", "suggested_grade": "关注",
                        "root_causes": [{"type": "x", "confidence": "62%",
                                         "evidence_ids": "EV-001-0001"}]}}
    out = schemas.normalize("risk_root_cause", loose)
    assert out["conclusion"] == "RISK_CONFIRMED", "大小写未归一"
    assert out["root_causes"][0]["confidence"] == 0.62, "百分数未还原"
    assert out["root_causes"][0]["evidence_ids"] == ["EV-001-0001"], "裸值未包成列表"
    assert not schemas.validate("risk_root_cause", out), "归一化后仍不合规"

    # 反向：证据为空时不得被「补」出来
    empty = schemas.normalize("risk_root_cause",
                              {"conclusion": "RISK_CONFIRMED", "suggested_grade": None,
                               "root_causes": [{"type": "x", "confidence": 0.8,
                                                "evidence_ids": []}]})
    assert empty["root_causes"][0]["evidence_ids"] == [], "归一化伪造了证据引用"
    assert schemas.validate("risk_root_cause", empty), "无证据的根因未被拒绝"


@check("质疑清单校验器：漏项与对不上的回执都算未覆盖")
def _():
    from poc.creditsentry import schemas
    v = schemas.checklist_validator(["R1", "R2"], ["主因甲", "主因乙"])
    assert not v({"checklist_resolutions": [
        {"item_id": "R1", "status": "REFUTED"},
        {"target": "主因乙", "status": "ATTEMPTED_FAILED"}]}), "按 target 对齐应被接受"
    assert v({"checklist_resolutions": [{"item_id": "R1", "status": "REFUTED"}]}), \
        "漏了 R2 却判为覆盖完整"
    assert v({"checklist_resolutions": [
        {"item_id": "R9", "status": "REFUTED"}]}), "对不上的回执被当作有效覆盖"


@check("失败策略：确定性推理器输出违约时立即失败，不进修复轮")
def _():
    """修复轮是给模型的，不是给代码 bug 的。让代码缺陷走修复轮，
    等于把必现问题伪装成偶发抖动。"""
    from poc.creditsentry import llm as llmmod
    from poc.creditsentry.llm import InferenceError, get_llm as _get
    gw = _get("stub")
    saved = llmmod._STUB_REASONERS["risk_root_cause"]
    llmmod._STUB_REASONERS["risk_root_cause"] = lambda p: {"conclusion": "什么都不是"}
    try:
        gw.complete_json("risk_root_cause", "sys", {"facts": {}}, caller="risk-analyst")
    except InferenceError as e:
        assert e.attempts == 1, f"确定性推理器不该重试，却试了 {e.attempts} 次"
    else:
        raise AssertionError("确定性推理器输出违约却未被拒绝")
    finally:
        llmmod._STUB_REASONERS["risk_root_cause"] = saved


@check("失败策略：定性失败降级为证据不足并回退，质疑失败一律阻断")
def _():
    """§1.5 失败策略表的执行点。两个方向都必须朝向「不往下走」——
    定性失败不得凭现有信息猜一个结论，质疑失败不得视同质疑通过。"""
    from poc.creditsentry import routing
    degraded_assertion = {"conclusion": "INSUFFICIENT", "root_causes": [],
                          "suggested_grade": None}
    r1 = routing.adjudicate(degraded_assertion, {"verdict": "SUPPORTED"})
    assert r1["verdict"] == "EVIDENCE_INSUFFICIENT", "定性降级后未回退补证"
    assert r1["final_grade"] is None, "回退补证却仍给出了五级分类"

    r2 = routing.adjudicate({"conclusion": "RISK_CONFIRMED",
                             "root_causes": [{"type": "x", "confidence": 0.9}]},
                            {"verdict": "INSUFFICIENT_EVIDENCE"})
    assert r2["verdict"] == "EVIDENCE_INSUFFICIENT", "质疑失效却未阻断"


@check("回退次数用尽即转人工：裁决反复判证据不足不得形成环路")
def _():
    """R-03 只管住了「证据充分度不达标」这条入口。裁决判证据不足时充分度可能是
    达标的（质疑器失效、清单反复覆盖不全），回退后会命中 R-04 再次派发，
    形成 R-04 ⇄ R-05 的环。stub 的确定性掩盖了它，接真实模型后会出现。"""
    from poc.creditsentry import routing
    d = routing.route(phase="ADJUDICATION", signal_types=[], evidence_sufficiency=0.95,
                      risk_tier=None, exposure_amount=1000,
                      adjudication_verdict="EVIDENCE_INSUFFICIENT",
                      evidence_retries=routing.MAX_EVIDENCE_RETRIES)
    assert d.rule_id == "R-12" and d.next_phase == "EVIDENCE_GAP", \
        f"回退次数用尽却仍在循环：{d.rule_id} → {d.next_phase}"


@check("绑定预设与 --profile 同样受四条不变量约束，不是绕过约束的后门")
def _():
    from poc.creditsentry import modelconfig, permissions
    for name in modelconfig.list_presets():
        cfg = modelconfig.resolve(name, None)   # 构造即校验，违规会抛 ConfigError
        a, b = cfg.profile_for("risk-analyst"), cfg.profile_for("devils-advocate")
        assert a.family != b.family, f"预设 {name} 让对抗双方落在同族 {a.family}"
        for agent in permissions.pii_touchpoints():
            assert cfg.profile_for(agent).provider.data_residency in \
                cfg.pii_allowed_residency, f"预设 {name} 让 PII 触点越出白名单"

    # 反向验证：预设里塞一个同族绑定，必须被拒绝
    try:
        modelconfig.load(overrides={"devils-advocate": "analyst-primary"})
    except modelconfig.ConfigError:
        pass
    else:
        raise AssertionError("覆盖成同族模型却未被拒绝")


# ===========================================================================
# 工作台：因子驱动与人工审批
# ===========================================================================

@check("结论由证据驱动：翻转决定性因子必须翻转对应主因的质疑结论")
def _():
    """工作台上「让评委改一个字段当场重跑」这一招，靠的就是这条性质。
    如果改掉决定性因子结论纹丝不动，说明结论其实是硬编码的——
    那么前面所有关于证据链的说法都不成立。"""
    from mcp_servers.world import FACTORS
    target_of = {
        "litigation_material": "偿债能力恶化",
        "txn_counterparty": "资金用途异常",
        "registration_change": "实际控制人风险",
    }
    for f in FACTORS["case_001"]:
        if f["key"] not in target_of:
            continue
        got = []
        for opt in f["options"]:
            world = World.load("case_001", opt["patch"])
            tracer = Tracer()
            st = Orchestrator(world, MCPClient(world, tracer), get_llm("stub")).run()
            refuted = {r["target"] for r in (st.rebuttal or {}).get("rebuttals", [])}
            got.append(target_of[f["key"]] in refuted)
        assert got == [False, True], (
            f"因子「{f['label']}」翻转后，主因「{target_of[f['key']]}」的质疑结论未随之改变"
            f"（原值被推翻={got[0]}，翻转后被推翻={got[1]}）")

    # 敞口维：同一个可逆动作，620 万 → L2，2 亿 → L3
    tiers = []
    for opt in next(f for f in FACTORS["case_001"] if f["key"] == "exposure")["options"]:
        world = World.load("case_001", opt["patch"])
        tracer = Tracer()
        st = Orchestrator(world, MCPClient(world, tracer), get_llm("stub")).run()
        tiers.append((st.gate or {}).get("action_tier"))
    assert tiers == ["L2", "L3"], f"敞口维未在分级：{tiers}"


@check("因子白名单：路径写错立即抛错，不静默创建")
def _():
    """静默创建会让拼错的路径表现为「改了但没效果」。
    在演示现场，这比直接报错糟糕得多。"""
    World.load("case_001", {"credit_core.total_exposure": 1})   # 合法路径
    for bad in ("judicial.nope", "judicial.cases.[99].closed", "nothing.at.all"):
        try:
            World.load("case_001", {bad: 1})
        except KeyError:
            continue
        raise AssertionError(f"非法路径 {bad!r} 未被拒绝")


@check("人工审批回调：驳回即降级 L0，且全程零写操作")
def _():
    """工作台把这条回调接到浏览器按钮上。此处断言的是回调契约本身：
    无论决策来自 CLI 开关还是人点击，被驳回的结果必须一致。"""
    world = World.load("case_001")
    tracer = Tracer()
    mcp = MCPClient(world, tracer)
    seen: list[dict] = []

    def handler(req):
        seen.append(req)
        return {"approved": False, "approver": "风险经理-钱", "reason": "先补财报"}

    st = Orchestrator(world, mcp, get_llm("stub"), approval_handler=handler).run()
    assert seen, "L2 案件未触发审批回调——审批闸门没有生效"
    assert seen[0]["action_tier"] == "L2", f"审批请求携带的层级不对：{seen[0]['action_tier']}"
    assert st.approval["decision"] == "REJECTED"
    assert st.approval["approver"] == "风险经理-钱", "审批人未透传"
    assert st.gate["action_tier"] == "L0", "审批被拒却未降级为只读"
    writes = [a for a in mcp.audit_log
              if a["tool"] in ("adjust_limit", "add_guarantee") and a["status"] == "OK"]
    assert not writes, f"审批被拒却仍发生了写操作：{[w['tool'] for w in writes]}"


@check("实时旁路不改变业务结果，且观察者抛错不影响链路")
def _():
    """Trace 观察者与日志出口是旁路，不是主路。
    可视化坏了必须不影响案件处置——方向性反了就成了「因为要展示所以跑挂了」。"""
    base = Orchestrator(w0 := World.load("case_001"),
                        MCPClient(w0, t0 := Tracer()), get_llm("stub")).run()

    world = World.load("case_001")
    tracer = Tracer()
    tracer.observers.append(lambda ev, sp: (_ for _ in ()).throw(RuntimeError("观察者炸了")))
    st = Orchestrator(world, MCPClient(world, tracer), get_llm("stub"),
                      log_sink=lambda rec: (_ for _ in ()).throw(RuntimeError("出口炸了"))).run()

    assert st.adjudication["verdict"] == base.adjudication["verdict"]
    assert st.gate["action_tier"] == base.gate["action_tier"]
    assert st.assertion == base.assertion, "旁路观察者改变了业务结果"


@check("原件可翻开且必须校验哈希：篡改快照会被当场识破")
def _():
    """「结论有据可查」只有当复核的人能当场翻到那份材料时才成立。
    但把原文摆出来的同时必须校验哈希——账本记哈希、快照存别处，
    两者对不上就说明有一方被动过，这时候界面必须告警而不是若无其事地显示。"""
    from poc.creditsentry import humanize
    world = World.load("case_001")
    tracer = Tracer()
    orch = Orchestrator(world, MCPClient(world, tracer), get_llm("stub"))
    orch.run()

    checked = 0
    for ev in orch.ledger.all():
        raw, ok = orch.ledger.snapshot(ev.evidence_id)
        assert ok, f"{ev.evidence_id} 的快照哈希与账本不符"
        doc = humanize.render_snapshot(ev.to_dict(), raw, ok)
        assert doc["title"] and doc["issuer"], f"{ev.evidence_id} 的原件缺标题或签发方"
        body = doc["body"]
        assert body["kind"] in ("text", "doc")
        if body["kind"] == "doc":
            # 内部锚点不得出现在给人看的正文里
            labels = [r[0] for r in body["rows"]]
            assert "source_doc_uri" not in labels and "_redacted_fields" not in labels, \
                f"{ev.evidence_id} 的原件正文里混进了内部字段：{labels}"
        checked += 1
    assert checked >= 10, f"只校验了 {checked} 条，覆盖不足"

    # 反向验证：篡改快照后必须被识破
    victim = orch.ledger.all()[0].evidence_id
    orch.ledger._snapshots[victim] = "（被改动过的内容）"
    _, ok = orch.ledger.snapshot(victim)
    assert not ok, "快照被篡改却仍通过哈希校验"


@check("PII 围栏对境外端点同样生效：数据出境的 provider 不得绑定 PII 触点")
def _():
    """百炼国际站（新加坡）节点对境内银行属数据出境，因此 data_residency 标为
    overseas，不在白名单内。这条断言存在的意义是：**围栏必须挡住真实存在的端点**，
    而不是只挡住一个虚构的反例——config 里现在真的有这样一个 provider。"""
    from poc.creditsentry import modelconfig, permissions
    cfg = modelconfig.load()
    overseas = [n for n, p in cfg.providers.items() if p.data_residency == "overseas"]
    assert overseas, "配置中没有境外 provider，这条断言失去了被测对象"

    # 找一个绑定到境外 provider 的 profile，拿它去绑 PII 触点，必须被拒绝
    prof = next(n for n, p in cfg.profiles.items() if p.provider.name in overseas)
    touchpoint = permissions.pii_touchpoints()[0]
    try:
        modelconfig.load(overrides={touchpoint: prof})
    except modelconfig.ConfigError as e:
        assert "数据驻留" in str(e) or "驻留区" in str(e), f"拒绝理由不对：{e}"
    else:
        raise AssertionError(f"把 PII 触点 {touchpoint} 绑到境外 provider 却未被拒绝")

    # 而定性/质疑侧绑境外是允许的——它们不碰 PII
    modelconfig.load(overrides={"risk-analyst": prof,
                                "devils-advocate": "advocate-primary"})


@check("运行时凭证只进内存：优先于环境变量，且不落入任何产物")
def _():
    """工作台允许在界面上填 API Key，否则演示者得先改 shell 再重启服务。
    但这不能松动「密钥不进配置、不进版本库」——改变的只是「到哪里取值」。"""
    import json as _json
    import os as _os
    from dataclasses import replace
    from poc.creditsentry import modelconfig

    cfg = modelconfig.load()
    prov = replace(cfg.providers["dashscope"], credential_ref="env:__CS_TEST_KEY__")
    sentinel = "runtime-only-secret-value"
    try:
        # 未设置时必须抛错，而不是静默返回空串放行
        try:
            cfg.resolve_credential(prov)
        except modelconfig.CredentialError:
            pass
        else:
            raise AssertionError("凭证缺失却未被拒绝")

        _os.environ["__CS_TEST_KEY__"] = "from-env"
        assert cfg.resolve_credential(prov) == "from-env"

        # 运行时凭证优先：界面上刚填的那个不该被陈旧的 shell 变量盖住
        modelconfig.set_runtime_credential("__CS_TEST_KEY__", sentinel)
        assert cfg.resolve_credential(prov) == sentinel, "运行时凭证未覆盖环境变量"
        assert "__CS_TEST_KEY__" in modelconfig.runtime_credential_names()
        assert modelconfig.credential_source("__CS_TEST_KEY__") == "runtime"

        # 只暴露名字与来源，没有任何取值接口
        assert not any(n for n in dir(modelconfig)
                       if n.startswith("get_runtime_credential")), \
            "不应存在读取运行时凭证明文的公开接口"

        # 跑一整条链路，断言凭证不出现在任何产物里
        world = World.load("case_001")
        tracer = Tracer()
        mcp = MCPClient(world, tracer)
        orch = Orchestrator(world, mcp, get_llm("stub"))
        st = orch.run()
        blob = _json.dumps([tracer.to_dict(), orch.ctx.logs, mcp.audit_log,
                            orch.ledger.to_dict(), st.to_dict()],
                           ensure_ascii=False, default=str)
        assert sentinel not in blob, "运行时凭证泄漏进了 Trace / 日志 / 审计 / 账本"

        modelconfig.set_runtime_credential("__CS_TEST_KEY__", "")
        assert modelconfig.credential_source("__CS_TEST_KEY__") == "env", "清除未生效"
    finally:
        modelconfig.set_runtime_credential("__CS_TEST_KEY__", "")
        _os.environ.pop("__CS_TEST_KEY__", None)


def main() -> int:
    print("信衡 CreditSentry · 安全边界回归测试")
    print("=" * 72)
    for name in PASSED:
        print(f"  通过    {name}")
    for name, err in FAILED:
        print(f"  失败    {name}\n          → {err}")
    print("=" * 72)
    print(f"合计 {len(PASSED) + len(FAILED)} 项：{len(PASSED)} 通过，{len(FAILED)} 失败")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
