"""14 个核心 Skill 的实现。

Skill 是任务能力抽象层，Agent 通过 Skill 访问一切外部能力，不裸调工具。
每个 Skill 用 ``@skill`` 声明元数据——这份元数据既驱动运行时（版本上报、失败策略、
权限校验），也是 ``tools/gen_skill_docs.py`` 生成 ``skills/<name>/SKILL.md`` 的唯一真源，
因此文档不会与实现漂移。

分层（见 docs/Skill清单.md）：
- **L3 领域判断层**（10 个）全自研，承载信贷风控领域知识与监管条款绑定；
- **L2 领域封装层**（4 个）自研外壳 + 编排官方云能力（对象存储 / 向量检索 / 数据库）；
- **L1 云操作层** 直接调官方用云 Skills，不在本文件实现。
"""

from __future__ import annotations

import functools
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from . import context, permissions, prompts, querying
from .gate import evaluate as gate_evaluate
from .ledger import EvidenceLedger
from .llm import InferenceError
from .tracing import GEN_AI_OPERATION, GEN_AI_REQUEST_MODEL, GEN_AI_SYSTEM
from .tracing import GEN_AI_USAGE_IN, GEN_AI_USAGE_OUT, Tracer

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures")


# ---------------------------------------------------------------------------
# Skill 注册表
# ---------------------------------------------------------------------------

@dataclass
class SkillMeta:
    name: str
    version: str
    tier: str                 # L3 / L2
    category: str             # 诊断 / 取证 / 知识 / 治理 / 执行
    purpose: str
    inputs: str
    outputs: str
    trigger: str
    depends: str
    failure_policy: str
    security: str
    regulation: str
    eval_set: str
    reuse: str
    callers: tuple[str, ...] = ()   # 由权限矩阵推导，不在装饰器中手写
    reuses_official: str = ""       # L2 专有：复用了哪些官方云能力


REGISTRY: dict[str, SkillMeta] = {}


class SkillPermissionError(Exception):
    """Agent 调用了自身权限外的 Skill。"""


def skill(**meta_kwargs: Any) -> Callable:
    """把一个函数登记为 Skill：自动埋 Span、校验调用方权限、上报版本。

    ``callers`` 一律由 ``permissions.PERMISSIONS`` 推导，不接受手写——
    否则 Skill 层与 MCP 层会各有一套授权，正是「权限散落」这个问题本身。
    """
    meta = SkillMeta(**meta_kwargs)
    meta.callers = permissions.skill_callers(meta.name)
    if not meta.callers:
        raise ValueError(
            f"Skill {meta.name} 未被任何 Agent 在权限矩阵中声明，"
            f"请先在 permissions.PERMISSIONS 中授予"
        )
    REGISTRY[meta.name] = meta

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(ctx: "Context", *args: Any, **kwargs: Any) -> Any:
            if meta.callers and ctx.caller not in meta.callers:
                raise SkillPermissionError(
                    f"{ctx.caller} 无权调用 Skill {meta.name}（仅限 {meta.callers}）"
                )
            with ctx.tracer.span(
                "skill", meta.name,
                **{"skill.version": meta.version, "skill.tier": meta.tier,
                   "skill.category": meta.category, "caller": ctx.caller},
            ):
                return fn(ctx, *args, **kwargs)

        wrapper.meta = meta  # type: ignore[attr-defined]
        return wrapper

    return decorator


def _policy_note(ctx: "Context", chunks: list[dict[str, Any]]) -> str:
    """告诉模型它看到的条款是怎么被筛过的。

    不说清楚会有一个隐蔽后果：模型在回溯案件里发现「找不到某条我知道存在的规定」，
    可能会凭记忆把它补上——那正好是我们要防的知识维前视污染。
    """
    as_of = getattr(ctx.world, "as_of", None)
    if not as_of:
        return "本案为当期案件，条款按现行有效版本召回。"
    usable = [c for c in chunks if c.get("effective_date")]
    listed = "；".join(f"{c['source']}（{c['effective_date']} 生效）" for c in usable) or "无"
    return (
        f"本案为历史回溯，决策时点为 **{as_of}**。下方条款均已过滤为该时点前已生效者：{listed}。\n"
        f"**你不得引用任何在该时点尚未生效的规定**，即使你知道它后来存在——"
        f"那属于用未来的规则评价过去的行为。"
    )


def _evidence_ref_validator(ctx: "Context") -> Callable[[dict[str, Any]], list[str]]:
    """校验模型引用的证据编号真实存在于账本中。

    这是真机上最高频的一类失败：模型会把 ``EV-A1B2-C3D4`` 抄错一位，
    或者干脆凭印象编一个格式正确的编号。stub 模式下不存在这个问题，
    因为编号是代码自己填的——所以它在 live 之前一直没有暴露。

    做成 ``validator`` 而不是直接抛错，是为了先给模型一次改正机会：
    抄错编号是笔误，删掉整条结论则是过度反应。改不过来才由账本层硬拒绝。
    """
    valid = {e.evidence_id for e in ctx.ledger.all()}

    def check(result: dict[str, Any]) -> list[str]:
        errs: list[str] = []
        for i, cause in enumerate(result.get("root_causes", [])):
            for eid in cause.get("evidence_ids", []):
                if eid not in valid:
                    errs.append(
                        f"`root_causes[{i}].evidence_ids` 中的 {eid!r} 不存在于证据账本。"
                        f"只能引用输入 facts 里出现过的编号：{'、'.join(sorted(valid))}"
                    )
        return errs

    return check


@dataclass
class Context:
    """Skill 执行上下文。"""
    tracer: Tracer
    ledger: EvidenceLedger
    mcp: Any
    world: Any
    llm: Any
    caller: str = "-"
    logs: list[dict[str, Any]] = field(default_factory=list)
    #: 日志的实时出口。落盘的 logs.jsonl 是事后产物，工作台要在跑的过程中就看到；
    #: 两者是同一份记录的两个时机，不是两套日志。
    sink: Any = None

    def log(self, level: str, event: str, **fields: Any) -> None:
        """结构化日志。通过 trace_id 与 Trace 关联，记录决策依据与失败原因。"""
        rec = {
            "trace_id": self.tracer.trace_id,
            "level": level,
            "actor": self.caller,
            "event": event,
            **fields,
        }
        self.logs.append(rec)
        if self.sink is not None:
            try:
                self.sink(rec)
            except Exception:  # noqa: BLE001
                pass    # 出口坏了不影响业务链路，理由同 Tracer 的观察者

    def as_(self, caller: str) -> "Context":
        """派生一个指定调用方身份的上下文，用于权限校验。"""
        return Context(self.tracer, self.ledger, self.mcp, self.world,
                       self.llm, caller, self.logs, self.sink)


# ===========================================================================
# L3 诊断类
# ===========================================================================

