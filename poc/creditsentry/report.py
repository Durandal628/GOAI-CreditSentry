"""报告成文引擎。

解决痛点三「报告依赖人工撰写、标准不统一、经验难沉淀复用」：
模板固化口径，证据引用自动注入正文。

**每一处结论都带 ``[EV-xxxx-xxxx]`` 角标**，ReportCompose 在返回前会逐个校验这些
编号在账本中确实存在——报告里出现无源结论会被直接拒绝生成，而不是打印出来了事。
"""

from __future__ import annotations

from typing import Any

from .ledger import EvidenceLedger


def _cite(evidence_ids: list[str]) -> str:
    return "".join(f"[{e}]" for e in evidence_ids) if evidence_ids else ""


def _money(v: Any) -> str:
    try:
        return f"{float(v):,.0f} 元"
    except (TypeError, ValueError):
        return "—"


def render(template_id: str, state: Any, ledger: EvidenceLedger, extra: dict[str, Any]) -> str:
    if template_id == "处置意见书":
        return _opinion(state, ledger, extra)
    if template_id == "审计报告":
        return _audit(state, ledger, extra)
    raise ValueError(f"未知报告模板：{template_id}")


def _opinion(state: Any, ledger: EvidenceLedger, extra: dict[str, Any]) -> str:
    subj = state.subject
    adj = state.adjudication or {}
    asrt = state.assertion or {}
    reb = state.rebuttal or {}
    exposure = state.exposure or {}
    L: list[str] = []

    L.append(f"# 风险处置意见书")
    L.append("")
    L.append(f"| 项目 | 内容 |")
    L.append(f"|---|---|")
    L.append(f"| 案件编号 | {state.case_id} |")
    L.append(f"| 客户名称 | {subj['name']}（{subj['subject_id']}） |")
    L.append(f"| 所属行业 | {subj.get('industry', '—')} · {subj.get('region', '—')} |")
    L.append(f"| 当前分类 | {subj.get('current_grade', '—')} |")
    L.append(f"| 当前敞口 | {_money(exposure.get('total_exposure'))} |")
    L.append(f"| 担保圈规模 | {len(exposure.get('guarantee_ring', []))} 户，"
             f"传染敞口 {_money(exposure.get('contagion_amount'))} |")
    L.append("")

    L.append("## 一、风险信号")
    ev = state.risk_event or {}
    L.append(f"本次共接收预警信号 {ev.get('input_count', 0)} 条，经归并降噪后保留 "
             f"{len(ev.get('kept', []))} 条，降噪率 {ev.get('denoise_rate', 0):.1%}。"
             f"有效信号类型：{'、'.join(ev.get('signal_types', [])) or '无'}。")
    if ev.get("dropped"):
        L.append("")
        L.append("被归并或压降的信号（全部可回溯）：")
        L.append("")
        L.append("| 信号 | 来源 | 处理原因 |")
        L.append("|---|---|---|")
        for d in ev["dropped"]:
            L.append(f"| {d['detail']} | {d['source']} | {d['drop_reason']} |")
    L.append("")

    L.append("## 二、定性结论与依据")
    L.append(f"**定性方结论**：{asrt.get('summary', '—')}")
    L.append("")
    if asrt.get("root_causes"):
        L.append("| 根因候选 | 置信度 | 依据 | 证据引用 |")
        L.append("|---|---|---|---|")
        for c in asrt["root_causes"]:
            L.append(f"| {c['type']} | {c['confidence']} | {c['rationale']} | "
                     f"{_cite(c.get('evidence_ids', []))} |")
    L.append("")

    L.append("## 三、对抗质疑结论")
    L.append(f"**质疑方结论**：{reb.get('summary', '—')}（verdict = `{reb.get('verdict')}`）")
    L.append("")
    if reb.get("rebuttals"):
        L.append("成立的反驳：")
        L.append("")
        for r in reb["rebuttals"]:
            L.append(f"- 针对「{r['target']}」：{r['argument']} {_cite(r.get('counter_evidence_ids', []))}")
        L.append("")
    if reb.get("attempted_but_failed"):
        L.append("已尝试但未能成立的反驳（记录在案以支撑处置结论）：")
        L.append("")
        for a in reb["attempted_but_failed"]:
            L.append(f"- 针对「{a['target']}」：{a['tried']} —— 未成立，原因：{a['failed_because']}")
        L.append("")
    if reb.get("evidence_gaps"):
        L.append("证据不足项：")
        L.append("")
        for g in reb["evidence_gaps"]:
            L.append(f"- {g}")
        L.append("")

    L.append("## 四、裁决")
    L.append(f"- **裁决结论**：`{adj.get('verdict')}`")
    L.append(f"- **裁决依据**：{adj.get('basis')}")
    L.append(f"- **裁决规则**：证据等级优先于置信度")
    L.append(f"- **建议分类**：{adj.get('final_grade') or '维持原分类'}")
    L.append("")

    L.append("## 五、处置方案与执行闸门")
    L.append("")
    L.append("| 动作 | 层级 | 可逆 | 需审批 | 回滚手段 | 定级规则 | 判定理由 |")
    L.append("|---|---|---|---|---|---|---|")
    for a in extra.get("actions", []):
        L.append(f"| {a['label']} | **{a['action_tier']}** | "
                 f"{'是' if a.get('reversible') else '否'} | "
                 f"{'是' if a.get('needs_approval') else '否'} | "
                 f"{a.get('rollback_point') or '—'} | `{a.get('rule_id')}` | {a.get('reason')} |")
    L.append("")

    L.append("## 六、证据清单")
    L.append("")
    L.append(f"共登记证据 {len(ledger.all())} 条，证据充分度 **{ledger.sufficiency()}**。")
    L.append("")
    L.append("| 证据编号 | 来源 | 类型 | 等级 | 定级理由 | 内容哈希 |")
    L.append("|---|---|---|---|---|---|")
    for e in ledger.all():
        L.append(f"| {e.evidence_id} | {e.source_system} | {e.fact_type} | "
                 f"{e.level} | {e.level_reason} | `{e.content_hash[:22]}…` |")
    L.append("")
    L.append("> 本意见书由信衡 CreditSentry 自动生成。所有结论均挂载可溯源证据引用，"
             "证据原文快照存于证据账本，账本 append-only 不可篡改。")
    return "\n".join(L)


