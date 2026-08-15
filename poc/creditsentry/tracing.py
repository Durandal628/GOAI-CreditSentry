"""全链路轨迹记录。

Span 类型对齐 OpenTelemetry GenAI 语义约定，并在其基础上增加两类本方案特有的 Span：

- ``routing``      路由决策本身即一条 Span，记录 routing_key / 命中规则 ID / 规则版本，
                   使「为什么当时走了这条分支」可举证、可回放。
- ``adjudication`` 对抗裁决 Span，记录定性方与质疑方结论、分歧点与采信理由。

导出格式为 trace.json（Span 列表 + 树形结构），可直接喂给 Jaeger / AgentScope Studio，
也可用 ``render_tree()`` 打印成人可读的时间线。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# OTel GenAI 语义约定中的标准属性名，统一在此声明避免各处拼写漂移。
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_OPERATION = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_USAGE_IN = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUT = "gen_ai.usage.output_tokens"

SPAN_KINDS = (
    "case",          # 根 Span：一次完整的案件处置
    "agent",         # Agent 执行
    "skill",         # Skill 调用
    "mcp",           # MCP 工具调用
    "rag",           # 检索
    "llm",           # 模型推理
    "routing",       # 路由决策（本方案特有）
    "adjudication",  # 对抗裁决（本方案特有）
    "approval",      # 人工审批等待
    "execution",     # 有副作用的处置执行
)


@dataclass
class Span:
    span_id: str
    parent_id: str | None
    kind: str
    name: str
    start: float
    end: float | None = None
    status: str = "OK"
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if self.end is None:
            return 0.0
        return round((self.end - self.start) * 1000, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "name": self.name,
            "start_unix_nano": int(self.start * 1e9),
            "end_unix_nano": int(self.end * 1e9) if self.end else None,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": self.attributes,
            "events": self.events,
        }


class Tracer:
    """轻量 Span 记录器。

    刻意不依赖 opentelemetry-sdk，使 PoC 零依赖即可运行；属性命名保持与 OTel 一致，
    因此接入真实 OTel Collector 时只需替换本类，不改调用方。
    """

    def __init__(self, trace_id: str | None = None) -> None:
        self.trace_id = trace_id or uuid.uuid4().hex
        self.spans: list[Span] = []
        self._stack: list[str] = []
        # Span 的实时观察者。落盘的 trace.json 是**事后**产物，而工作台要在案件
        # 跑的过程中就把决策链路显示出来——两者取的是同一份数据，只是时机不同。
        # 观察者抛错一律吞掉：可视化坏了不能反过来弄坏被观察的业务链路。
        self.observers: list[Any] = []

    # ---- 上下文管理 -------------------------------------------------
    def span(self, kind: str, name: str, **attributes: Any) -> "_SpanCtx":
        if kind not in SPAN_KINDS:
            raise ValueError(f"未知 Span 类型：{kind}（允许：{SPAN_KINDS}）")
        return _SpanCtx(self, kind, name, attributes)

    def _start(self, kind: str, name: str, attributes: dict[str, Any]) -> Span:
        sp = Span(
            span_id=uuid.uuid4().hex[:16],
            parent_id=self._stack[-1] if self._stack else None,
            kind=kind,
            name=name,
            start=time.time(),
            attributes=dict(attributes),
        )
        self.spans.append(sp)
        self._stack.append(sp.span_id)
        self._notify("span_start", sp)
        return sp

    def _end(self, sp: Span, status: str = "OK") -> None:
        sp.end = time.time()
        sp.status = status
        if self._stack and self._stack[-1] == sp.span_id:
            self._stack.pop()
        self._notify("span_end", sp)

    def _notify(self, event: str, sp: Span) -> None:
        for obs in self.observers:
            try:
                obs(event, sp)
            except Exception:  # noqa: BLE001
                # 可视化坏了不能反过来弄坏被观察的业务链路。
                # 这是观察者模式在这里必须遵守的方向性：Trace 是旁路，不是主路。
                pass

    # ---- 导出 -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_count": len(self.spans),
            "spans": [s.to_dict() for s in self.spans],
        }

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    def render_tree(self) -> str:
        """渲染人可读的 Span 树，用于终端输出与 PPT 证据截图。"""
        children: dict[str | None, list[Span]] = {}
        for sp in self.spans:
            children.setdefault(sp.parent_id, []).append(sp)

        badge = {
            "case": "CASE", "agent": "AGENT", "skill": "SKILL", "mcp": "MCP",
            "rag": "RAG", "llm": "LLM", "routing": "ROUTE", "adjudication": "ADJUD",
            "approval": "APPRV", "execution": "EXEC",
        }
        lines: list[str] = []

        def walk(parent: str | None, prefix: str) -> None:
            kids = children.get(parent, [])
            for i, sp in enumerate(kids):
                last = i == len(kids) - 1
                connector = "└─ " if last else "├─ "
                tag = badge.get(sp.kind, sp.kind).ljust(5)
                dur = f"{sp.duration_ms:>8.1f}ms"
                flag = "" if sp.status == "OK" else f"  [{sp.status}]"
                lines.append(f"{prefix}{connector}{tag} {sp.name:<40} {dur}{flag}")
                # 路由与裁决 Span 的关键属性直接展开，这是可举证的核心
                if sp.kind in ("routing", "adjudication"):
                    ind = prefix + ("    " if last else "│   ")
                    for k in ("routing_key", "rule_id", "rule_version",
                              "verdict", "basis", "dissent"):
                        if k in sp.attributes:
                            lines.append(f"{ind}    · {k} = {sp.attributes[k]}")
                walk(sp.span_id, prefix + ("    " if last else "│   "))

        walk(None, "")
        return "\n".join(lines)

    # ---- 指标汇总 ---------------------------------------------------
    def metrics(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        failed = 0
        tok_in = tok_out = 0
        for sp in self.spans:
            by_kind[sp.kind] = by_kind.get(sp.kind, 0) + 1
            if sp.status != "OK":
                failed += 1
            tok_in += int(sp.attributes.get(GEN_AI_USAGE_IN, 0) or 0)
            tok_out += int(sp.attributes.get(GEN_AI_USAGE_OUT, 0) or 0)

        tool_spans = [s for s in self.spans if s.kind in ("mcp", "skill")]
        tool_ok = len([s for s in tool_spans if s.status == "OK"])
        root = next((s for s in self.spans if s.kind == "case"), None)

        return {
            "span_count": len(self.spans),
            "span_by_kind": by_kind,
            "failed_spans": failed,
            "tool_calls": len(tool_spans),
            "tool_success_rate": round(tool_ok / len(tool_spans), 4) if tool_spans else None,
            "tokens_in": tok_in,
            "tokens_out": tok_out,
            "end_to_end_ms": root.duration_ms if root else None,
        }


class _SpanCtx:
    def __init__(self, tracer: Tracer, kind: str, name: str, attrs: dict[str, Any]) -> None:
        self._tracer = tracer
        self._kind = kind
        self._name = name
        self._attrs = attrs
        self.span: Span | None = None

    def __enter__(self) -> Span:
        self.span = self._tracer._start(self._kind, self._name, self._attrs)
        return self.span

    def __exit__(self, exc_type, exc, tb) -> bool:
        assert self.span is not None
        if exc_type is not None:
            self.span.events.append({"name": "exception", "message": str(exc)})
            self._tracer._end(self.span, status="ERROR")
        else:
            self._tracer._end(self.span, status=self.span.status)
        return False