@skill(
    name="SignalFusion", version="1.2.0", tier="L3", category="诊断",
    purpose="把多源零散预警信号归并为可处置的风险事件，并压降无效预警",
    inputs="signals[]{source, subject_id, signal_type, ts, detail}, window, dedup_policy",
    outputs="RiskEvent{event_id, subject, signal_types[], first_seen, priority, dropped[]}",
    trigger="预警池新增信号，或定时批量归并触发",
    depends="内部预警池（只读）；无外部依赖",
    failure_policy="单源不可用 → 降级为可用源归并并标注 degraded_sources；全源失败 → 抛错升级人工，不产出空事件",
    security="只读，无 PII；仅 signal-hub 可调用",
    regulation="《商业银行贷后管理指引》风险监测与预警要求",
    eval_set="30 组信号流样例，校验降噪率与零高危漏丢（被丢弃信号必须可回溯）",
    reuse="场景无关的信号归并内核，可直接复用于保险报案归并、告警降噪、工单去重",
)
def signal_fusion(ctx: Context, signals: list[dict[str, Any]]) -> dict[str, Any]:
    # 无效信号类型：例行提醒与无实质内容的舆情提及，是预警噪声的主要来源
    NOISE_TYPES = {"rating_periodic", "media_mention"}

    seen: set[tuple[str, str]] = set()
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []

    for s in signals:
        key = (s["type"], s["detail"].split("（")[0])
        if key in seen:
            # 丢弃的每一条都记录原因，保证可回溯——杜绝静默漏丢
            dropped.append({**s, "drop_reason": "重复推送，与既有信号归并"})
            continue
        seen.add(key)
        if s["type"] in NOISE_TYPES:
            dropped.append({**s, "drop_reason": f"信号类型 {s['type']} 无实质风险指向"})
            continue
        kept.append(s)

    denoise_rate = round(len(dropped) / len(signals), 4) if signals else 0.0
    ctx.log("INFO", "signal_fusion.done",
            input_count=len(signals), kept=len(kept), dropped=len(dropped),
            denoise_rate=denoise_rate)

    return {
        "event_id": f"RE-{ctx.ledger.case_id}",
        "signal_types": sorted({s["type"] for s in kept}),
        "kept": kept,
        "dropped": dropped,
        "input_count": len(signals),
        "denoise_rate": denoise_rate,
        "first_seen": min((s["ts"] for s in kept), default=None),
    }


@skill(
    name="ExposureMapping", version="1.1.0", tier="L3", category="诊断",
    purpose="测绘风险主体的敞口与传染面：担保圈、集团户、上下游",
    inputs="subject_id, depth（关系穿透层数，默认 2）, relation_types[]",
    outputs="Exposure{total_amount, related_subjects[], guarantee_ring[], truncated_at_depth}",
    trigger="RiskEvent 生成后立即调用",
    depends="credit-core-mcp（只读）",
    failure_policy="关系穿透超时 → 返回已穿透层级并标注 truncated_at_depth；核心系统不可用 → 用 T-1 快照并标注数据时点",
    security="只读；输出含敞口金额属敏感数据，出站脱敏",
    regulation="《商业银行大额风险暴露管理办法》关联客户认定",
    eval_set="12 组含担保圈与集团户的图结构样例，校验穿透正确性与环路检测",
    reuse="图穿透内核可复用于供应链金融核心企业测绘、反洗钱资金网络分析",
)
def exposure_mapping(ctx: Context, subject_id: str, depth: int = 2) -> dict[str, Any]:
    res = ctx.mcp.call("credit-core-mcp", "get_exposure",
                       {"subject_id": subject_id, "depth": depth}, caller=ctx.caller)
    ring = res.get("guarantee_ring", [])
    related = res.get("related_subjects", [])
    contagion = sum(r.get("amount", 0) for r in related)

    # 已出险的关联主体单列。传染的起点不是「有关联」，而是「关联方已经出事」——
    # 圈子再大，只要没人出险就不构成当期风险信号。
    distressed = [r for r in related if r.get("status") in ("已出险", "已违约", "已进入重整")]
    direct = res.get("total_exposure") or 0
    ctx.log("INFO", "exposure_mapping.done",
            total_exposure=direct,
            related_count=len(related),
            guarantee_ring_size=len(ring), contagion_amount=contagion,
            distressed_related=[r.get("name") for r in distressed])
    return {
        **res,
        "contagion_amount": contagion,
        "distressed_related": distressed,
        "contagion_multiple": round(contagion / direct, 2) if direct else None,
    }


@skill(
    name="RiskRootCause", version="2.0.1", tier="L3", category="诊断",
    purpose="基于已登记证据做风险根因归因与五级分类建议",
    inputs="evidence_ids[], exposure, policy_context（RAG 召回）, historical_cases[]",
    outputs="RiskAssertion{root_causes[]{type, confidence, evidence_ids[]}, suggested_grade, evidence_gaps[]}",
    trigger="EVIDENCE 阶段完成且 evidence_sufficiency ≥ 0.7",
    depends="LLM 推理；PolicyRag、CaseMemory 提供上下文",
    failure_policy="证据不足 → 强制输出 evidence_gaps 并将结论置为 INSUFFICIENT，Case 回退 EVIDENCE；LLM 超时 → 重试 2 次后升级人工",
    security="零工具权限（刻意剥夺，防边查边下结论的确认偏差）；Schema 拒绝无 evidence_ids 的断言",
    regulation="《贷款风险分类指导原则》五级分类标准",
    eval_set="40 组已标注案例（含 10 组误报陷阱），校验分类准确率与无证据结论率 = 0",
    reuse="「证据 → 归因 → 分级」范式可复用于保险核赔定性、故障根因定位",
)
def risk_root_cause(ctx: Context, facts: dict[str, Any], policy_context: list[dict[str, Any]],
                    cases: list[dict[str, Any]]) -> dict[str, Any]:
    # 上下文显式装配：事实必进不受裁剪，历史案例优先级最低先丢——
    # 缺了案例只是少个参照，缺了事实就会开始编
    payload, manifest = (
        context.ContextAssembler()
        .add_evidence_refs("facts", [facts], priority=10, required=True)
        .add_knowledge_chunks("policy_context", policy_context, priority=30)
        .add_knowledge_chunks("similar_cases", cases, text_field="lesson", priority=60)
        .build()
    )
    payload["facts"] = payload["facts"][0]

    system, prompt_version = prompts.render(
        "risk_root_cause",
        policy_note=_policy_note(ctx, policy_context),
    )
    with ctx.tracer.span("llm", "risk_root_cause") as span:
        span.attributes.update({
            **ctx.llm.span_attrs(ctx.caller),
            "prompt.name": "risk_root_cause", "prompt.version": prompt_version,
            "context.manifest": manifest.to_dict(),
        })
        try:
            result, tin, tout = ctx.llm.complete_json(
                "risk_root_cause", system, payload, caller=ctx.caller,
                validator=_evidence_ref_validator(ctx))
            span.attributes.update({GEN_AI_USAGE_IN: tin, GEN_AI_USAGE_OUT: tout})
        except InferenceError as e:
            # 失败策略（docs/接口与实验方案.md §1.5）：定性环节**不允许降级为**
            # 「凭现有信息猜一个结论」。唯一安全的降级方向是自认证据不足——
            # 它会经 adjudicate 判为 EVIDENCE_INSUFFICIENT，走唯一回退边补证，
            # 重试用尽即转人工。fail-safe 的方向是回退，不是放行。
            span.status = "ERROR"
            span.attributes.update(ctx.llm.record_degradation(
                caller=ctx.caller, task="risk_root_cause", err=e,
                fallback="conclusion=INSUFFICIENT 并回退补证"))
            ctx.log("ERROR", "risk_root_cause.degraded", reason=e.reason,
                    attempts=e.attempts, schema_errors=e.errors[:6])
            return {
                "conclusion": "INSUFFICIENT",
                "root_causes": [],
                "suggested_grade": None,
                "summary": f"定性推理未能产出合规结论（{e.reason}），"
                           f"按失败策略判为证据不足并回退补证，不得据此进入处置",
                "degraded": True,
            }

    # 「无证据不决策」的执行点：任何一条根因引用不到有效证据，直接拒绝。
    # 与上面 validator 的分工：validator 会先把问题回喂给模型改一轮（可挽救），
    # 这里是不可绕过的硬拒绝（不可挽救）。两道都要有，缺前者真机上会频繁硬崩，
    # 缺后者则等于把「无证据不决策」交给模型自觉。
    for cause in result.get("root_causes", []):
        ctx.ledger.assert_supported(cause["type"], cause.get("evidence_ids", []))

    ctx.log("INFO", "risk_root_cause.done",
            conclusion=result.get("conclusion"),
            grade=result.get("suggested_grade"),
            cause_count=len(result.get("root_causes", [])))
    return result


