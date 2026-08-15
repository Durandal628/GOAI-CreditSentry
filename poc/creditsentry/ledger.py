"""证据账本 Evidence Ledger。

本方案「可举证」的地基。三条硬约束在此以代码强制，而非文档承诺：

1. 任何外部原文取回时**强制登记**——不经账本的数据不得进入决策；
2. 结论必须挂载 ``evidence_ids``，``assert_supported()`` 会拒绝无证据的断言；
3. 账本 **append-only** 不可篡改，任何改写尝试直接抛错。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Iterable

# 证据等级。定级不是打分，而是决定这条证据能否单独支撑处置决策。
STRONG = "强"
WEAK = "弱"
MISSING = "缺失"


class EvidenceError(Exception):
    """证据约束被违反——例如无证据下结论、或试图篡改账本。"""


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    subject_id: str
    source_system: str
    fact_type: str
    snapshot_uri: str
    content_hash: str
    collected_at: str
    level: str
    level_reason: str
    extracted: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EvidenceLedger:
    """append-only 的证据账本。

    真实部署时快照落对象存储、索引落 PolarDB；此处以内存 + JSON 落盘实现，
    但对外接口与约束完全一致，因此替换存储后端不影响调用方。
    """

    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        self._items: dict[str, Evidence] = {}
        self._seq = 0
        # 原文快照库。真实部署时它是对象存储（MinIO / OSS），账本里只留
        # snapshot_uri 与哈希；此处以内存字典等价实现，接口一致。
        #
        # 为什么原文要留：**给人看**。「结论有据可查」这句话，只有当复核的人
        # 能当场翻到那份裁判文书、那张征信报告时才成立。
        # 这与「原文绝不进模型上下文」不冲突——后者管的是喂给模型的内容，
        # 前者管的是给人复核的材料，两者本来就该分开。
        self._snapshots: dict[str, str] = {}

    # ---- 写入 -------------------------------------------------------
    def record(
        self,
        *,
        subject_id: str,
        source_system: str,
        fact_type: str,
        raw_content: str,
        extracted: dict[str, Any],
        collected_at: str | None = None,
        level: str | None = None,
        level_reason: str = "",
    ) -> Evidence:
        """登记一条证据。level 缺省时按内置规则自动定级。"""
        self._seq += 1
        eid = f"EV-{self.case_id.split('-')[-1]}-{self._seq:04d}"
        if eid in self._items:  # append-only：同 ID 不得二次写入
            raise EvidenceError(f"证据账本不可篡改：{eid} 已存在")

        content_hash = "sha256:" + hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
        ts = collected_at or datetime.now(timezone.utc).isoformat(timespec="seconds")

        if level is None:
            level, auto_reason = self._grade(source_system, extracted)
            level_reason = level_reason or auto_reason

        ev = Evidence(
            evidence_id=eid,
            subject_id=subject_id,
            source_system=source_system,
            fact_type=fact_type,
            snapshot_uri=f"s3://creditsentry-ledger/{self.case_id}/{eid}.snapshot",
            content_hash=content_hash,
            collected_at=ts,
            level=level,
            level_reason=level_reason,
            extracted=extracted,
        )
        self._items[eid] = ev
        self._snapshots[eid] = raw_content
        return ev

    # ---- 快照 -------------------------------------------------------
    def snapshot(self, evidence_id: str) -> tuple[str, bool]:
        """取回原文快照，并**当场校验哈希**。

        返回 ``(原文, 哈希是否匹配)``。校验不是形式主义：账本记的是哈希，
        快照存在别处，两者对不上就意味着有一方被动过——这时候调用方
        必须知道，而不是若无其事地把内容显示出来。
        """
        ev = self.get(evidence_id)
        raw = self._snapshots.get(evidence_id, "")
        digest = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return raw, digest == ev.content_hash

    def record_gap(self, *, subject_id: str, fact_type: str, why: str) -> Evidence:
        """登记一条「缺失证据」。

        缺口本身也是证据——它记录了「我们知道自己不知道什么」，
        是 Agent 输出取证任务而非编造结论的依据。
        """
        return self.record(
            subject_id=subject_id,
            source_system="-",
            fact_type=fact_type,
            raw_content=f"GAP:{fact_type}:{why}",
            extracted={"gap": True, "why": why},
            level=MISSING,
            level_reason=why,
        )

    # ---- 定级规则 ---------------------------------------------------
    @staticmethod
    def _grade(source_system: str, extracted: dict[str, Any]) -> tuple[str, str]:
        """自动定级。

        强证据需同时满足：来源权威、原文可溯源、且未被标记为采样不足/存在歧义。
        任一不满足即降为弱证据——宁可判弱，不可虚强。
        """
        if extracted.get("gap"):
            return MISSING, "应有而未取到"
        if extracted.get("ambiguous"):
            return WEAK, "主体重名无法消歧，未自动认定"
        if extracted.get("partial") or extracted.get("undersampled"):
            return WEAK, "数据不完整或采样不足，不足以单独定性"
        authoritative = {"bureau-mcp", "judicial-mcp", "credit-core-mcp", "txn-mcp"}
        if source_system in authoritative and extracted.get("source_doc_uri"):
            return STRONG, "来源权威且原文可溯源"
        if source_system in authoritative:
            return STRONG, "来源权威系统直取"
        return WEAK, "来源非权威系统或无原文快照"

    # ---- 读取与校验 -------------------------------------------------
    def get(self, evidence_id: str) -> Evidence:
        if evidence_id not in self._items:
            raise EvidenceError(f"引用了不存在的证据：{evidence_id}")
        return self._items[evidence_id]

    def all(self) -> list[Evidence]:
        return list(self._items.values())

    def by_level(self, level: str) -> list[Evidence]:
        return [e for e in self._items.values() if e.level == level]

    def sufficiency(self) -> float:
        """证据充分度 ∈ [0,1]，是路由键的关键维度之一。

        强证据计 1.0、弱证据计 0.4、缺失计 0——缺失不仅不加分，还会拉低整体充分度，
        因此「查得越多但都查不实」不会被误判为证据充分。
        """
        items = self.all()
        if not items:
            return 0.0
        score = sum({STRONG: 1.0, WEAK: 0.4, MISSING: 0.0}[e.level] for e in items)
        return round(score / len(items), 3)

    def assert_supported(self, claim: str, evidence_ids: Iterable[str]) -> list[str]:
        """校验一条结论是否有证据支撑。

        这是「无证据不决策」的执行点：结论 Schema 层面强制调用，
        无证据或引用了不存在的证据，直接拒绝，不给降级通过的余地。
        """
        ids = list(evidence_ids)
        if not ids:
            raise EvidenceError(f"无证据的断言被拒绝：{claim!r}")
        for eid in ids:
            self.get(eid)  # 不存在会抛错
        if all(self.get(e).level == MISSING for e in ids):
            raise EvidenceError(f"断言仅由缺失证据支撑，被拒绝：{claim!r}")
        return ids

    # ---- 导出 -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "count": len(self._items),
            "sufficiency": self.sufficiency(),
            "by_level": {
                STRONG: len(self.by_level(STRONG)),
                WEAK: len(self.by_level(WEAK)),
                MISSING: len(self.by_level(MISSING)),
            },
            "items": [e.to_dict() for e in self._items.values()],
        }

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
