"""bureau-mcp —— 征信系统（只读 · PII）。

高敏数据源。两条硬规则：
1. 只对 due-diligence 一个 Worker 开放（PII 触点唯一化，使出站脱敏范围可收敛）；
2. **无授权查询立即失败并记录合规事件，绝不降级绕过** —— 征信查询必须有明确授权用途。
"""

from __future__ import annotations

from typing import Any

from .world import MCPError, World

SERVER_NAME = "bureau-mcp"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_credit_report",
        "description": "获取主体征信报告（需授权编号）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject_id": {"type": "string"},
                "report_date": {"type": "string"},
                "authorization_id": {"type": "string", "description": "征信查询授权编号"},
            },
            "required": ["subject_id", "authorization_id"],
        },
    },
    {
        "name": "diff_report",
        "description": "比对两个时点的征信报告变动",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject_id": {"type": "string"},
                "baseline_date": {"type": "string"},
                "report_date": {"type": "string"},
                "authorization_id": {"type": "string"},
            },
            "required": ["subject_id", "baseline_date", "authorization_id"],
        },
    },
    {
        "name": "get_query_history",
        "description": "获取征信被查询记录（用于识别多头授信）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject_id": {"type": "string"},
                "months": {"type": "integer", "default": 6},
                "authorization_id": {"type": "string"},
            },
            "required": ["subject_id", "authorization_id"],
        },
    },
]

# 敏感字段清单。经 Higress 出站时按此清单脱敏，Worker 侧永远拿不到明文。
PII_FIELDS = ("legal_rep_id_no", "contact_phone", "bank_account")


class BureauServer:
    def __init__(self, world: World) -> None:
        self.world = world
        self.compliance_events: list[dict[str, Any]] = []
        self._retry_budget = {"get_credit_report": 0}

    def call(self, tool: str, args: dict[str, Any], *, caller: str) -> dict[str, Any]:
        # 权限校验已由 registry 依据权限矩阵统一完成（PII 触点唯一化在矩阵中约束）
        # 授权校验：这条规则不允许降级绕过
        if not args.get("authorization_id"):
            self.compliance_events.append({
                "type": "UNAUTHORIZED_BUREAU_QUERY",
                "tool": tool,
                "subject_id": args.get("subject_id"),
                "detail": "征信查询未提供授权编号，已拒绝",
            })
            raise MCPError("AUTHORIZATION_MISSING", "征信查询缺少授权编号，拒绝执行且不降级")

        handler = getattr(self, f"_{tool}", None)
        if handler is None:
            raise MCPError("UNKNOWN_TOOL", f"{SERVER_NAME} 无工具 {tool}")
        return handler(args)

    def _get_credit_report(self, args: dict[str, Any]) -> dict[str, Any]:
        bureau = self.world.section("bureau")
        # 模拟限流：首次调用触发一次限流，验证 Skill 侧的指数退避重试
        if bureau.get("simulate_rate_limit") and self._retry_budget["get_credit_report"] == 0:
            self._retry_budget["get_credit_report"] += 1
            raise MCPError("RATE_LIMITED", "征信查询触发限流，请退避后重试")
        return self._redact(bureau.get("report", {}))

    def _diff_report(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._redact(self.world.section("bureau").get("diff", {}))

    def _get_query_history(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.world.section("bureau").get("query_history", {})

    @staticmethod
    def _redact(payload: dict[str, Any]) -> dict[str, Any]:
        """模拟 Higress 出站脱敏。真实部署时由网关统一执行，此处内联以便 Demo 可见。"""
        out = dict(payload)
        for f in PII_FIELDS:
            if f in out and out[f]:
                val = str(out[f])
                out[f] = val[:3] + "*" * max(0, len(val) - 6) + val[-3:]
        out["_redacted_fields"] = [f for f in PII_FIELDS if f in payload]
        return out