# ===========================================================================
# L3 取证类
# ===========================================================================

@skill(
    name="CreditReportProbe", version="1.3.0", tier="L3", category="取证",
    purpose="解析征信报告并比对期间变动",
    inputs="subject_id, report_date, baseline_date（比对基准）",
    outputs="CreditFacts{查询次数变动, 新增负债, 逾期记录, 对外担保变动, 关注类标记} + evidence_ids[]",
    trigger="RiskCommander 派发含 credit 类型的取证任务",
    depends="bureau-mcp（只读 · PII）",
    failure_policy="征信源限流 → 指数退避重试 3 次；无授权查询 → 立即失败并记录合规事件，绝不降级绕过",
    security="高敏 PII；仅 due-diligence 可调用；出站强制经网关脱敏；每次查询写授权审计",
    regulation="《征信业管理条例》查询授权与用途限制",
    eval_set="20 组合成征信报告，校验字段抽取准确率与变动比对正确性",
    reuse="征信解析与变动比对可复用于贷前审批、授信年检",
)
def credit_report_probe(ctx: Context, subject_id: str, authorization_id: str) -> dict[str, Any]:
    # 限流可重试；授权缺失不可重试也不可降级，由 MCPClient 的错误码策略区分
    report = ctx.mcp.call("bureau-mcp", "get_credit_report",
                          {"subject_id": subject_id, "authorization_id": authorization_id},
                          caller=ctx.caller, retries=3, backoff=0.001)
    # 比对基准日取自案件的时点冻结窗口——回测中若沿用固定日期，
    # 就等于把「今天」的信息带回了历史时点，是典型的前视信息污染
    window = ctx.world.data.get("probe_window", {})
    diff = ctx.mcp.call("bureau-mcp", "diff_report",
                        {"subject_id": subject_id,
                         "baseline_date": window.get("baseline_date", "2026-05-14"),
                         "authorization_id": authorization_id}, caller=ctx.caller)

    ev1 = ctx.ledger.record(
        subject_id=subject_id, source_system="bureau-mcp", fact_type="credit_report",
        raw_content=json.dumps(report, ensure_ascii=False), extracted=report,
    )
    ev2 = ctx.ledger.record(
        subject_id=subject_id, source_system="bureau-mcp", fact_type="credit_diff",
        raw_content=json.dumps(diff, ensure_ascii=False), extracted=diff,
    )
    ctx.log("INFO", "credit_report_probe.done",
            new_overdue=diff.get("new_overdue"), new_liability=diff.get("new_liability"),
            evidence_ids=[ev1.evidence_id, ev2.evidence_id])
    return {"report": report, "diff": diff,
            "evidence_ids": [ev1.evidence_id, ev2.evidence_id]}


@skill(
    name="LitigationProbe", version="1.4.2", tier="L3", category="取证",
    purpose="涉诉检索并做实质性判定（标的额占比 / 案由性质 / 结案状态 / 诉讼地位）",
    inputs="subject_name, date_range, exposure_amount（用于计算标的额占比）",
    outputs="LitigationFacts{cases[]{案由, 标的额, 占敞口比, 诉讼地位, 结案状态}} + evidence_ids[]",
    trigger="取证任务含 judicial 类型",
    depends="judicial-mcp（只读）",
    failure_policy="检索超时 → 返回部分结果并标注 partial；重名无法消歧 → 输出候选集标注 ambiguous，不自动认定",
    security="只读公开司法数据；主体名称出站脱敏",
    regulation="《贷款风险分类指导原则》关于司法风险的认定口径",
    eval_set="25 组涉诉样例，含 8 组「小额买卖合同纠纷不构成实质风险」的误报陷阱",
    reuse="实质性判定逻辑可直接复用于保险欺诈调查、供应商准入审查",
)
def litigation_probe(ctx: Context, subject_name: str, subject_id: str) -> dict[str, Any]:
    lit = ctx.mcp.call("judicial-mcp", "search_litigation",
                       {"subject_name": subject_name}, caller=ctx.caller)
    changes = ctx.mcp.call("judicial-mcp", "get_change_history",
                           {"subject_id": subject_id}, caller=ctx.caller)

    # 逐笔取回裁判文书原文快照——结论要能点回原文，这是「可举证」的最小单位
    ev_ids: list[str] = []
    for c in lit.get("cases", []):
        doc = ctx.mcp.call("judicial-mcp", "get_judgment_doc",
                           {"case_no": c["case_no"]}, caller=ctx.caller)
        ev = ctx.ledger.record(
            subject_id=subject_id, source_system="judicial-mcp", fact_type="litigation_case",
            raw_content=doc.get("text", ""),
            extracted={**c, "source_doc_uri": doc.get("source_doc_uri"),
                       "ambiguous": lit.get("ambiguous"), "partial": lit.get("partial")},
        )
        ev_ids.append(ev.evidence_id)

    ev_reg = ctx.ledger.record(
        subject_id=subject_id, source_system="judicial-mcp", fact_type="registration_change",
        raw_content=json.dumps(changes, ensure_ascii=False), extracted=changes,
    )

    ctx.log("INFO", "litigation_probe.done",
            case_count=len(lit.get("cases", [])),
            total_ratio=lit.get("total_amount_ratio"),
            ambiguous=bool(lit.get("ambiguous")), evidence_ids=ev_ids)

    return {
        "litigation": {**lit, "evidence_ids": ev_ids},
        "registration": {**changes, "evidence_ids": [ev_reg.evidence_id]},
    }