def _audit(state: Any, ledger: EvidenceLedger, extra: dict[str, Any]) -> str:
    comp = extra.get("compliance", {})
    dist = extra.get("distilled", {})
    L: list[str] = []

    L.append("# 贷后风险处置审计报告")
    L.append("")
    L.append(f"| 项目 | 内容 |")
    L.append(f"|---|---|")
    L.append(f"| 案件编号 | {state.case_id} |")
    L.append(f"| 客户名称 | {state.subject['name']} |")
    L.append(f"| 审计执行方 | compliance-auditor（与执行方职责分离） |")
    L.append(f"| 合规项通过 | {comp.get('passed', 0)} / {len(comp.get('items', []))} |")
    L.append("")

    L.append("## 一、处置结果核验")
    ex = state.execution
    if ex:
        L.append("")
        L.append("| 动作 | 执行状态 | 审计流水号 | 回滚点 |")
        L.append("|---|---|---|---|")
        for r in ex.get("results", []):
            L.append(f"| {r.get('label', r['action'])} | {r['status']} | "
                     f"{r.get('audit_serial', '—')} | {r.get('rollback_point_id', '—')} |")
    else:
        L.append("")
        L.append("本次未产生处置动作（裁决未确认风险或降级为只读）。")
    L.append("")

    L.append("## 二、合规项逐条核查")
    L.append("")
    L.append("| 规则 | 条款出处 | 结论 | 核查详情 |")
    L.append("|---|---|---|---|")
    for i in comp.get("items", []):
        mark = {"PASS": "通过", "FAIL": "**未通过**", "N/A": "不适用"}[i["result"]]
        L.append(f"| `{i['rule_id']}` | {i['source']} | {mark} | {i['detail']} |")
    L.append("")
    if comp.get("remediation"):
        L.append("**整改建议**（审计方只报告不修复，由 RiskCommander 升级人工处理）：")
        L.append("")
        for r in comp["remediation"]:
            L.append(f"- {r}")
        L.append("")

    L.append("## 三、证据链完备性")
    total = len(ledger.all())
    strong = len(ledger.by_level("强"))
    weak = len(ledger.by_level("弱"))
    missing = len(ledger.by_level("缺失"))
    L.append("")
    L.append(f"- 证据总数 **{total}** 条：强 {strong} / 弱 {weak} / 缺失 {missing}")
    L.append(f"- 证据充分度 **{ledger.sufficiency()}**")
    unsupported = [c["type"] for c in (state.assertion or {}).get("root_causes", [])
                   if not c.get("evidence_ids")]
    L.append(f"- 无证据结论数 **{len(unsupported)}**"
             f"{'（' + '、'.join(unsupported) + '）' if unsupported else '　— 全部结论均可举证'}")
    if state.evidence_gaps:
        L.append("")
        L.append("已登记的证据缺口：")
        L.append("")
        for g in state.evidence_gaps:
            L.append(f"- [{g['evidence_id']}] {g['fact_type']}：{g['why']}")
    L.append("")

    L.append("## 四、经验沉淀")
    rp = dist.get("risk_pattern", {})
    if rp:
        L.append("")
        L.append(f"- **模式编号**：`{rp.get('pattern_id')}`")
        L.append(f"- **模式描述**：{rp.get('description')}")
        L.append(f"- **触发条件**：{rp.get('trigger')}")
        L.append(f"- **判定要点**：{rp.get('key_points')}")
        L.append(f"- **反例说明**：{rp.get('counter_example')}")
        L.append(f"- **适用边界**：{rp.get('boundary')}")
        L.append("")
        L.append(f"已回流至：`{dist.get('written_to')}`")
    L.append("")

    L.append("## 五、阶段迁移轨迹")
    L.append("")
    L.append("| 从 | 到 | 迁移原因 |")
    L.append("|---|---|---|")
    for h in state.history:
        L.append(f"| {h['from']} | {h['to']} | {h['reason']} |")
    L.append("")
    L.append("> 本审计报告由 compliance-auditor 独立生成，该 Agent 无任何处置执行权限，"
             "满足《商业银行内部控制指引》不相容职务分离要求。")
    return "\n".join(L)
