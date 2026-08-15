#!/usr/bin/env python3
"""由 Skill 注册表生成 CreditSkill Spec 三件套。

    python tools/gen_skill_docs.py

为 14 个 Skill 各生成：
    skills/<name>/SKILL.md      附录 B 全字段 + 两个金融特色字段
    skills/<name>/schema.json   输入 / 输出 JSON Schema（运行时强校验用）
    skills/<name>/eval/         回归评估集 + 覆盖度清单

真源是 ``poc/creditsentry/skills.py`` 中的 ``@skill`` 元数据与
``poc/creditsentry/permissions.py`` 中的权限矩阵，因此文档不会与实现漂移。

关于 eval 覆盖度：``eval/manifest.json`` 如实记录**声明的目标用例数**与
**当前已实现数**。我们不假装 14 个 Skill 的评估集都已完备——初赛阶段
RiskGate 等安全核心已做到全组合覆盖，其余为种子集，差额在 manifest 中明示。
"""

from __future__ import annotations

import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from poc.creditsentry import gate, skills  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "skills")

S = {"type": "string"}
N = {"type": "number"}
B = {"type": "boolean"}
A = lambda items: {"type": "array", "items": items}  # noqa: E731
O = lambda props, req=(): {  # noqa: E731, E741
    "type": "object", "properties": props, "required": list(req)}

EV_ID = {"type": "string", "pattern": "^EV-\\d+-\\d{4}$"}
LEVEL = {"enum": ["强", "弱", "缺失"]}