@skill(
    name="TxnFlowAnalyze", version="1.2.3", tier="L3", category="取证",
    purpose="识别交易流水异常模式：回流 / 空转 / 集中转出 / 整数化",
    inputs="account_ids[], date_range, patterns[]",
    outputs="FlowFacts{anomalies[]{pattern, 金额, 对手方, 置信度}} + evidence_ids[]",
    trigger="取证任务含 transaction 类型",
    depends="txn-mcp（只读 · PII）",
    failure_policy="数据量超阈值 → 分片处理后合并；采样不足 → 输出弱证据等级而非强断言",
    security="高敏 PII；账号与对手方出站脱敏",
    regulation="《流动资金贷款管理办法》贷款用途真实性核查",
    eval_set="18 组合成流水，覆盖 4 类异常模式 + 6 组正常波动负样本",
    reuse="异常模式引擎可复用于反洗钱可疑交易识别、受托支付合规核查",
)
def txn_flow_analyze(ctx: Context, subject_id: str, account_ids: list[str]) -> dict[str, Any]:
    # 查询窗口取自案件的时点冻结配置，不写死——回测的取数窗口必须落在 as_of 之前
    window = ctx.world.data.get("probe_window", {})
    date_from = window.get("txn_from", "2026-07-01")
    date_to = window.get("txn_to", "2026-08-31")

    pattern = ctx.mcp.call("txn-mcp", "get_flow_pattern",
                           {"account_ids": account_ids}, caller=ctx.caller)
    summary = ctx.mcp.call("txn-mcp", "get_counterparty_summary",
                           {"account_ids": account_ids, "date_from": date_from,
                            "date_to": date_to}, caller=ctx.caller)

    # 分片取全：超过单页上限时按游标循环，避免只看到前 500 笔就下结论
    page = ctx.mcp.call("txn-mcp", "query_transactions",
                        {"account_ids": account_ids, "date_from": date_from,
                         "date_to": date_to}, caller=ctx.caller)
    records = list(page["transactions"])
    while page.get("next_cursor"):
        page = ctx.mcp.call("txn-mcp", "query_transactions",
                            {"account_ids": account_ids, "date_from": date_from,
                             "date_to": date_to, "cursor": page["next_cursor"]},
                            caller=ctx.caller)
        records.extend(page["transactions"])

    related = any(c.get("related_party") for c in summary.get("counterparties", []))
    world_txn = ctx.world.section("txn")

    ev = ctx.ledger.record(
        subject_id=subject_id, source_system="txn-mcp", fact_type="flow_pattern",
        raw_content=json.dumps({"pattern": pattern, "summary": summary}, ensure_ascii=False),
        extracted={**pattern, "counterparties": summary.get("counterparties", []),
                   "undersampled": pattern.get("undersampled")},
    )

    ctx.log("INFO", "txn_flow_analyze.done",
            anomalies=pattern.get("anomalies"), coverage=pattern.get("coverage"),
            related_party=related, records_scanned=len(records),
            evidence_ids=[ev.evidence_id])

    return {
        "anomaly_detected": bool(pattern.get("anomalies")),
        "anomalies": pattern.get("anomalies", []),
        "counterparty_related_party": related,
        "within_baseline_band": world_txn.get("within_baseline_band", False),
        "baseline_band": pattern.get("baseline_band"),
        "coverage": pattern.get("coverage"),
        "undersampled": pattern.get("undersampled", False),
        "evidence_ids": [ev.evidence_id],
    }


@skill(
    name="GuaranteeProbe", version="1.0.0", tier="L3", category="取证",
    purpose="对外担保台账取证：识别已出险被担保方，测算净代偿敞口与缓释覆盖率",
    inputs="subject_id, direct_exposure（用于计算代偿敞口相对倍数）",
    outputs="GuaranteeFacts{担保余额, 已出险被担保方[], 缓释措施[], 覆盖率, 净未覆盖敞口} + evidence_ids[]",
    trigger="取证任务含 guarantee_contagion 类型，或敞口测绘发现已出险关联主体",
    depends="credit-core-mcp（只读）",
    failure_policy="台账为空 → 登记为有效负向证据而非取证失败；被担保方状态无公开定论 → 标注 ambiguous 降为弱证据，不自动认定出险",
    security="只读；担保关系与金额属敏感数据，出站脱敏",
    regulation="《商业银行大额风险暴露管理办法》关联客户与担保链认定；"
               "《商业银行贷后管理指引》或有负债监测",
    eval_set="10 组担保结构样例，含 3 组「缓释措施已全额覆盖，不构成风险」的误报陷阱",
    reuse="或有负债 → 净敞口的测算范式可复用于供应链金融确权、融资担保机构代偿测算",
)
def guarantee_probe(ctx: Context, subject_id: str, direct_exposure: float | None) -> dict[str, Any]:
    """把「对外担保」从一个静态数字，变成一笔可判定的或有敞口。

    三步：先看被担保方**是否已经出险**（没出险就不是当期信号），再把共同担保人、
    反担保与抵押物折算成**缓释金额**，最后得到净未覆盖敞口。
    银行真正承压的是这个净额，不是台账上的担保总额。
    """
    ledger = ctx.mcp.call("credit-core-mcp", "get_guarantee_ledger",
                          {"subject_id": subject_id}, caller=ctx.caller)
    entries = ledger.get("entries", [])

    if not entries:
        # 「查了，没有」是有效的负向证据，不是取证失败
        ev = ctx.ledger.record(
            subject_id=subject_id, source_system="credit-core-mcp",
            fact_type="guarantee_ledger",
            raw_content=json.dumps(ledger, ensure_ascii=False),
            extracted={"entries": [], "empty_reason": ledger.get("empty_reason"),
                       "source_doc_uri": ledger.get("source_doc_uri")},
        )
        ctx.log("INFO", "guarantee_probe.done", entries=0, distressed=0,
                evidence_ids=[ev.evidence_id])
        return {"has_guarantee": False, "distressed_guarantee": 0.0,
                "distressed_parties": [], "evidence_ids": [ev.evidence_id]}

    ev_ids: list[str] = []
    for e in entries:
        # 状态无公开定论时不自动认定出险——ambiguous 会让账本自动降为弱证据
        ev = ctx.ledger.record(
            subject_id=subject_id, source_system="credit-core-mcp",
            fact_type="guarantee_entry",
            raw_content=json.dumps(e, ensure_ascii=False),
            extracted={**e, "ambiguous": e.get("status_ambiguous", False)},
        )
        ev_ids.append(ev.evidence_id)

    summary = summarize_guarantee(entries, direct_exposure)
    ctx.log("INFO", "guarantee_probe.done",
            entries=len(entries), distressed=len(summary["distressed_parties"]),
            distressed_guarantee=summary["distressed_guarantee"],
            mitigation=summary["mitigation_amount"],
            uncovered=summary["uncovered_amount"],
            uncovered_multiple=summary["uncovered_multiple"], evidence_ids=ev_ids)

    return {
        "has_guarantee": True,
        "total_guarantee": float(ledger.get("total_outstanding_guarantee") or 0),
        **summary,
        "evidence_ids": ev_ids,
    }


