"""Case State Store —— 多 Agent 共享状态。

对应赛题 RAG 要求第 3 项「共享状态管理：管理多 Agent 协作状态，保证并发下的一致性」。

设计要点：
- 与 Matrix 消息流**分离存储**。消息承载「人可读的协作轨迹」，State 承载「机器可查的一致性状态」，
  两者以 case_id 关联。消息不承载状态，状态不承载大对象。
- 写入走 **乐观锁**（version 比对），并发写冲突抛 ``ConcurrencyError`` 由调用方重试。
  真实部署时同一 case_id 经 RocketMQ 顺序消息路由至同一队列，冲突概率进一步降低。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any


class ConcurrencyError(Exception):
    """乐观锁冲突：读取后到写入前，状态已被其他 Agent 修改。"""


# 五阶段固定生命周期。流程骨架是确定性的，LLM 只在阶段内做判定。
PHASES = ("INTAKE", "EVIDENCE", "ADJUDICATION", "DISPOSITION", "AUDIT", "CLOSED",
          "EVIDENCE_GAP")  # EVIDENCE_GAP：取证重试用尽仍不充分，转人工的终态


@dataclass
class CaseState:
    case_id: str
    subject: dict[str, Any]
    phase: str = "INTAKE"
    version: int = 0
    risk_event: dict[str, Any] | None = None
    exposure: dict[str, Any] | None = None
    assertion: dict[str, Any] | None = None      # RiskAnalyst 产出
    rebuttal: dict[str, Any] | None = None       # DevilsAdvocate 产出
    adjudication: dict[str, Any] | None = None   # RiskCommander 裁决
    gate: dict[str, Any] | None = None           # RiskGate 定级
    approval: dict[str, Any] | None = None       # 审批结果
    execution: dict[str, Any] | None = None      # 执行回执
    audit: dict[str, Any] | None = None          # 审计结论
    # 转人工交接单。只在案件移交人工时产生——它记录的是「工作交接」而非风险结论，
    # 因为证据不足以定论时给结论就是编
    handoff: dict[str, Any] | None = None
    evidence_gaps: list[dict[str, Any]] = field(default_factory=list)
    idempotency_keys: dict[str, str] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    retry_counts: dict[str, int] = field(default_factory=dict)
    # 质疑清单：由系统从定性方断言派生，模型返回后逐项核对。
    # 存进 State 而非只存在 Agent 内存里，是因为覆盖率要参与裁决并落审计报告。
    rebuttal_checklist: Any = None
    # 取证清单：按信号类型派生「本应取到什么」，与账本实际取到的比对后落缺口
    evidence_checklist: Any = None
    # 各次检索的改写计划，含维度、子查询与澄清项。用于事后复核「为什么召回了这些条款」
    query_plans: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items()}
        # 清单是对象，落盘前转普通结构
        for k in ("rebuttal_checklist", "evidence_checklist"):
            if d.get(k) is not None:
                d[k] = d[k].to_dict()
        return d


class CaseStateStore:
    """带乐观锁的状态存储。内存实现，接口对齐 PolarDB 版本。"""

    def __init__(self) -> None:
        self._data: dict[str, CaseState] = {}
        self._lock = threading.Lock()

    def create(self, case_id: str, subject: dict[str, Any]) -> CaseState:
        with self._lock:
            st = CaseState(case_id=case_id, subject=subject)
            self._data[case_id] = st
            return st

    def read(self, case_id: str) -> CaseState:
        with self._lock:
            return self._data[case_id]

    def update(self, case_id: str, expected_version: int, **changes: Any) -> CaseState:
        """乐观锁写入。expected_version 与当前不符即冲突。"""
        with self._lock:
            st = self._data[case_id]
            if st.version != expected_version:
                raise ConcurrencyError(
                    f"{case_id} 版本冲突：期望 v{expected_version}，实际 v{st.version}"
                )
            for k, v in changes.items():
                if not hasattr(st, k):
                    raise AttributeError(f"CaseState 无字段 {k}")
                setattr(st, k, v)
            st.version += 1
            return st

    def transition(self, case_id: str, to_phase: str, reason: str) -> CaseState:
        """阶段迁移。所有迁移记入 history，供审计回放。"""
        if to_phase not in PHASES:
            raise ValueError(f"未知阶段：{to_phase}")
        with self._lock:
            st = self._data[case_id]
            st.history.append({"from": st.phase, "to": to_phase, "reason": reason})
            st.phase = to_phase
            st.version += 1
            return st

    def save(self, case_id: str, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.read(case_id).to_dict(), f, ensure_ascii=False, indent=2)
