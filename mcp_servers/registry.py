"""MCP 客户端与服务注册表。

PoC 默认以**进程内直连**方式调用四个 Server，以保证零依赖、零网络即可复现；
真实部署时把 ``MCPClient`` 换成经 Higress 网关的 MCP over stdio / SSE 客户端即可，
Tool 名称、入参 Schema、返回结构与错误码完全不变。

所有调用统一在此收敛，因此三件事只需实现一次：
- **权限校验**：唯一来源是 ``poc.creditsentry.permissions.PERMISSIONS``——同一份矩阵
  既在此处拦截越权调用，又生成 ``agentteams/workers/*.yaml``，两边不会漂移；
- **审计日志**：每次调用（含失败）都留痕，TraceId 关联；
- **失败策略**：错误码上抛，由 Skill 层的 failure policy 决定 retry / degrade / escalate。

这里对 ``poc.creditsentry.permissions`` 的依赖是刻意的：权限矩阵是全系统共享的契约，
且该模块为纯数据、无下游依赖，因此不会把 Agent 运行时耦合进 Server 实现。
"""

from __future__ import annotations

import time
from typing import Any

from poc.creditsentry import permissions

from .bureau_mcp import TOOLS as BUREAU_TOOLS, BureauServer
from .credit_core_mcp import TOOLS as CREDIT_TOOLS, CreditCoreServer
from .judicial_mcp import TOOLS as JUDICIAL_TOOLS, JudicialServer
from .txn_mcp import TOOLS as TXN_TOOLS, TxnServer
from .world import MCPError, World

SERVER_TOOLS = {
    "credit-core-mcp": CREDIT_TOOLS,
    "bureau-mcp": BUREAU_TOOLS,
    "judicial-mcp": JUDICIAL_TOOLS,
    "txn-mcp": TXN_TOOLS,
}


class MCPClient:
    """进程内 MCP 客户端。"""

    def __init__(self, world: World, tracer: Any = None) -> None:
        self.world = world
        self.tracer = tracer
        self.servers = {
            "credit-core-mcp": CreditCoreServer(world),
            "bureau-mcp": BureauServer(world),
            "judicial-mcp": JudicialServer(world),
            "txn-mcp": TxnServer(world),
        }
        self.audit_log: list[dict[str, Any]] = []

    def list_tools(self, server: str) -> list[dict[str, Any]]:
        return SERVER_TOOLS[server]

    def call(
        self,
        server: str,
        tool: str,
        args: dict[str, Any],
        *,
        caller: str,
        retries: int = 0,
        backoff: float = 0.0,
    ) -> dict[str, Any]:
        """调用一个 MCP 工具。

        retries 用于实现 Skill 声明的退避重试策略（如征信限流后退避重试 3 次）。
        """
        if server not in self.servers:
            raise MCPError("UNKNOWN_SERVER", f"未注册的 MCP Server：{server}")

        # 权限矩阵是唯一的授权来源：不在表内即无权，不存在默认放行
        if not permissions.is_allowed(caller, server, tool):
            self._audit(server, tool, caller, args, "ERROR", "PERMISSION_DENIED", time.time())
            raise MCPError(
                "PERMISSION_DENIED",
                f"{caller} 无权调用 {server}.{tool}（权限矩阵未授予）",
            )

        attempt = 0
        last_err: MCPError | None = None
        while attempt <= retries:
            started = time.time()
            span_ctx = (
                self.tracer.span("mcp", f"{server}.{tool}", **{
                    "mcp.server": server, "mcp.tool": tool, "caller": caller,
                    "attempt": attempt + 1,
                })
                if self.tracer else _NullCtx()
            )
            with span_ctx as span:
                try:
                    result = self.servers[server].call(tool, args, caller=caller)
                except MCPError as e:
                    last_err = e
                    if span is not None:
                        span.status = "ERROR"
                        span.attributes["error.code"] = e.code
                    self._audit(server, tool, caller, args, "ERROR", e.code, started)
                    # 仅限流类错误值得重试；权限、授权类错误立即上抛，不重试不降级
                    if e.code != "RATE_LIMITED" or attempt >= retries:
                        raise
                    attempt += 1
                    if backoff:
                        time.sleep(backoff * (2 ** attempt))
                    continue
                self._audit(server, tool, caller, args, "OK", None, started)
                return result

        assert last_err is not None
        raise last_err

    def _audit(
        self, server: str, tool: str, caller: str,
        args: dict[str, Any], status: str, err: str | None, started: float,
    ) -> None:
        """审计日志。参数只记摘要键名，不落敏感值。"""
        self.audit_log.append({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "trace_id": getattr(self.tracer, "trace_id", None),
            "server": server,
            "tool": tool,
            "caller": caller,
            "arg_keys": sorted(args.keys()),
            "status": status,
            "error_code": err,
            "latency_ms": round((time.time() - started) * 1000, 1),
        })


class _NullCtx:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False
