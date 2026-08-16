#!/usr/bin/env python3
"""风险处置工作台 —— 后端服务。

**这个工作台要解决的问题不是「让 Demo 好看」，而是让抽象的内控设计变成可操作的东西。**

命令行能证明系统跑得通，但证明不了三件事：一个风险经理**看不看得懂**它给的结论；
「L2 必须人工审批」在被演示时**是不是真的停下来等人**；以及「结论由证据驱动而非硬编码」
——这一条只有当着人的面改一个因子重跑翻案，才算说清楚。

因此本服务提供的不是一套 CRUD 接口，而是三种交互：

1. **实时旁听**（SSE）。案件在跑的过程中，Span 与结构化日志实时推送到浏览器。
   数据源就是落盘 trace.json 的那一份，只是时机不同——不是为可视化另造一套。
2. **阻塞式人工审批**。L2 动作会真的停在这里等浏览器点按钮，等的是同一个
   ``approval_handler``，真实部署时它等的是审批方签发并验签的令牌。
3. **因子调节**。按 ``mcp_servers/world.FACTORS`` 白名单改案件的决定性因子后重跑。
   可改字段是**被逐条说清楚的那几个**，不是任意改写案件数据。

零第三方依赖（``http.server`` + ``ThreadingHTTPServer``），与项目其余部分一致。

用法::

    python3 poc/serve.py                      # 默认 http://127.0.0.1:8090
    python3 poc/serve.py --port 9000 --open   # 起好后自动打开浏览器
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.registry import SERVER_TOOLS, MCPClient  # noqa: E402
from mcp_servers.world import FACTORS, MCPError, World  # noqa: E402
from poc.creditsentry import gate, humanize, modelconfig, permissions, routing  # noqa: E402
from poc.creditsentry.agents import Orchestrator  # noqa: E402
from poc.creditsentry.llm import get_llm  # noqa: E402
from poc.creditsentry.tracing import Tracer  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
OUT_ROOT = os.path.join(ROOT, "poc", "out")

#: 预警池里展示的案件。文案面向业务，不是 fixture 文件名
CASES: dict[str, dict[str, Any]] = {
    "CASE-001": {
        "fixture": "case_001",
        "title": "浙XX精密机械有限公司",
        "scene": "三类信号相互印证",
        "brief": "新增涉诉 3 笔、近 30 日集中转出 480 万、法定代表人变更",
        "expect": "风险成立 · L2 压降 30% + 追加担保（需审批）",
        "priority": "高",
    },
    "CASE-002": {
        "fixture": "case_002",
        "title": "苏XX电子科技有限公司",
        "scene": "信号类型完全相同，但个案层面均不具实质性",
        "brief": "同样是涉诉 + 集中转出 + 法代变更，差别只在个案细节",
        "expect": "风险不成立 · L0 维持原状 + 加强监测",
        "priority": "中",
    },
    "CASE-003": {
        "fixture": "case_003",
        "title": "山东某大型集团（真实历史回溯）",
        "scene": "担保圈风险传染 · 时点冻结在 2017-04-01",
        "brief": "直接敞口 6.5 亿，净未覆盖代偿敞口为直接敞口的 2.02 倍",
        "expect": "风险成立 · L3 只出方案，系统不执行",
        "priority": "高",
    },
}


# ===========================================================================
# 运行实例
# ===========================================================================

class Run:
    """一次案件处置的运行实例。

    事件是 **append-only 的序列**而不是可变状态：浏览器带着已收到的 seq 来订阅，
    断线重连后从断点续传即可。这与证据账本 append-only 是同一个理由——
    可回放的前提是没人能偷偷改历史。
    """

    def __init__(self, run_id: str, case_key: str, *, llm_mode: str, preset: str | None,
                 factor_choices: dict[str, int], approval_mode: str) -> None:
        self.id = run_id
        self.case_key = case_key
        self.llm_mode = llm_mode
        self.preset = preset
        self.factor_choices = factor_choices
        self.approval_mode = approval_mode      # ask / auto-approve / auto-reject
        self.status = "pending"                 # pending/running/awaiting_approval/done/failed
        self.error: str | None = None
        self.started_at = time.time()

        self._events: list[dict[str, Any]] = []
        self._cond = threading.Condition()
        self._seq = 0

        self.pending_approval: dict[str, Any] | None = None
        self._approval_q: "queue.Queue[dict]" = queue.Queue(maxsize=1)

        # 跑完后仍持有，供「越权调用演示」与产出下载使用
        self.orch: Orchestrator | None = None
        self.mcp: MCPClient | None = None
        self.tracer: Tracer | None = None
        self.state: Any = None
        # 取当前快照的闭包，由 execute() 装配。审批事件必须自带快照——
        # 闸门定级与审批发起之间既没有 Agent span 也没有阶段迁移，
        # 只靠既有事件的话，界面收到审批请求时 gate 还是空的，审批框根本渲染不出来
        self.snapshot_fn: Any = None

    # ---- 事件 -------------------------------------------------------
    def emit(self, kind: str, **payload: Any) -> None:
        with self._cond:
            self._seq += 1
            self._events.append({"seq": self._seq, "ts": time.time(),
                                 "kind": kind, **payload})
            self._cond.notify_all()

    def events_since(self, seq: int, timeout: float = 20.0) -> list[dict[str, Any]]:
        """取 seq 之后的事件；没有新事件则最多阻塞 timeout 秒。"""
        deadline = time.time() + timeout
        with self._cond:
            while True:
                fresh = [e for e in self._events if e["seq"] > seq]
                if fresh or self.status in ("done", "failed"):
                    return fresh
                remain = deadline - time.time()
                if remain <= 0:
                    return []
                self._cond.wait(remain)

    # ---- 审批 -------------------------------------------------------
    def approval_handler(self, request: dict[str, Any]) -> dict[str, Any]:
        """被编排层调用，**阻塞**直到拿到决策。

        自动模式仍然走同一条路径而不是绕开它——绕开的话，
        「演示时用的代码」和「跑真链路时用的代码」就不是同一份了。
        """
        if self.approval_mode == "auto-approve":
            return {"approved": True, "approver": "风险经理-赵（自动）"}
        if self.approval_mode == "auto-reject":
            return {"approved": False, "approver": "风险经理-赵（自动）",
                    "reason": "要求先补充最近一期财务报表后再议"}

        self.pending_approval = request
        self.status = "awaiting_approval"
        snap = None
        if self.snapshot_fn is not None:
            try:
                snap = self.snapshot_fn()
            except Exception:  # noqa: BLE001
                pass
        self.emit("approval_required", request=request, snapshot=snap)
        decision = self._approval_q.get()          # 阻塞等浏览器
        self.pending_approval = None
        self.status = "running"
        self.emit("approval_decided", decision=decision)
        return decision

    def submit_approval(self, decision: dict[str, Any]) -> bool:
        if self.status != "awaiting_approval":
            return False
        self._approval_q.put(decision)
        return True


# ===========================================================================
# 执行
# ===========================================================================

def _factor_overrides(case_key: str, choices: dict[str, int]) -> tuple[dict, list[dict]]:
    """把前端选择的因子档位翻译成字段补丁。"""
    fixture = CASES[case_key]["fixture"]
    catalog = {f["key"]: f for f in FACTORS.get(fixture, [])}
    patch: dict[str, Any] = {}
    applied: list[dict[str, Any]] = []
    for key, idx in (choices or {}).items():
        f = catalog.get(key)
        if f is None or not (0 <= int(idx) < len(f["options"])):
            continue
        opt = f["options"][int(idx)]
        patch.update(opt["patch"])
        applied.append({"key": key, "label": f["label"], "option": opt["name"],
                        "expect": opt["expect"], "changed": int(idx) != 0})
    return patch, applied


def execute(run: Run) -> None:
    """在后台线程里跑完整链路，全程把事件推给订阅者。"""
    try:
        run.status = "running"
        patch, applied = _factor_overrides(run.case_key, run.factor_choices)
        run.emit("run_started", case=run.case_key, llm_mode=run.llm_mode,
                 preset=run.preset, factors=applied)

        # 「single」是界面概念（只用一个平台），底层就是自定义端点
        cfg = modelconfig.resolve(
            "custom" if run.preset == "single" else run.preset, None)
        world = World.load(CASES[run.case_key]["fixture"], patch)
        tracer = Tracer()
        mcp = MCPClient(world, tracer)
        llm = get_llm(run.llm_mode, cfg=cfg)

        # Span 与日志的实时旁路。注意这是**同一份数据**的另一个时机，
        # 不是为可视化另开的一套记录
        def on_span(event: str, sp: Any) -> None:
            if event == "span_start":
                run.emit("span_start", span={"span_id": sp.span_id,
                                             "parent_id": sp.parent_id,
                                             "kind": sp.kind, "name": sp.name,
                                             "attributes": _jsonable(sp.attributes)})
                return
            run.emit("span_end", span_id=sp.span_id, status=sp.status,
                     duration_ms=sp.duration_ms, attributes=_jsonable(sp.attributes))
            # 每个 Worker 干完就推一次快照。只按阶段推的话，整个 EVIDENCE 阶段
            # （占了一半的工作量）在界面上是一片空白，直到阶段结束才一次性出现——
            # 看的人无法把「系统在干什么」和「结论怎么来的」对应起来
            if sp.kind == "agent":
                try:
                    cur = orch.store.read(world.case_id)
                    run.emit("progress", agent=sp.name,
                             snapshot=_snapshot(world, cur, orch, mcp, llm, tracer))
                except Exception:  # noqa: BLE001
                    pass          # 旁路，出错不影响业务链路

        tracer.observers.append(on_span)

        orch = Orchestrator(world, mcp, llm,
                            approval_handler=run.approval_handler,
                            log_sink=lambda rec: run.emit("log", record=_jsonable(rec)))
        run.orch, run.mcp, run.tracer = orch, mcp, tracer
        run.snapshot_fn = lambda: _snapshot(
            world, orch.store.read(world.case_id), orch, mcp, llm, tracer)

        # 阶段迁移事件：包一层 store.transition，让流水线能实时点亮
        original_transition = orch.store.transition

        def traced_transition(case_id: str, to_phase: str, reason: str):
            st = original_transition(case_id, to_phase, reason)
            run.emit("phase", phase=to_phase, reason=reason,
                     snapshot=_snapshot(world, st, orch, mcp, llm, tracer))
            return st

        orch.store.transition = traced_transition   # type: ignore[method-assign]

        st = orch.run()
        run.state = st

        _persist(run, world, st, orch, mcp, tracer, llm)
        run.status = "done"
        run.emit("done", snapshot=_snapshot(world, st, orch, mcp, llm, tracer))
    except Exception as e:  # noqa: BLE001
        run.status = "failed"
        run.error = f"{type(e).__name__}: {e}"
        run.emit("error", message=run.error, traceback=traceback.format_exc()[-1500:])
    finally:
        with run._cond:
            run._cond.notify_all()


def _persist(run: Run, world, st, orch, mcp, tracer, llm) -> None:
    """落盘全部产出，与 run_demo.py 完全一致——工作台不另造一套产物。"""
    out = os.path.join(OUT_ROOT, run.case_key)
    os.makedirs(out, exist_ok=True)
    tracer.save(os.path.join(out, "trace.json"))
    with open(os.path.join(out, "trace.txt"), "w", encoding="utf-8") as f:
        f.write(tracer.render_tree())
    orch.ledger.save(os.path.join(out, "evidence_ledger.json"))
    orch.store.save(st.case_id, os.path.join(out, "case_state.json"))
    with open(os.path.join(out, "logs.jsonl"), "w", encoding="utf-8") as f:
        for rec in orch.ctx.logs:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with open(os.path.join(out, "mcp_audit.jsonl"), "w", encoding="utf-8") as f:
        for rec in mcp.audit_log:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    produced: dict[str, str] = {}
    if st.gate and st.gate.get("opinion_report"):
        produced["处置意见书.md"] = st.gate["opinion_report"]["markdown"]
    if st.audit and st.audit.get("report"):
        name = ("取证任务清单.md" if st.phase == "EVIDENCE_GAP" else "审计报告.md")
        produced[name] = st.audit["report"]["markdown"]
    _write_reports(out, produced)


#: 三种报告互斥：正常闭环出「处置意见书 + 审计报告」，转人工出「取证任务清单」。
#: 目录里留着上一次运行的旧报告会直接打脸——一个声明「系统未作出任何风险结论」
#: 的案件，目录里却躺着一份处置意见书。所以每次运行都要清掉本次没产出的那些。
REPORT_FILES = ("处置意见书.md", "审计报告.md", "取证任务清单.md")


def _write_reports(out_dir: str, produced: dict) -> None:
    for name in REPORT_FILES:
        path = os.path.join(out_dir, name)
        if name in produced:
            with open(path, "w", encoding="utf-8") as f:
                f.write(produced[name])
        elif os.path.exists(path):
            os.remove(path)


def _jsonable(obj: Any) -> Any:
    """把任意对象降级成可 JSON 序列化的形状。宁可转成字符串也不让推送中断。"""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if hasattr(obj, "to_dict"):
        return _jsonable(obj.to_dict())
    return str(obj)


def _snapshot(world, st, orch, mcp, llm, tracer) -> dict[str, Any]:
    """把 CaseState 翻译成界面要的形状。

    刻意在后端做这层翻译而不是把裸 State 丢给前端：界面上的每一块都要回答
    一个业务问题（「证据够不够」「为什么是 L2」「谁批的」），
    这些问题的答案分散在 State、账本、Trace 与审计日志里，拼装逻辑应当只有一份。
    """
    ev = st.risk_event or {}
    gate_d = st.gate or {}
    usage = llm.usage()
    # 每条证据附一层「人话翻译」。翻译只重述 extracted 里已有的字段，
    # 不做任何推断——界面上不该出现账本里没有的结论（见 humanize 模块头）
    ledger_items = [{**e.to_dict(), "human": humanize.summarize_evidence(e.to_dict())}
                    for e in orch.ledger.all()]

    return {
        "case_id": st.case_id,
        "phase": st.phase,
        "history": st.history,
        "subject": st.subject,
        "as_of": world.as_of,
        "exposure": st.exposure,
        "signal": {
            "types": ev.get("signal_types", []),
            "denoise_rate": ev.get("denoise_rate"),
            "kept": ev.get("signals", ev.get("kept", [])),
            "dropped": ev.get("dropped", []),
        },
        "evidence": {
            "items": ledger_items,
            "sufficiency": orch.ledger.sufficiency(),
            "gaps": st.evidence_gaps,
            "checklist": (st.evidence_checklist.to_dict()
                          if st.evidence_checklist else None),
        },
        # 知识与经验：召回的条款、沉淀下来的风险模式。这两块是「资料库」，
        # 界面上要给结论而不是给一串编号
        "knowledge": {
            "clauses": [i["human"] for i in ledger_items
                        if i["source_system"] == "policy-kb"],
            "pattern": humanize.summarize_pattern((st.audit or {}).get("distilled", {})),
        },
        "assertion": st.assertion,
        "rebuttal": st.rebuttal,
        "rebuttal_checklist": (st.rebuttal_checklist.to_dict()
                               if st.rebuttal_checklist else None),
        "adjudication": st.adjudication,
        "gate": gate_d,
        "approval": st.approval,
        "execution": st.execution,
        "audit": st.audit,
        # 转人工交接单。只在案件移交人工时存在——它给的是工作交接，不是风险结论
        "handoff": st.handoff,
        "query_plans": st.query_plans,
        "model_usage": usage,
        "mcp_audit": mcp.audit_log,
        "metrics": tracer.metrics(),
        "expected": world.data.get("expected_outcome"),
        # 历史结局在 World 构造时就被摘出，Agent 全程够不到；
        # 案件闭环后才允许展示，且明确标注「仅事后评分」
        "retrospective": world.retrospective if st.phase in ("CLOSED", "EVIDENCE_GAP") else None,
    }


# ===========================================================================
# HTTP
# ===========================================================================

RUNS: dict[str, Run] = {}
RUNS_LOCK = threading.Lock()


def bootstrap() -> dict[str, Any]:
    """界面启动时需要的全部静态信息，一次给全，避免开局连打五个请求。"""
    cfg = modelconfig.load()
    return {
        "cases": [{"key": k, **{kk: vv for kk, vv in v.items() if kk != "fixture"},
                   "factors": [
                       {"key": f["key"], "label": f["label"], "why": f["why"],
                        "binding": f["binding"],
                        "options": [{"name": o["name"], "expect": o["expect"]}
                                    for o in f["options"]]}
                       for f in FACTORS.get(v["fixture"], [])]}
                  for k, v in CASES.items()],
        # single 排在最前：绝大多数人手上只有一个平台的 Key，
        # 让他先看到「一个就够」，而不是先看到一堆需要两个 Key 的方案
        "presets": [{"name": "single",
                     "desc": "只用一个平台 · 自动检测你能用哪些模型（推荐）"}]
                   + [{"name": n, "desc": (b or {}).get("desc", "")}
                      for n, b in sorted(modelconfig.list_presets().items())]
                   + [{"name": "custom",
                       "desc": "自定义端点 · 手动填两个端点"}],
        "endpoints": KNOWN_ENDPOINTS,
        "agents": [
            {
                "id": a,
                "role": e["role"],
                "equivalence_class": e["equivalence_class"],
                "is_leader": a == permissions.TEAM_LEADER,
                "llm": e["llm"],
                "model_profile": e["model_profile"],
                "pii_access": e["pii_access"],
                "notes": e["notes"],
                "decision_boundary": e["decision_boundary"],
                "mcp": {srv: tools for srv, tools in e["mcp"].items()},
                "skills": e["skills"],
                "writes": sorted({t for tools in e["mcp"].values()
                                  for t, m in tools.items() if m == permissions.WRITE}),
            }
            for a, e in permissions.PERMISSIONS.items()
        ],
        "bindings": {a: (cfg.profiles[p].__dict__ | {"provider": cfg.profiles[p].provider.name}
                         if (p := cfg.profile_name_for(a)) and p in cfg.profiles else None)
                     for a in permissions.PERMISSIONS},
        "action_catalog": [
            {"action": s.action, "label": s.label, "reversible": s.reversible,
             "base_tier": s.base_tier, "rollback": s.rollback}
            for s in gate.ACTION_CATALOG.values()
        ],
        "routing_rules": routing.RULES_DOC,
        "routing_version": routing.ROUTING_TABLE_VERSION,
        "mcp_servers": {srv: [t["name"] for t in tools]
                        for srv, tools in SERVER_TOOLS.items()},
        "tiers": {
            "L0": "只读诊断，全自主",
            "L1": "轻度干预，全自主",
            "L2": "影响授信，必须人工审批后执行",
            "L3": "不可逆或大额，只出方案，系统永不执行",
        },
    }


#: 每个 provider 的 Key 去哪儿拿。纯呈现信息，因此放在服务端而不是配置里——
#: config/providers.yaml 回答的是「连到哪里」，不该混入操作指引。
PROVIDER_GUIDE: dict[str, dict[str, str]] = {
    "dashscope": {
        "console": "https://bailian.console.aliyun.com/",
        "tip": "百炼控制台（中国大陆站）→ API-KEY。与国际站的 Key 不通用，跨地域调用会返回 401",
    },
    "dashscope-intl": {
        "console": "https://bailian.console.alibabacloud.com/",
        "tip": "百炼国际站（新加坡）控制台 → API-KEY。账号注册在新加坡时用这个；"
               "国际站默认通常只开通 qwen 系列",
    },
    "deepseek": {
        "console": "https://platform.deepseek.com/api_keys",
        "tip": "DeepSeek 开放平台 → API keys，注册即有免费额度",
    },
    "volcengine": {
        "console": "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
        "tip": "火山方舟控制台 → API Key。还需在「开通管理」里开通对应的豆包模型",
    },
    "zhipu": {
        "console": "https://open.bigmodel.cn/usercenter/apikeys",
        "tip": "智谱开放平台 → API Keys",
    },
    "moonshot": {
        "console": "https://platform.moonshot.cn/console/api-keys",
        "tip": "月之暗面开放平台 → API Key",
    },
    "ollama": {
        "console": "https://ollama.com/download",
        "tip": "本地服务，无需 Key。先 ollama pull 对应模型再 ollama serve",
    },
}


def credential_status(preset: str | None) -> dict[str, Any]:
    """某个预设跑起来需要哪些凭证，以及它们当前是否已就绪。

    只返回**名字与来源**，绝不返回值——这个接口的响应会被贴进浏览器控制台、
    截图、聊天窗口，任何一处泄漏都是真泄漏。
    """
    eps = modelconfig.runtime_endpoints()
    if preset == "single":
        # 「只用一个平台」是 UI 概念，底层复用自定义端点：两个角色指向同一个
        # base_url，只是模型不同。异构对抗约束的是**模型族**，不是厂商——
        # 同一个平台上挂着两个族，一个 Key 就够。
        cur = eps.get("risk-analyst") or {}
        return {
            "single": True,
            "endpoints": KNOWN_ENDPOINTS,
            "base_url": cur.get("base_url", ""),
            "key_ready": modelconfig.credential_source(
                modelconfig.custom_key_env("risk-analyst")) != "none",
            "configured": {r: eps.get(r) for r in modelconfig.CUSTOM_ROLES},
            "items": [],
        }
    if preset == "custom":
        # 自定义端点还没填全时，返回一张「要填什么」的表单说明而不是报错——
        # 使用者此刻要的是「怎么填」，不是「你没填」
        return {
            "custom": True,
            "roles": [{
                "role": r,
                "label": {"risk-analyst": "风险定性官", "devils-advocate": "对抗质疑官"}[r],
                "hint": ("求证「风险成立」，是核心判断环节，建议用较强的模型"
                         if r == "risk-analyst" else
                         "求证「风险不成立」，必须与定性官不同模型族，否则对抗在权重层面坍缩"),
                "env_name": modelconfig.custom_key_env(r),
                "configured": r in eps,
                "source": modelconfig.credential_source(modelconfig.custom_key_env(r)),
                **eps.get(r, {}),
            } for r in modelconfig.CUSTOM_ROLES],
            "items": [],
        }

    try:
        cfg = modelconfig.resolve(preset, None)
    except modelconfig.ConfigError as e:
        return {"error": str(e), "items": []}

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    # 只列真正会发起 live 调用的两个 Agent 涉及的 provider，
    # 免得让人以为要准备一堆 key
    for agent in ("risk-analyst", "devils-advocate"):
        prof = cfg.profile_for(agent)
        prov = prof.provider
        if prov.name in seen:
            continue
        seen.add(prov.name)
        ref = prov.credential_ref or "none:"
        scheme, _, rest = ref.partition(":")
        guide = PROVIDER_GUIDE.get(prov.name, {})
        items.append({
            "provider": prov.name,
            "base_url": prov.base_url,
            "model": prof.model,
            "family": prof.family,
            "used_by": agent,
            "role_label": {"risk-analyst": "风险定性官",
                           "devils-advocate": "对抗质疑官"}.get(agent, agent),
            "scheme": scheme,
            "env_name": rest if scheme == "env" else None,
            "needed": scheme == "env",
            "source": modelconfig.credential_source(rest) if scheme == "env" else "none",
            "note": prov.note,
            "console": guide.get("console"),
            "tip": guide.get("tip"),
        })
    return {"items": items, "preset": preset}


#: 常见平台的服务地址。使用者一般不知道 base_url 长什么样，
#: 更不知道百炼有两个互不相通的地域端点——给个下拉比让他去翻文档强。
KNOWN_ENDPOINTS: list[dict[str, str]] = [
    {"label": "阿里云百炼 · 国际站（新加坡）",
     "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
     "hint": "控制台域名是 bailian.console.alibabacloud.com 的选这个"},
    {"label": "阿里云百炼 · 中国大陆（北京）",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "hint": "控制台域名是 bailian.console.aliyun.com 的选这个"},
    {"label": "DeepSeek 开放平台",
     "base_url": "https://api.deepseek.com/v1", "hint": ""},
    {"label": "智谱开放平台",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "hint": ""},
    {"label": "月之暗面 Moonshot",
     "base_url": "https://api.moonshot.cn/v1", "hint": ""},
    {"label": "本地 Ollama",
     "base_url": "http://127.0.0.1:11434/v1", "hint": "无需 Key，需先 ollama serve"},
]

#: 探测用的候选模型。按「族」分组——**要凑的是两个族，不是两个模型**，
#: 所以每族只要探到一个能用的就够了。排在前面的优先被选为该族代表。
DISCOVERY_CANDIDATES: list[tuple[str, str]] = [
    ("qwen-max", "qwen"), ("qwen3-max", "qwen"), ("qwen-plus", "qwen"),
    ("qwen-turbo", "qwen"),
    ("deepseek-v3", "deepseek"), ("deepseek-v3.1", "deepseek"),
    ("deepseek-r1", "deepseek"), ("deepseek-chat", "deepseek"),
    ("glm-4-plus", "glm"), ("glm-4", "glm"), ("glm-4-9b-chat", "glm"),
    ("llama3.3-70b-instruct", "llama"), ("llama3.1-70b-instruct", "llama"),
    ("llama3.1:8b", "llama"),
    ("moonshot-v1-8k", "moonshot"), ("kimi-k2-0711-preview", "moonshot"),
    ("doubao-1-5-pro-32k", "doubao"),
    ("baichuan2-13b-chat-v1", "baichuan"),
    ("yi-large", "yi"),
    ("qwen2.5:7b", "qwen-local"),
]


def _try_model(base_url: str, api_key: str, model: str, timeout: float = 20.0
               ) -> tuple[bool, str]:
    """对一个模型发最小请求，判断当前 Key 能不能用它。"""
    body = {"model": model, "max_tokens": 4,
            "messages": [{"role": "user", "content": "hi"}]}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
        return True, ""
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(raw).get("error", {}).get("message", "")[:90]
        except Exception:  # noqa: BLE001
            msg = raw[:90]
        return False, f"{e.code} {msg}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}"


def discover_models(base_url: str, api_key: str, extra: list[str] | None = None
                    ) -> dict[str, Any]:
    """探测这个 Key 在这个端点上到底能用哪些模型。

    存在的理由：使用者拿到 404 时无从判断是「模型名写错了」「这个地域没有」
    还是「没开通」。与其让他去控制台一个个试，不如系统直接告诉他
    **你能用什么、够不够凑出两个族、不够的话差什么**。
    """
    from concurrent.futures import ThreadPoolExecutor

    cands = list(DISCOVERY_CANDIDATES)
    for m in (extra or []):
        if m and not any(m == c[0] for c in cands):
            cands.insert(0, (m, modelconfig._guess_family(m)))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda mf: (mf[0], mf[1], *_try_model(base_url, api_key, mf[0])), cands))

    ok = [{"model": m, "family": f} for m, f, good, _ in results if good]
    failed = [{"model": m, "family": f, "why": why} for m, f, good, why in results
              if not good]

    # 每族取第一个可用的作为代表
    by_family: dict[str, str] = {}
    for r in ok:
        by_family.setdefault(r["family"], r["model"])

    auth_bad = any("401" in (x["why"] or "") for x in failed) and not ok
    fams = list(by_family)
    suggestion = None
    if len(fams) >= 2:
        # 定性侧优先给排在候选表更靠前的（通常是更强的那个）
        order = [f for _, f in cands]
        fams.sort(key=lambda f: order.index(f))
        suggestion = {"analyst": {"model": by_family[fams[0]], "family": fams[0]},
                      "advocate": {"model": by_family[fams[1]], "family": fams[1]}}

    return {
        "base_url": base_url,
        "available": ok,
        "families": by_family,
        "can_pair": len(by_family) >= 2,
        "suggestion": suggestion,
        "auth_failed": auth_bad,
        "tried": len(cands),
        # 只回未通过的前若干条，避免一屏全是 404
        "failed_sample": failed[:6],
    }


def _http_hint(code: int, p: Any) -> str:
    """把 HTTP 状态码翻译成「下一步该做什么」。

    报错文本原样抛给使用者是不负责任的：真实端点返回的
    「Incorrect API key provided」在**地域不匹配**时也会出现，
    照着字面去查 Key 只会一直查不出问题。这里把最容易踩的那几个坑写清楚。
    """
    base = p.provider.base_url
    is_dashscope = "dashscope" in base
    intl = "dashscope-intl" in base
    if code == 401:
        if is_dashscope:
            return ("API Key 不被接受。**百炼有两个互不相通的地域端点，Key 不能跨地域用**："
                    + ("当前打的是国际站（新加坡）。若你的账号注册在中国大陆，"
                       "请改用 dashscope-only 这类国内站预设。" if intl else
                       "当前打的是中国大陆（北京）。若你的账号注册在国际站（新加坡），"
                       "请改用 dashscope-intl-* 预设。")
                    + "其次才是检查 Key 有没有复制全、是否已删除")
        return "API Key 不正确或已过期，检查是否复制完整"
    if code == 403:
        return "该 Key 无权访问这个模型，去控制台确认已开通"
    if code == 404:
        return (f"模型 {p.model} 在你的账号或地域下不存在／未开通。"
                f"去控制台核对模型名，或换一个已开通的模型"
                + ("。百炼国际站默认通常只有 qwen 系列" if intl else ""))
    if code == 429:
        return "触发限流，稍后重试或检查额度"
    return f"核对 {base} 与模型名 {p.model}"


def probe_preset(preset: str | None) -> dict[str, Any]:
    """对预设涉及的端点各发一次最小探针：连通性 + 鉴权 + 模型名 + JSON 模式。

    与 ``tools/preflight.py`` 同一套判断，只是把结果做成界面能显示的形状。
    在界面上填完 key 就能立刻验证，比跑一个案件失败再去猜原因高效得多。
    """
    if preset == "single":
        preset = "custom"
    try:
        cfg = modelconfig.resolve(preset, None)
    except modelconfig.ConfigError as e:
        return {"ok": False, "error": str(e), "results": []}

    results = []
    for agent in ("risk-analyst", "devils-advocate"):
        p = cfg.profile_for(agent)
        row: dict[str, Any] = {"agent": agent, "profile": p.name,
                               "provider": p.provider.name, "model": p.model,
                               "family": p.family,
                               # 判断「是不是同一个平台」要看服务地址，不能看内部
                               # provider 名——自定义端点的名字是按角色生成的，
                               # 两个角色即便指向同一个地址，名字也必然不同
                               "base_url": p.provider.base_url}
        if p.provider.protocol == "deterministic":
            results.append({**row, "ok": True, "detail": "内置确定性推理器，无需联网"})
            continue
        try:
            key = cfg.resolve_credential(p.provider)
        except modelconfig.CredentialError as e:
            results.append({**row, "ok": False, "detail": str(e)})
            continue

        body: dict[str, Any] = {
            "model": p.model, "temperature": 0.0, "max_tokens": 32,
            "messages": [{"role": "system", "content": "只输出 JSON，不要任何解释。"},
                         {"role": "user", "content": '返回 {"ok": true}'}],
        }
        if p.json_mode in ("on", "auto"):
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = f"{p.provider.base_url.rstrip('/')}/chat/completions"
        t0 = time.time()
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                         headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=min(30.0, p.timeout_ms / 1000)) as r:
                data = json.loads(r.read().decode("utf-8"))
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            results.append({**row, "ok": True,
                            "latency_ms": round((time.time() - t0) * 1000),
                            "detail": f"连通且已鉴权，返回 {content.strip()[:40]}"})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:180]
            results.append({**row, "ok": False,
                            "detail": f"HTTP {e.code}　{_http_hint(e.code, p)}",
                            "raw": detail})
        except Exception as e:  # noqa: BLE001
            results.append({**row, "ok": False,
                            "detail": f"端点不可达：{e}（本地端点请确认服务已启动）"})

    fams = {r["family"] for r in results}
    return {"ok": all(r["ok"] for r in results), "results": results,
            "heterogeneous": len(fams) == len(results),
            "diagnosis": _diagnose(results)}


def _diagnose(results: list[dict[str, Any]]) -> str:
    """跨行诊断：**一行通一行不通**时，问题几乎不在密钥本身。

    单看每一行的报错，使用者最自然的推论是「Key 有问题」——毕竟两行用的是
    同一个 Key。但同一个 Key 能通过第一行的鉴权，就证明密钥是有效的；
    第二行失败只可能是**那个模型**或**那个平台**的问题。
    这个推论跨行才成立，所以必须由系统给出，不能指望使用者自己拼。
    """
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    if not bad:
        return ""

    def _is(r: dict[str, Any], *marks: str) -> bool:
        d = r.get("detail") or ""
        return any(m in d for m in marks)

    def _host(u: str) -> str:
        u = (u or "").split("//")[-1]
        return u.split("/")[0] or u

    endpoints = {_host(r.get("base_url", "")) for r in results}
    missing = [r for r in bad if _is(r, "凭证")]
    auth = [r for r in bad if _is(r, "401")]
    notfound = [r for r in bad if _is(r, "404")]
    # 两个岗位落在不同平台时，「同一个 Key」这个前提本身就不成立——
    # 这是使用者最容易误解的一点，必须先说清楚
    multi = ("这两个岗位连的是**两个不同的服务地址**"
             f"（{'、'.join(sorted(endpoints))}），**各自需要一个 Key**。"
             "想只用一个平台，把「模型方案」改选 `single · 只用一个平台`。")

    if not ok:
        if len(endpoints) > 1 and (missing or auth):
            return multi
        if auth and len(auth) == len(bad):
            return ("两个岗位都鉴权失败。密钥无效，或者**选错了地域**"
                    "（百炼的国际站与中国大陆站互不相通，Key 不能跨站使用）。")
        if missing and len(missing) == len(bad):
            return "两个岗位的密钥都还没填。"
        parts = []
        if missing:
            parts.append(f"{len(missing)} 个还没填密钥")
        if auth:
            parts.append(f"{len(auth)} 个鉴权失败")
        if notfound:
            parts.append(f"{len(notfound)} 个模型未开通")
        return ("；".join(parts) + "。") if parts else ""

    # 一通一不通：这才是最容易被误判成「Key 有问题」的情形
    b = bad[0]
    same_provider = _host(ok[0].get("base_url", "")) == _host(b.get("base_url", ""))
    detail = b.get("detail") or ""
    passer = "风险定性岗" if ok[0]["agent"] == "risk-analyst" else "对抗质疑岗"
    head = f"**密钥本身没问题**——「{passer}」已经用它通过了鉴权。"

    if "404" in detail:
        return (head + f"失败的是**模型 `{b['model']}` 在你的账号里没有开通**，不是密钥。"
                + ("去这个平台的「模型广场」把它开通，或回到上一步"
                   "「检测可用模型」换一个已开通的模型。" if same_provider else
                   "这一行用的是另一个平台，需要那个平台自己的 Key。"))
    if "401" in detail or "凭证" in detail:
        if same_provider:
            return head + "同平台却鉴权失败，检查这一行的密钥是否被覆盖成了别的值。"
        return head + multi
    if "403" in detail:
        return head + f"失败的是**权限**：这个 Key 无权访问 `{b['model']}`。"
    return head + "失败原因见该行说明。"


class Handler(BaseHTTPRequestHandler):
    server_version = "CreditSentryConsole/1.0"

    # ---- 路由 -------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        try:
            if path == "/" or path == "/index.html":
                return self._file(os.path.join(WEB_DIR, "index.html"))
            if path.startswith("/static/"):
                return self._file(os.path.join(WEB_DIR, os.path.basename(path)))
            if path == "/api/bootstrap":
                return self._json(200, bootstrap())
            if path == "/api/credentials":
                return self._json(200, credential_status((q.get("preset") or [""])[0] or None))
            if path.startswith("/api/runs/"):
                rest = path[len("/api/runs/"):].split("/")
                run = RUNS.get(rest[0])
                if run is None:
                    return self._json(404, {"error": "run 不存在"})
                if len(rest) == 1:
                    return self._json(200, self._run_view(run))
                if rest[1] == "stream":
                    return self._stream(run, int((q.get("from") or ["0"])[0]))
                if rest[1] == "report":
                    return self._report(run, rest[2] if len(rest) > 2 else "")
                if rest[1] == "snapshot" and len(rest) > 2:
                    return self._snapshot_doc(run, rest[2])
            return self._json(404, {"error": f"未知路径 {path}"})
        except BrokenPipeError:
            pass                      # 浏览器关掉了页面，不是错误
        except Exception as e:        # noqa: BLE001
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        try:
            body = self._body()
            if u.path == "/api/credentials":
                # 只进内存，不落盘、不进日志。值在这里出现一次，随后只有
                # resolve_credential 会读到它
                for name, value in (body.get("values") or {}).items():
                    modelconfig.set_runtime_credential(str(name), str(value or "").strip())
                # 自定义端点：base_url / model / family 与 key 一并登记
                for role, ep in (body.get("endpoints") or {}).items():
                    try:
                        modelconfig.set_runtime_endpoint(
                            role, base_url=(ep.get("base_url") or "").strip(),
                            model=(ep.get("model") or "").strip(),
                            family=(ep.get("family") or "").strip() or None,
                            data_residency=(ep.get("data_residency") or "cn").strip(),
                            json_mode=(ep.get("json_mode") or "auto").strip())
                    except modelconfig.ConfigError as e:
                        return self._json(400, {"error": str(e)})
                    if key := (ep.get("api_key") or "").strip():
                        modelconfig.set_runtime_credential(
                            modelconfig.custom_key_env(role), key)
                return self._json(200, credential_status(body.get("preset")))
            if u.path == "/api/credentials/test":
                return self._json(200, probe_preset(body.get("preset")))
            if u.path == "/api/credentials/discover":
                base = (body.get("base_url") or "").strip()
                key = (body.get("api_key") or "").strip()
                if not key:  # 留空表示沿用已登记的
                    key = modelconfig._RUNTIME_CREDENTIALS.get(
                        modelconfig.custom_key_env("risk-analyst"), "")
                if not base:
                    return self._json(400, {"error": "请先选择或填写服务地址"})
                return self._json(200, discover_models(
                    base, key, (body.get("extra_models") or [])))
            if u.path == "/api/credentials/apply-single":
                # 一个平台两个族：两个角色指向同一个 base_url，只是模型不同。
                # 走的仍是自定义端点那条路径，四条不变量照样校验
                base = (body.get("base_url") or "").strip()
                key = (body.get("api_key") or "").strip()
                pair = body.get("pair") or {}
                for role, side in (("risk-analyst", "analyst"),
                                   ("devils-advocate", "advocate")):
                    spec = pair.get(side) or {}
                    try:
                        modelconfig.set_runtime_endpoint(
                            role, base_url=base, model=spec.get("model", ""),
                            family=spec.get("family") or None,
                            data_residency=(body.get("data_residency") or "cn"))
                    except modelconfig.ConfigError as e:
                        return self._json(400, {"error": str(e)})
                    if key:
                        modelconfig.set_runtime_credential(
                            modelconfig.custom_key_env(role), key)
                try:
                    modelconfig.resolve("custom", None)   # 立刻校验四条不变量
                except modelconfig.ConfigError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(200, probe_preset("custom"))
            if u.path == "/api/runs":
                return self._create_run(body)
            if u.path.startswith("/api/runs/"):
                rest = u.path[len("/api/runs/"):].split("/")
                run = RUNS.get(rest[0])
                if run is None:
                    return self._json(404, {"error": "run 不存在"})
                if len(rest) > 1 and rest[1] == "approve":
                    ok = run.submit_approval({
                        "approved": bool(body.get("approved")),
                        "approver": body.get("approver") or "风险经理-赵",
                        "reason": body.get("reason") or "",
                    })
                    return self._json(200 if ok else 409,
                                      {"ok": ok, "error": None if ok else "当前不在等待审批"})
                if len(rest) > 1 and rest[1] == "probe":
                    return self._json(200, self._probe(run, body))
            return self._json(404, {"error": f"未知路径 {u.path}"})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    # ---- 处理器 -----------------------------------------------------
    def _create_run(self, body: dict[str, Any]) -> None:
        case_key = body.get("case")
        if case_key not in CASES:
            return self._json(400, {"error": f"未知案件 {case_key}"})
        run = Run(uuid.uuid4().hex[:12], case_key,
                  llm_mode=body.get("llm_mode") or "stub",
                  preset=body.get("preset") or None,
                  factor_choices=body.get("factors") or {},
                  approval_mode=body.get("approval_mode") or "ask")
        with RUNS_LOCK:
            RUNS[run.id] = run
        threading.Thread(target=execute, args=(run,), daemon=True).start()
        return self._json(201, {"run_id": run.id})

    def _probe(self, run: Run, body: dict[str, Any]) -> dict[str, Any]:
        """越权调用演示：让没有写权限的 Agent 去调信贷核心的写接口。

        这个按钮的意义在于**被拒绝的调用也会出现在审计日志里**。
        一个只在成功路径留痕的系统，没法向监管解释「有没有人试过绕过去」。
        """
        if run.mcp is None:
            return {"error": "案件尚未开始，无可用的 MCP 客户端"}
        caller = body.get("caller") or "risk-analyst"
        server = body.get("server") or "credit-core-mcp"
        tool = body.get("tool") or "adjust_limit"
        before = len(run.mcp.audit_log)
        try:
            result = run.mcp.call(server, tool,
                                  {"subject_id": run.state.subject["subject_id"]
                                   if run.state else "SUB-000",
                                   "new_limit": 1, "idempotency_key": "probe"},
                                  caller=caller)
            outcome = {"allowed": True, "result": _jsonable(result)}
        except MCPError as e:
            outcome = {"allowed": False, "code": e.code, "message": e.message}
        outcome["audit_entries"] = run.mcp.audit_log[before:]
        outcome["request"] = {"caller": caller, "server": server, "tool": tool}
        return outcome

    def _run_view(self, run: Run) -> dict[str, Any]:
        view = {
            "run_id": run.id, "case": run.case_key, "status": run.status,
            "llm_mode": run.llm_mode, "preset": run.preset,
            "error": run.error, "pending_approval": run.pending_approval,
            "elapsed_ms": round((time.time() - run.started_at) * 1000),
        }
        if run.state is not None and run.orch is not None:
            view["snapshot"] = _snapshot(run.orch.world, run.state, run.orch,
                                         run.mcp, run.orch.llm, run.tracer)
        return view

    def _snapshot_doc(self, run: Run, evidence_id: str) -> None:
        """取回一条证据的原文快照并渲染成可阅读的「文件」。

        取回时当场校验哈希：账本记哈希、快照存别处，两者对不上就说明有一方
        被动过——这时候界面必须显示告警，而不是若无其事地把内容摆出来。
        """
        if run.orch is None:
            return self._json(404, {"error": "案件尚未开始"})
        try:
            ev = run.orch.ledger.get(evidence_id).to_dict()
            raw, ok = run.orch.ledger.snapshot(evidence_id)
        except Exception as e:  # noqa: BLE001
            return self._json(404, {"error": f"取不到该证据的快照：{e}"})
        return self._json(200, humanize.render_snapshot(ev, raw, ok))

    def _report(self, run: Run, kind: str) -> None:
        st = run.state
        text = ""
        if st is not None:
            if kind == "opinion" and st.gate and st.gate.get("opinion_report"):
                text = st.gate["opinion_report"]["markdown"]
            elif kind in ("audit", "handoff") and st.audit and st.audit.get("report"):
                text = st.audit["report"]["markdown"]
        return self._json(200, {"kind": kind, "markdown": text})

    def _stream(self, run: Run, from_seq: int) -> None:
        """SSE。断线重连带上 ?from=<已收到的最大 seq> 即可续传。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        seq = from_seq
        while True:
            batch = run.events_since(seq, timeout=15.0)
            for e in batch:
                seq = e["seq"]
                self.wfile.write(
                    f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
            if not batch:
                if run.status in ("done", "failed"):
                    break
                self.wfile.write(b": keepalive\n\n")   # 防中间层掐连接
                self.wfile.flush()

    # ---- 工具 -------------------------------------------------------
    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _json(self, code: int, obj: Any) -> None:
        raw = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _file(self, path: str) -> None:
        if not os.path.isfile(path):
            return self._json(404, {"error": f"文件不存在：{os.path.basename(path)}"})
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            raw = f.read()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a: Any) -> None:
        """默认静音，只在 --verbose 下打印。"""
        if getattr(self.server, "verbose", False):
            sys.stderr.write("  %s\n" % (a[0] % a[1:]))


def main() -> int:
    p = argparse.ArgumentParser(description="信衡 CreditSentry 风险处置工作台")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    try:
        modelconfig.load()
    except modelconfig.ConfigError as e:
        print(f"模型配置校验未通过，拒绝启动：\n{e}", file=sys.stderr)
        return 2

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.verbose = args.verbose          # type: ignore[attr-defined]
    url = f"http://{args.host}:{args.port}/"
    print("信衡 CreditSentry · 贷后风险处置工作台")
    print("═" * 62)
    print(f"  {url}")
    print(f"  推理后端　stub（确定性可复现）；页面上可切 live")
    print(f"  产出目录　poc/out/<case>/")
    print("  Ctrl-C 停止")
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
