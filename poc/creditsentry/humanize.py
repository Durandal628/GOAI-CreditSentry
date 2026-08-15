"""把结构化证据翻译成人话。

证据账本里存的是**机器要的形状**：字段名、比值、布尔量、快照 URI。
这份形状对下游推理是对的——模型要的是抽取字段，不是散文。但直接端到界面上，
一个风险经理看到的就是一堆 ``amount_ratio: 0.155`` 和 ``s3://...``，
他无法在三秒内判断「这条证据说明了什么」。

因此增加一层**呈现翻译**，而不是改账本的存储形状。三条约定：

1. **只翻译，不加工。** 摘要里出现的每一个数都必须能在 ``extracted`` 里找到出处，
   不做任何推断、汇总或修辞。翻译层一旦开始「总结」，界面上就会出现账本里没有的结论。
2. **内部字段不外泄。** ``source_doc_uri`` / ``_redacted_fields`` / ``subject_id``
   这些是系统内部锚点，对看的人没有意义，收进详情而不是摘要。
3. **缺失也要说清楚。** 「无对外担保记录」和「没查到」是两回事，前者是结论，
   后者是缺口——这个区别在界面上必须保留。

这一层刻意放在后端：字段语义属于领域知识，散落到前端 JS 里就会和账本漂移。
"""

from __future__ import annotations

import json
from typing import Any, Callable

# 事实类型 → 人能看懂的名字。界面上不出现英文字段名。
FACT_LABELS: dict[str, str] = {
    "credit_report": "征信报告",
    "credit_diff": "征信期间变动",
    "litigation_case": "涉诉案件",
    "registration_change": "工商登记变更",
    "flow_pattern": "资金流水模式",
    "guarantee_ledger": "对外担保台账",
    "guarantee_entry": "担保台账明细",
    "financial_statement": "财务报表",
    "policy_clause": "政策条款",
    "case_memory": "历史案例",
    "collateral": "抵质押物",
    "facility": "授信额度",
    "exposure": "敞口测绘",
}

SOURCE_LABELS: dict[str, str] = {
    "bureau-mcp": "人行征信系统",
    "judicial-mcp": "司法与工商公开数据",
    "txn-mcp": "行内账务流水",
    "credit-core-mcp": "信贷核心系统",
    "policy-kb": "行内制度知识库",
    "case-kb": "历史案例库",
    "-": "—",
}


