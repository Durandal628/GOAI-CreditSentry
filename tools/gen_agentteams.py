#!/usr/bin/env python3
"""由权限矩阵生成 AgentTeams 声明式配置。

    python tools/gen_agentteams.py

生成 ``agentteams/`` 下的全部内容：
    souls/<agent>/SOUL.md      Worker 包内的身份定义
    workers/<agent>.yaml       Worker CR（K8s 风格，含声明式 MCP 与权限）
    team.yaml                  Team CR
    routing-table.yaml         SOP 阶段状态机路由表

真源是 ``poc/creditsentry/permissions.py`` 与 ``routing.RULES_DOC``。
手工改这些生成物没有意义——下次生成会被覆盖，且 ``poc/test_safety.py``
会断言磁盘内容与真源一致。要改权限，改矩阵。

不依赖 PyYAML：YAML 由内置的最小发射器输出，保证零依赖可复现。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from poc.creditsentry import permissions, routing, skills  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "agentteams")

TEAM_NAME = "credit-sentry"
API_VERSION = "agentteams.io/v1"
HEADER = "# 由 tools/gen_agentteams.py 自动生成，请勿手工编辑。\n# 真源：poc/creditsentry/permissions.py\n"


def _q(s: str) -> str:
    """YAML 标量。含特殊字符时加引号并转义。"""
    if s == "":
        return '""'
    if any(c in s for c in ':#{}[]&*?|-<>=!%@`"\'\n') or s.strip() != s:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'
    return s


def _block(text: str, indent: int) -> str:
    """多行文本用 YAML 折叠块，避免长行。"""
    pad = " " * indent
    return ">-\n" + "\n".join(pad + line for line in _wrap(text, 76))


def _wrap(text: str, width: int) -> list[str]:
    """按显示宽度折行（中文按 2 计）。"""
    out, cur, w = [], "", 0
    for ch in text.replace("\n", " "):
        cw = 2 if ord(ch) > 0x2E80 else 1
        if w + cw > width and ch == " ":
            out.append(cur.strip())
            cur, w = "", 0
            continue
        cur += ch
        w += cw
    if cur.strip():
        out.append(cur.strip())
    return out or [""]


# ---------------------------------------------------------------------------
# SOUL.md
# ---------------------------------------------------------------------------

def render_soul(agent: str, e: dict) -> str:
    is_leader = agent == permissions.TEAM_LEADER
    mcp_lines = []
    for server, tools in sorted(e["mcp"].items()):
        for tool, mode in sorted(tools.items()):
            mark = "**写**" if mode == permissions.WRITE else "读"
            mcp_lines.append(f"| `{server}` | `{tool}` | {mark} |")

    L = [
        f"# SOUL · {agent}",
        "",
        "> 本文件是 AgentTeams Worker 包内的身份定义，由权限矩阵自动生成。",
        f"> 角色：{e['role']}",
        f"> 权限等价类：{e['equivalence_class']}",
        "",
        "## 我是谁",
        "",
        f"我是 `{agent}`，{'贷后处置团队的 Team Leader' if is_leader else 'credit-sentry 团队的职能 Worker'}。",
        f"{e['role']}。",
        "",
        "## 我能做什么",
        "",
        e["capabilities"] + "。",
        "",
        "## 我不能做什么",
        "",
        "以下是**刻意的能力剥夺**，是内控职责分离在 Agent 拓扑上的落地，不是疏漏：",
        "",
        f"- {e['notes']}",
    ]
    if not e["llm"]:
        L.append("- 不进行自由推理：本 Agent 仅按规则驱动执行，不对是否执行做任何判断")
        L.append("- 不持有任何模型入口：权限矩阵中 `model_profile` 为空，配置校验会拒绝为我绑定模型")
    if not e["pii_access"]:
        L.append("- 不接触任何 PII 数据源（征信、交易流水）")
    if e["llm"] and e["model_profile"]:
        L += [
            "",
            "## 我用哪个模型",
            "",
            f"档位 `{e['model_profile']}`（定义见 `config/models.yaml`）。"
            f"模型绑定与工具权限同处一份真源，受同一套审计约束。",
        ]
        if agent in ("risk-analyst", "devils-advocate"):
            other = "devils-advocate" if agent == "risk-analyst" else "risk-analyst"
            L.append("")
            L.append(
                f"我与 `{other}` 的目标函数刻意对立，因此**必须使用不同模型族**。"
                f"若两者跑在同一批权重上，对抗只是从上下文层面搬到权重层面坍缩——"
                f"同一个模型的两个实例共享相同先验与盲区，不构成两个独立观点。"
                f"该约束由 `poc/test_safety.py` 强制。"
            )
    L += [
        "",
        "## 我的决策边界",
        "",
        e["decision_boundary"] + "。",
        "",
        "## 我可以调用什么",
        "",
        "### Skill",
        "",
    ]
    for s in e["skills"]:
        meta = skills.REGISTRY.get(s)
        L.append(f"- `{s}`（{meta.tier} · {meta.category}）—— {meta.purpose}" if meta else f"- `{s}`")
    L += ["", "### MCP 工具", ""]
    if mcp_lines:
        L += ["| Server | Tool | 权限 |", "|---|---|---|"] + mcp_lines
    else:
        L.append("**无**。本 Agent 不持有任何 MCP 工具权限。")
        if agent in ("risk-analyst", "devils-advocate"):
            L.append("")
            L.append("这是刻意设计：纯推理角色若持有取证工具，会「边查边下结论」产生确认偏差。")
    L += [
        "",
        "## 我的执行过程如何被记录",
        "",
        e["trace"] + "。",
        "",
        "---",
        "",
        "本 Agent 的全部权限声明见 `agentteams/workers/" + agent + ".yaml`；",
        "拆分依据见 `docs/agent-decomposition-law.md`。",
        "",
    ]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Worker CR
# ---------------------------------------------------------------------------

def render_worker_cr(agent: str, e: dict) -> str:
    L = [HEADER, f"apiVersion: {API_VERSION}", "kind: Worker", "metadata:",
         f"  name: {agent}", "  labels:", f"    team: {TEAM_NAME}",
         f"    equivalence-class: {_q(e['equivalence_class'])}",
         f"    pii-touchpoint: {str(e['pii_access']).lower()}",
         "spec:", f"  runtime: {e['runtime']}",
         f"  soul: souls/{agent}/SOUL.md",
         "  env:",
         f"    - name: AGENT_ID", f"      value: {agent}",
         f"    - name: TEAM", f"      value: {TEAM_NAME}",
         "    - name: CASE_STATE_DSN",
         "      valueFrom: { secretKeyRef: { name: creditsentry-db, key: dsn } }",
         "  llm:", f"    enabled: {str(e['llm']).lower()}"]
    if not e["llm"]:
        L.append("    # 仅规则驱动：执行环节不给模型发挥空间")
        L.append("    profile: null   # 唯一写触点刻意不持有模型入口")
    else:
        L.append(f"    profile: {e['model_profile']}"
                 f"   # 档位定义见 config/models.yaml")
        if agent in ("risk-analyst", "devils-advocate"):
            L.append("    # 对抗双方必须分属不同模型族：同族权重会让对抗在权重层面坍缩")
    L += ["    # 凭证由 Higress 统一签发 consumer token，Worker 侧无长期密钥",
          "    gateway: higress-ai-gateway"]

    L.append("  mcp:")
    if e["mcp"]:
        for server, tools in sorted(e["mcp"].items()):
            L.append(f"    - server: {server}")
            L.append("      allowedTools:")
            for tool, mode in sorted(tools.items()):
                L.append(f"        - name: {tool}")
                L.append(f"          mode: {mode}")
            L.append("      auth:")
            L.append("        type: consumer-token")
            L.append(f"        consumer: {agent}")
            if server in ("bureau-mcp", "txn-mcp"):
                L.append("      egress:")
                L.append("        piiRedaction: true   # 出站脱敏由网关执行")
    else:
        L.append("    []   # 刻意为空：本 Agent 不持有任何工具权限")

    L.append("  skills:")
    for s in e["skills"]:
        meta = skills.REGISTRY.get(s)
        ver = meta.version if meta else "latest"
        L.append(f"    - name: {s}")
        L.append(f"      version: {_q(ver)}")
        L.append("      source: nacos://creditsentry/skills")
    L += ["  policy:",
          f"    piiAccess: {str(e['pii_access']).lower()}",
          f"    prohibited: {_block(e['notes'], 6)}",
          f"    decisionBoundary: {_block(e['decision_boundary'], 6)}",
          "  observability:",
          "    tracing: otel-genai",
          f"    notes: {_block(e['trace'], 6)}",
          ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Team CR
# ---------------------------------------------------------------------------

def render_team_cr() -> str:
    leader = permissions.PERMISSIONS[permissions.TEAM_LEADER]
    L = [HEADER, f"apiVersion: {API_VERSION}", "kind: Team",
         "metadata:", f"  name: {TEAM_NAME}",
         "  labels: { domain: credit-risk, stage: post-loan }",
         "spec:",
         "  # Manager 保持 AgentTeams 默认的轻量管理指令集。",
         "  # 单一贷后场景下它接近透明，我们不为它编造职责——它不可省的只有三件事：",
         "  #   1. 人机会话入口与实时干预  2. case room 生命周期  3. 跨 Team 移交",
         "  manager:",
         "    runtime: openclaw",
         "    soul: souls/manager/SOUL.md",
         "    responsibilities:",
         "      - human-interaction-entry",
         "      - case-room-lifecycle",
         "      - cross-team-handoff",
         "  teamLeader:",
         f"    worker: {permissions.TEAM_LEADER}",
         f"    soul: souls/{permissions.TEAM_LEADER}/SOUL.md",
         f"    description: {_block(leader['role'], 6)}",
         "  workers:"]
    for a in permissions.WORKERS:
        L.append(f"    - {a}")
    L += ["  collaboration:",
          "    protocol: matrix",
          "    roomStrategy: one-room-per-case   # 一案一房间，人始终在房间内",
          "    fileSharing: minio                # 证据附件不进消息体",
          "    humanInLoop:",
          "      approvalChannel: matrix-mention",
          "      approverRoles: [风险经理, 授信审批人]",
          "  routing:",
          "    table: routing-table.yaml",
          f"    version: {_q(routing.ROUTING_TABLE_VERSION)}",
          "    source: nacos://creditsentry/routing",
          "  state:",
          "    store: postgres",
          "    concurrency: optimistic-lock",
          "    ordering: rocketmq-fifo-by-case-id",
          "  invariants:",
          "    # 这些不变量由 poc/test_safety.py 断言，破坏即测试失败",
          f"    uniqueWriter: {permissions.writers_of('credit-core-mcp')[0]}",
          f"    piiTouchpoints: {permissions.pii_touchpoints()}",
          "    adjudicationRequiresAdvocate: true",
          "    irreversibleActionsNeverAutoExecute: true",
          ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 路由表
# ---------------------------------------------------------------------------

def render_routing_table() -> str:
    L = [HEADER, f"apiVersion: {API_VERSION}", "kind: RoutingTable",
         "metadata:", f"  name: {TEAM_NAME}-sop",
         f"  version: {_q(routing.ROUTING_TABLE_VERSION)}",
         "spec:",
         "  # SOP 五阶段固定生命周期。LLM 只做阶段内判定，不决定流程走向。",
         "  phases: [INTAKE, EVIDENCE, ADJUDICATION, DISPOSITION, AUDIT, CLOSED, EVIDENCE_GAP]",
         "  routingKey:",
         "    - phase", "    - signal_type", "    - evidence_sufficiency",
         "    - risk_tier", "    - exposure_amount",
         "  thresholds:",
         f"    evidenceSufficiency: {routing.EVIDENCE_SUFFICIENCY_THRESHOLD}",
         f"    maxEvidenceRetries: {routing.MAX_EVIDENCE_RETRIES}",
         "  rules:"]
    for r in routing.RULES_DOC:
        L.append(f"    - id: {r['id']}")
        L.append(f"      phase: {r['phase']}")
        L.append(f"      when: {_q(r['when'])}")
        L.append(f"      next: {r['next']}")
        L.append(f"      dispatch: [{', '.join(r['dispatch'])}]")
        if r["parallel"]:
            L.append("      parallel: true   # 硬约束：不可跳过任一方")
        L.append(f"      description: {_q(r['desc'])}")
    L += ["  audit:",
          "    # 路由决策本身即一条 Trace Span，「为什么走了这条分支」可举证",
          "    emitSpan: true",
          "    spanKind: routing",
          "    attributes: [routing_key, rule_id, rule_version]",
          ""]
    return "\n".join(L)


MANAGER_SOUL = """# SOUL · manager

