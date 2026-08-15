"""待办清单原语 —— 我们对「to do list」这类上下文工程手法的落地方式。

**to do list 为什么有效**：它把长程任务外化成可枚举、可检查的子项，
让模型不必在长上下文里靠注意力维持目标，从而减少遗漏、重复与提前收工。
本质是拿显式状态换注意力预算。

**但它有个根本弱点**：清单通常由模型自己维护，模型可以偷偷改、悄悄划掉。
「模型说它做完了」和「它真的做完了」是两回事。

因此本模块把清单定位为**完成性契约**而非提示技巧：

- 清单由**系统**从上游产物派生，不由模型生成；
- 清单块注入提示词，告诉模型完成标准是什么；
- 模型返回后，**由代码逐项核对覆盖**，未覆盖项不是「默认通过」而是**阻断**。

提示词里的清单只是把契约告知模型；真正起作用的是事后那次核对。

当前有两类清单：

- ``rebuttal``  质疑清单。定性方提出 N 条主因，质疑方必须逐条给出结论。
  这堵住了一个真实的洞：此前质疑方只反驳 1 条、对另外 2 条保持沉默时，
  系统无法区分「反驳失败」与「根本没看」。
- ``evidence``  取证清单。按信号类型派生应取事实，未取到的落为证据缺口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

PENDING = "PENDING"
REFUTED = "REFUTED"                  # 找到成立的反证
ATTEMPTED_FAILED = "ATTEMPTED_FAILED"  # 试过但反驳不成立
INSUFFICIENT = "INSUFFICIENT"        # 证据不足以判断
COLLECTED = "COLLECTED"              # 取证清单：已取到
GAP = "GAP"                          # 取证清单：确认缺失

#: 视为「已处理」的状态。PENDING 之外皆已处理——
#: 包括 INSUFFICIENT，因为「我看了，判断不了」也是一个负责任的结论。
ADDRESSED = frozenset({REFUTED, ATTEMPTED_FAILED, INSUFFICIENT, COLLECTED, GAP})


class ChecklistError(Exception):
    """清单未被完整处理，或标记了不存在的条目。"""


@dataclass
class ChecklistItem:
    item_id: str
    target: str
    why: str                       # 这一项为什么必须被处理
    context: dict[str, Any] = field(default_factory=dict)
    status: str = PENDING
    resolution: str = ""
    evidence_ids: list[str] = field(default_factory=list)

    @property
    def addressed(self) -> bool:
        return self.status in ADDRESSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id, "target": self.target, "why": self.why,
            "status": self.status, "resolution": self.resolution,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass
class Checklist:
    kind: str                      # rebuttal / evidence
    items: list[ChecklistItem] = field(default_factory=list)

    # ---- 构造 -------------------------------------------------------
    def add(self, target: str, why: str, **context: Any) -> ChecklistItem:
        prefix = {"rebuttal": "R", "evidence": "E"}.get(self.kind, "C")
        item = ChecklistItem(f"{prefix}{len(self.items) + 1}", target, why, context)
        self.items.append(item)
        return item

    # ---- 标记 -------------------------------------------------------
    def mark(self, item_id: str, status: str, resolution: str,
             evidence_ids: Iterable[str] = ()) -> None:
        if status not in ADDRESSED:
            raise ChecklistError(f"非法状态 {status!r}，允许：{sorted(ADDRESSED)}")
        for it in self.items:
            if it.item_id == item_id:
                it.status = status
                it.resolution = resolution
                it.evidence_ids = list(evidence_ids)
                return
        raise ChecklistError(f"标记了不存在的清单项：{item_id}（现有 {[i.item_id for i in self.items]}）")

    def mark_by_target(self, target: str, status: str, resolution: str,
                       evidence_ids: Iterable[str] = ()) -> bool:
        """按目标名标记。模型只知道目标名不知道 item_id 时用它，找不到返回 False。"""
        for it in self.items:
            if it.target == target:
                self.mark(it.item_id, status, resolution, evidence_ids)
                return True
        return False

    # ---- 核对 -------------------------------------------------------
    def unaddressed(self) -> list[ChecklistItem]:
        return [i for i in self.items if not i.addressed]

    def coverage(self) -> float:
        if not self.items:
            return 1.0
        return round(sum(1 for i in self.items if i.addressed) / len(self.items), 4)

    def complete(self) -> bool:
        return not self.unaddressed()

    def assert_complete(self) -> None:
        """未完整处理即抛错。调用方据此阻断，而不是放行。"""
        missing = self.unaddressed()
        if missing:
            raise ChecklistError(
                f"{self.kind} 清单未完整处理：{len(missing)}/{len(self.items)} 项未处理"
                f"（{[i.target for i in missing]}）"
            )

    # ---- 注入提示词 -------------------------------------------------
    def as_prompt_block(self) -> str:
        """渲染成提示词里的清单块。空清单也要明确说明，不能渲染成空白。"""
        if not self.items:
            return "（本轮无需处理的条目）"
        lines = []
        for it in self.items:
            box = "[ ]" if not it.addressed else "[x]"
            lines.append(f"- {box} `{it.item_id}` **{it.target}** —— {it.why}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "coverage": self.coverage(),
            "complete": self.complete(),
            "items": [i.to_dict() for i in self.items],
            "unaddressed": [i.target for i in self.unaddressed()],
        }


# ---------------------------------------------------------------------------
# 两类清单的派生规则
# ---------------------------------------------------------------------------

#: 主因置信度低于此值不进入质疑清单——不值得质疑的主因本来也进不了处置
REBUTTAL_THRESHOLD = 0.5


def rebuttal_checklist(assertion: dict[str, Any]) -> Checklist:
    """从定性方的断言派生质疑清单。**由系统派生，不由模型自报。**"""
    cl = Checklist("rebuttal")
    for cause in assertion.get("root_causes", []):
        if cause.get("confidence", 0) < REBUTTAL_THRESHOLD:
            continue
        cl.add(
            cause["type"],
            f"定性方以置信度 {cause['confidence']} 主张该主因成立，"
            f"依据 {cause.get('evidence_ids', [])}",
            confidence=cause.get("confidence"),
            evidence_ids=cause.get("evidence_ids", []),
            rationale=cause.get("rationale", ""),
        )
    return cl


#: 信号类型 → 该类型下必须取到的事实。取不到就要落成显式缺口，
#: 而不是让它从证据链里静悄悄消失。
REQUIRED_FACTS: dict[str, list[tuple[str, str]]] = {
    "judicial_new_case": [
        ("litigation_case", "涉诉实质性判定依赖案由、标的额占比、结案状态与诉讼地位"),
        ("registration_change", "需排除涉诉与治理变动的时间窗重合"),
    ],
    "txn_concentrated_outflow": [
        ("flow_pattern", "资金异常判定依赖对手方性质与历史波动区间"),
    ],
    "registration_change": [
        ("registration_change", "法代/股权变更需核对变更时点与风险窗口"),
    ],
    "guarantee_contagion": [
        ("guarantee_entry", "代偿敞口测算依赖被担保方状态与缓释措施明细"),
    ],
    "guarantee_ring_alert": [
        ("guarantee_entry", "担保圈传染需逐笔核对担保余额与共同担保人"),
    ],
    "rating_downgrade": [
        ("credit_report", "外部评级变动需与征信对外担保余额相互印证"),
    ],
}


def evidence_checklist(signal_types: Iterable[str]) -> Checklist:
    """从信号类型派生取证清单，去重后保序。"""
    cl = Checklist("evidence")
    seen: set[str] = set()
    for st in signal_types:
        for fact_type, why in REQUIRED_FACTS.get(st, []):
            if fact_type in seen:
                continue
            seen.add(fact_type)
            cl.add(fact_type, why, signal_type=st)
    return cl