def money(n: Any) -> str:
    """金额按中文习惯分档。界面上不出现 ``11800000`` 这种数字。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if abs(n) >= 1e8:
        return f"{n / 1e8:.2f} 亿元"
    if abs(n) >= 1e4:
        return f"{n / 1e4:,.0f} 万元"
    return f"{n:,.0f} 元"


def _pct(x: Any) -> str:
    try:
        return f"{float(x) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


# ---------------------------------------------------------------------------
# 逐类型的翻译器
# ---------------------------------------------------------------------------
# 每个翻译器返回 (一句话摘要, [(字段名, 值, 是否重点)])。
# 「是否重点」决定界面上是否高亮——高亮的是**驱动了判断的那几个字段**，
# 不是「看起来重要的字段」。

def _credit_report(e: dict[str, Any]) -> tuple[str, list]:
    parts = [f"负债合计 {money(e.get('total_liability'))}"]
    if e.get("overdue_records"):
        parts.append(f"逾期 {e['overdue_records']} 笔")
    if e.get("external_guarantee"):
        parts.append(f"对外担保 {money(e['external_guarantee'])}")
    if e.get("watch_flag"):
        parts.append("已被标记关注")
    return (
        f"截至 {e.get('report_date', '—')} 的征信报告：" + "、".join(parts) + "。",
        [("报告日期", e.get("report_date"), False),
         ("负债合计", money(e.get("total_liability")), True),
         ("逾期记录", f"{e.get('overdue_records', 0)} 笔", bool(e.get("overdue_records"))),
         ("对外担保余额", money(e.get("external_guarantee")), True),
         ("关注类标记", "有" if e.get("watch_flag") else "无", bool(e.get("watch_flag"))),
         ("已脱敏字段", "、".join(e.get("_redacted_fields") or []) or "无", False)],
    )


def _credit_diff(e: dict[str, Any]) -> tuple[str, list]:
    ch = []
    if e.get("new_liability"):
        ch.append(f"新增负债 {money(e['new_liability'])}")
    if e.get("new_overdue"):
        ch.append(f"新增逾期 {e['new_overdue']} 笔")
    if e.get("external_guarantee_delta"):
        ch.append(f"对外担保增加 {money(e['external_guarantee_delta'])}")
    if e.get("query_count_delta"):
        ch.append(f"被查询次数增加 {e['query_count_delta']} 次")
    return (
        f"相比 {e.get('baseline_date', '—')}，本期征信发生变动："
        + ("、".join(ch) if ch else "无实质变动") + "。",
        [("比对基准日", e.get("baseline_date"), False),
         ("本期报告日", e.get("report_date"), False),
         ("新增负债", money(e.get("new_liability")), True),
         ("新增逾期", f"{e.get('new_overdue', 0)} 笔", bool(e.get("new_overdue"))),
         ("对外担保变动", money(e.get("external_guarantee_delta")), True),
         ("被查询次数变动", f"+{e.get('query_count_delta', 0)} 次",
          (e.get("query_count_delta") or 0) >= 5)],
    )


def _litigation(e: dict[str, Any]) -> tuple[str, list]:
    status = "已结案" if e.get("closed") else "未结案"
    role = e.get("our_role") or "—"
    return (
        f"{e.get('filed_at', '—')} 立案的{e.get('cause', '案件')}，"
        f"标的 {money(e.get('amount'))}（占本行敞口 {_pct(e.get('amount_ratio'))}），"
        f"{status}，我方为{role}。",
        [("案号", e.get("case_no"), False),
         ("案由", e.get("cause"), False),
         ("立案日期", e.get("filed_at"), False),
         ("标的金额", money(e.get("amount")), True),
         ("占敞口比重", _pct(e.get("amount_ratio")), True),
         ("结案状态", status, not e.get("closed")),
         ("我方诉讼地位", role, role == "被告")],
    )


def _registration(e: dict[str, Any]) -> tuple[str, list]:
    bits = []
    if e.get("legal_rep_changed"):
        bits.append("法定代表人变更")
    if e.get("equity_changed"):
        bits.append("股权结构变动")
    overlap = e.get("change_overlaps_risk_window")
    return (
        (f"{e.get('changed_at', '—')} 发生" + "、".join(bits or ["登记信息变更"])
         + ("，且变更时点落在风险事件窗口内。" if overlap else "，变更时点早于风险事件窗口。")),
        [("变更日期", e.get("changed_at"), False),
         ("变更内容", e.get("detail"), False),
         ("风险窗口", " ~ ".join(e.get("risk_window") or []) or "—", False),
         ("与风险窗口重合", "是" if overlap else "否", bool(overlap)),
         ("同期股权变动", "是" if e.get("equity_changed") else "否",
          bool(e.get("equity_changed")))],
    )


def _flow(e: dict[str, Any]) -> tuple[str, list]:
    cps = e.get("counterparties") or []
    rel = [c for c in cps if c.get("related_party")]
    anomalies = e.get("anomalies") or []
    top = ("，最大对手方为「" + rel[0]["name"] + f"」（{rel[0].get('note', '')}），"
           f"金额 {money(rel[0].get('amount'))}") if rel else ""
    return (
        (f"流水识别到 {len(anomalies)} 类异常模式（{'、'.join(anomalies) or '无'}）；"
         f"共 {len(cps)} 个对手方，其中 {len(rel)} 个经穿透为关联方{top}。"),
        [("异常模式", "、".join(anomalies) or "无", bool(anomalies)),
         ("对手方总数", f"{len(cps)} 个", False),
         ("其中关联方", f"{len(rel)} 个", bool(rel)),
         ("历史波动区间", e.get("baseline_band"), False),
         ("流水采样覆盖率", _pct(e.get("coverage")),
          (e.get("coverage") or 1) < 0.9)],
    )


def _guarantee(e: dict[str, Any]) -> tuple[str, list]:
    entries = e.get("entries") or []
    if not entries:
        # 「查了，没有」与「没查到」必须区分开——前者是结论，后者是缺口
        why = e.get("empty_reason") or "无对外担保记录"
        return (why, [("查询结果", "无记录", False), ("说明", why, False)])
    total = sum(float(x.get("amount") or 0) for x in entries)
    distressed = [x for x in entries if x.get("distressed")]
    return (
        f"对外担保 {len(entries)} 笔，合计 {money(total)}；"
        f"其中被担保方已出险 {len(distressed)} 笔。",
        [("担保笔数", f"{len(entries)} 笔", False),
         ("担保余额合计", money(total), True),
         ("被担保方已出险", f"{len(distressed)} 笔", bool(distressed))]
        + [(f"· {x.get('party')}", money(x.get("amount")), bool(x.get("distressed")))
           for x in entries[:6]],
    )


def _policy(e: dict[str, Any]) -> tuple[str, list]:
    dims = e.get("matched_dimensions") or []
    return (
        f"{e.get('title', '条款')}——出自{e.get('source', '—')}，"
        f"{e.get('effective_date', '—')} 起生效。",
        [("条款标题", e.get("title"), True),
         ("出处", e.get("source"), False),
         ("生效日期", e.get("effective_date"), False),
         ("命中的改写维度", "、".join(dims) or "—", False)],
    )


def _gap(e: dict[str, Any]) -> tuple[str, list]:
    return (f"未取得该项材料：{e.get('why', '原因未记录')}",
            [("缺口原因", e.get("why"), True)])


_TRANSLATORS: dict[str, Callable[[dict], tuple[str, list]]] = {
    "credit_report": _credit_report,
    "credit_diff": _credit_diff,
    "litigation_case": _litigation,
    "registration_change": _registration,
    "flow_pattern": _flow,
    "guarantee_ledger": _guarantee,
    "guarantee_entry": _guarantee,
    "policy_clause": _policy,
}

#: 详情里不展示的内部字段。它们是系统锚点，对看的人没有意义。
_INTERNAL = {"source_doc_uri", "_redacted_fields", "subject_id", "gap",
             "ambiguous", "partial", "undersampled"}


def summarize_evidence(ev: dict[str, Any]) -> dict[str, Any]:
    """把一条证据翻译成 {摘要, 关键字段, 其余字段}。"""
    extracted = ev.get("extracted") or {}
    if extracted.get("gap"):
        headline, facts = _gap(extracted)
    else:
        fn = _TRANSLATORS.get(ev.get("fact_type", ""))
        if fn is None:
            # 没有专门翻译器时退化为字段罗列，但仍滤掉内部字段。
            # 宁可朴素，也不要在界面上出现 s3:// 这种东西
            headline = f"{FACT_LABELS.get(ev.get('fact_type', ''), ev.get('fact_type'))}记录"
            facts = [(k, v, False) for k, v in extracted.items()
                     if k not in _INTERNAL and not isinstance(v, (dict, list))]
        else:
            headline, facts = fn(extracted)

    return {
        "fact_label": FACT_LABELS.get(ev.get("fact_type", ""), ev.get("fact_type")),
        "source_label": SOURCE_LABELS.get(ev.get("source_system", ""), ev.get("source_system")),
        "headline": headline,
        "facts": [{"k": k, "v": ("—" if v is None else str(v)), "hot": bool(hot)}
                  for k, v, hot in facts if v is not None or True],
        # 原始快照仍然给，但收进详情的最底部——它是举证锚点，不是阅读材料
        "snapshot_uri": extracted.get("source_doc_uri") or ev.get("snapshot_uri"),
    }


# ---------------------------------------------------------------------------
# 原文快照的呈现
# ---------------------------------------------------------------------------
# 账本里存的是 ``s3://.../EV-001-0003.snapshot`` 这样的地址。给系统看没问题，
# 给人看就是一句「你自己去查」。复核的人要的是**当场翻到那份材料**，
# 所以这里把快照渲染成一份看起来像文件的文件。

#: 各类原文的标题与副标题。文件得有个抬头，不然还是一坨数据。
SNAPSHOT_TITLES: dict[str, tuple[str, str]] = {
    "credit_report": ("个人/企业信用报告", "中国人民银行征信中心"),
    "credit_diff": ("信用报告期间变动比对表", "中国人民银行征信中心"),
    "litigation_case": ("民事判决书（节选）", "中国裁判文书网"),
    "registration_change": ("企业登记变更记录", "国家企业信用信息公示系统"),
    "flow_pattern": ("账户交易流水与资金模式分析", "行内账务系统"),
    "guarantee_ledger": ("对外担保台账", "行内信贷核心系统"),
    "guarantee_entry": ("对外担保台账明细", "行内信贷核心系统"),
    "policy_clause": ("制度条款原文", "行内制度知识库"),
    "financial_statement": ("材料缺失说明", "—"),
}

#: 原文里的字段名 → 中文。快照是给人看的，不该出现 total_liability 这种东西。
SNAPSHOT_FIELDS: dict[str, str] = {
    "subject_id": "主体编号", "report_date": "报告日期", "baseline_date": "比对基准日",
    "legal_rep_id_no": "法定代表人证件号", "contact_phone": "联系电话",
    "total_liability": "负债合计", "overdue_records": "逾期记录",
    "external_guarantee": "对外担保余额", "watch_flag": "关注类标记",
    "new_liability": "新增负债", "new_overdue": "新增逾期",
    "external_guarantee_delta": "对外担保变动", "query_count_delta": "被查询次数变动",
    "case_no": "案号", "cause": "案由", "amount": "标的金额",
    "amount_ratio": "占本行敞口比重", "closed": "结案状态", "our_role": "我方诉讼地位",
    "filed_at": "立案日期", "legal_rep_changed": "法定代表人变更",
    "changed_at": "变更日期", "risk_window": "风险事件窗口",
    "change_overlaps_risk_window": "与风险窗口重合", "equity_changed": "股权结构变动",
    "detail": "变更详情", "established_at": "成立日期",
    "registered_capital": "注册资本", "status": "登记状态",
    "anomalies": "识别到的异常模式", "coverage": "流水采样覆盖率",
    "baseline_band": "历史同期波动区间", "counterparties": "交易对手方",
    "transactions": "交易明细", "entries": "担保明细", "empty_reason": "查询结果说明",
    "title": "条款标题", "source": "出处", "effective_date": "生效日期",
    "text": "正文", "why": "说明", "name": "名称", "note": "备注",
    "related_party": "关联方", "party": "被担保方", "ts": "日期",
    "counterparty": "对手方", "summary": "摘要", "account_no": "账号",
    "counterparty_account": "对手方账号", "matched_dimensions": "命中的检索维度",
}

#: 不出现在快照正文里的内部字段。注意 ``pattern`` / ``summary`` 这类
#: **包装层**不在此列——它们要被展开，不是被丢掉。
_SNAPSHOT_HIDE = {"source_doc_uri", "_redacted_fields", "ambiguous", "partial",
                  "undersampled", "gap", "next_cursor", "total_count"}


def _fmt(key: str, v: Any) -> str:
    if isinstance(v, bool):
        return {"closed": ("已结案", "未结案"), "watch_flag": ("有", "无"),
                "legal_rep_changed": ("是", "否"), "equity_changed": ("是", "否"),
                "change_overlaps_risk_window": ("是", "否"),
                "related_party": ("是", "否")}.get(key, ("是", "否"))[0 if v else 1]
    if key in ("amount", "total_liability", "external_guarantee", "new_liability",
               "external_guarantee_delta", "registered_capital"):
        return money(v)
    if key in ("amount_ratio", "coverage"):
        return _pct(v)
    if isinstance(v, list):
        return "、".join(str(x) for x in v)
    return str(v)


#: 同名字段在不同语境下含义不同。``amount`` 在涉诉里是「标的金额」，
#: 在流水与担保台账里就是「金额」——按父字段覆盖，避免张冠李戴。
_FIELD_IN_CONTEXT: dict[str, dict[str, str]] = {
    "counterparties": {"amount": "往来金额"},
    "transactions": {"amount": "交易金额"},
    "entries": {"amount": "担保余额"},
}


def _label(key: str, parent: str = "") -> str:
    return (_FIELD_IN_CONTEXT.get(parent, {}).get(key)
            or SNAPSHOT_FIELDS.get(key, key))


def _walk(node: dict[str, Any], rows: list, tables: list, depth: int) -> None:
    """把嵌套的原文摊平成「字段清单 + 若干张表」。

    深度设了上限：快照是给人扫一眼的，嵌套三层以上的结构摊开来只会更难读，
    那种情况本来就该在 MCP 侧先整形。
    """
    if depth > 2:
        return
    for k, v in node.items():
        if k in _SNAPSHOT_HIDE:
            continue
        if isinstance(v, list) and v and isinstance(v[0], dict):
            cols = [c for c in v[0] if c not in _SNAPSHOT_HIDE]
            tables.append({
                "caption": _label(k),
                "cols": [_label(c, k) for c in cols],
                # 明细最多列 30 行，再多就不是「看一眼原文」而是导数据了
                "rows": [[_fmt(c, r.get(c)) for c in cols] for r in v[:30]],
                "total": len(v),
            })
        elif isinstance(v, dict):
            _walk(v, rows, tables, depth + 1)
        elif v is not None and v != []:
            label = _label(k)
            # 同一个字段可能在包装结构里出现两次（如 pattern 与 summary 都带
            # coverage）。摊平后重复显示只会让人以为是两份数据
            if not any(r[0] == label for r in rows):
                rows.append([label, _fmt(k, v)])


def render_snapshot(ev: dict[str, Any], raw: str, hash_ok: bool) -> dict[str, Any]:
    """把原文快照渲染成一份可直接阅读的「文件」。

    三种形态：纯文本正文（裁判文书）、字段清单（征信报告、工商登记）、
    表格（流水明细、担保台账）。判断依据是原文本身的结构，不是事实类型——
    同一类事实在不同来源下形态可能不同。
    """
    ft = ev.get("fact_type", "")
    title, issuer = SNAPSHOT_TITLES.get(ft, ("原文快照", ev.get("source_system", "—")))

    body: dict[str, Any] = {"kind": "text", "text": raw}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        # 不是 JSON，就是一段正文（裁判文书节选就属于这种）
        return {"title": title, "issuer": issuer, "hash_ok": hash_ok,
                "uri": ev.get("snapshot_uri"), "hash": ev.get("content_hash"),
                "body": body,
                "redacted": (ev.get("extracted") or {}).get("_redacted_fields") or []}

    if isinstance(data, dict):
        rows: list[list[str]] = []
        tables: list[dict[str, Any]] = []
        _walk(data, rows, tables, depth=0)
        body = {"kind": "doc", "rows": rows, "tables": tables}

    return {"title": title, "issuer": issuer, "hash_ok": hash_ok,
            "uri": ev.get("snapshot_uri"), "hash": ev.get("content_hash"),
            "body": body,
            "redacted": (ev.get("extracted") or {}).get("_redacted_fields") or []}


def summarize_pattern(distilled: dict[str, Any]) -> dict[str, Any] | None:
    """把沉淀下来的风险模式翻译成可读条目。"""
    rp = (distilled or {}).get("risk_pattern")
    if not rp:
        return None
    return {
        "id": rp.get("pattern_id"),
        "title": rp.get("description"),
        "rows": [
            ("触发条件", rp.get("trigger")),
            ("要点", rp.get("key_points")),
            ("反例", rp.get("counter_example")),
            ("适用边界", rp.get("boundary")),
        ],
        "written_to": (distilled or {}).get("written_to"),
    }