# 真实的输入 / 输出 Schema，逐个对齐 poc/creditsentry/skills.py 中的函数实现。
SCHEMAS: dict[str, dict] = {
    "SignalFusion": {
        "input": O({"signals": A(O({"alert_id": S, "source": S, "type": S,
                                    "ts": S, "detail": S},
                                   ("alert_id", "source", "type", "ts", "detail")))},
                   ("signals",)),
        "output": O({"event_id": S, "signal_types": A(S),
                     "kept": A({"type": "object"}),
                     "dropped": A(O({"alert_id": S, "drop_reason": S}, ("drop_reason",))),
                     "input_count": {"type": "integer"},
                     "denoise_rate": {"type": "number", "minimum": 0, "maximum": 1},
                     "first_seen": {"type": ["string", "null"]}},
                    ("event_id", "signal_types", "dropped", "denoise_rate")),
    },
    "ExposureMapping": {
        "input": O({"subject_id": S, "depth": {"type": "integer", "minimum": 1, "maximum": 3}},
                   ("subject_id",)),
        "output": O({"total_exposure": N,
                     "related_subjects": A(O({"subject_id": S, "name": S,
                                              "relation": S, "amount": N, "depth": {"type": "integer"}})),
                     "guarantee_ring": A(S),
                     "contagion_amount": N,
                     "truncated_at_depth": {"type": ["integer", "null"]}},
                    ("total_exposure", "related_subjects", "contagion_amount")),
    },
    "RiskRootCause": {
        "input": O({"facts": {"type": "object"},
                    "policy_context": A({"type": "object"}),
                    "cases": A({"type": "object"})},
                   ("facts",)),
        "output": O({"conclusion": {"enum": ["RISK_CONFIRMED", "INSUFFICIENT"]},
                     "root_causes": A(O({"type": S,
                                         "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                         "evidence_ids": {**A(EV_ID), "minItems": 1},
                                         "rationale": S},
                                        ("type", "confidence", "evidence_ids", "rationale"))),
                     "suggested_grade": {"enum": ["正常", "关注", "次级", "可疑", "损失", None]},
                     "summary": S},
                    ("conclusion", "root_causes", "summary")),
        "note": "root_causes[].evidence_ids 的 minItems=1 是「无证据不决策」的 Schema 级执行点",
    },
    "CreditReportProbe": {
        "input": O({"subject_id": S, "authorization_id": S}, ("subject_id", "authorization_id")),
        "output": O({"report": {"type": "object"}, "diff": {"type": "object"},
                     "evidence_ids": A(EV_ID)},
                    ("report", "diff", "evidence_ids")),
    },
    "LitigationProbe": {
        "input": O({"subject_name": S, "subject_id": S}, ("subject_name", "subject_id")),
        "output": O({
            "litigation": O({"cases": A(O({"case_no": S, "cause": S, "amount": N,
                                           "amount_ratio": N, "closed": B, "our_role": S})),
                             "total_amount_ratio": N,
                             "ambiguous": B, "partial": B,
                             "evidence_ids": A(EV_ID)}),
            "registration": O({"legal_rep_changed": B, "change_overlaps_risk_window": B,
                               "equity_changed": B, "evidence_ids": A(EV_ID)}),
        }, ("litigation", "registration")),
    },
    "TxnFlowAnalyze": {
        "input": O({"subject_id": S, "account_ids": A(S)}, ("subject_id", "account_ids")),
        "output": O({"anomaly_detected": B, "anomalies": A(S),
                     "counterparty_related_party": B, "within_baseline_band": B,
                     "baseline_band": S, "coverage": N, "undersampled": B,
                     "evidence_ids": A(EV_ID)},
                    ("anomaly_detected", "anomalies", "coverage", "evidence_ids")),
    },
    "QueryRewrite": {
        "input": O({"base_query": S, "stance": {"enum": ["PROVE", "REFUTE"]},
                    "signal_types": A(S), "facts": O({}, ()), "as_of": S},
                   ("base_query", "stance")),
        "output": O({
            "caller": S, "stance": S, "as_of": S, "dimensions_used": A(S),
            "subqueries": A(O({"dimension": {"enum": list(
                ["stance", "terminology", "signal_topic",
                 "clause_ref", "negation", "recency"])},
                "text": S, "why": S, "weight": N, "filters": O({}, ())},
                ("dimension", "text", "why"))),
            "clarifications": A(O({"clarification_id": S, "question": S, "reason": S,
                                   "channel": {"enum": ["AUTO", "SYSTEM_TASK",
                                                        "HUMAN_CHOICE"]},
                                   "options": A(S), "blocking": B},
                                  ("clarification_id", "question", "channel"))),
        }, ("stance", "dimensions_used", "subqueries", "clarifications")),
    },
    "GuaranteeProbe": {
        "input": O({"subject_id": S, "direct_exposure": N}, ("subject_id",)),
        "output": O({"has_guarantee": B, "total_guarantee": N,
                     "distressed_guarantee": N,
                     "distressed_parties": A(O({"party": S, "outstanding": N,
                                                "status": S, "status_basis": S,
                                                "mitigation_amount": N},
                                               ("party", "outstanding", "status"))),
                     "mitigation_amount": N, "mitigation_coverage": N,
                     "uncovered_amount": N, "uncovered_multiple": N,
                     "direct_exposure": N, "evidence_ids": A(EV_ID)},
                    ("has_guarantee", "distressed_guarantee", "evidence_ids")),
    },
    "RiskGate": {
        "input": O({"case_id": S,
                    "action_type": {"enum": sorted(gate.ACTION_CATALOG)},
                    "risk_grade": {"type": ["string", "null"]},
                    "evidence_level": {"anyOf": [LEVEL, {"type": "null"}]},
                    "exposure_amount": {"type": ["number", "null"]},
                    "params": {"type": "object"}},
                   ("case_id", "action_type")),
        "output": O({"action": S, "action_label": S,
                     "action_tier": {"enum": ["L0", "L1", "L2", "L3"]},
                     "needs_approval": B, "approver_roles": A(S),
                     "idempotency_key": S,
                     "rollback_point": {"type": ["string", "null"]},
                     "reversible": B,
                     "rule_id": {"type": "string", "pattern": "^G-\\d{2}$"},
                     "reason": S},
                    ("action_tier", "needs_approval", "idempotency_key", "rule_id", "reason")),
        "note": "入参可为 null 是刻意的——缺失时由 G-02 fail-safe 降级为 L3，而非拒绝调用",
    },
    "ComplianceCheck": {
        "input": O({"case_id": S, "trace_id": S, "rule_set_version": S}, ("case_id",)),
        "output": O({"items": A(O({"rule_id": S, "source": S,
                                   "result": {"enum": ["PASS", "FAIL", "N/A"]},
                                   "detail": S},
                                  ("rule_id", "source", "result", "detail"))),
                     "passed": {"type": "integer"}, "failed": {"type": "integer"},
                     "remediation": A(S)},
                    ("items", "passed", "failed")),
    },
    "SafeDisposition": {
        "input": O({"action": {"enum": sorted(gate.ACTION_CATALOG)},
                    "params": {"type": "object"},
                    "action_tier": {"enum": ["L0", "L1", "L2", "L3"]},
                    "idempotency_key": S,
                    "rollback_point_id": {"type": ["string", "null"]},
                    "approval_token": {"type": ["string", "null"]}},
                   ("action", "params", "action_tier", "idempotency_key")),
        "output": O({"status": {"enum": ["SUCCESS", "NO_ACTION", "PLAN_ONLY",
                                         "ROLLED_BACK", "FROZEN_ESCALATED", "FAILED"]},
                     "audit_serial": S, "rollback_point_id": S,
                     "effective_at": S, "idempotent_replay": B,
                     "error_code": {"enum": ["APPROVAL_REQUIRED", "APPROVAL_INVALID",
                                             "PERMISSION_DENIED", "ROLLBACK_POINT_NOT_FOUND",
                                             "RATE_LIMITED", "UNKNOWN"]},
                     "error": S},
                    ("status",)),
        "note": "action_tier=L3 时必定返回 PLAN_ONLY——不可逆动作永不执行",
    },
    "PostmortemDistill": {
        "input": O({"case_id": S, "final_adjudication": {"type": "object"},
                    "execution_result": {"type": "object"}}, ("case_id",)),
        "output": O({"risk_pattern": O({"pattern_id": S, "description": S, "trigger": S,
                                        "key_points": S, "counter_example": S, "boundary": S},
                                       ("pattern_id", "description", "trigger")),
                     "written_to": S},
                    ("risk_pattern", "written_to")),
    },
    "EvidenceLedger": {
        "input": O({"subject_id": S, "source_system": S, "fact_type": S,
                    "raw_content": S, "extracted": {"type": "object"},
                    "collected_at": S, "level": LEVEL, "level_reason": S},
                   ("subject_id", "source_system", "fact_type", "raw_content", "extracted")),
        "output": O({"evidence_id": EV_ID, "subject_id": S, "source_system": S,
                     "fact_type": S, "snapshot_uri": S,
                     "content_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                     "collected_at": S, "level": LEVEL, "level_reason": S,
                     "extracted": {"type": "object"}},
                    ("evidence_id", "snapshot_uri", "content_hash", "level", "level_reason")),
        "note": "content_hash 由原文计算，账本 append-only，改写会产生不同 evidence_id",
    },
    "PolicyRag": {
        "input": O({"query": S, "top_k": {"type": "integer", "minimum": 1}},
                   ("query",)),
        "output": A(O({"doc_id": S, "title": S, "source": S, "effective_date": S,
                       "text": S, "similarity": N, "evidence_id": EV_ID})),
        "note": "召回即证据化：每条召回结果都带 evidence_id，使结论能点回条款原文与生效日期",
    },
    "CaseMemory": {
        "input": O({"signal_types": A(S), "top_k": {"type": "integer", "minimum": 1}},
                   ("signal_types",)),
        "output": A(O({"case_id": S, "summary": S, "signal_types": A(S),
                       "conclusion": S, "action": S, "verified_outcome": S,
                       "lesson": S, "similarity": N})),
        "note": "无相似案例时返回空数组，不用低相似度结果凑数",
    },
    "ReportCompose": {
        "input": O({"template_id": {"enum": ["处置意见书", "审计报告"]},
                    "case_state": {"type": "object"},
                    "extra": {"type": "object"}},
                   ("template_id", "case_state")),
        "output": O({"template_id": S, "markdown": S, "archive_uri": S},
                    ("template_id", "markdown", "archive_uri")),
        "note": "正文中出现的每个 EV- 编号都会被校验存在于账本，否则拒绝生成",
    },
}

