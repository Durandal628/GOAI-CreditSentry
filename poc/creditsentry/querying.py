"""多维查询改写与澄清机制。

一句自然语言检索词在合规场景里几乎必然召回不全：行内口语与监管术语对不上、
求证方与证伪方要找的根本是不同的条款、条款还有生效日期。因此把「一次检索」
拆成**六个维度的子查询**，各自带独立的过滤条件与权重，融合后再排序。

===================== =========================================================
维度                   解决什么
===================== =========================================================
``stance``            立场维。求证方找**构成要件与认定标准**，证伪方找**认定门槛**。
                      这是对抗式设计在检索层的延伸——同一批事实，两种检索意图。
``terminology``       术语规范化维。行内口语 → 监管术语（抽贷→提前收回贷款）。
                      不做这一维，用业务口语几乎检不到条款原文。
``signal_topic``      信号主题维。信号类型 → 该类型绑定的条款主题。
                      让检索由**案件事实**驱动，而不只由角色的固定查询词驱动。
``clause_ref``        条款直查维。事实里出现条款号或法规名时**精确召回**，不走语义。
                      语义检索在「第十八条」这种精确引用上表现很差。
``negation``          否定式维。**仅证伪方**：主动检索「不构成 / 除外 / 豁免 / 不适用」。
                      正向检索天然找不到除外条款，这是质疑方最容易漏的一块。
``recency``           时效维。**生效日 ≤ 案件时点**。
                      用 2025 年的行内制度去评价 2017 年的案子，是知识维度的前视污染
                      ——和把后来才披露的证据放进决策时点是同一类错误。
===================== =========================================================

**澄清（clarification）的设计**与常规做法不同：我们不默认「问用户」。

银行场景里补充信息的成本不在用户打字，而在于「去哪个系统查、有没有查询授权、
谁有权限」。所以澄清的产物应当是**一张可派发的任务单**，不是一个问题。
三条渠道按优先级排序，能自动解决的绝不问人；确实需要人做业务判断时，
给**选项**而不是开放问题——开放问题的回答无法结构化，也无法进证据账本。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

PROVE = "PROVE"      # 求证成立
REFUTE = "REFUTE"    # 求证不成立

DIMENSIONS = ("stance", "terminology", "signal_topic", "clause_ref", "negation", "recency")

# 澄清渠道，按优先级从高到低
AUTO = "AUTO"                  # 能从 Case State / 证据账本直接推出，不打扰任何人
SYSTEM_TASK = "SYSTEM_TASK"    # 派一张取证任务单给系统
HUMAN_CHOICE = "HUMAN_CHOICE"  # Matrix room 内选项式提问（不问开放问题）


# ---------------------------------------------------------------------------
# 维度一：立场
# ---------------------------------------------------------------------------
# 同一个案件，求证方与证伪方要找的是不同的东西。这不是措辞差异，是检索目标差异。
STANCE_TERMS: dict[str, list[str]] = {
    PROVE: ["认定标准", "构成要件", "风险信号", "应当", "监测", "处置"],
    REFUTE: ["认定门槛", "除外情形", "不构成", "豁免", "不适用", "例外"],
}

# ---------------------------------------------------------------------------
# 维度二：术语规范化（行内口语 → 监管术语）
# ---------------------------------------------------------------------------
# 这张表是领域资产：不做映射，业务同事的口语几乎检不到任何条款原文。
TERMINOLOGY: dict[str, list[str]] = {
    "抽贷": ["提前收回贷款", "提前收贷"],
    "断贷": ["停止发放贷款", "暂停授信"],
    "跑路": ["实际控制人失联", "经营异常"],
    "老赖": ["失信被执行人"],
    "互保": ["关联客户担保", "对外担保", "或有负债"],
    "担保圈": ["关联客户认定", "大额风险暴露", "担保链"],
    "空转": ["资金用途真实性", "受托支付"],
    "过桥": ["贷款用途真实性", "资金挪用"],
    "压降": ["授信额度调整", "风险限额管理"],
    "共债": ["多头授信", "对外负债"],
    "代偿": ["或有负债", "保证责任", "担保代偿"],
}

# ---------------------------------------------------------------------------
# 维度三：信号类型 → 条款主题
# ---------------------------------------------------------------------------
SIGNAL_TOPICS: dict[str, list[str]] = {
    "judicial_new_case": ["涉诉信息", "风险分类", "偿债能力"],
    "txn_concentrated_outflow": ["资金用途真实性", "受托支付", "资金流向监测"],
    "registration_change": ["公司治理", "实际控制人", "重大事项报告"],
    "guarantee_contagion": ["关联客户认定", "大额风险暴露", "或有负债", "担保代偿"],
    "guarantee_ring_alert": ["关联客户认定", "担保链", "风险暴露集中度"],
    "rating_downgrade": ["外部评级", "风险预警", "持续监测"],
    "media_mention": [],       # 噪声类信号不产生检索主题
    "rating_periodic": [],
}

_CLAUSE_RE = re.compile(r"《[^》]{2,30}》(?:第[一二三四五六七八九十百零〇\d]+条)?")


@dataclass
class SubQuery:
    dimension: str
    text: str
    why: str
    weight: float = 1.0
    filters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"dimension": self.dimension, "text": self.text, "why": self.why,
                "weight": self.weight, "filters": self.filters}


@dataclass
class Clarification:
    clarification_id: str
    question: str
    reason: str
    channel: str
    options: list[str] = field(default_factory=list)
    task: dict[str, Any] | None = None   # SYSTEM_TASK 渠道的任务单
    blocking: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"clarification_id": self.clarification_id, "question": self.question,
                "reason": self.reason, "channel": self.channel,
                "options": self.options, "task": self.task, "blocking": self.blocking}


@dataclass
class QueryPlan:
    caller: str
    stance: str
    subqueries: list[SubQuery]
    clarifications: list[Clarification]
    as_of: str | None

    @property
    def dimensions_used(self) -> list[str]:
        return sorted({q.dimension for q in self.subqueries})

    @property
    def blocking_clarifications(self) -> list[Clarification]:
        return [c for c in self.clarifications if c.blocking]

    def to_dict(self) -> dict[str, Any]:
        return {
            "caller": self.caller, "stance": self.stance, "as_of": self.as_of,
            "dimensions_used": self.dimensions_used,
            "subqueries": [q.to_dict() for q in self.subqueries],
            "clarifications": [c.to_dict() for c in self.clarifications],
        }


# ---------------------------------------------------------------------------
# 改写
# ---------------------------------------------------------------------------

def _terminology_hits(text: str) -> list[tuple[str, list[str]]]:
    return [(k, v) for k, v in TERMINOLOGY.items() if k in text]


def rewrite(*, caller: str, base_query: str, stance: str,
            signal_types: Iterable[str] = (), facts: dict[str, Any] | None = None,
            as_of: str | None = None) -> QueryPlan:
    """把一个基础查询改写成多维子查询集合。

    ``as_of`` 非空时，所有子查询都带上 ``effective_before`` 过滤——
    时效维不是可选项，它和取证的时点冻结是同一条纪律。
    """
    if stance not in (PROVE, REFUTE):
        raise ValueError(f"未知立场：{stance}（可选 {PROVE} / {REFUTE}）")

    facts = facts or {}
    signal_types = list(signal_types)
    recency = {"effective_before": as_of} if as_of else {}
    subs: list[SubQuery] = []

    # 维度一：立场
    subs.append(SubQuery(
        "stance", f"{base_query} {' '.join(STANCE_TERMS[stance])}",
        why=("求证方检索构成要件与认定标准" if stance == PROVE
             else "证伪方检索认定门槛与除外情形——正向检索找不到除外条款"),
        weight=1.0, filters=dict(recency),
    ))

    # 维度二：术语规范化
    for slang, formal in _terminology_hits(base_query):
        subs.append(SubQuery(
            "terminology", " ".join(formal),
            why=f"行内口语「{slang}」映射为监管术语，否则检不到条款原文",
            weight=0.9, filters=dict(recency),
        ))

    # 维度三：信号主题
    topics = sorted({t for st in signal_types for t in SIGNAL_TOPICS.get(st, [])})
    if topics:
        subs.append(SubQuery(
            "signal_topic", " ".join(topics),
            why=f"由信号类型 {signal_types} 展开条款主题，使检索由案件事实驱动",
            weight=1.0, filters=dict(recency),
        ))

    # 维度四：条款直查（精确召回，不走语义）
    blob = " ".join(str(v) for v in facts.values())
    for ref in sorted(set(_CLAUSE_RE.findall(blob + " " + base_query))):
        subs.append(SubQuery(
            "clause_ref", ref,
            why=f"事实中出现明确条款引用 {ref}，走精确召回——语义检索在精确引用上表现很差",
            weight=1.2, filters={**recency, "exact": True},
        ))

    # 维度五：否定式（仅证伪方）
    if stance == REFUTE:
        subs.append(SubQuery(
            "negation", f"{base_query} 不构成 除外 豁免 不适用 认定门槛",
            why="质疑方最容易漏的是除外条款；正向检索天然覆盖不到",
            weight=1.1, filters=dict(recency),
        ))

    # 维度六：时效
    if as_of:
        subs.append(SubQuery(
            "recency", base_query,
            why=f"限定生效日不晚于案件时点 {as_of}——"
                f"用后生效的条款评价历史案件是知识维度的前视污染",
            weight=0.8, filters={"effective_before": as_of, "strict": True},
        ))

    return QueryPlan(caller, stance, subs, detect_clarifications(facts, as_of), as_of)


# ---------------------------------------------------------------------------
# 澄清
# ---------------------------------------------------------------------------

def detect_clarifications(facts: dict[str, Any],
                          as_of: str | None = None) -> list[Clarification]:
    """识别需要澄清的歧义，并为每一条选定解决渠道。

    渠道选择原则：**能自动解决的绝不问人，能派任务的绝不问开放问题。**
    """
    out: list[Clarification] = []
    lit = facts.get("litigation", {}) or {}
    gua = facts.get("guarantee", {}) or {}
    txn = facts.get("transaction", {}) or {}

    # 主体重名 → 派工商精确查询任务。这是典型的「不该问人」：
    # 人也答不上来，得去系统里查统一社会信用代码。
    if lit.get("ambiguous"):
        out.append(Clarification(
            "CLR-1", "涉诉主体存在重名，需确认是否为本主体",
            reason="重名未消歧的涉诉信息不得用于风险定性（会被账本降为弱证据）",
            channel=SYSTEM_TASK,
            task={"action": "precise_lookup", "server": "judicial-mcp",
                  "tool": "get_business_registration",
                  "note": "按统一社会信用代码精确匹配后重新检索涉诉"},
            blocking=False,
        ))

    # 检索结果不全 → 同样派任务，而不是问人
    if lit.get("partial"):
        out.append(Clarification(
            "CLR-2", "司法检索仅返回部分结果，需补全后重新判定",
            reason="部分结果不足以支撑「无实质性涉诉」这一负向结论",
            channel=SYSTEM_TASK,
            task={"action": "paginate_full", "server": "judicial-mcp",
                  "tool": "search_litigation"},
            blocking=False,
        ))

    # 被担保方状态无公开定论 → 这是**业务判断**，必须问人，但给选项
    if gua.get("distressed_parties") and any(
            not p.get("status_basis") for p in gua["distressed_parties"]):
        out.append(Clarification(
            "CLR-3", "被担保方是否已实质出险？",
            reason="出险认定直接决定担保余额是否计入代偿敞口，"
                   "且无公开定论时不得由系统自行认定",
            channel=HUMAN_CHOICE,
            options=["已出险（有债权人主张或公开违约记录）",
                     "未出险（仅舆情，无实质证据）",
                     "无法判断，转专项调查"],
            blocking=True,
        ))

    # 流水采样不足 → 能自动决定：直接降为弱证据，不打扰任何人
    if txn.get("undersampled"):
        out.append(Clarification(
            "CLR-4", "流水覆盖率不足，异常判定的证据等级如何处理",
            reason="已有明确规则：覆盖率低于阈值即降为弱证据",
            channel=AUTO,
            task={"action": "downgrade_evidence_level", "to": "弱"},
            blocking=False,
        ))

    return out
