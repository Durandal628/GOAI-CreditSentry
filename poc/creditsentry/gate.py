"""RiskGate —— 风险分级执行闸门（安全核心）。

四维定级：风险等级 × 证据等级 × 敞口金额 × 可逆性 → L0 / L1 / L2 / L3。

三条设计铁律：
1. **纯规则引擎，无外部依赖** —— 安全判定不依赖网络可用性，也不依赖 LLM 的当次发挥。
2. **fail-safe 而非 fail-open** —— 任何入参缺失或规则未命中，一律降级为 L3（只出方案不执行），
   而不是「没匹配上就放行」。这是安全系统与演示系统的根本区别。
3. **不可逆动作永不自动执行** —— 无论风险多高、证据多强、金额多小，irreversible 恒定 L3。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, asdict
from typing import Any

L0, L1, L2, L3 = "L0", "L1", "L2", "L3"
_ORDER = {L0: 0, L1: 1, L2: 2, L3: 3}


@dataclass(frozen=True)
class ActionSpec:
    """动作白名单条目。白名单之外的动作一律拒绝执行。"""
    action: str
    label: str
    reversible: bool
    base_tier: str
    rollback: str | None  # 回滚手段；不可逆动作为 None


# ---- 动作白名单 ---------------------------------------------------------
# 新增动作必须在此显式登记并声明可逆性与回滚手段，否则 G-01 会将其判为 L3。
ACTION_CATALOG: dict[str, ActionSpec] = {
    "monitor_only":            ActionSpec("monitor_only", "维持原状 + 加强监测", True,  L0, "无需回滚"),
    "tag_watch":               ActionSpec("tag_watch", "风险打标（关注）", True,  L1, "清除标记"),
    "request_documents":       ActionSpec("request_documents", "发起补充资料通知", True,  L1, "撤回通知"),
    "reduce_limit":            ActionSpec("reduce_limit", "授信额度压降", True,  L2, "额度冲正至原值"),
    "add_guarantee":           ActionSpec("add_guarantee", "追加担保要求", True,  L2, "解除追加要求"),
    "suspend_drawdown":        ActionSpec("suspend_drawdown", "暂停提款", True,  L2, "恢复提款权限"),
    "early_recall":            ActionSpec("early_recall", "提前收贷", False, L3, None),
    "litigation_preservation": ActionSpec("litigation_preservation", "诉讼保全", False, L3, None),
    "downgrade_classification": ActionSpec("downgrade_classification", "五级分类下调", False, L3, None),
}

# 敞口金额升档阈值（元）。金额越大，同一动作要求的管控层级越高。
EXPOSURE_ESCALATE = 5_000_000    # 敞口 ≥ 500 万，L1 升 L2
EXPOSURE_ESCALATE_HIGH = 20_000_000  # 敞口 ≥ 2000 万，L2 升 L3

APPROVER_BY_TIER = {
    L0: [],
    L1: [],
    L2: ["风险经理"],
    L3: ["风险经理", "授信审批人"],
}


@dataclass
class GateDecision:
    action: str
    action_label: str
    action_tier: str
    needs_approval: bool
    approver_roles: list[str]
    idempotency_key: str
    rollback_point: str | None
    reversible: bool
    rule_id: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _idem_key(case_id: str, action: str, params: dict[str, Any]) -> str:
    """幂等键：同一案件 + 同一动作 + 同一参数 → 同一键，重复投递被去重。"""
    raw = f"{case_id}|{action}|{sorted(params.items())}"
    return "idem-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def evaluate(
    *,
    case_id: str,
    action_type: str | None,
    risk_grade: str | None,
    evidence_level: str | None,
    exposure_amount: float | None,
    params: dict[str, Any] | None = None,
) -> GateDecision:
    """计算处置动作的执行层级。

    规则按优先级顺序判定，第一条命中即返回，rule_id 写入 Trace 供审计回放。
    """
    params = params or {}

    def decide(tier: str, rule_id: str, reason: str, spec: ActionSpec | None = None) -> GateDecision:
        label = spec.label if spec else (action_type or "未知动作")
        reversible = spec.reversible if spec else False
        rollback = spec.rollback if (spec and tier != L3) else None
        return GateDecision(
            action=action_type or "unknown",
            action_label=label,
            action_tier=tier,
            needs_approval=tier in (L2, L3),
            approver_roles=APPROVER_BY_TIER[tier],
            idempotency_key=_idem_key(case_id, action_type or "unknown", params),
            rollback_point=rollback,
            reversible=reversible,
            rule_id=rule_id,
            reason=reason,
        )

    # G-01 白名单校验：未登记的动作一律 L3，不给「未知即放行」的机会
    spec = ACTION_CATALOG.get(action_type or "")
    if spec is None:
        return decide(L3, "G-01", f"动作 {action_type!r} 不在白名单内，fail-safe 降级为 L3")

    # G-02 入参完备性：任何维度缺失即 L3。缺信息时保守，不猜。
    missing = [
        name for name, val in (
            ("risk_grade", risk_grade),
            ("evidence_level", evidence_level),
            ("exposure_amount", exposure_amount),
        ) if val is None
    ]
    if missing:
        return decide(L3, "G-02", f"入参缺失 {missing}，fail-safe 降级为 L3", spec)

    # G-03 不可逆红线：不可逆动作恒定 L3，任何条件都不能豁免
    if not spec.reversible:
        return decide(L3, "G-03", f"{spec.label} 为不可逆动作，Agent 永不自动执行，仅生成方案", spec)

    # G-04 证据缺失兜底：证据缺失时最多只读诊断，不得触碰业务系统
    if evidence_level == "缺失":
        return decide(L0, "G-04", "证据缺失，仅允许只读诊断与取证建议", spec)

    tier = spec.base_tier
    reasons = [f"{spec.label} 基线层级 {tier}"]

    # G-05 弱证据升档：证据不强则加一道人工闸
    if evidence_level == "弱" and _ORDER[tier] < _ORDER[L2]:
        tier = L2
        reasons.append("证据等级为弱 → 升档至 L2 要求人工审批")

    # G-06 / G-07 敞口金额升档。
    # 只读动作（基线 L0）不参与金额升档——只读诊断不触碰业务系统，
    # 因敞口大就要求人工审批是无意义的摩擦。
    assert exposure_amount is not None
    if spec.base_tier == L0:
        return decide(tier, "G-09", "；".join(reasons + ["只读动作不参与敞口金额升档"]), spec)
    if exposure_amount >= EXPOSURE_ESCALATE_HIGH and _ORDER[tier] < _ORDER[L3]:
        tier = L3
        reasons.append(f"敞口 {exposure_amount:,.0f} ≥ {EXPOSURE_ESCALATE_HIGH:,} → 升档至 L3 人工决策")
        rule = "G-07"
    elif exposure_amount >= EXPOSURE_ESCALATE and _ORDER[tier] < _ORDER[L2]:
        tier = L2
        reasons.append(f"敞口 {exposure_amount:,.0f} ≥ {EXPOSURE_ESCALATE:,} → 升档至 L2 要求人工审批")
        rule = "G-06"
    else:
        rule = "G-05" if "弱" == evidence_level else "G-08"

    # G-08 风险等级校正：正常类客户不得直接执行影响授信的动作
    if risk_grade == "正常" and _ORDER[tier] >= _ORDER[L2]:
        reasons.append("客户当前为正常类，处置影响面大 → 保持人工审批闸门")

    return decide(tier, rule, "；".join(reasons), spec)
