"""credit-core-mcp —— 信贷核心 / 额度管理。

系统中**唯一提供写能力**的 MCP Server。写类工具（adjust_limit / add_guarantee /
rollback_adjustment）必须携带 idempotency_key 与 approval_token，
且只对 disposition-executor 一个 Worker 开放。
"""

from __future__ import annotations

from typing import Any

from .world import MCPError, World

SERVER_NAME = "credit-core-mcp"

# 真实 MCP Tool 定义。Mock 与生产实现共用这一份，保证零 Schema 差异。
TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_facility",
        "description": "查询主体的授信额度与产品明细",
        "inputSchema": {
            "type": "object",
            "properties": {"subject_id": {"type": "string"}},
            "required": ["subject_id"],
        },
    },
    {
        "name": "get_exposure",
        "description": "查询主体当前敞口，并按 depth 穿透关联主体（担保圈 / 集团户 / 上下游）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject_id": {"type": "string"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 3, "default": 2},
            },
            "required": ["subject_id"],
        },
    },
    {
        "name": "get_collateral",
        "description": "查询抵质押物状态与估值",
        "inputSchema": {
            "type": "object",
            "properties": {"subject_id": {"type": "string"}},
            "required": ["subject_id"],
        },
    },
    {
        "name": "get_guarantee_ledger",
        "description": "查询主体对外担保台账：被担保方、担保余额、被担保方状态、"
                       "以及共同担保人/反担保/抵押等缓释措施",
        "inputSchema": {
            "type": "object",
            "properties": {"subject_id": {"type": "string"}},
            "required": ["subject_id"],
        },
    },
    {
        "name": "adjust_limit",
        "description": "调整授信额度（写操作，需幂等键与审批令牌）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject_id": {"type": "string"},
                "new_limit": {"type": "number"},
                "idempotency_key": {"type": "string"},
                "approval_token": {"type": "string"},
            },
            "required": ["subject_id", "new_limit", "idempotency_key"],
        },
    },
    {
        "name": "add_guarantee",
        "description": "追加担保要求（写操作，需幂等键与审批令牌）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject_id": {"type": "string"},
                "guarantee_type": {"type": "string"},
                "idempotency_key": {"type": "string"},
                "approval_token": {"type": "string"},
            },
            "required": ["subject_id", "guarantee_type", "idempotency_key"],
        },
    },
    {
        "name": "rollback_adjustment",
        "description": "按回滚点冲正此前的额度调整（写操作）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject_id": {"type": "string"},
                "rollback_point_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["subject_id", "rollback_point_id", "idempotency_key"],
        },
    },
]


class CreditCoreServer:
    def __init__(self, world: World) -> None:
        self.world = world
        # 幂等去重表：同一 idempotency_key 重复投递返回首次结果，不重复执行
        self._executed: dict[str, dict[str, Any]] = {}
        self._rollback_points: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def call(self, tool: str, args: dict[str, Any], *, caller: str) -> dict[str, Any]:
        # 权限校验已由 registry 依据权限矩阵统一完成（唯一写触点在矩阵中约束）
        handler = getattr(self, f"_{tool}", None)
        if handler is None:
            raise MCPError("UNKNOWN_TOOL", f"{SERVER_NAME} 无工具 {tool}")
        return handler(args)

    # ---- 读 ---------------------------------------------------------
    def _get_facility(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.world.section("credit_core").get("facility", {})

    def _get_exposure(self, args: dict[str, Any]) -> dict[str, Any]:
        depth = int(args.get("depth", 2))
        cc = self.world.section("credit_core")
        related = cc.get("related_subjects", [])
        truncated = None
        if depth < 2 and related:
            related = [r for r in related if r.get("depth", 1) <= depth]
            truncated = depth
        return {
            "total_exposure": cc.get("total_exposure"),
            "related_subjects": related,
            "guarantee_ring": cc.get("guarantee_ring", []),
            "truncated_at_depth": truncated,
            "source_doc_uri": cc.get("source_doc_uri"),
        }

    def _get_collateral(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.world.section("credit_core").get("collateral", {})

    def _get_guarantee_ledger(self, args: dict[str, Any]) -> dict[str, Any]:
        """对外担保台账。

        没有担保业务的主体返回空台账而非报错——「查了，没有」与「查不到」是
        两种不同的证据状态，前者是有效的负向证据，不应被当成取证失败。
        """
        cc = self.world.section("credit_core")
        ledger = cc.get("guarantee_ledger")
        if not ledger:
            return {"entries": [], "total_outstanding_guarantee": 0,
                    "empty_reason": "该主体无对外担保记录（已查询，非取证失败）"}
        return ledger

    # ---- 写 ---------------------------------------------------------
    def _require_approval(self, args: dict[str, Any], action: str) -> None:
        """L2 及以上动作必须携带有效审批令牌。缺失即拒绝，不放行。"""
        token = args.get("approval_token")
        if not token:
            raise MCPError("APPROVAL_REQUIRED", f"{action} 缺少审批令牌，拒绝执行")
        if not str(token).startswith("apv-"):
            raise MCPError("APPROVAL_INVALID", f"{action} 审批令牌验签失败")

    def _adjust_limit(self, args: dict[str, Any]) -> dict[str, Any]:
        key = args["idempotency_key"]
        if key in self._executed:  # 幂等：重复投递直接返回首次结果
            return {**self._executed[key], "idempotent_replay": True}

        self._require_approval(args, "adjust_limit")
        facility = self.world.section("credit_core").get("facility", {})
        old_limit = facility.get("total_limit")
        self._seq += 1
        rp_id = f"RP-{self.world.case_id.split('-')[-1]}-{self._seq:03d}"
        self._rollback_points[rp_id] = {"field": "total_limit", "old_value": old_limit}
        facility["total_limit"] = args["new_limit"]

        result = {
            "status": "SUCCESS",
            "old_limit": old_limit,
            "new_limit": args["new_limit"],
            "rollback_point_id": rp_id,
            "audit_serial": f"AUD-{self.world.case_id}-{self._seq:03d}",
            "effective_at": "2026-08-14T10:23:41Z",
        }
        self._executed[key] = result
        return result

    def _add_guarantee(self, args: dict[str, Any]) -> dict[str, Any]:
        key = args["idempotency_key"]
        if key in self._executed:
            return {**self._executed[key], "idempotent_replay": True}
        self._require_approval(args, "add_guarantee")
        self._seq += 1
        rp_id = f"RP-{self.world.case_id.split('-')[-1]}-{self._seq:03d}"
        self._rollback_points[rp_id] = {"field": "guarantee", "old_value": None}
        result = {
            "status": "SUCCESS",
            "guarantee_type": args["guarantee_type"],
            "rollback_point_id": rp_id,
            "audit_serial": f"AUD-{self.world.case_id}-{self._seq:03d}",
            "effective_at": "2026-08-14T10:23:52Z",
        }
        self._executed[key] = result
        return result

    def _rollback_adjustment(self, args: dict[str, Any]) -> dict[str, Any]:
        rp_id = args["rollback_point_id"]
        rp = self._rollback_points.get(rp_id)
        if rp is None:
            # 回滚失败不做二次尝试，直接上抛由 Executor 冻结 Case 并升级人工
            raise MCPError("ROLLBACK_POINT_NOT_FOUND", f"回滚点 {rp_id} 不存在")
        if rp["field"] == "total_limit":
            self.world.section("credit_core")["facility"]["total_limit"] = rp["old_value"]
        return {"status": "ROLLED_BACK", "restored": rp}
