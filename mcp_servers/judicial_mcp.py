"""judicial-mcp —— 司法涉诉 / 工商登记（只读，公开数据）。

两条降级规则，都指向同一个原则：**宁可返回「不确定」，不可自动认定**。
- 检索超时 → 返回部分结果并标注 partial，由证据账本降为弱证据；
- 主体重名无法消歧 → 输出候选集并标注 ambiguous，绝不替人做主体认定。
"""

from __future__ import annotations

from typing import Any

from .world import MCPError, World

SERVER_NAME = "judicial-mcp"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_litigation",
        "description": "按主体名称检索涉诉案件",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject_name": {"type": "string"},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
            },
            "required": ["subject_name"],
        },
    },
    {
        "name": "get_judgment_doc",
        "description": "获取裁判文书原文快照",
        "inputSchema": {
            "type": "object",
            "properties": {"case_no": {"type": "string"}},
            "required": ["case_no"],
        },
    },
    {
        "name": "get_business_registration",
        "description": "获取工商登记信息",
        "inputSchema": {
            "type": "object",
            "properties": {"subject_id": {"type": "string"}},
            "required": ["subject_id"],
        },
    },
    {
        "name": "get_change_history",
        "description": "获取工商变更历史（法定代表人、股权、经营范围）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject_id": {"type": "string"},
                "months": {"type": "integer", "default": 12},
            },
            "required": ["subject_id"],
        },
    },
]


class JudicialServer:
    def __init__(self, world: World) -> None:
        self.world = world

    def call(self, tool: str, args: dict[str, Any], *, caller: str) -> dict[str, Any]:
        # 权限校验已由 registry 依据权限矩阵统一完成
        handler = getattr(self, f"_{tool}", None)
        if handler is None:
            raise MCPError("UNKNOWN_TOOL", f"{SERVER_NAME} 无工具 {tool}")
        return handler(args)

    def _search_litigation(self, args: dict[str, Any]) -> dict[str, Any]:
        jud = self.world.section("judicial")
        result = {
            "cases": jud.get("cases", []),
            "source_doc_uri": jud.get("source_doc_uri"),
        }
        # 重名歧义：输出候选集，标注 ambiguous，不自动认定
        if jud.get("name_ambiguous"):
            result["ambiguous"] = True
            result["candidates"] = jud.get("candidates", [])
            result["note"] = "主体名称存在重名，未完成消歧，结果不可直接用于定性"
        # 检索超时：返回部分结果，标注 partial
        if jud.get("simulate_timeout_partial"):
            result["partial"] = True
            result["note"] = "检索超时，仅返回部分结果"
        return result

    def _get_judgment_doc(self, args: dict[str, Any]) -> dict[str, Any]:
        docs = self.world.section("judicial").get("documents", {})
        doc = docs.get(args["case_no"])
        if doc is None:
            raise MCPError("NOT_FOUND", f"裁判文书 {args['case_no']} 不存在")
        return doc

    def _get_business_registration(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.world.section("judicial").get("registration", {})

    def _get_change_history(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.world.section("judicial").get("change_history", {})