# 声明的目标用例数（与 SKILL.md 的「回归评估集」字段对应）
DECLARED = {
    "SignalFusion": 30, "ExposureMapping": 12, "RiskRootCause": 40,
    "CreditReportProbe": 20, "LitigationProbe": 25, "TxnFlowAnalyze": 18,
    "GuaranteeProbe": 10, "QueryRewrite": 18,
    "RiskGate": 0,  # 全组合遍历，数量由程序算出
    "ComplianceCheck": 24, "SafeDisposition": 16, "PostmortemDistill": 10,
    "EvidenceLedger": 14, "PolicyRag": 22, "CaseMemory": 15, "ReportCompose": 8,
}


def gen_riskgate_eval() -> list[dict]:
    """RiskGate 的评估集是**程序全组合生成**的，不是手写样例。

    安全核心的正确性不能靠抽样验证——这里穷举动作 × 风险等级 × 证据等级 × 敞口，
    每条用例的期望值由不变量推导（而非由当前实现反推），因此实现回归时会被抓住。
    """
    cases = []
    grades = ["正常", "关注", "次级", None]
    levels = ["强", "弱", "缺失", None]
    amounts = [0, 1, 4_999_999, 5_000_000, 20_000_000, None]
    for action, grade, level, amount in itertools.product(
            sorted(gate.ACTION_CATALOG), grades, levels, amounts):
        spec = gate.ACTION_CATALOG[action]
        # 期望值由安全不变量推导：
        if not spec.reversible:
            expect = "L3"                      # 不可逆恒 L3
        elif None in (grade, level, amount):
            expect = "L3"                      # 入参缺失 fail-safe
        elif level == "缺失":
            expect = "L0"                      # 证据缺失只读
        else:
            expect = None                      # 其余由分级规则决定，不在此断言
        cases.append({
            "input": {"case_id": "EVAL", "action_type": action, "risk_grade": grade,
                      "evidence_level": level, "exposure_amount": amount},
            "expect_tier": expect,
            "invariant": ("irreversible-never-auto" if not spec.reversible else
                          "fail-safe-on-missing-input" if expect == "L3" else
                          "no-evidence-read-only" if expect == "L0" else "tiering-rules"),
        })
    # 白名单外动作
    cases.append({
        "input": {"case_id": "EVAL", "action_type": "delete_customer",
                  "risk_grade": "次级", "evidence_level": "强", "exposure_amount": 1000},
        "expect_tier": "L3", "invariant": "unknown-action-denied",
    })
    return cases


