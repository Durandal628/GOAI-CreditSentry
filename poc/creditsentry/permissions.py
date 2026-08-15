"""权限矩阵与 Agent 身份 —— 全系统唯一真源。

这是「内控即拓扑」从口号变成事实的地方。同一份声明驱动四处：

1. **运行时授权**：``mcp_servers/registry.py`` 在派发 MCP 调用前逐条校验；
2. **Skill 授权**：``skills.skill()`` 装饰器由此推导 ``callers``，不接受手写；
3. **Worker CR 与 SOUL.md**：``tools/gen_agentteams.py`` 由此生成 ``agentteams/`` 下的
   全部声明，因此「权限矩阵即 CR 配置」是可校验的事实，不是修辞；
4. **回归测试**：``poc/test_safety.py`` 断言磁盘上的 CR 与本文件一致，防止两边漂移。

Agent 数量由此矩阵推导而来（见 ``docs/agent-decomposition-law.md``）：
**6 = 5 个权限等价类 + 1 个目标函数对立拆分**。
"""

from __future__ import annotations

from typing import Any

# 读 / 写标记，用于生成 CR 时标注工具的副作用性质
READ = "read"
WRITE = "write"

# ---------------------------------------------------------------------------
# 权限矩阵 + Agent 身份
# ---------------------------------------------------------------------------
# 每个 Worker 声明：角色、权限等价类、可访问的 MCP 工具、可调用的 Skill、
# 是否允许 LLM 自由推理、绑定的模型档位、禁止事项、决策边界与轨迹记录方式。
# 「不在表内即无权」——不存在默认放行。
#
# ``model_profile`` 指向 ``config/models.yaml`` 中的档位名，是模型配置四层分离的
# 第 3 层（见 ``modelconfig.py``）。它和工具权限一样属于**受审计的绑定**：
# 对抗双方必须不同模型族、PII 触点的模型不得越出数据驻留白名单、llm=False 的
# Agent 不得持有任何模型入口——三条均由 ``poc/test_safety.py`` 强制。

