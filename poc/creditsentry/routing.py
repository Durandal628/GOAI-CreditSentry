"""RiskCommander 的 SOP 阶段状态机路由。

核心原则：**LLM 只做阶段内判定，不决定流程走向。**

金融场景要求可复现、可审计——纯 LLM 自由编排不可审计，因此流程骨架必须是确定性的。
路由表在此以 Python 常量表达，生产部署时由 Nacos 下发同构的 YAML（见
``agentteams/routing-table.yaml``），支持标签灰度与一键回滚。

每一次路由决策都会落一条 ``routing`` Span，记录 routing_key、命中规则 ID 与规则版本，
使「为什么当时走了这条分支」可举证、可回放。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ROUTING_TABLE_VERSION = "v1.4.0"

# 证据充分度阈值：低于此值不得进入裁决阶段
EVIDENCE_SUFFICIENCY_THRESHOLD = 0.7
# 取证重试上限：超过后转 EVIDENCE_GAP 交人工，杜绝无限循环
MAX_EVIDENCE_RETRIES = 2


# ---------------------------------------------------------------------------
# 路由表的声明式镜像
# ---------------------------------------------------------------------------
# 生产部署时这张表由 Nacos 以 YAML 下发（见 agentteams/routing-table.yaml，
# 由 tools/gen_agentteams.py 从本列表生成），支持标签灰度与一键回滚。
# poc/test_safety.py 断言 route() 能产出的每个 rule_id 都在此登记，防止两边漂移。

RULES_DOC: list[dict[str, Any]] = [
    {"id": "R-01", "phase": "INTAKE", "when": "always",
     "next": "EVIDENCE", "dispatch": ["signal-hub"], "parallel": False,
     "desc": "新预警进入，先做信号归并与敞口测绘"},
    {"id": "R-02", "phase": "EVIDENCE",
     "when": f"evidence_sufficiency < {EVIDENCE_SUFFICIENCY_THRESHOLD} "
             f"and evidence_retries < {MAX_EVIDENCE_RETRIES}",
     "next": "EVIDENCE", "dispatch": ["due-diligence"], "parallel": False,
     "desc": "证据不充分，发起补充取证"},
    {"id": "R-03", "phase": "EVIDENCE",
     "when": f"evidence_sufficiency < {EVIDENCE_SUFFICIENCY_THRESHOLD} "
             f"and evidence_retries >= {MAX_EVIDENCE_RETRIES}",
     "next": "EVIDENCE_GAP", "dispatch": [], "parallel": False,
     "desc": "取证重试用尽仍不充分，输出取证清单转人工"},
    {"id": "R-04", "phase": "EVIDENCE",
     "when": f"evidence_sufficiency >= {EVIDENCE_SUFFICIENCY_THRESHOLD}",
     "next": "ADJUDICATION", "dispatch": ["risk-analyst", "devils-advocate"], "parallel": True,
     "desc": "硬约束：强制并行派发定性与质疑，质疑环节不可跳过"},
    {"id": "R-05", "phase": "ADJUDICATION", "when": "verdict == EVIDENCE_INSUFFICIENT",
     "next": "EVIDENCE", "dispatch": ["due-diligence"], "parallel": False,
     "desc": "分歧源于证据缺失，回退补证（全流程唯一回退边）"},
    {"id": "R-06", "phase": "ADJUDICATION", "when": "verdict == RISK_REFUTED",
     "next": "DISPOSITION", "dispatch": [], "parallel": False,
     "desc": "定性被推翻，仍出具「维持原状 + 加强监测」结论并留痕"},
    {"id": "R-07", "phase": "ADJUDICATION", "when": "verdict == RISK_CONFIRMED",
     "next": "DISPOSITION", "dispatch": [], "parallel": False,
     "desc": "裁决风险成立，进入执行闸门定级"},
    {"id": "R-08", "phase": "DISPOSITION", "when": "action_tier == L3",
     "next": "AUDIT", "dispatch": [], "parallel": False,
     "desc": "L3 不可逆动作，仅生成方案，不派发执行"},
    {"id": "R-09", "phase": "DISPOSITION", "when": "action_tier == L2 and not approved",
     "next": "DISPOSITION", "dispatch": [], "parallel": False,
     "desc": "L2 需人工审批，房间内 @风险经理并等待审批回调"},
    {"id": "R-10", "phase": "DISPOSITION", "when": "action_tier in (L0, L1) or approved",
     "next": "AUDIT", "dispatch": ["disposition-executor"], "parallel": False,
     "desc": "条件满足，派发执行后进入审计"},
    {"id": "R-11", "phase": "AUDIT", "when": "always",
     "next": "CLOSED", "dispatch": ["compliance-auditor"], "parallel": False,
     "desc": "审计核验、合规校验与经验沉淀，案件闭环"},
    {"id": "R-12", "phase": "ADJUDICATION",
     "when": f"verdict == EVIDENCE_INSUFFICIENT and evidence_retries >= {MAX_EVIDENCE_RETRIES}",
     "next": "EVIDENCE_GAP", "dispatch": [], "parallel": False,
     "desc": "裁决反复判定证据不足且回退次数用尽，转人工"},
]


@dataclass
class RoutingDecision:
    routing_key: dict[str, Any]
    rule_id: str
    rule_version: str
    next_phase: str
    dispatch: list[str]          # 本阶段要 fan-out 的 Worker
    parallel: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "routing_key": self.routing_key,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "next_phase": self.next_phase,
            "dispatch": self.dispatch,
            "parallel": self.parallel,
            "reason": self.reason,
        }


def route(
    *,
    phase: str,
    signal_types: list[str],
    evidence_sufficiency: float,
    risk_tier: str | None,
    exposure_amount: float | None,
    evidence_retries: int = 0,
    adjudication_verdict: str | None = None,
    action_tier: str | None = None,
    approved: bool | None = None,
) -> RoutingDecision:
    """按五元组路由键决定下一跳。第一条命中的规则即返回。"""
    key = {
        "phase": phase,
        "signal_type": signal_types,
        "evidence_sufficiency": evidence_sufficiency,
        "risk_tier": risk_tier,
        "exposure_amount": exposure_amount,
    }

    def d(rule_id: str, nxt: str, dispatch: list[str], reason: str,
          parallel: bool = False) -> RoutingDecision:
        return RoutingDecision(key, rule_id, ROUTING_TABLE_VERSION, nxt,
                               dispatch, parallel, reason)

    # ---- R-01 INTAKE：归并与影响面测绘 --------------------------------
    if phase == "INTAKE":
        return d("R-01", "EVIDENCE", ["signal-hub"],
                 "新预警进入，先做信号归并与敞口测绘")

    # ---- R-02 / R-03 EVIDENCE：取证充分性判定 -------------------------
    if phase == "EVIDENCE":
        if evidence_sufficiency < EVIDENCE_SUFFICIENCY_THRESHOLD:
            if evidence_retries < MAX_EVIDENCE_RETRIES:
                return d("R-02", "EVIDENCE", ["due-diligence"],
                         f"证据充分度 {evidence_sufficiency} < {EVIDENCE_SUFFICIENCY_THRESHOLD}，"
                         f"第 {evidence_retries + 1} 轮补充取证（上限 {MAX_EVIDENCE_RETRIES} 轮）")
            # 重试用尽仍不足：转人工，不硬着头皮往下走
            return d("R-03", "EVIDENCE_GAP", [],
                     f"取证重试已达上限 {MAX_EVIDENCE_RETRIES} 轮仍不充分，"
                     f"输出取证清单转人工处理")
        # R-04 硬约束：强制并行 fan-out 给定性官与质疑官，质疑官不可跳过
        return d("R-04", "ADJUDICATION", ["risk-analyst", "devils-advocate"],
                 "证据充分，强制并行派发定性与质疑（质疑环节为硬约束，不可跳过）",
                 parallel=True)

    # ---- R-05 ~ R-07 ADJUDICATION：裁决收敛 ---------------------------
    if phase == "ADJUDICATION":
        if adjudication_verdict == "EVIDENCE_INSUFFICIENT":
            # R-12 回退次数上限。R-03 只管住了「证据充分度不达标」这条路径；
            # 但裁决判证据不足时证据充分度可能是达标的（例如质疑器本身失效、
            # 或质疑清单反复覆盖不全），此时回退到 EVIDENCE 会命中 R-04 再次派发，
            # 形成 R-04 ⇄ R-05 的环。stub 模式下推理确定性使这个环不可能出现，
            # 接真实模型后它会出现——这是「重试用尽即转人工」必须覆盖的第二条入口。
            if evidence_retries >= MAX_EVIDENCE_RETRIES:
                return d("R-12", "EVIDENCE_GAP", [],
                         f"裁决已连续 {evidence_retries} 次判定证据不足，"
                         f"回退次数用尽，输出取证清单转人工")
            # 全流程唯一允许的回退边
            return d("R-05", "EVIDENCE", ["due-diligence"],
                     "分歧源于证据缺失，回退 EVIDENCE 补证（全流程唯一回退边）")
        if adjudication_verdict == "RISK_REFUTED":
            # 仍进入 DISPOSITION：被驳回的预警也要产出一条显式的「维持原状」处置记录。
            # 「我们看过并决定不动」必须有据可查，不能表现为什么都没发生。
            return d("R-06", "DISPOSITION", [],
                     "定性结论被反证推翻，进入处置阶段出具「维持原状 + 加强监测」结论并留痕")
        return d("R-07", "DISPOSITION", [],
                 "裁决风险成立，进入处置阶段执行闸门定级")

    # ---- R-08 ~ R-10 DISPOSITION：按闸门层级分流 -----------------------
    if phase == "DISPOSITION":
        if action_tier == "L3":
            return d("R-08", "AUDIT", [],
                     "L3 不可逆动作，仅生成方案供人工决策，不派发执行")
        if action_tier == "L2" and not approved:
            return d("R-09", "DISPOSITION", [],
                     "L2 动作需人工审批，房间内 @风险经理并等待审批回调")
        return d("R-10", "AUDIT", ["disposition-executor"],
                 f"{action_tier} 动作条件满足，派发执行后进入审计")

    # ---- R-11 AUDIT ---------------------------------------------------
    if phase == "AUDIT":
        return d("R-11", "CLOSED", ["compliance-auditor"],
                 "执行审计核验、合规逐条校验与经验沉淀，案件闭环")

    raise ValueError(f"路由表未覆盖阶段：{phase}")


def adjudicate(assertion: dict[str, Any], rebuttal: dict[str, Any],
               rebuttal_checklist: dict[str, Any] | None = None) -> dict[str, Any]:
    """裁决规则：**证据等级优先于置信度**。

    刻意不用 LLM 做裁决——裁决是责任分配的时刻，必须可复现、可复核。
    质疑官对自动执行拥有一票阻断权：判定证据不足即强制回退补证。

    ``rebuttal_checklist`` 传入时还会校验**质疑覆盖率**。未被质疑过的主因
    和「质疑后未被推翻」的主因，在证据强度上完全不同——前者只是没人看过。
    把两者混为一谈会让整个对抗环节可以被静默跳过，所以覆盖不满即回退补证。
    """
    verdict_map = {
        # 质疑官全盘推翻 → 不进入处置
        "REFUTED": ("RISK_REFUTED", "质疑方推翻全部主因，且定性方未能提供更强证据"),
        # 证据不足 → 一票阻断，回退补证
        "INSUFFICIENT_EVIDENCE": ("EVIDENCE_INSUFFICIENT", "质疑方指出证据不足，行使阻断权回退补证"),
        # 部分推翻 → 仍有存活主因，风险成立但降档
        "PARTIALLY_REFUTED": ("RISK_CONFIRMED", "部分主因被推翻，剩余主因仍构成实质风险"),
        # 反驳不成立 → 定性成立
        "SUPPORTED": ("RISK_CONFIRMED", "质疑方已逐条尝试反驳但均不成立，定性结论采信"),
    }
    adv = rebuttal.get("verdict", "SUPPORTED")
    verdict, basis = verdict_map[adv]

    # 定性方自认证据不足时，无论质疑方结论如何都回退——两方任一说不足即不往下走
    if assertion.get("conclusion") == "INSUFFICIENT":
        verdict, basis = "EVIDENCE_INSUFFICIENT", "定性方自评证据不足，回退补证"

    # 质疑清单未全覆盖 → 视同质疑方未能完成质疑，走已有的唯一回退边补证再议。
    # 之所以归入 EVIDENCE_INSUFFICIENT 而不新开一条边：质疑方处理不了某条主因，
    # 最常见的原因就是缺少判断它所需的证据；且回退带重试上限，用尽即转人工，
    # 是一条有界且 fail-safe 的路径。
    coverage = None
    if rebuttal_checklist is not None:
        coverage = rebuttal_checklist.get("coverage")
        if not rebuttal_checklist.get("complete", True):
            missing = rebuttal_checklist.get("unaddressed", [])
            verdict = "EVIDENCE_INSUFFICIENT"
            basis = (f"质疑清单未全覆盖（{coverage:.0%}），"
                     f"主因 {missing} 未被质疑过——未被质疑不等于质疑通过，回退补证")

    surviving = rebuttal.get("surviving_causes", [])
    grade = assertion.get("suggested_grade")
    if verdict == "RISK_CONFIRMED" and adv == "PARTIALLY_REFUTED":
        grade = "关注"  # 部分主因被推翻，不采用更重的分类
    if verdict != "RISK_CONFIRMED":
        grade = None

    return {
        "verdict": verdict,
        "basis": basis,
        "final_grade": grade,
        "surviving_causes": surviving,
        "rebuttal_coverage": coverage,
        "dissent": {
            "analyst_conclusion": assertion.get("conclusion"),
            "advocate_verdict": adv,
            "rebuttals": rebuttal.get("rebuttals", []),
            "attempted_but_failed": rebuttal.get("attempted_but_failed", []),
        },
    }
