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
    if template_id == "取证任务清单":
        return _handoff(state, ledger, extra)
    raise ValueError(f"未知报告模板：{template_id}")


def _handoff(state: Any, ledger: EvidenceLedger, extra: dict[str, Any]) -> str:
    """转人工交接单。

    「转人工」不等于「甩给人」。一个负责任的系统在放弃自动处置时，
    至少要交代清楚四件事：**为什么停下来、已经查到了什么、还缺什么、
    每一项该找谁去补**。少了最后一条，接手的人得从头再来一遍。

    这份单子刻意**不给风险结论**——证据不足以定论时给结论就是编。
    它给的是工作交接，不是判断。
    """
    from .checklist import FACT_SOURCING     # 延迟导入避免循环依赖

    tasks = extra.get("tasks", [])
    comp = extra.get("compliance", {})
    degraded = extra.get("degradations", [])
    subj = state.subject
    ev = state.risk_event or {}
    suff = ledger.sufficiency()
    L: list[str] = []

    # 移交原因必须**取自实际发生的事**，不能套一句模板话。
    # 同样是转人工，「材料确实不够」与「质疑环节失效」是两码事：
    # 前者要人去补材料，后者要人去看系统。写错了会把人引向错误的方向。
    last = state.history[-1]["reason"] if state.history else ""
    if degraded:
        who = "、".join(sorted({d["caller"] for d in degraded}))
        reason = (f"**推理环节失效**（{who}），按失败即阻断处置。"
                  f"注意证据充分度为 {suff}，材料本身未必不足")
    elif suff < 0.7:
        reason = f"自动取证已达重试上限，证据充分度 {suff} 仍低于 0.7"
    else:
        reason = last or "回退次数用尽，转人工"

    L.append("# 取证任务清单 · 转人工交接单")
    L.append("")
    L.append("| 项目 | 内容 |")
    L.append("|---|---|")
    L.append(f"| 案件编号 | {state.case_id} |")
    L.append(f"| 客户名称 | {subj['name']}（{subj.get('subject_id', '—')}） |")
    L.append(f"| 当前分类 | {subj.get('current_grade', '—')} |")
    L.append(f"| 移交原因 | {reason} |")
    L.append(f"| 移交时状态 | 未定论（**系统未对本案作出任何风险结论**） |")
    L.append("")

    if degraded:
        L.append("> ⚠ **本次移交不是因为材料不够，而是系统自身的推理环节没能产出合规结论。**")
        # 每轮补证都会把同一个环节的失败重记一次。交接单上列四遍同样的话，
        # 读的人会以为出了四种毛病——按环节归并，改用次数表达
        seen: dict[tuple, dict] = {}
        for d in degraded:
            k = (d["caller"], d["task"])
            seen.setdefault(k, {**d, "times": 0})["times"] += 1
        for d in seen.values():
            rep = f"（累计 {d['times']} 次）" if d["times"] > 1 else ""
            L.append(f"> - {d['caller']} 的 {d['task']}{rep}：{d['reason']}　→ {d['fallback']}")
        L.append("> 请先由技术侧核查该环节，再判断是否需要补充材料。")
        L.append("")

    L.append("## 一、为什么停在这里")
    L.append("")
    back = [h for h in state.history if h["to"] == "EVIDENCE"]
    rounds = max(0, len(back) - 1)
    L.append(f"本案共回退补证 **{rounds}** 轮，"
             + ("每轮均因推理环节失效而未能完成裁决。" if degraded
                else "仍未达到进入裁决所需的证据充分度。"))
    L.append("")
    L.append("阶段轨迹：")
    L.append("")
    L.append("```")
    L.append(" → ".join([state.history[0]["from"]] + [h["to"] for h in state.history])
             if state.history else "（无迁移记录）")
    L.append("```")
    L.append("")
    for h in state.history:
        if h["to"] in ("EVIDENCE", "EVIDENCE_GAP"):
            L.append(f"- **{h['from']} → {h['to']}**　{h['reason']}")
    L.append("")
    L.append("> 系统在这里停下，是因为**证据不足时给结论就是编**。"
             "回退带重试上限，用尽即转人工——这是一条有界且失败即阻断的路径。")
    L.append("")

    L.append("## 二、已经查到了什么（不必重复劳动）")
    L.append("")
    collected = [e for e in ledger.all() if e.level != "缺失"]
    # 账本是 append-only 的，每轮补证都会把同一类材料重新登记一次——这在账本层
    # 是对的（保留了每一次取数的痕迹），但交接单上列出三份「征信报告」
    # 只会让接手的人以为真有三份。按类型归并，标注取了几次。
    grouped: dict[tuple, list] = {}
    for e in collected:
        grouped.setdefault((e.fact_type, e.source_system), []).append(e)
    L.append(f"已取得并登记 **{len(grouped)}** 类材料（账本内共 {len(collected)} 条记录，"
             f"含补证重取），证据充分度 **{suff}**（进入裁决需 ≥ 0.7）。")
    L.append("")
    L.append("| 材料 | 来源 | 等级 | 证据编号 |")
    L.append("|---|---|---|---|")
    for (ft, src), items in grouped.items():
        label = (FACT_SOURCING.get(ft) or {}).get("label") or ft
        ids = "".join(f"[{e.evidence_id}]" for e in items[:3])
        more = f" 等 {len(items)} 次" if len(items) > 3 else ""
        L.append(f"| {label} | {src} | {items[-1].level} | {ids}{more} |")
    L.append("")
    if ev.get("signal_types"):
        L.append(f"触发本案的信号类型：{'、'.join(ev['signal_types'])}。")
        L.append("")

    L.append("## 三、还缺什么，找谁补")
    L.append("")
    if not tasks:
        L.append("（无显式缺口登记，请人工复核证据充分度口径）")
    else:
        auto = [t for t in tasks if t["automatable"]]
        manual = [t for t in tasks if not t["automatable"]]
        L.append(f"共 **{len(tasks)}** 项待补，其中 **{len(auto)}** 项可由系统重取、"
                 f"**{len(manual)}** 项必须人工获取。")
        L.append("")
        L.append("| # | 缺失材料 | 去哪里取 | 授权要求 | 责任岗位 | 建议动作 |")
        L.append("|---|---|---|---|---|---|")
        for i, t in enumerate(tasks, 1):
            flag = "" if t["automatable"] else " ⚠"
            L.append(f"| {i}{flag} | {t['label']} | {t['system']} | {t['authorization']} "
                     f"| {t['owner']} | {t['action']} |")
        L.append("")
        for t in tasks:
            if t.get("why"):
                L.append(f"- **{t['label']}**：{t['why']}")
        L.append("")
        if manual:
            L.append(f"> ⚠ 标记的 {len(manual)} 项**不在任何可查系统内**，"
                     f"系统重试多少次都拿不到，只能由 "
                     f"{'、'.join(sorted({t['owner'] for t in manual}))} 去获取。"
                     f"这正是本案必须转人工的原因。")
            L.append("")

    L.append("## 四、补齐之后怎么办")
    L.append("")
    L.append("1. 按上表补齐材料，在系统内登记为证据；")
    L.append("2. 证据充分度达到 0.7 以上后，案件可重新进入自动处置流程；")
    L.append("3. 若确认材料无法取得，请在系统内标注原因并按人工流程出具处置意见——"
             "**此时不得引用本单子作为风险结论**，它只说明系统未能定论。")
    L.append("")

    if comp.get("items"):
        L.append("## 五、本次流程的合规留痕")
        L.append("")
        L.append("| 结果 | 合规项 | 说明 |")
        L.append("|---|---|---|")
        for it in comp["items"]:
            mark = {"PASS": "通过", "FAIL": "**未通过**"}.get(it["result"], "不适用")
            L.append(f"| {mark} | {it['rule_id']} {it['source']} | {it['detail']} |")
        L.append("")
        L.append("> 「我们看过、没能定论、于是转人工」这个过程本身也要可举证——"
                 "否则事后无法区分「查过但材料不足」与「根本没查」。")
        L.append("")

    L.append("---")
    L.append("")
    L.append(f"本单由 compliance-auditor 生成于案件移交时点；"
             f"全过程轨迹见 `trace.json`，取数记录见 `mcp_audit.jsonl`。")
    return "\n".join(L)


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