PERMISSIONS: dict[str, dict[str, Any]] = {
    "risk-commander": {
        "role": "Team Leader · 路由与裁决",
        "equivalence_class": "编排层（不持有任何业务工具）",
        "runtime": "openclaw",
        # 刻意为空：Commander 可以裁决与派发，但不得亲自取证或执行
        "mcp": {},
        "skills": ["RiskGate", "ReportCompose"],
        "llm": True,
        "model_profile": "commander-route",
        "pii_access": False,
        "capabilities": "阶段状态机路由、任务 fan-out、冲突裁决、风险分级、发起审批、升级人工",
        "notes": "不得调用任何取证或处置工具；不得修改证据账本；不得跳过 DevilsAdvocate",
        "decision_boundary": "L0/L1 自主推进；L2 必须发起审批并等待人工回调；"
                             "L3 只产出方案、不派发执行。裁决遵循「证据等级优先于置信度」",
        "trace": "每次路由决策落一条 routing Span（含 routing_key / 规则 ID / 规则版本）；"
                 "每次裁决落一条 adjudication Span（含双方结论与采信理由）",
    },
    "signal-hub": {
        "role": "多源信号聚合与影响面测绘",
        "equivalence_class": "内部只读聚合",
        "runtime": "openclaw",
        "mcp": {
            "credit-core-mcp": {
                "get_facility": READ,
                "get_exposure": READ,
                "get_collateral": READ,
            },
        },
        "skills": ["SignalFusion", "ExposureMapping"],
        "llm": True,
        "model_profile": "worker-light",
        "pii_access": False,
        "capabilities": "跨源信号归并去重、噪声压降、敞口与担保圈 / 集团户 / 上下游测绘",
        "notes": "不得判断风险是否成立；不得访问征信 / 司法 / 流水等外部 PII 源",
        "decision_boundary": "全自主（纯只读，无外部副作用）",
        "trace": "记录输入信号条数、归并后条数、降噪率，以及每条被丢弃信号的丢弃理由"
                 "（可回溯，防止误丢高风险信号）",
    },
    "due-diligence": {
        "role": "尽调取证 · 唯一外部 PII 触点",
        "equivalence_class": "唯一外部 PII 取证触点",
        "runtime": "openclaw",
        "mcp": {
            "bureau-mcp": {
                "get_credit_report": READ,
                "diff_report": READ,
                "get_query_history": READ,
            },
            "judicial-mcp": {
                "search_litigation": READ,
                "get_judgment_doc": READ,
                "get_business_registration": READ,
                "get_change_history": READ,
            },
            "txn-mcp": {
                "query_transactions": READ,
                "get_counterparty_summary": READ,
                "get_flow_pattern": READ,
            },
            "credit-core-mcp": {
                "get_facility": READ,
                "get_collateral": READ,
                # 担保台账取证需要穿透关联主体，取的是原文用于登记证据，仍为只读
                "get_exposure": READ,
                "get_guarantee_ledger": READ,
            },
        },
        "skills": ["CreditReportProbe", "LitigationProbe", "TxnFlowAnalyze",
                   "GuaranteeProbe", "EvidenceLedger"],
        "llm": True,
        "model_profile": "worker-light",
        "pii_access": True,
        "capabilities": "调取征信报告、裁判文书、工商登记、交易流水、押品状态的原文，"
                        "做结构化抽取并登记证据与证据等级",
        "notes": "不得给出风险定性结论；不得提出处置方案；不得写信贷核心",
        "decision_boundary": "全自主（只读 + 写证据账本）。PII 出站强制经 Higress 脱敏——"
                             "因取证触点唯一，脱敏范围可收敛、可验证",
        "trace": "每次外部调用落 MCP Span（工具名、参数摘要、耗时、成功/失败）；"
                 "每条证据落 evidence.write Span 并关联 evidence_id",
    },
    "risk-analyst": {
        "role": "风险根因定位与定性 · 零工具权限",
        "equivalence_class": "零工具纯推理（求证风险成立）",
        "runtime": "hermes",
        # 刻意剥夺全部取证工具：防止「边查边下结论」的确认偏差
        "mcp": {},
        "skills": ["RiskRootCause", "QueryRewrite", "PolicyRag", "CaseMemory"],
        "llm": True,
        # 与 devils-advocate 刻意分属不同模型族：拆成两个 Agent 是为了避免自洽坍缩，
        # 若共用同一批权重，坍缩只是从上下文层面搬到了权重层面。由回归测试断言。
        "model_profile": "analyst-primary",
        "pii_access": False,
        "capabilities": "基于已登记证据做根因归因，给出五级分类建议、置信度与证据引用",
        "notes": "不得自行取证（防「边查边下结论」的确认偏差）；不得执行处置",
        "decision_boundary": "只有提议权，无执行权。证据不足时必须输出「证据缺口清单」"
                             "而非推测结论",
        "trace": "落 llm Span（prompt 版本、token、耗时）+ assertion Span（结论与证据引用），"
                 "供事后复核归因链",
    },
    "devils-advocate": {
        "role": "对抗质疑 · 目标函数与定性官对立",
        "equivalence_class": "零工具纯推理（求证风险不成立）",
        "runtime": "hermes",
        # 与 risk-analyst 权限完全相同——拆分理由是目标函数对立，不是权限差异
        "mcp": {},
        "skills": ["QueryRewrite", "PolicyRag", "CaseMemory"],
        "llm": True,
        # 与 risk-analyst 不同族——这是异构对抗，不是随手换个模型
        "model_profile": "advocate-primary",
        "pii_access": False,
        "capabilities": "提出反证与替代解释、指出证据不足与逻辑跳跃、质疑证据时效性与代表性；"
                        "反驳未成立也须记录尝试过程",
        "notes": "不得提出处置方案（避免既当质疑者又当决策者）；不得自行取证",
        "decision_boundary": "对自动执行拥有一票阻断权：一旦判定「证据不足」，"
                             "Case 强制回退 EVIDENCE 补证，不得进入 DISPOSITION",
        "trace": "落独立 llm Span 与 rebuttal Span；与定性方结论一并进入 adjudication Span，"
                 "分歧全程留痕",
    },
    "disposition-executor": {
        "role": "处置执行 · 唯一写触点",
        "equivalence_class": "唯一写触点",
        "runtime": "copaw",
        "mcp": {
            "credit-core-mcp": {
                "get_facility": READ,
                "adjust_limit": WRITE,
                "add_guarantee": WRITE,
                "rollback_adjustment": WRITE,
            },
        },
        "skills": ["SafeDisposition"],
        # 仅规则驱动，不做自由推理——执行环节不给模型发挥空间
        "llm": False,
        # 刻意为 None：唯一写触点不应有模型入口。绑定非空会被不变量校验直接拒绝
        "model_profile": None,
        "pii_access": False,
        "capabilities": "执行白名单内动作：打标关注、发起补充资料、额度压降、追加担保、"
                        "生成收贷方案",
        "notes": "不得自主决定是否执行；不得执行白名单外动作；不得审计自身结果",
        "decision_boundary": "零自主决策。L0/L1 凭裁决执行；L2 必须校验审批回调签名后执行；"
                             "L3 永不执行，仅落方案。所有动作幂等，失败自动回滚至 rollback_point",
        "trace": "落 execution Span（含幂等键、动作前后快照、回执）；"
                 "写操作强制双写审计日志，TraceId 关联",
    },
    "compliance-auditor": {
        "role": "结果核验、合规审计与经验沉淀",
        "equivalence_class": "唯一 Trace 读 + 知识库写",
        "runtime": "openclaw",
        "mcp": {
            "credit-core-mcp": {
                "get_facility": READ,
                "get_exposure": READ,
            },
        },
        "skills": ["ComplianceCheck", "PostmortemDistill", "ReportCompose",
                   "QueryRewrite", "PolicyRag", "CaseMemory"],
        "llm": True,
        # 全系统唯一允许失败降级为模板成文的档位——它不影响决策
        "model_profile": "auditor-narrate",
        "pii_access": False,
        "capabilities": "核验处置是否真实生效、逐条校验监管合规项、生成审计报告、"
                        "把确认的风险模式沉淀为 RiskPattern 与向量案例",
        "notes": "不得执行或修改任何处置动作；发现合规缺失只报告不修复，由 Commander 升级人工",
        "decision_boundary": "全自主（只读业务系统 + 写知识库）",
        "trace": "读取全链路 Trace 生成审计视图；沉淀动作落 knowledge.write Span，"
                 "可追溯「哪次案件产生了哪条规则」",
    },
}