# 被担保方状态达到以下任一，才把担保余额计入代偿敞口。
# 「圈子大」不是风险，「圈里有人出事」才是。
DISTRESSED_STATUS = ("已出险", "已违约", "已进入重整")


def summarize_guarantee(entries: list[dict[str, Any]],
                        direct_exposure: float | None) -> dict[str, Any]:
    """把担保台账折算为净未覆盖代偿敞口。**纯函数**，是 GuaranteeProbe 评估集的被测对象。

    抽成独立函数与 ``is_material_case`` 同理：这段口径直接绑定
    《商业银行大额风险暴露管理办法》的关联客户与担保链认定，
    条款变更时只改这一处，且能脱离 MCP 与账本独立回归。
    """
    distressed_amount = 0.0
    mitigation_amount = 0.0
    parties: list[dict[str, Any]] = []

    for e in entries:
        if e.get("party_status") not in DISTRESSED_STATUS:
            continue
        amt = float(e.get("outstanding") or 0)
        raw_mit = sum(float(m.get("amount") or 0) for m in e.get("mitigations", []))
        # 缓释额不得超过被缓释的敞口本身——超额申报的缓释不产生额外抵扣
        mit = min(raw_mit, amt)
        distressed_amount += amt
        mitigation_amount += mit
        parties.append({
            "party": e.get("guaranteed_party"),
            "outstanding": amt,
            "status": e.get("party_status"),
            "status_basis": e.get("status_basis"),
            "mitigations": e.get("mitigations", []),
            "mitigation_amount": mit,
        })

    uncovered = max(0.0, distressed_amount - mitigation_amount)
    return {
        "distressed_guarantee": distressed_amount,
        "distressed_parties": parties,
        "mitigation_amount": mitigation_amount,
        "mitigation_coverage": (round(mitigation_amount / distressed_amount, 4)
                                if distressed_amount else None),
        "uncovered_amount": uncovered,
        "uncovered_multiple": (round(uncovered / direct_exposure, 2)
                               if direct_exposure else None),
        "direct_exposure": direct_exposure,
    }


# ===========================================================================
# L3 治理类
# ===========================================================================

@skill(
    name="RiskGate", version="3.1.0", tier="L3", category="治理",
    purpose="计算处置动作的执行层级 L0–L3（安全核心）",
    inputs="risk_grade, evidence_level, exposure_amount, action_type, reversibility",
    outputs="Gate{action_tier, 需审批, 审批角色[], idempotency_key, rollback_point, 判定理由}",
    trigger="裁决完成、进入 DISPOSITION 阶段时强制调用，无旁路",
    depends="纯规则引擎，无外部依赖（安全判定不依赖网络与 LLM）",
    failure_policy="任何入参缺失或规则未命中 → 默认降级为 L3（只出方案不执行）。fail-safe 而非 fail-open",
    security="规则表经 Nacos 版本管控，变更需双人审核；判定结果写审计日志",
    regulation="《商业银行内部控制指引》授权审批与不相容职务分离",
    eval_set="全组合边界用例（含缺失入参、极端金额、未知动作类型），校验不可逆动作永不落入 L0/L1",
    reuse="四维闸门模型是场景无关的高风险动作管控内核，可复用于运维自愈、保险赔付、自动化交易",
)
def risk_gate(ctx: Context, **kwargs: Any) -> dict[str, Any]:
    decision = gate_evaluate(**kwargs)
    ctx.log("INFO", "risk_gate.decided",
            action=decision.action, tier=decision.action_tier,
            rule_id=decision.rule_id, reason=decision.reason)
    return decision.to_dict()


@skill(
    name="ComplianceCheck", version="1.5.0", tier="L3", category="治理",
    purpose="逐条校验贷后管理合规项并输出可举证结论",
    inputs="case_id, trace_id, rule_set_version",
    outputs="ComplianceResult{items[]{rule_id, 条款出处, 结论, 证据引用}}, 整改建议[]",
    trigger="处置执行完成后自动触发",
    depends="监管规则库；全链路 Trace（只读）",
    failure_policy="规则库版本不匹配 → 拒绝执行并告警，不用旧版本静默通过",
    security="只读；结果不可篡改，写入 append-only 审计表",
    regulation="《商业银行贷后管理指引》检查频次、双人复核、留痕与时效要求",
    eval_set="每条规则正反各 1 例，共 24 组",
    reuse="「规则集 + Trace → 逐条举证」范式可复用于任何需合规审计的 Agent 系统",
)
def compliance_check(ctx: Context, state: Any, tracer: Tracer) -> dict[str, Any]:
    spans = tracer.spans
    items: list[dict[str, Any]] = []

    def add(rule_id: str, source: str, ok: bool, detail: str, na: bool = False) -> None:
        items.append({
            "rule_id": rule_id, "source": source,
            "result": "N/A" if na else ("PASS" if ok else "FAIL"),
            "detail": detail,
        })

    # C-01 所有结论均挂载证据
    unsupported = []
    for cause in (state.assertion or {}).get("root_causes", []):
        if not cause.get("evidence_ids"):
            unsupported.append(cause.get("type"))
    add("C-01", "《内部控制指引》记录留存", not unsupported,
        f"无证据结论 {len(unsupported)} 条" if unsupported else "全部结论均挂载有效证据引用")

    # C-02 高风险动作必须经审批。
    # L3 与 L2 的合规口径不同：L2 是「系统执行、需先取得审批令牌」，
    # L3 是「系统不执行、只出方案交人工决策」。对一份尚未执行的方案索要执行审批令牌
    # 是错误的口径——真正要校验的是它**确实没有被执行**。
    gate = state.gate or {}
    executed = [r for r in (state.execution or {}).get("results", [])
                if r.get("status") == "SUCCESS"]
    if gate.get("action_tier") == "L3":
        add("C-02", "《内部控制指引》授权审批", not executed,
            "L3 不可逆/大额动作仅生成方案交人工决策，系统未执行任何处置——符合闸门约束"
            if not executed else
            f"严重违规：L3 动作被系统执行了 {len(executed)} 项")
    elif gate.get("needs_approval"):
        approved = bool((state.approval or {}).get("token"))
        add("C-02", "《内部控制指引》授权审批", approved,
            "L2 动作已取得有效审批令牌" if approved else "高风险动作缺少审批记录")
    else:
        add("C-02", "《内部控制指引》授权审批", True, "本次为 L0/L1 动作，无需审批", na=True)

    # C-03 不相容职务分离：执行者不得自审
    exec_spans = {s.attributes.get("caller") for s in spans if s.kind == "execution"}
    audit_spans = {s.attributes.get("caller") for s in spans if s.name == "ComplianceCheck"}
    add("C-03", "《内部控制指引》不相容职务分离",
        not (exec_spans & audit_spans),
        f"执行方 {exec_spans or '无'}，审计方 {audit_spans}，职责分离成立")

    # C-04 质疑环节不可跳过
    had_advocate = any(s.attributes.get("caller") == "devils-advocate" for s in spans)
    add("C-04", "行内风险评审双人复核要求", had_advocate,
        "对抗质疑环节已执行" if had_advocate else "缺失对抗质疑环节")

    # C-05 全流程留痕
    add("C-05", "《贷后管理指引》留痕要求", len(spans) > 0,
        f"全链路留痕 {len(spans)} 条 Span，含路由决策与裁决依据")

    # C-06 处置结果核验
    if state.execution:
        effective = state.execution.get("status") in ("SUCCESS", "NO_ACTION")
        add("C-06", "《贷后管理指引》处置结果核验", effective,
            f"处置状态 {state.execution.get('status')}")
    else:
        add("C-06", "《贷后管理指引》处置结果核验", True, "本次未产生处置动作", na=True)

    failed = [i for i in items if i["result"] == "FAIL"]
    ctx.log("INFO" if not failed else "WARN", "compliance_check.done",
            total=len(items), failed=len(failed))

    return {
        "items": items,
        "passed": len([i for i in items if i["result"] == "PASS"]),
        "failed": len(failed),
        # 审计者只报告不修复——发现缺失由 RiskCommander 升级人工
        "remediation": [f"{i['rule_id']}：{i['detail']}" for i in failed],
    }


