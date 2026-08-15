"""txn-mcp —— 交易流水（只读 · PII）。

降级规则：**采样不足时输出弱证据等级，而非强断言。**
流水分析最容易产生「看起来很确定」的错觉——覆盖率不够时给出的异常判定必须被降级，
否则会成为误杀好客户的主要来源。
"""

from __future__ import annotations

from typing import Any

from .world import MCPError, World

SERVER_NAME = "txn-mcp"

# 单次查询返回上限，超过则分片处理后合并
PAGE_LIMIT = 500
# 覆盖率低于此阈值即判定采样不足
MIN_COVERAGE = 0.8

TOOLS: list[dict[str, Any]] = [
    {
        "name": "query_transactions",
        "description": "查询账户流水明细（超过单页上限自动分片）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_ids": {"type": "array", "items": {"type": "string"}},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "cursor": {"type": "string"},
            },
            "required": ["account_ids", "date_from", "date_to"],
        },
    },
    {
        "name": "get_counterparty_summary",
        "description": "按对手方汇总资金往来，标注是否关联方",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_ids": {"type": "array", "items": {"type": "string"}},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
            },
            "required": ["account_ids", "date_from", "date_to"],
        },
    },
    {
        "name": "get_flow_pattern",
        "description": "识别资金异常模式：回流 / 空转 / 集中转出 / 整数化",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_ids": {"type": "array", "items": {"type": "string"}},
                "patterns": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["account_ids"],
        },
    },
]

PII_FIELDS = ("account_no", "counterparty_account")


class TxnServer:
    def __init__(self, world: World) -> None:
        self.world = world

    def call(self, tool: str, args: dict[str, Any], *, caller: str) -> dict[str, Any]:
        # 权限校验已由 registry 依据权限矩阵统一完成（PII 触点唯一化在矩阵中约束）
        handler = getattr(self, f"_{tool}", None)
        if handler is None:
            raise MCPError("UNKNOWN_TOOL", f"{SERVER_NAME} 无工具 {tool}")
        return handler(args)

    def _query_transactions(self, args: dict[str, Any]) -> dict[str, Any]:
        txn = self.world.section("txn")
        records = txn.get("transactions", [])
        total = txn.get("total_count", len(records))
        # 分片：超过单页上限时返回游标，由 Skill 侧循环取全并合并
        page = records[:PAGE_LIMIT]
        next_cursor = "p2" if total > PAGE_LIMIT and not args.get("cursor") else None
        return {
            "transactions": [self._redact(r) for r in page],
            "returned": len(page),
            "total_count": total,
            "next_cursor": next_cursor,
        }

    def _get_counterparty_summary(self, args: dict[str, Any]) -> dict[str, Any]:
        txn = self.world.section("txn")
        return {
            "counterparties": txn.get("counterparties", []),
            "coverage": txn.get("coverage", 1.0),
            "source_doc_uri": txn.get("source_doc_uri"),
        }

    def _get_flow_pattern(self, args: dict[str, Any]) -> dict[str, Any]:
        txn = self.world.section("txn")
        coverage = txn.get("coverage", 1.0)
        result = {
            "anomalies": txn.get("anomalies", []),
            "coverage": coverage,
            "baseline_band": txn.get("baseline_band"),
            "source_doc_uri": txn.get("source_doc_uri"),
        }
        # 采样不足 → 标注 undersampled，证据账本据此降为弱证据
        if coverage < MIN_COVERAGE:
            result["undersampled"] = True
            result["note"] = (
                f"流水覆盖率 {coverage:.0%} 低于 {MIN_COVERAGE:.0%}，"
                f"异常判定降为弱证据，不可单独定性"
            )
        return result

    @staticmethod
    def _redact(record: dict[str, Any]) -> dict[str, Any]:
        out = dict(record)
        for f in PII_FIELDS:
            if f in out and out[f]:
                val = str(out[f])
                out[f] = val[:4] + "*" * max(0, len(val) - 8) + val[-4:]
        return out
