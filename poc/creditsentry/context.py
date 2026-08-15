"""上下文装配器 —— 把「往上下文里放什么」变成一次显式、有预算、留痕的决策。

上下文工程最常见的三个坑，本模块逐个堵：

1. **静默截断。** 超预算时悄悄丢掉尾部，模型看不到关键事实却照样给结论。
   → 这里的裁剪必须留痕：``manifest.dropped`` 记录丢了什么、为什么丢，并进 Span。
2. **原文灌入。** 把裁判文书全文塞进上下文，既爆预算，又让「模型当时看的是哪一份」
   无法举证。→ :meth:`ContextAssembler.add_evidence_refs` **强制**只放
   ``evidence_id`` + 抽取字段，检测到长文本直接抛错。这是硬约束不是约定。
3. **关键块被挤掉。** 预算压力下把任务清单、输出 Schema 这类必需块裁掉。
   → ``required=True`` 的块不参与裁剪；必需块本身就超预算时抛错，而不是硬塞。

排序原则是**目标与契约在前，事实在中，参考在后**：模型在长上下文中对首尾更敏感，
而「你要做什么、做完的标准是什么」被挤到中段是最糟的情况。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: 单条证据进入上下文的原文长度上限。超过即判定为「灌原文」。
#: 抽取后的结构化字段不应该有这么长——真有，说明抽取没做。
MAX_INLINE_CHARS = 400

#: 默认上下文预算（字符数）。真实部署按模型窗口换算。
DEFAULT_BUDGET = 12000


class ContextError(Exception):
    """上下文装配失败：原文灌入、必需块超预算。"""


@dataclass
class ContextBlock:
    key: str
    priority: int       # 越小越靠前
    required: bool
    content: Any

    @property
    def size(self) -> int:
        return len(json.dumps(self.content, ensure_ascii=False))


@dataclass
class ContextManifest:
    """本次装配的账单。进 Span，使「模型看到了什么」可回放。"""
    included: list[str] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    used_chars: int = 0
    budget_chars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "included": self.included,
            "dropped": self.dropped,
            "used_chars": self.used_chars,
            "budget_chars": self.budget_chars,
            "utilization": (round(self.used_chars / self.budget_chars, 3)
                            if self.budget_chars else None),
        }


class ContextAssembler:
    """按优先级装配上下文，超预算时从**最低优先级的非必需块**开始丢。"""

    def __init__(self, budget_chars: int = DEFAULT_BUDGET) -> None:
        self.budget_chars = budget_chars
        self._blocks: list[ContextBlock] = []

    def add(self, key: str, content: Any, *, priority: int = 50,
            required: bool = False) -> "ContextAssembler":
        self._blocks.append(ContextBlock(key, priority, required, content))
        return self

    def add_evidence_refs(self, key: str, items: list[dict[str, Any]], *,
                          priority: int = 30, required: bool = False) -> "ContextAssembler":
        """加入证据引用。**强制剥离原文**，只保留 id 与抽取字段。

        两个理由：上下文放原文会爆预算；更要紧的是，放了原文之后
        「模型当时看的是不是这一份」就无法举证了——原文快照的哈希才是锚点。
        """
        refs: list[dict[str, Any]] = []
        for it in items:
            ref = {k: v for k, v in it.items()
                   if k not in ("raw_content", "text", "snapshot", "body")}
            for k, v in ref.items():
                if isinstance(v, str) and len(v) > MAX_INLINE_CHARS:
                    raise ContextError(
                        f"{key}.{k} 长度 {len(v)} 超过 {MAX_INLINE_CHARS}，疑似把原文灌入上下文。"
                        f"上下文只放 evidence_id 与抽取字段，原文留在对象存储靠哈希关联"
                    )
            refs.append(ref)
        return self.add(key, refs, priority=priority, required=required)

    def add_knowledge_chunks(self, key: str, chunks: list[dict[str, Any]], *,
                             text_field: str = "text", max_chunk_chars: int = 600,
                             priority: int = 40) -> "ContextAssembler":
        """加入检索到的知识片段（政策条款、历史案例）。

        与 :meth:`add_evidence_refs` 的区别值得讲清楚，这不是一回事：

        - **证据原文**（裁判文书全文、征信 PDF）绝不入上下文——模型不需要读它，
          需要的是抽取后的字段；原文靠哈希锚定，入了反而无法举证看的是哪一份。
        - **知识片段**的正文**就是**模型要推理的对象，必须入上下文。

        所以这里不剥离正文，只做**逐片截断**，且截断留痕（``_truncated``）——
        默默砍掉条款后半段，会让模型看不到但书和除外情形，这是合规场景里的致命错误。
        """
        out: list[dict[str, Any]] = []
        for c in chunks:
            item = dict(c)
            body = item.get(text_field)
            if isinstance(body, str) and len(body) > max_chunk_chars:
                item[text_field] = body[:max_chunk_chars]
                item["_truncated"] = {"original_chars": len(body), "kept": max_chunk_chars}
            out.append(item)
        return self.add(key, out, priority=priority)

    def build(self) -> tuple[dict[str, Any], ContextManifest]:
        man = ContextManifest(budget_chars=self.budget_chars)
        ordered = sorted(self._blocks, key=lambda b: (b.priority, b.key))

        required = [b for b in ordered if b.required]
        optional = [b for b in ordered if not b.required]

        used = sum(b.size for b in required)
        if used > self.budget_chars:
            raise ContextError(
                f"必需块合计 {used} 字符已超预算 {self.budget_chars}："
                f"{[b.key for b in required]}。这是设计问题，不能靠裁剪掩盖"
            )

        keep = {b.key for b in required}
        # 非必需块按优先级依次纳入，装不下的**逐条记录原因**，不静默丢弃
        for b in optional:
            if used + b.size <= self.budget_chars:
                keep.add(b.key)
                used += b.size
            else:
                man.dropped.append({
                    "key": b.key, "size": b.size,
                    "why": f"剩余预算 {self.budget_chars - used} 字符不足以容纳",
                })

        payload = {b.key: b.content for b in ordered if b.key in keep}
        man.included = [b.key for b in ordered if b.key in keep]
        man.used_chars = used
        return payload, man