> AgentTeams 框架原生的 Manager 角色。**保持极薄。**

## 职责取舍（诚实说明）

在单一贷后场景下，Manager 接近透明——我们不为它编造职责。
它不可省的只有三件事：

1. **人机会话入口与实时干预** —— 风险经理在 Matrix 房间内下达与介入指令；
2. **多案并发下的 case room 生命周期** —— 创建、归档、超时回收；
3. **跨 Team 移交** —— 涉嫌欺诈移交反欺诈 Team、需诉讼保全移交法务 Team。
   **Manager 是唯一有权跨 Team 的角色。**

## 为什么不能砍掉它

若砍掉 Manager，Team Leader 需同时承担人机接口 + 领域调度 + 跨团队移交，
SOUL 显著变胖；且**跨 Team 就必须持有其他 Team 的权限**，破坏最小权限原则。

## 演进计划

初赛保持框架默认的轻量管理指令集；复赛扩展至「贷后 + 反欺诈 + 法务」
三 Team 时，第 3 项职责才真正变厚。

## 我不能做什么

- 不参与任何风险判断、取证或处置执行
- 不持有任何业务系统的 MCP 工具权限
"""


def main() -> int:
    written = []
    for sub in ("souls", "workers"):
        os.makedirs(os.path.join(OUT, sub), exist_ok=True)

    for agent, e in permissions.PERMISSIONS.items():
        d = os.path.join(OUT, "souls", agent)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "SOUL.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(render_soul(agent, e))
        written.append(p)

        if agent != permissions.TEAM_LEADER:
            p = os.path.join(OUT, "workers", f"{agent}.yaml")
        else:
            p = os.path.join(OUT, "workers", f"{agent}.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(render_worker_cr(agent, e))
        written.append(p)

    d = os.path.join(OUT, "souls", "manager")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "SOUL.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(MANAGER_SOUL)
    written.append(p)

    for name, content in (("team.yaml", render_team_cr()),
                          ("routing-table.yaml", render_routing_table())):
        p = os.path.join(OUT, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(p)

    for p in written:
        print("  生成", os.path.relpath(p, ROOT))
    print(f"\n共 {len(written)} 个文件。真源：poc/creditsentry/permissions.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