EVAL_SEEDS: dict[str, list[dict]] = {
    "SignalFusion": [
        {"desc": "重复推送应被归并且记录原因",
         "input": {"signals": [
             {"alert_id": "A1", "source": "预警", "type": "judicial_new_case",
              "ts": "2026-08-14T09:00:00", "detail": "新增涉诉 2 条"},
             {"alert_id": "A2", "source": "预警", "type": "judicial_new_case",
              "ts": "2026-08-14T09:01:00", "detail": "新增涉诉 2 条（重复推送）"}]},
         "expect": {"kept_count": 1, "dropped_count": 1, "all_drops_have_reason": True}},
        {"desc": "例行提醒与无实质舆情应被压降",
         "input": {"signals": [
             {"alert_id": "B1", "source": "预警", "type": "rating_periodic",
              "ts": "2026-08-14T09:00:00", "detail": "季度评级例行复核提醒"},
             {"alert_id": "B2", "source": "舆情", "type": "media_mention",
              "ts": "2026-08-14T09:02:00", "detail": "行业名单提及"}]},
         "expect": {"kept_count": 0, "dropped_count": 2, "all_drops_have_reason": True}},
        {"desc": "高危信号不得被丢弃",
         "input": {"signals": [
             {"alert_id": "C1", "source": "预警", "type": "judicial_new_case",
              "ts": "2026-08-14T09:00:00", "detail": "新增涉诉 3 条"},
             {"alert_id": "C2", "source": "资金", "type": "txn_concentrated_outflow",
              "ts": "2026-08-14T09:05:00", "detail": "集中转出 480 万"}]},
         "expect": {"kept_count": 2, "dropped_count": 0, "all_drops_have_reason": True}},
    ],
    "EvidenceLedger": [
        {"desc": "权威来源 + 原文可溯源 → 强证据",
         "input": {"source_system": "judicial-mcp", "extracted": {"source_doc_uri": "s3://x"}},
         "expect": {"level": "强"}},
        {"desc": "采样不足 → 降为弱证据",
         "input": {"source_system": "txn-mcp", "extracted": {"undersampled": True}},
         "expect": {"level": "弱"}},
        {"desc": "重名未消歧 → 降为弱证据",
         "input": {"source_system": "judicial-mcp", "extracted": {"ambiguous": True}},
         "expect": {"level": "弱"}},
        {"desc": "应有而未取到 → 缺失证据",
         "input": {"source_system": "-", "extracted": {"gap": True, "why": "未取到"}},
         "expect": {"level": "缺失"}},
    ],
    "LitigationProbe": [
        {"desc": "小额已结案且我方为原告 → 不具实质性",
         "input": {"amount_ratio": 0.005, "closed": True, "our_role": "原告"},
         "expect": {"material": False}},
        {"desc": "未结案、我方为被告、占敞口 15% → 具实质性",
         "input": {"amount_ratio": 0.155, "closed": False, "our_role": "被告"},
         "expect": {"material": True}},
        {"desc": "未结案但占敞口不足 5% → 不具实质性",
         "input": {"amount_ratio": 0.048, "closed": False, "our_role": "被告"},
         "expect": {"material": False}},
    ],
    # 时效陷阱：2025-06-01 生效的行内制度，在 2017 年的案子里根本不存在。
    # 这类污染比证据层的前视更隐蔽，因为条款看起来「一直都在」。
    "PolicyRag": [
        {"desc": "当期案件：正常召回，不做时效过滤",
         "input": {"query": "涉诉 认定 口径", "top_k": 5},
         "expect": {"min_hits": 1}},
        {"desc": "时效陷阱：2017 年案件不得召回 2025 年生效的行内制度",
         "input": {"query": "涉诉 认定 口径", "as_of": "2017-04-01", "top_k": 5},
         "expect": {"must_exclude": "POL-007"}},
        {"desc": "时效陷阱：2017 年案件不得召回 2018 年生效的大额风险暴露办法",
         "input": {"query": "关联客户 大额风险暴露", "as_of": "2017-04-01", "top_k": 5},
         "expect": {"must_exclude": "POL-005"}},
        {"desc": "2019 年案件可以召回 2018 年生效的条款",
         "input": {"query": "关联客户 大额风险暴露", "as_of": "2019-01-01", "top_k": 5},
         "expect": {"must_include": "POL-005"}},
        {"desc": "老条款在任何时点都可召回",
         "input": {"query": "五级分类 关注 次级", "as_of": "2017-04-01", "top_k": 5},
         "expect": {"must_include": "POL-001"}},
        {"desc": "全部候选都晚于案件时点时返回空集并说明原因，不降级放行",
         "input": {"query": "涉诉 认定 口径 资金 异常", "as_of": "2001-01-01", "top_k": 5},
         "expect": {"empty_with_reason": True}},
    ],
    # 六个维度各自的**触发与不触发**条件都要测。只测触发会漏掉最典型的错误：
    # 求证方也产生了否定式子查询，等于两个角色又在做同一件事。
    "QueryRewrite": [
        {"desc": "求证方产生立场维，且**不得**产生否定式维",
         "input": {"stance": "PROVE", "base_query": "涉诉 偿债能力"},
         "expect": {"has_dimensions": ["stance"], "lacks_dimensions": ["negation"]}},
        {"desc": "证伪方必须产生否定式维——正向检索找不到除外条款",
         "input": {"stance": "REFUTE", "base_query": "涉诉 偿债能力"},
         "expect": {"has_dimensions": ["stance", "negation"]}},
        {"desc": "术语维：行内口语「抽贷」映射为监管术语",
         "input": {"stance": "PROVE", "base_query": "是否应当抽贷"},
         "expect": {"has_dimensions": ["terminology"], "text_contains": "提前收回贷款"}},
        {"desc": "术语维：「担保圈」映射到关联客户认定与大额风险暴露",
         "input": {"stance": "PROVE", "base_query": "担保圈风险"},
         "expect": {"has_dimensions": ["terminology"], "text_contains": "大额风险暴露"}},
        {"desc": "术语表未覆盖时不产生术语维，不硬凑",
         "input": {"stance": "PROVE", "base_query": "季度例行复核"},
         "expect": {"lacks_dimensions": ["terminology"]}},
        {"desc": "信号主题维：担保传染信号展开为或有负债主题",
         "input": {"stance": "PROVE", "base_query": "风险", "signal_types": ["guarantee_contagion"]},
         "expect": {"has_dimensions": ["signal_topic"], "text_contains": "或有负债"}},
        {"desc": "噪声类信号不产生检索主题",
         "input": {"stance": "PROVE", "base_query": "风险", "signal_types": ["media_mention"]},
         "expect": {"lacks_dimensions": ["signal_topic"]}},
        {"desc": "条款直查维：事实中出现条款引用则精确召回",
         "input": {"stance": "PROVE", "base_query": "风险",
                   "facts_blob": "依据《商业银行贷后管理指引》第十八条应持续监测"},
         "expect": {"has_dimensions": ["clause_ref"], "exact_filter": True}},
        {"desc": "时效维：给定案件时点时，全部子查询都带 effective_before",
         "input": {"stance": "PROVE", "base_query": "涉诉", "as_of": "2017-04-01"},
         "expect": {"has_dimensions": ["recency"], "all_filtered_before": "2017-04-01"}},
        {"desc": "当期案件无案件时点，不产生时效维",
         "input": {"stance": "PROVE", "base_query": "涉诉"},
         "expect": {"lacks_dimensions": ["recency"]}},
        {"desc": "澄清选路：主体重名 → 派系统任务，不问人",
         "input": {"stance": "PROVE", "base_query": "涉诉", "ambiguous": True},
         "expect": {"clarification_channel": "SYSTEM_TASK", "blocking": False}},
        {"desc": "澄清选路：检索仅部分返回 → 派系统任务补全",
         "input": {"stance": "PROVE", "base_query": "涉诉", "partial": True},
         "expect": {"clarification_channel": "SYSTEM_TASK", "blocking": False}},
        {"desc": "澄清选路：出险认定无公开定论 → 选项式问人且阻断",
         "input": {"stance": "PROVE", "base_query": "担保", "unbased_distress": True},
         "expect": {"clarification_channel": "HUMAN_CHOICE", "blocking": True,
                    "has_options": True}},
        {"desc": "澄清选路：流水采样不足 → 已有规则可自动处理，不打扰任何人",
         "input": {"stance": "PROVE", "base_query": "流水", "undersampled": True},
         "expect": {"clarification_channel": "AUTO", "blocking": False}},
    ],
    # 担保代偿的定级只看「净未覆盖敞口 / 直接敞口」。
    # 三组误报陷阱刻意放在覆盖率边界上：缓释足额时**根本不应进入主因列表**。
    "GuaranteeProbe": [
        {"desc": "被担保方未出险 → 不构成当期信号（担保余额再大也一样）",
         "input": {"outstanding": 2.9e9, "party_status": "正常",
                   "mitigations": 0, "direct_exposure": 6.5e8},
         "expect": {"distressed_guarantee": 0.0, "confidence": 0.0}},
        {"desc": "误报陷阱：缓释措施全额覆盖 → 净敞口为零，置信度 0",
         "input": {"outstanding": 1.0e9, "party_status": "已出险",
                   "mitigations": 1.0e9, "direct_exposure": 5.0e8},
         "expect": {"uncovered_amount": 0.0, "confidence": 0.0}},
        {"desc": "误报陷阱：缓释超额申报 → 覆盖额不得超过被缓释的敞口本身",
         "input": {"outstanding": 1.0e9, "party_status": "已出险",
                   "mitigations": 1.6e9, "direct_exposure": 5.0e8},
         "expect": {"mitigation_amount": 1.0e9, "uncovered_amount": 0.0,
                    "confidence": 0.0}},
        {"desc": "误报陷阱：净敞口极小 → 置信度低于 0.5，不足以单独定性",
         "input": {"outstanding": 1.0e9, "party_status": "已出险",
                   "mitigations": 9.8e8, "direct_exposure": 5.0e8},
         "expect": {"uncovered_amount": 2.0e7, "confidence": 0.45}},
        {"desc": "净未覆盖敞口达直接敞口 2 倍 → 置信度 0.65（关注类）",
         "input": {"outstanding": 2.9e9, "party_status": "已出险",
                   "mitigations": 1.584e9, "direct_exposure": 6.5e8},
         "expect": {"uncovered_multiple": 2.02, "confidence": 0.65}},
        {"desc": "净未覆盖敞口达直接敞口 3 倍以上 → 置信度封顶 0.75",
         "input": {"outstanding": 4.0e9, "party_status": "已出险",
                   "mitigations": 0.0, "direct_exposure": 5.0e8},
         "expect": {"uncovered_multiple": 8.0, "confidence": 0.75}},
        {"desc": "无对外担保 → 有效负向证据，不是取证失败",
         "input": {"empty": True, "direct_exposure": 5.0e8},
         "expect": {"has_guarantee": False, "distressed_guarantee": 0.0}},
    ],
    "SafeDisposition": [
        {"desc": "L3 动作必定 PLAN_ONLY，不执行",
         "input": {"action_tier": "L3", "action": "early_recall"},
         "expect": {"status": "PLAN_ONLY"}},
        {"desc": "L0 动作不触碰业务系统",
         "input": {"action_tier": "L0", "action": "monitor_only"},
         "expect": {"status": "NO_ACTION"}},
        {"desc": "L2 无审批令牌应被拒绝",
         "input": {"action_tier": "L2", "action": "reduce_limit", "approval_token": None},
         "expect": {"status": "FAILED", "error_code": "APPROVAL_REQUIRED"}},
        {"desc": "同一幂等键重复投递不重复执行",
         "input": {"action_tier": "L2", "action": "reduce_limit", "replay": True},
         "expect": {"idempotent_replay": True}},
    ],
}