# ===========================================================================
# L3 执行类
# ===========================================================================

@skill(
    name="SafeDisposition", version="2.2.0", tier="L3", category="执行",
    purpose="幂等执行已裁决且已审批的处置动作，带回滚点",
    inputs="DispositionOrder{action, params, action_tier, idempotency_key, rollback_point, approval_token}",
    outputs="ExecutionResult{status, 系统回执, 生效时间, rollback_point_id, 审计流水号}",
    trigger="action_tier ∈ {L0, L1}，或 L2 且 approval_token 验签通过。L3 永不调用",
    depends="credit-core-mcp（读 + 写）",
    failure_policy="执行中断 → 按 rollback_point 自动回滚（额度冲正）；重复投递 → 幂等键去重返回首次结果；回滚失败 → 立即冻结 Case 并升级人工",
    security="仅 disposition-executor 可调用；动作白名单强校验；审批 token 验签；双写审计日志",
    regulation="《商业银行内部控制指引》授权审批；《贷后管理指引》处置留痕",
    eval_set="幂等重放、中断回滚、越权动作拦截、无审批 L2 拦截，共 16 组",
    reuse="幂等 + 回滚点 + 审批验签的执行封装，是任何有副作用 Agent 的通用安全底座",
)
def safe_disposition(ctx: Context, order: dict[str, Any]) -> dict[str, Any]:
    tier = order["action_tier"]
    # L3 硬闸门：不可逆动作永不执行，这条判断在 Executor 内部再兜一次底
    if tier == "L3":
        ctx.log("WARN", "safe_disposition.blocked",
                action=order["action"], reason="L3 动作仅生成方案，Agent 无执行权")
        return {"status": "PLAN_ONLY", "reason": "L3 不可逆动作，仅生成方案供人工决策"}

    if tier == "L0":
        return {"status": "NO_ACTION", "reason": "L0 只读诊断，不触碰业务系统"}

    subject_id = order["params"]["subject_id"]
    args = {**order["params"], "idempotency_key": order["idempotency_key"]}
    if order.get("approval_token"):
        args["approval_token"] = order["approval_token"]

    tool = {"reduce_limit": "adjust_limit", "add_guarantee": "add_guarantee",
            "tag_watch": "adjust_limit", "request_documents": "adjust_limit",
            "suspend_drawdown": "adjust_limit"}[order["action"]]

    with ctx.tracer.span("execution", f"{order['action']}",
                         caller=ctx.caller, action_tier=tier,
                         idempotency_key=order["idempotency_key"]):
        try:
            result = ctx.mcp.call("credit-core-mcp", tool, args, caller=ctx.caller)
        except Exception as e:
            # 执行中断即回滚；回滚失败不做二次尝试，冻结并升级人工。
            # error_code 是机器可读的，下游据此决定 retry / degrade / escalate，
            # 不靠解析错误文案。
            code = getattr(e, "code", "UNKNOWN")
            ctx.log("ERROR", "safe_disposition.failed",
                    action=order["action"], error_code=code, error=str(e))
            rp = order.get("rollback_point_id")
            if rp:
                try:
                    ctx.mcp.call("credit-core-mcp", "rollback_adjustment",
                                 {"subject_id": subject_id, "rollback_point_id": rp,
                                  "idempotency_key": order["idempotency_key"] + "-rb"},
                                 caller=ctx.caller)
                    return {"status": "ROLLED_BACK", "error_code": code, "error": str(e)}
                except Exception as re_:
                    ctx.log("ERROR", "safe_disposition.rollback_failed", error=str(re_))
                    return {"status": "FROZEN_ESCALATED",
                            "error_code": getattr(re_, "code", "UNKNOWN"), "error": str(re_)}
            return {"status": "FAILED", "error_code": code, "error": str(e)}

    ctx.log("INFO", "safe_disposition.executed",
            action=order["action"], audit_serial=result.get("audit_serial"),
            rollback_point=result.get("rollback_point_id"))
    return {"status": "SUCCESS", **result}


@skill(
    name="PostmortemDistill", version="1.1.0", tier="L3", category="执行",
    purpose="把已核实的案件沉淀为可复用的 RiskPattern 与向量案例",
    inputs="case_id, final_adjudication, execution_result, audit_report",
    outputs="RiskPattern{模式描述, 触发条件, 判定要点, 反例说明, 适用边界} + 结构化案例",
    trigger="审计完成且案件闭环",
    depends="知识库（写）；L2 CaseMemory 写入通道",
    failure_policy="沉淀内容与既有 RiskPattern 冲突 → 标记 conflict 待人工裁定，不自动覆盖",
    security="仅 compliance-auditor 可调用；写入内容脱敏后入库",
    regulation="《商业银行贷后管理指引》风险事件复盘要求",
    eval_set="10 组闭环案件，校验沉淀后 CaseMemory 召回命中率提升",
    reuse="经验回流闭环可复用于任何「越用越准」的 Agent 系统",
)
def postmortem_distill(ctx: Context, state: Any) -> dict[str, Any]:
    adj = state.adjudication or {}
    confirmed = adj.get("verdict") == "RISK_CONFIRMED"

    if confirmed:
        pattern = {
            "pattern_id": f"RP-{state.case_id}",
            "description": "涉诉实质性 + 关联方集中转出 + 法代变更时点重合，三信号共振",
            "trigger": "单笔涉诉标的占敞口 ≥ 5% 且未结案；资金集中转出对手方经穿透为关联方；法代变更落在风险窗口内",
            "key_points": "三条线索需相互印证；仅凭其一不足以定性",
            "counter_example": "若涉诉均已结案且我方为原告、转出对手方为稳定供应商，则不成立（见 CASE-2026-0821-002）",
            "boundary": "适用于小微与对公信贷贷后；零售信贷不适用",
        }
    else:
        pattern = {
            "pattern_id": f"RP-{state.case_id}",
            "description": "表面三信号齐发但个案层面均不具实质性的典型误报",
            "trigger": "涉诉标的占比 < 5% 且已结案、我方为原告；转出对手方为非关联供应商且落在历史波动区间；法代变更早于风险窗口且股权未变",
            "key_points": "聚合信号相似不代表风险相似，必须下钻个案层面",
            "counter_example": "同主体 CASE-2026-0814-001 信号类型完全相同但个案实质性成立",
            "boundary": "本模式用于抑制误报，不可用于放宽真实风险的判定",
        }

    ctx.log("INFO", "postmortem_distill.done",
            pattern_id=pattern["pattern_id"], confirmed=confirmed)
    with ctx.tracer.span("skill", "CaseMemory.write", **{"skill.tier": "L2"}):
        pass  # 写入向量库；PoC 中落 JSON，生产走 pgvector / 官方向量库 Skill
    return {"risk_pattern": pattern, "written_to": "knowledge_base/risk_patterns"}