# 6 个 Worker（Team Leader 不计入 Worker 数）
WORKERS = [k for k in PERMISSIONS if k != "risk-commander"]
TEAM_LEADER = "risk-commander"


class PermissionDenied(Exception):
    """越权调用。错误信息包含调用方与目标，便于审计定位。"""


def is_allowed(caller: str, server: str, tool: str) -> bool:
    """判断某 Agent 是否有权调用指定 MCP 工具。不在表内即无权。"""
    entry = PERMISSIONS.get(caller)
    if entry is None:
        return False
    return tool in entry["mcp"].get(server, {})


def check(caller: str, server: str, tool: str) -> None:
    """校验并在越权时抛错。这是运行时的执行点。"""
    if not is_allowed(caller, server, tool):
        raise PermissionDenied(
            f"{caller} 无权调用 {server}.{tool}（权限矩阵未授予）"
        )


def writers_of(server: str) -> list[str]:
    """列出对某 Server 拥有写权限的 Agent。用于校验「唯一写触点」不变量。"""
    return [
        agent for agent, e in PERMISSIONS.items()
        if any(mode == WRITE for mode in e["mcp"].get(server, {}).values())
    ]


def pii_touchpoints() -> list[str]:
    """列出可接触 PII 的 Agent。用于校验「PII 触点唯一」不变量。"""
    return [a for a, e in PERMISSIONS.items() if e["pii_access"]]


def skill_callers(skill_name: str) -> tuple[str, ...]:
    """列出有权调用某 Skill 的 Agent，供 Skill 装饰器做权限校验。"""
    return tuple(a for a, e in PERMISSIONS.items() if skill_name in e["skills"])