def render_skill_md(meta, schema: dict, declared: int, implemented: int) -> str:
    rows = [
        ("Skill 名称", f"`{meta.name}`"),
        ("Skill 类型", f"自定义 Skill（{meta.tier} · {meta.category}类）"),
        ("版本", f"`{meta.version}`"),
        ("使用场景", meta.purpose),
        ("输入参数", f"`{meta.inputs}`"),
        ("输出结果", f"`{meta.outputs}`"),
        ("调用条件", meta.trigger),
        ("依赖工具 / 系统", meta.depends),
        ("失败处理", meta.failure_policy),
        ("权限与安全", meta.security),
        ("可调用的 Agent", "、".join(f"`{c}`" for c in meta.callers)),
        ("**绑定监管条款**", meta.regulation),
        ("**回归评估集**", meta.eval_set),
        ("复用价值", meta.reuse),
    ]
    if meta.reuses_official:
        rows.insert(8, ("复用官方云能力", meta.reuses_official))

    L = [f"# Skill · {meta.name}", "",
         "> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。",
         "> 真源是实现本身，因此本文档不会与代码漂移。", "",
         "| 字段 | 内容 |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in rows]

    L += ["", "## 接口 Schema", "",
          "完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。", ""]
    if schema.get("note"):
        L += [f"> **设计要点**：{schema['note']}", ""]

    L += ["## 回归评估集", "",
          f"- 声明覆盖目标：**{declared if declared else '全组合遍历'}** 组",
          f"- 当前已实现：**{implemented}** 组",
          ""]
    if declared and implemented < declared:
        L.append(f"> 初赛阶段为种子集，差额 {declared - implemented} 组在复赛补齐。"
                 f"我们如实记录覆盖度而非声称已完备——见 `eval/manifest.json`。")
    else:
        L.append("> 已达成声明的覆盖目标。")
    L += ["",
          "评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，",
          "异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。",
          "",
          "## 与多 Agent 协同流程的关系", "",
          f"本 Skill 由 {'、'.join(f'`{c}`' for c in meta.callers)} 调用。",
          "调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，",
          "越权调用在运行时被拒绝（见 `poc/test_safety.py`）。", ""]
    return "\n".join(L)


def main() -> int:
    manifest_all = {}
    count = 0
    for name, meta in skills.REGISTRY.items():
        d = os.path.join(OUT, name)
        os.makedirs(os.path.join(d, "eval"), exist_ok=True)
        schema = SCHEMAS[name]

        if name == "RiskGate":
            cases = gen_riskgate_eval()
        else:
            cases = EVAL_SEEDS.get(name, [])
        declared = DECLARED[name]
        implemented = len(cases)

        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(render_skill_md(meta, schema, declared, implemented))

        with open(os.path.join(d, "schema.json"), "w", encoding="utf-8") as f:
            json.dump({
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "title": name, "version": meta.version,
                "description": meta.purpose,
                "note": schema.get("note"),
                "input": schema["input"], "output": schema["output"],
            }, f, ensure_ascii=False, indent=2)

        with open(os.path.join(d, "eval", "cases.jsonl"), "w", encoding="utf-8") as f:
            for c in cases:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")

        m = {"skill": name, "version": meta.version,
             "declared_target": declared or "全组合遍历",
             "implemented": implemented,
             "gap": max(0, declared - implemented) if declared else 0,
             "status": "COMPLETE" if (not declared or implemented >= declared) else "SEED"}
        with open(os.path.join(d, "eval", "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
        manifest_all[name] = m
        count += 1
        print(f"  {name:20} schema✓  eval {implemented:>4} / {declared or '全组合':<6} {m['status']}")

    with open(os.path.join(OUT, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump({"skills": manifest_all,
                   "total": count,
                   "complete": len([m for m in manifest_all.values() if m["status"] == "COMPLETE"]),
                   "seed": len([m for m in manifest_all.values() if m["status"] == "SEED"])},
                  f, ensure_ascii=False, indent=2)

    print(f"\n共 {count} 个 Skill。覆盖度清单：skills/MANIFEST.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