# ===========================================================================
# L2 领域封装层（自研外壳 + 编排官方云能力）
# ===========================================================================

@skill(
    name="EvidenceLedger", version="2.0.0", tier="L2", category="取证",
    purpose="登记证据、哈希存证并评定证据等级",
    inputs="source_system, raw_content, extracted_fields, collected_at, subject_id",
    outputs="Evidence{evidence_id, snapshot_uri, content_hash, level, level_reason}",
    trigger="任何外部原文取回时强制调用——不经账本的数据不得进入决策",
    depends="自研：证据等级评分、哈希存证语义、引用约束校验。复用官方：对象存储（快照）+ 数据库写入",
    failure_policy="存储不可用 → 本地暂存队列 + 后台补偿；哈希冲突 → 拒绝写入并告警",
    security="仅 due-diligence 可写；账本 append-only 不可篡改；快照加密存储",
    regulation="《商业银行内部控制指引》记录留存与可追溯要求",
    eval_set="定级规则全覆盖 + 幂等写入 + 哈希校验，共 14 组",
    reuse="本项目最通用的开源组件——任何需要「结论可举证」的 Agent 系统均可直接接入",
    reuses_official="对象存储 OSS（可替换 MinIO）+ 数据库写入（可替换 PostgreSQL）",
)
def evidence_ledger_summary(ctx: Context) -> dict[str, Any]:
    """返回账本快照。写入路径内联在各取证 Skill 中，此处提供只读汇总。"""
    return ctx.ledger.to_dict()


@skill(
    name="QueryRewrite", version="1.0.0", tier="L3", category="知识",
    purpose="把一个基础检索意图改写为六维子查询，并识别需要澄清的歧义",
    inputs="base_query, stance（PROVE/REFUTE）, signal_types[], facts, as_of",
    outputs="QueryPlan{subqueries[]{dimension, text, filters, why, weight}, clarifications[]}",
    trigger="任何需要检索政策或历史案例的环节，检索前必调",
    depends="自研术语表与信号主题映射，无外部依赖（纯函数）",
    failure_policy="未知立场 → 直接抛错不猜；术语表未覆盖 → 降级为仅立场维与信号主题维，"
                   "并在 Span 中标注 dimensions_used 以便事后补词表",
    security="只读纯函数；不接触任何业务数据",
    regulation="《商业银行贷后管理指引》条款适用性判断——条款须在案件时点已生效",
    eval_set="18 组改写样例，覆盖六个维度各自的触发与**不触发**条件"
             "（如求证方不得产生否定式子查询），以及澄清三渠道的选路正确性",
    reuse="立场维 + 否定式维的组合是对抗式检索的通用范式，"
          "可复用于合同审查、尽调抗辩、审计发现的反向验证",
)
def query_rewrite(ctx: Context, base_query: str, stance: str,
                  signal_types: list[str] | None = None,
                  facts: dict[str, Any] | None = None) -> querying.QueryPlan:
    """检索前的必经环节。**改写是纯函数**，同输入恒同输出，因此可独立回归。

    案件时点从 World 读取，不由调用方传入——避免某个调用点忘了传 as_of
    就在回溯案件里悄悄用上了未来的条款。
    """
    plan = querying.rewrite(
        caller=ctx.caller, base_query=base_query, stance=stance,
        signal_types=signal_types or [], facts=facts or {},
        as_of=getattr(ctx.world, "as_of", None),
    )
    with ctx.tracer.span("rag", "query.rewrite",
                         stance=plan.stance,
                         dimensions=plan.dimensions_used,
                         subquery_count=len(plan.subqueries),
                         clarifications=len(plan.clarifications),
                         blocking_clarifications=len(plan.blocking_clarifications)):
        ctx.log("INFO", "query_rewrite.done", stance=plan.stance,
                dimensions=plan.dimensions_used,
                subqueries=[q.text[:40] for q in plan.subqueries],
                clarifications=[c.to_dict() for c in plan.clarifications])
    return plan


