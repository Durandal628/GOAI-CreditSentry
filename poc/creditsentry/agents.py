"""6 个职能 Worker 与 RiskCommander 的编排实现。

Agent 拆分遵循 ``docs/agent-decomposition-law.md``：**Agent 边界 = 工具权限边界**。
每个 Worker 的 ``caller`` 身份在调用 Skill 与 MCP 时被强制校验，
因此 ``docs/Agent-Identity清单.md`` 中的权限矩阵不是文档承诺，而是运行时约束。
"""

from __future__ import annotations

from typing import Any

from . import checklist, context, prompts, querying, routing, schemas, skills
from .ledger import EvidenceLedger
from .llm import InferenceError
from .state import CaseStateStore
from .tracing import Tracer


class Orchestrator:
    """RiskCommander（Team Leader）驱动的五阶段状态机。

    对应 AgentTeams 中的 Team Leader 角色：负责阶段路由、任务 fan-out、
    冲突裁决、风险分级与审批发起——但**自身不持有任何取证或处置工具权限**。
    """

    def __init__(self, world: Any, mcp: Any, llm: Any, *, auto_approve: bool = True,
                 approval_handler: Any = None, log_sink: Any = None) -> None:
        self.world = world
        self.mcp = mcp
        self.llm = llm
        self.auto_approve = auto_approve
        # 审批回调。缺省时按 auto_approve 模拟（CLI 与回归测试用）；
        # 工作台传入一个**阻塞**的回调，真正停在这里等人点按钮——
        # 「L2 必须人工审批」如果在演示里也是自动放行的，那它就没有被演示过。
        self.approval_handler = approval_handler
        self.tracer: Tracer = mcp.tracer
        self.ledger = EvidenceLedger(world.case_id)
        self.store = CaseStateStore()
        self.ctx = skills.Context(self.tracer, self.ledger, mcp, world, llm,
                                  caller="risk-commander", sink=log_sink)

    # ---- 主循环 -----------------------------------------------------
    def run(self) -> Any:
        st = self.store.create(self.world.case_id, self.world.subject)
        with self.tracer.span("case", self.world.case_id,
                              **{"subject": self.world.subject["name"]}):
            guard = 0
            while st.phase not in ("CLOSED", "EVIDENCE_GAP"):
                guard += 1
                if guard > 12:  # 防御性上限；正常链路 5~7 跳
                    raise RuntimeError("状态机未收敛，疑似路由表存在环路")
                st = self._step(st)
        return st

    def _step(self, st: Any) -> Any:
        # 阶段内的收敛动作先完成，再路由——路由键要反映本阶段的最新状态
        if st.phase == "ADJUDICATION" and st.assertion and st.rebuttal and not st.adjudication:
            self._adjudicate(st)
        if st.phase == "DISPOSITION" and not st.gate:
            self._gate_and_plan(st)

        decision = routing.route(
            phase=st.phase,
            signal_types=(st.risk_event or {}).get("signal_types", []),
            evidence_sufficiency=self.ledger.sufficiency(),
            risk_tier=(st.adjudication or {}).get("final_grade"),
            exposure_amount=(st.exposure or {}).get("total_exposure"),
            evidence_retries=st.retry_counts.get("EVIDENCE", 0),
            adjudication_verdict=(st.adjudication or {}).get("verdict"),
            action_tier=(st.gate or {}).get("action_tier"),
            approved=bool((st.approval or {}).get("token")),
        )

        # 路由决策本身即一条 Span——这是「为什么走了这条分支」可举证的关键
        with self.tracer.span(
            "routing", f"{st.phase}→{decision.next_phase}",
            routing_key=decision.routing_key,
            rule_id=decision.rule_id,
            rule_version=decision.rule_version,
            dispatch=decision.dispatch,
            parallel=decision.parallel,
        ):
            self.ctx.log("INFO", "routing.decided", **decision.to_dict())

        for worker in decision.dispatch:
            handler = getattr(self, f"_run_{worker.replace('-', '_')}")
            handler(st)

        # R-02 补充取证：留在 EVIDENCE 阶段，计数后重新路由
        if decision.rule_id == "R-02":
            st.retry_counts["EVIDENCE"] = st.retry_counts.get("EVIDENCE", 0) + 1
            return st

        # R-05 回退补证：清空裁决相关状态，使其在补证后被重新推导
        if decision.rule_id == "R-05":
            st.assertion = st.rebuttal = st.adjudication = None
            st.retry_counts["EVIDENCE"] = st.retry_counts.get("EVIDENCE", 0) + 1

        # R-09 等待审批：留在 DISPOSITION 阶段，取得审批结论后重新路由
        if decision.rule_id == "R-09":
            self._request_approval(st)
            return st

        return self.store.transition(st.case_id, decision.next_phase, decision.reason)

    # ---- Worker：SignalHub -------------------------------------------
    def _run_signal_hub(self, st: Any) -> None:
        ctx = self.ctx.as_("signal-hub")
        with self.tracer.span("agent", "signal-hub", caller="signal-hub"):
            event = skills.signal_fusion(ctx, self.world.data["alerts"])
            exposure = skills.exposure_mapping(ctx, st.subject["subject_id"], depth=2)
        st.risk_event = event
        st.exposure = exposure

    # ---- Worker：DueDiligence ----------------------------------------
    def _run_due_diligence(self, st: Any) -> None:
        ctx = self.ctx.as_("due-diligence")
        sid = st.subject["subject_id"]
        with self.tracer.span("agent", "due-diligence", caller="due-diligence"):
            credit = skills.credit_report_probe(ctx, sid, authorization_id="AUTH-2026-0814-77")
            jud = skills.litigation_probe(ctx, st.subject["name"], sid)
            txn = skills.txn_flow_analyze(ctx, sid, self.world.data.get(
                "probe_accounts", ["6222021001088217"]))
            # 对外担保是或有负债，敞口测绘只能看到「有多少关联」，
            # 判定净代偿敞口需要取台账原文，因此归在取证方而非聚合方
            gua = skills.guarantee_probe(
                ctx, sid, (st.exposure or {}).get("total_exposure"))

            # 已知缺口登记为「缺失证据」——记录我们知道自己不知道什么
            for gap in self.world.data.get("known_gaps", []):
                ev = self.ledger.record_gap(subject_id=sid, fact_type=gap["fact_type"],
                                            why=gap["why"])
                st.evidence_gaps.append({"evidence_id": ev.evidence_id, **gap})

            # 取证清单：按信号类型派生「本应取到什么」，再与账本实际取到的比对。
            # 没有这一步，一条本该取而未取的事实会从证据链里静悄悄消失——
            # 缺口只有被显式登记，才谈得上「输出取证清单交人工」。
            ecl = checklist.evidence_checklist(
                (st.risk_event or {}).get("signal_types", []))
            collected = {e.fact_type: e for e in self.ledger.all()}
            declared_gaps = {g["fact_type"] for g in st.evidence_gaps}
            for item in ecl.items:
                if (ev := collected.get(item.target)) is not None:
                    ecl.mark(item.item_id, checklist.COLLECTED,
                             f"已取得，证据等级{ev.level}", [ev.evidence_id])
                elif item.target in declared_gaps:
                    ecl.mark(item.item_id, checklist.GAP, "已登记为显式证据缺口")
                else:
                    # 未取到也未登记为缺口——这才是真正危险的一类，必须补登记
                    gev = self.ledger.record_gap(
                        subject_id=sid, fact_type=item.target,
                        why=f"取证清单要求但本轮未取到：{item.why}")
                    st.evidence_gaps.append({"evidence_id": gev.evidence_id,
                                             "fact_type": item.target, "why": item.why,
                                             "source": "checklist"})
                    ecl.mark(item.item_id, checklist.GAP, "清单核对时发现遗漏，已补登记")
            st.evidence_checklist = ecl
            ctx.log("INFO", "evidence_checklist.checked", **ecl.to_dict())

        st.risk_event = {**(st.risk_event or {}), "facts": {
            "credit": credit, "litigation": jud["litigation"],
            "registration": jud["registration"], "transaction": txn,
            "guarantee": gua,
        }}

    # ---- Worker：RiskAnalyst -----------------------------------------
    def _run_risk_analyst(self, st: Any) -> None:
        ctx = self.ctx.as_("risk-analyst")
        facts = (st.risk_event or {}).get("facts", {})
        signals = (st.risk_event or {}).get("signal_types", [])
        with self.tracer.span("agent", "risk-analyst", caller="risk-analyst"):
            # 检索前必经改写：立场 PROVE，找构成要件与认定标准
            plan = skills.query_rewrite(ctx, "涉诉 资金用途 五级分类 偿债能力",
                                        querying.PROVE, signals, facts)
            policy = skills.policy_rag(ctx, plan, top_k=3)
            cases = skills.case_memory(ctx, signals)
            st.assertion = skills.risk_root_cause(ctx, facts, policy, cases)
            st.query_plans.append(plan.to_dict())

    # ---- Worker：DevilsAdvocate ---------------------------------------
    def _run_devils_advocate(self, st: Any) -> None:
        """与 RiskAnalyst 输入完全相同、上下文完全独立、目标函数刻意对立。

        质疑清单在这里从「提示技巧」变成「完成性契约」：清单由系统从定性方的
        断言派生（不由模型自报），注入提示词告知完成标准，**模型返回后由代码逐项核对**。
        未覆盖项不是「默认通过」，而是让质疑判定为未完成 —— 见 :meth:`_adjudicate`。
        """
        ctx = self.ctx.as_("devils-advocate")
        facts = (st.risk_event or {}).get("facts", {})
        signals = (st.risk_event or {}).get("signal_types", [])

        # 清单由系统派生。这一步不经过模型，因此模型无法通过少报主因来减轻自己的工作量
        cl = checklist.rebuttal_checklist(st.assertion or {})
        st.rebuttal_checklist = cl

        with self.tracer.span("agent", "devils-advocate", caller="devils-advocate"):
            # 立场 REFUTE：除了常规检索，还会触发否定式维，主动找除外与豁免条款
            plan = skills.query_rewrite(ctx, "涉诉认定口径 资金异常 波动区间 担保代偿",
                                        querying.REFUTE, signals, facts)
            policy = skills.policy_rag(ctx, plan, top_k=3)
            cases = skills.case_memory(ctx, signals)
            st.query_plans.append(plan.to_dict())

            payload, manifest = (
                context.ContextAssembler()
                .add_evidence_refs("facts", [facts], priority=10, required=True)
                .add("assertion", st.assertion, priority=15, required=True)
                .add("checklist", [i.to_dict() for i in cl.items],
                     priority=5, required=True)          # 清单必进，永不被裁剪
                .add_knowledge_chunks("policy_context", policy, priority=30)
                .add_knowledge_chunks("similar_cases", cases, text_field="lesson", priority=60)
                .build()
            )
            payload["facts"] = payload["facts"][0]

            system, prompt_version = prompts.render(
                "devils_advocate",
                item_count=len(cl.items),
                checklist=cl.as_prompt_block(),
                policy_note=skills._policy_note(ctx, policy),
            )

            from .tracing import GEN_AI_USAGE_IN, GEN_AI_USAGE_OUT
            with self.tracer.span("llm", "devils_advocate") as span:
                span.attributes.update({
                    **self.llm.span_attrs(ctx.caller),
                    "prompt.name": "devils_advocate", "prompt.version": prompt_version,
                    "context.manifest": manifest.to_dict(),
                })
                try:
                    result, tin, tout = self.llm.complete_json(
                        "devils_advocate", system, payload, caller=ctx.caller,
                        validator=schemas.checklist_validator(
                            [i.item_id for i in cl.items], [i.target for i in cl.items]))
                    span.attributes.update({GEN_AI_USAGE_IN: tin, GEN_AI_USAGE_OUT: tout})
                except InferenceError as e:
                    # 失败策略（docs/接口与实验方案.md §1.5）：质疑器失效时
                    # fail-safe 的方向是**阻断**而不是放行。质疑器坏了就当作
                    # 「无法完成质疑」，案件不得进入处置——与 RiskGate 的
                    # fail-safe 是同一条原则在不同层的体现。
                    span.status = "ERROR"
                    span.attributes.update(self.llm.record_degradation(
                        caller=ctx.caller, task="devils_advocate", err=e,
                        fallback="verdict=INSUFFICIENT_EVIDENCE 并阻断"))
                    ctx.log("ERROR", "devils_advocate.degraded", reason=e.reason,
                            attempts=e.attempts, schema_errors=e.errors[:6])
                    result = {
                        "verdict": "INSUFFICIENT_EVIDENCE",
                        "rebuttals": [], "attempted_but_failed": [],
                        "evidence_gaps": [f"质疑推理未能产出合规输出：{e.reason}"],
                        "surviving_causes": [],
                        "checklist_resolutions": [],
                        "summary": "质疑环节失效，按 fail-safe 判定为质疑未完成，阻断进入处置",
                        "degraded": True,
                    }

            # 逐项核对：模型自称处理了什么不算数，以清单为准。
            # 回执用 item_id 还是 target 对齐由模型决定——提示词给的是 item_id，
            # 但模型（尤其是中小档位）经常只回目标名。两种都接受、都对不上才算漏项，
            # 因为这里要判的是「有没有真的处理」，不是「有没有按格式称呼它」。
            for res in result.get("checklist_resolutions", []):
                self._apply_resolution(cl, res, ctx)

            with self.tracer.span("adjudication", "质疑清单核对",
                                  coverage=cl.coverage(),
                                  complete=cl.complete(),
                                  unaddressed=[i.target for i in cl.unaddressed()]):
                ctx.log("INFO", "rebuttal_checklist.checked", **cl.to_dict())

            ctx.log("INFO", "devils_advocate.done", verdict=result["verdict"],
                    rebuttals=len(result.get("rebuttals", [])),
                    attempted=len(result.get("attempted_but_failed", [])),
                    checklist_coverage=cl.coverage())
        st.rebuttal = result

    @staticmethod
    def _apply_resolution(cl: Any, res: Any, ctx: Any) -> bool:
        """把一条质疑回执对齐到清单项。先按 item_id，再按 target。

        对不上不是静默丢弃，而是记一条 WARN 并让该清单项保持未处理——
        未处理会拉低覆盖率，进而由 :meth:`_adjudicate` 阻断。
        这条路径存在的意义就是：**模型答非所问时，系统的表现必须与它沉默时一致。**
        """
        if not isinstance(res, dict):
            ctx.log("WARN", "rebuttal_checklist.bad_resolution", resolution=str(res)[:120])
            return False
        status = res.get("status")
        resolution = res.get("resolution", "")
        evidence_ids = res.get("evidence_ids", []) or []
        if status not in checklist.ADDRESSED:
            ctx.log("WARN", "rebuttal_checklist.bad_status", status=str(status),
                    item_id=res.get("item_id"), target=res.get("target"))
            return False
        if item_id := res.get("item_id"):
            try:
                cl.mark(item_id, status, resolution, evidence_ids)
                return True
            except checklist.ChecklistError:
                pass  # 编号对不上，退回按目标名匹配
        if target := res.get("target"):
            if cl.mark_by_target(target, status, resolution, evidence_ids):
                return True
        ctx.log("WARN", "rebuttal_checklist.unmatched_resolution",
                item_id=res.get("item_id"), target=res.get("target"))
        return False

    # ---- Commander：裁决 ----------------------------------------------
    def _adjudicate(self, st: Any) -> None:
        # 质疑清单的覆盖率参与裁决：未覆盖的主因等于没被质疑过，
        # 不能当作「质疑通过」放行——这是 fail-safe，不是 fail-open
        cl = getattr(st, "rebuttal_checklist", None)
        result = routing.adjudicate(st.assertion, st.rebuttal,
                                    rebuttal_checklist=cl.to_dict() if cl else None)
        with self.tracer.span(
            "adjudication", "定性 ⇄ 质疑",
            verdict=result["verdict"], basis=result["basis"],
            rebuttal_coverage=result.get("rebuttal_coverage"),
            dissent=f"analyst={result['dissent']['analyst_conclusion']} "
                    f"advocate={result['dissent']['advocate_verdict']}",
        ):
            self.ctx.log("INFO", "adjudication.done", **{
                k: v for k, v in result.items() if k != "dissent"})
        st.adjudication = result

    # ---- Commander：闸门定级与方案生成 ----------------------------------
    def _gate_and_plan(self, st: Any) -> None:
        """按裁决结论选择处置动作，并逐个动作过 RiskGate 定级，取最高档。"""
        adj = st.adjudication or {}
        grade = adj.get("final_grade")
        causes = {c["type"] for c in (st.assertion or {}).get("root_causes", [])}
        surviving = set(adj.get("surviving_causes", []))
        # 风险未被确认时不存在存活主因，一律落到「维持原状 + 加强监测」
        active: set[str] = set()
        if adj.get("verdict") == "RISK_CONFIRMED":
            active = (causes & surviving) if surviving else causes

        actions: list[dict[str, Any]] = []
        if active & {"偿债能力恶化", "资金用途异常", "担保圈代偿风险"}:
            new_limit = round((st.exposure or {}).get("total_exposure", 0) * 0.7 / 10000) * 10000
            actions.append({"action": "reduce_limit",
                            "params": {"subject_id": st.subject["subject_id"],
                                       "new_limit": new_limit},
                            "label": "授信额度压降 30%"})
        if active & {"实际控制人风险", "偿债能力恶化", "担保圈代偿风险"}:
            # 担保圈代偿的对症动作是切断传染路径，不是再加一层同源担保
            gtype = ("担保置换或追加反担保" if "担保圈代偿风险" in active
                     else "实控人连带责任担保")
            actions.append({"action": "add_guarantee",
                            "params": {"subject_id": st.subject["subject_id"],
                                       "guarantee_type": gtype},
                            "label": f"追加{gtype}"})
        if not actions:
            actions.append({"action": "monitor_only",
                            "params": {"subject_id": st.subject["subject_id"]},
                            "label": "维持原状 + 加强监测"})

        # 证据等级取账本中的最高等级——有强证据才允许更高层级的动作
        levels = {e.level for e in self.ledger.all()}
        evidence_level = "强" if "强" in levels else ("弱" if "弱" in levels else "缺失")

        gated: list[dict[str, Any]] = []
        for a in actions:
            g = skills.risk_gate(
                self.ctx,
                case_id=st.case_id,
                action_type=a["action"],
                risk_grade=grade or st.subject.get("current_grade"),
                evidence_level=evidence_level,
                exposure_amount=(st.exposure or {}).get("total_exposure"),
                params=a["params"],
            )
            gated.append({**a, **g})

        # 整体层级取所有动作中的最高档：一个动作需要审批，整批就走审批
        order = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
        top = max(gated, key=lambda x: order[x["action_tier"]])
        st.gate = {**top, "actions": gated}

        # 处置意见书在执行前生成——先有可复核的书面方案，再谈执行
        report = skills.report_compose(self.ctx, "处置意见书", st, {"actions": gated})
        st.gate["opinion_report"] = report

    # ---- Commander：审批 ------------------------------------------------
    def _request_approval(self, st: Any) -> None:
        """在 Matrix 房间内 @风险经理并等待审批回调。

        PoC 中按 ``--auto-approve`` 模拟审批结果；真实部署时此处阻塞等待
        房间内的人工回复或工单系统回调，审批令牌由审批方签发并验签。
        """
        with self.tracer.span("approval", "等待人工审批",
                              approver_roles=st.gate.get("approver_roles"),
                              action_tier=st.gate.get("action_tier")):
            if self.approval_handler is not None:
                # 阻塞等待外部决策（工作台点击 / 工单系统回调）。
                # 真实部署时此处等的是审批方签发并验签的令牌
                decision = self.approval_handler({
                    "case_id": st.case_id,
                    "action_tier": st.gate.get("action_tier"),
                    "approver_roles": st.gate.get("approver_roles"),
                    "actions": st.gate.get("actions", []),
                    "rule_id": st.gate.get("rule_id"),
                    "reason": st.gate.get("reason"),
                })
                approved = bool(decision.get("approved"))
                approver = decision.get("approver") or "风险经理-赵"
                note = decision.get("reason") or ""
            else:
                approved, approver, note = self.auto_approve, "风险经理-赵", \
                    "要求先补充最近一期财务报表后再议"

            if approved:
                token = f"apv-{st.case_id}-{st.gate['idempotency_key'][-6:]}"
                st.approval = {"token": token, "approver": approver,
                               "decision": "APPROVED", "reason": note,
                               "channel": "Matrix room @risk-manager"}
                self.ctx.log("INFO", "approval.granted", approver=approver, token=token)
            else:
                st.approval = {"token": None, "approver": approver,
                               "decision": "REJECTED",
                               "reason": note or "审批未通过"}
                self.ctx.log("WARN", "approval.rejected", approver=approver, reason=note)
                # 审批被拒即降级为只读，不执行任何处置动作
                st.gate = {**st.gate, "action_tier": "L0",
                           "reason": "审批被拒，降级为只读诊断"}

    # ---- Worker：DispositionExecutor -------------------------------------
    def _run_disposition_executor(self, st: Any) -> None:
        ctx = self.ctx.as_("disposition-executor")
        results = []
        with self.tracer.span("agent", "disposition-executor", caller="disposition-executor"):
            for a in st.gate.get("actions", []):
                order = {
                    "action": a["action"],
                    "params": a["params"],
                    "action_tier": st.gate["action_tier"],
                    "idempotency_key": a["idempotency_key"],
                    "rollback_point_id": None,
                    "approval_token": (st.approval or {}).get("token"),
                }
                results.append({"action": a["action"], "label": a["label"],
                                **skills.safe_disposition(ctx, order)})
        ok = all(r["status"] in ("SUCCESS", "NO_ACTION", "PLAN_ONLY") for r in results)
        st.execution = {"status": "SUCCESS" if ok else "PARTIAL", "results": results}

    # ---- Worker：ComplianceAuditor ---------------------------------------
    def _run_compliance_auditor(self, st: Any) -> None:
        ctx = self.ctx.as_("compliance-auditor")
        with self.tracer.span("agent", "compliance-auditor", caller="compliance-auditor"):
            compliance = skills.compliance_check(ctx, st, self.tracer)
            distilled = skills.postmortem_distill(ctx, st)
            report = skills.report_compose(ctx, "审计报告", st,
                                           {"compliance": compliance, "distilled": distilled})
        st.audit = {"compliance": compliance, "distilled": distilled, "report": report}