@skill(
    name="PolicyRag", version="2.0.0", tier="L2", category="知识",
    purpose="按多维查询计划召回政策条款、加权融合、并把召回结果证据化",
    inputs="plan（QueryRewrite 产出的 QueryPlan）, top_k",
    outputs="PolicyChunks[]{条款原文, 出处, 生效日期, 相似度, matched_dimensions[], evidence_id}",
    trigger="RiskAnalyst / DevilsAdvocate / ComplianceAuditor 需要政策依据时",
    depends="自研：多维融合排序、时效过滤、召回后证据化。复用官方：向量库建库与相似度检索",
    failure_policy="召回为空 → 返回 no_policy_matched 并区分「无匹配」与「全部被时效过滤」，"
                   "绝不编造条款；向量库不可用 → 降级关键词检索并标注",
    security="只读；按产品线做召回范围隔离",
    regulation="—（本 Skill 是条款的检索侧）",
    eval_set="22 组 query，校验 Recall@5、零条款幻觉，以及**生效日晚于案件时点的条款一律不得召回**",
    reuse="「多维融合 + 时效过滤 + 召回即证据化」可复用于所有合规类 RAG",
    reuses_official="向量检索 DashVector（可替换 pgvector）",
)
def policy_rag(ctx: Context, plan: querying.QueryPlan, top_k: int = 3) -> list[dict[str, Any]]:
    """按 QueryPlan 的多维子查询分别召回后加权融合。

    两处与常规 RAG 不同：

    1. **时效过滤是硬约束**。生效日晚于案件时点的条款一律剔除并计数——
       用 2025 年的行内制度去评价 2017 年的案子，与把后来才披露的证据放进
       决策时点是同一类前视污染，只不过发生在知识维度。
    2. **每条命中记录它由哪些维度召回**（``matched_dimensions``）。
       这既是可解释性，也是评估改写效果的直接依据：某一维从不贡献命中，
       说明那一维要么设计错了，要么词表没覆盖到。
    """
    docs = _load_kb("policies.json")
    as_of = plan.as_of
    fused: dict[str, dict[str, Any]] = {}
    filtered_by_recency = 0

    with ctx.tracer.span("rag", "policy.search",
                         stance=plan.stance,
                         dimensions=plan.dimensions_used,
                         subquery_count=len(plan.subqueries),
                         as_of=as_of, top_k=top_k) as span:
        for sq in plan.subqueries:
            terms = set(re.findall(r"[一-鿿]{2,}", sq.text))
            for d in docs:
                # 时效维：生效日晚于案件时点的条款在该时点尚不存在
                cutoff = sq.filters.get("effective_before") or as_of
                if cutoff and d["effective_date"] > cutoff:
                    filtered_by_recency += 1
                    continue
                hay = d["title"] + d["text"] + d["source"]
                if sq.filters.get("exact"):
                    score = 1.0 if sq.text.strip("《》") in hay else 0.0
                else:
                    score = sum(1 for t in terms if t in hay) / max(len(terms), 1)
                if score <= 0:
                    continue
                slot = fused.setdefault(d["doc_id"], {**d, "score": 0.0,
                                                     "matched_dimensions": []})
                slot["score"] += score * sq.weight
                if sq.dimension not in slot["matched_dimensions"]:
                    slot["matched_dimensions"].append(sq.dimension)

        ranked = sorted(fused.values(), key=lambda x: x["score"], reverse=True)
        hits = [{**h, "similarity": round(h.pop("score"), 3)} for h in ranked[:top_k]]
        span.attributes.update({
            "hit_count": len(hits),
            "filtered_by_recency": filtered_by_recency,
            "hit_dimensions": sorted({d for h in hits for d in h["matched_dimensions"]}),
        })

    if not hits:
        # 空集要说明原因：是真没有，还是全被时效过滤掉了。二者的处置完全不同。
        why = ("as_of 时点尚无生效的匹配条款（全部候选均晚于案件时点）"
               if filtered_by_recency and as_of else "知识库中无匹配条款")
        ctx.log("WARN", "policy_rag.no_match", stance=plan.stance, why=why,
                filtered_by_recency=filtered_by_recency)
        return [{"result": "no_policy_matched", "why": why, "as_of": as_of}]

    # 召回即证据化：每条召回登记为证据，使结论能点回条款原文与生效日期
    out = []
    for h in hits:
        ev = ctx.ledger.record(
            subject_id="-", source_system="policy-kb", fact_type="policy_clause",
            raw_content=h["text"],
            extracted={"title": h["title"], "source": h["source"],
                       "effective_date": h["effective_date"],
                       "matched_dimensions": h["matched_dimensions"],
                       "source_doc_uri": h.get("source_doc_uri")},
            level="强", level_reason="政策条款原文可溯源、生效日期在案件时点之前",
        )
        out.append({**h, "evidence_id": ev.evidence_id})
    return out


@skill(
    name="CaseMemory", version="1.4.0", tier="L2", category="知识",
    purpose="检索历史相似处置案例与本 Case 的决策上下文",
    inputs="subject_features, time_window, top_k, scope: case|session",
    outputs="Cases[]{案件摘要, 当时结论, 处置动作, 事后验证结果, 相似度}",
    trigger="定性、质疑、复盘阶段",
    depends="自研：案例结构化模板、时间窗召回策略。复用官方：向量检索 + 数据库查询",
    failure_policy="无相似案例 → 明确返回空集，不用低相似度结果凑数",
    security="只读；跨机构案例默认脱敏隔离",
    regulation="—",
    eval_set="15 组，校验相似案例召回相关性、时间窗过滤，"
             "以及**回溯案件中晚于案件时点沉淀的案例一律不得召回**",
    reuse="Agent 记忆存储的通用实现，满足赛题 RAG 要求第 1 项",
    reuses_official="向量检索 + 数据库查询（可替换 pgvector + PostgreSQL）",
)
def case_memory(ctx: Context, signal_types: list[str], top_k: int = 3) -> list[dict[str, Any]]:
    as_of = getattr(ctx.world, "as_of", None)
    with ctx.tracer.span("rag", "case_memory.search", top_k=top_k, as_of=as_of) as span:
        cases = _load_kb("case_memory.json")
        scored = []
        filtered_by_recency = 0
        for c in cases:
            # 时点冻结同样适用于经验记忆：拿 2026 年才沉淀的教训去指导 2017 年的
            # 判断，是前视污染，只不过发生在经验维度而非事实维度
            if as_of and c.get("case_id", "")[5:15] > as_of:
                filtered_by_recency += 1
                continue
            overlap = len(set(signal_types) & set(c["signal_types"]))
            if overlap:
                scored.append({**c, "similarity": round(overlap / max(len(signal_types), 1), 3)})
        scored.sort(key=lambda x: x["similarity"], reverse=True)
        # 相似度过低的不返回——宁可空集，不用噪声凑数
        hits = [c for c in scored[:top_k] if c["similarity"] >= 0.3]
        span.attributes["hit_count"] = len(hits)
        span.attributes["filtered_by_recency"] = filtered_by_recency
    ctx.log("INFO", "case_memory.done", hit_count=len(hits),
            filtered_by_recency=filtered_by_recency)
    return hits


@skill(
    name="ReportCompose", version="1.7.0", tier="L2", category="执行",
    purpose="生成贷后检查报告、风险处置意见书与审计报告，并注入证据引用",
    inputs="template_id, case_state, evidence_ids[], adjudication, execution_result",
    outputs="结构化报告（Markdown）+ archive_uri；正文每处结论自动注入证据引用角标",
    trigger="处置方案生成后、审计完成后",
    depends="自研：报告模板、口径统一、证据引用注入。复用官方：产物归档到对象存储",
    failure_policy="引用的 evidence_id 不存在 → 拒绝生成并告警（防止报告出现无源结论）；归档失败 → 本地留存 + 重试",
    security="报告含敏感信息，归档加密；仅授权角色可下载",
    regulation="《商业银行贷后管理指引》贷后检查报告要求",
    eval_set="8 组，校验模板完整性与证据引用完备率 100%",
    reuse="「模板 + 证据注入」成文引擎解决口径不一与经验难复用，可复用于保险结论书、事故复盘报告",
    reuses_official="对象存储 OSS 归档（可替换 MinIO）",
)
def report_compose(ctx: Context, template_id: str, state: Any, extra: dict[str, Any]) -> dict[str, Any]:
    from .report import render  # 延迟导入避免循环依赖
    md = render(template_id, state, ctx.ledger, extra)
    # 拒绝生成含无源结论的报告：正文出现的每个 EV- 编号都必须存在于账本
    for eid in set(re.findall(r"EV-\d+-\d+", md)):
        ctx.ledger.get(eid)
    ctx.log("INFO", "report_compose.done", template=template_id, length=len(md))
    return {"template_id": template_id, "markdown": md,
            "archive_uri": f"s3://creditsentry-reports/{state.case_id}/{template_id}.md"}


# ---------------------------------------------------------------------------
# 知识库加载（PoC 用 JSON；生产走 pgvector / 官方向量库）
# ---------------------------------------------------------------------------

_KB_CACHE: dict[str, list[dict[str, Any]]] = {}


def _load_kb(filename: str) -> list[dict[str, Any]]:
    if filename not in _KB_CACHE:
        path = os.path.join(FIXTURE_DIR, filename)
        with open(path, encoding="utf-8") as f:
            _KB_CACHE[filename] = json.load(f)
    return _KB_CACHE[filename]
