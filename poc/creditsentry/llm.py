"""推理网关与可插拔后端。

全系统的模型调用只有这一个入口，而入口的核心职责是**按调用方解析模型档位**。

早期实现用三个进程级环境变量（BASE_URL / API_KEY / MODEL）指定唯一端点，
所有 Agent 共用。那样做有两个问题：一是同一进程内无法让不同 Agent 用不同模型，
「对抗双方必须异构」这条约束根本无法落地；二是无法回答银行必然会问的
「哪些数据发给了哪个厂商」。因此改为四层配置（见 ``modelconfig.py``），
本模块只负责按 ``caller`` 取到档位并发起调用。

两种模式，通过 ``--llm`` 切换：

- ``stub``（默认）**确定性规则推理器**。不调用任何模型，同样的输入恒定产出同样的结论。
  这样做不是为了掩饰能力，而是因为评测集需要可复现：Skill 的 ``eval/`` 回归集要作为发布门禁，
  门禁就不能依赖模型的当次发挥。编排、路由、闸门、账本、审计全部走真实代码路径，
  被替换的只有「定性」与「质疑」两处自然语言推理。
  即便如此，网关仍会解析并上报该 Agent **声明绑定**的档位，Trace 里两者分开呈现。
- ``live`` 按各 Agent 各自绑定的档位分别调用真实端点（OpenAI 兼容协议）。
  仅依赖标准库 urllib，不引入 SDK 依赖。

两种模式产出**同一套 JSON Schema**，因此切换模式不影响下游任何环节。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from . import modelconfig, schemas
from .modelconfig import ModelConfig, Profile
from .tracing import (GEN_AI_OPERATION, GEN_AI_REQUEST_MODEL, GEN_AI_SYSTEM)

# ---------------------------------------------------------------------------
# 确定性推理器：stub 模式下真实读取证据并推导结论，而不是回放预置答案。
# ---------------------------------------------------------------------------

# 涉诉实质性判定阈值：标的额占敞口比重低于此值，即便未结案也不构成偿债能力信号。
MATERIAL_RATIO = 0.05


def is_material_case(case: dict[str, Any]) -> bool:
    """单笔涉诉是否具备实质性。

    抽成独立谓词有两个原因：一是它是 LitigationProbe 回归评估集的被测对象，
    二是这条口径直接绑定行内《贷后风险信号认定标准》，条款变更时只改这一处。

    三个条件必须同时成立：未结案、我方非原告、单笔标的占敞口不低于阈值。
    """
    return (
        not case.get("closed", False)
        and case.get("our_role") != "原告"
        and case.get("amount_ratio", 0.0) >= MATERIAL_RATIO
    )


# 净未覆盖代偿敞口相对直接敞口每高 1 倍，置信度 +0.10；倍数在 3.0 处封顶。
CONTAGION_BASE = 0.45
CONTAGION_STEP = 0.10
CONTAGION_MULTIPLE_CAP = 3.0
# 由上述三个参数决定的置信度上限：0.45 + 0.10 × 3.0 = 0.75。
# 这个数字低于 _reason_root_cause 中「次级」的 0.85 门槛，是刻意的——
# 或有代偿**单独不足以**把客户定到次级，必须与已实现的风险信号叠加才行。
CONTAGION_CEILING = round(CONTAGION_BASE + CONTAGION_STEP * CONTAGION_MULTIPLE_CAP, 2)


def _contagion_confidence(gua: dict[str, Any]) -> float:
    """担保代偿的置信度定级。

    刻意比已实现风险给得低：代偿是**或有**负债，需要被担保方真的违约、
    债权人真的主张、缓释措施真的不足，三件事同时发生才会转化为本行损失。
    因此定级只看**净未覆盖敞口相对直接敞口的倍数**——缓释覆盖得越足，
    这个数越小，置信度越低；覆盖满了就根本不进入主因列表。
    """
    multiple = gua.get("uncovered_multiple")
    if not multiple or multiple <= 0:
        return 0.0
    scaled = min(float(multiple), CONTAGION_MULTIPLE_CAP)
    return round(CONTAGION_BASE + CONTAGION_STEP * scaled, 2)


def _reason_root_cause(payload: dict[str, Any]) -> dict[str, Any]:
    """RiskAnalyst：基于**聚合信号**做根因归因。

    分工是刻意的：定性官看的是「有没有出现这类信号、量级多大」，
    个案层面的实质性判定留给质疑官。这样两个角色才真正在做不同的事，
    而不是同一套逻辑跑两遍。任一结论都必须携带其依据的 evidence_id。
    """
    facts = payload["facts"]
    causes: list[dict[str, Any]] = []

    lit = facts.get("litigation", {})
    txn = facts.get("transaction", {})
    reg = facts.get("registration", {})

    cases = lit.get("cases", [])
    if cases:
        ratio = lit.get("total_amount_ratio", 0.0)
        causes.append({
            "type": "偿债能力恶化",
            "confidence": round(min(0.95, 0.55 + min(ratio, 0.30)), 2),
            "evidence_ids": lit.get("evidence_ids", []),
            "rationale": (
                f"报告期内新增涉诉 {len(cases)} 笔，标的合计占敞口 {ratio:.1%}，"
                f"存在对偿债能力形成压力的可能"
            ),
        })

    if txn.get("anomaly_detected"):
        causes.append({
            "type": "资金用途异常",
            "confidence": 0.62,
            "evidence_ids": txn.get("evidence_ids", []),
            "rationale": (
                f"流水识别到 {len(txn.get('anomalies', []))} 类异常模式"
                f"（{'、'.join(txn.get('anomalies', [])) or '集中转出'}），存在资金用途偏离嫌疑"
            ),
        })

    if reg.get("legal_rep_changed"):
        causes.append({
            "type": "实际控制人风险",
            "confidence": 0.58,
            "evidence_ids": reg.get("evidence_ids", []),
            "rationale": "报告期内发生法定代表人变更，需关注公司治理与实控人稳定性",
        })

    gua = facts.get("guarantee", {})
    if gua.get("distressed_guarantee", 0) > 0:
        causes.append({
            "type": "担保圈代偿风险",
            "confidence": _contagion_confidence(gua),
            "evidence_ids": gua.get("evidence_ids", []),
            "rationale": (
                f"对已出险主体（{'、'.join(p['party'] for p in gua['distressed_parties'])}）"
                f"存在担保余额 {gua['distressed_guarantee'] / 1e8:.2f} 亿元，"
                f"缓释措施覆盖 {(gua.get('mitigation_coverage') or 0):.1%}，"
                f"净未覆盖代偿敞口 {gua['uncovered_amount'] / 1e8:.2f} 亿元，"
                f"为本行直接敞口的 {gua.get('uncovered_multiple')} 倍"
            ),
        })

    causes.sort(key=lambda c: c["confidence"], reverse=True)
    top = causes[0] if causes else None

    # 无高置信根因时不硬凑结论，如实降级并输出取证方向。
    if top is None or top["confidence"] < 0.5:
        return {
            "conclusion": "INSUFFICIENT",
            "root_causes": causes,
            "suggested_grade": None,
            "summary": "现有证据不足以支撑风险定性，需补充取证后重新评估",
        }

    grade = "次级" if top["confidence"] > 0.85 else "关注"
    return {
        "conclusion": "RISK_CONFIRMED",
        "root_causes": causes,
        "suggested_grade": grade,
        "summary": (
            f"主因判定为「{top['type']}」（置信度 {top['confidence']}）：{top['rationale']}。"
            f"建议五级分类调整为「{grade}」。"
        ),
    }


def _reason_devils_advocate(payload: dict[str, Any]) -> dict[str, Any]:
    """DevilsAdvocate：目标函数与定性官对立，尽力证伪「风险成立」。

    它下钻到**个案层面**：逐笔看案由、标的额占比、结案状态与诉讼地位；
    逐个对手方看是否关联方、是否落在历史波动区间。
    只要能对某条主因给出成立的替代解释，就推翻它——宁可漏杀，不可误杀。

    每次质疑无论成立与否都会被记录（``attempted``），因为「我们试过反驳但没能反驳掉」
    本身就是支撑处置结论的重要证据，不能因为反驳失败就把过程丢掉。
    """
    facts = payload["facts"]
    assertion = payload["assertion"]

    lit = facts.get("litigation", {})
    txn = facts.get("transaction", {})
    reg = facts.get("registration", {})

    rebuttals: list[dict[str, Any]] = []
    attempted: list[dict[str, Any]] = []
    gaps: list[str] = []

    # 质疑一：逐笔下钻涉诉的实质性
    cases = lit.get("cases", [])
    if cases:
        material = [c for c in cases if is_material_case(c)]
        if not material:
            ratio = lit.get("total_amount_ratio", 0.0)
            causes_txt = "、".join(sorted({c.get("cause", "未知") for c in cases}))
            rebuttals.append({
                "target": "偿债能力恶化",
                "counter_evidence_ids": lit.get("evidence_ids", []),
                "argument": (
                    f"逐笔核查 {len(cases)} 笔涉诉均不具实质性：标的合计仅占敞口 {ratio:.1%}"
                    f"（低于 {MATERIAL_RATIO:.0%} 实质性阈值），案由为{causes_txt}，"
                    f"且均已结案、我方为原告而非被执行人，不构成偿债能力恶化信号"
                ),
            })
        else:
            attempted.append({
                "target": "偿债能力恶化",
                "tried": "尝试论证涉诉不具实质性",
                "failed_because": (
                    f"{len(material)} 笔涉诉满足实质性条件（未结案、我方为被告、"
                    f"单笔标的占敞口 ≥ {MATERIAL_RATIO:.0%}），反驳不成立"
                ),
            })

    # 质疑二：资金异常是否有正常业务解释
    if txn.get("anomaly_detected"):
        related = txn.get("counterparty_related_party", False)
        in_band = txn.get("within_baseline_band", False)
        if not related and in_band:
            rebuttals.append({
                "target": "资金用途异常",
                "counter_evidence_ids": txn.get("evidence_ids", []),
                "argument": (
                    "集中转出对手方均为历史稳定供应商而非关联方，且金额落在近 24 个月"
                    f"同期波动区间 {txn.get('baseline_band')} 内，"
                    "更合理的解释是季节性备料付款而非资金挪用"
                ),
            })
        else:
            attempted.append({
                "target": "资金用途异常",
                "tried": "尝试论证集中转出属正常经营资金调度",
                "failed_because": (
                    "对手方经穿透确认为关联方，资金流向体外，无法用正常采购解释"
                    if related else
                    "转出金额显著超出历史同期波动区间，无法用季节性调度解释"
                ),
            })

    # 质疑三：治理变动是否与风险窗口相关
    if reg.get("legal_rep_changed"):
        overlap = reg.get("change_overlaps_risk_window", False)
        equity_changed = reg.get("equity_changed", False)
        if not overlap and not equity_changed:
            rebuttals.append({
                "target": "实际控制人风险",
                "counter_evidence_ids": reg.get("evidence_ids", []),
                "argument": (
                    "法定代表人变更发生在风险窗口之前，且股权结构未发生变动，"
                    "实际控制人未变化，不构成逃废债信号"
                ),
            })
        else:
            attempted.append({
                "target": "实际控制人风险",
                "tried": "尝试论证法代变更属常规治理调整",
                "failed_because": (
                    "变更时点与风险事件窗口重合" if overlap else "同期发生股权结构变动"
                ),
            })

    # 质疑四：担保代偿是否已被缓释措施覆盖
    gua = facts.get("guarantee", {})
    if gua.get("distressed_guarantee", 0) > 0:
        coverage = gua.get("mitigation_coverage") or 0.0
        mit_desc = "、".join(
            f"{m.get('type')} {float(m.get('amount') or 0) / 1e8:.2f} 亿"
            for p in gua["distressed_parties"] for m in p.get("mitigations", [])
        ) or "无"
        if coverage >= 1.0:
            rebuttals.append({
                "target": "担保圈代偿风险",
                "counter_evidence_ids": gua.get("evidence_ids", []),
                "argument": (
                    f"代偿敞口已被缓释措施全额覆盖（{mit_desc}），"
                    f"覆盖率 {coverage:.1%}，净未覆盖敞口为零，不构成本行当期风险敞口"
                ),
            })
        else:
            attempted.append({
                "target": "担保圈代偿风险",
                "tried": "尝试论证共同担保人与抵押物已覆盖代偿敞口",
                "failed_because": (
                    f"缓释措施（{mit_desc}）合计仅覆盖代偿敞口的 {coverage:.1%}，"
                    f"净未覆盖 {gua['uncovered_amount'] / 1e8:.2f} 亿元，"
                    f"仍达本行直接敞口的 {gua.get('uncovered_multiple')} 倍，反驳不成立"
                ),
            })

    # 证据充分性质疑：采样不足或存在歧义的证据不能用于定性
    for key, node in (("litigation", lit), ("transaction", txn), ("registration", reg),
                      ("guarantee", gua)):
        if node.get("undersampled") or node.get("partial"):
            gaps.append(f"{key}：数据采样不足或仅取到部分结果，不足以支撑强断言")
        if node.get("ambiguous"):
            gaps.append(f"{key}：主体存在重名歧义，未完成消歧")

    asserted = {c["type"] for c in assertion.get("root_causes", []) if c["confidence"] >= 0.5}
    refuted = {r["target"] for r in rebuttals}

    # 逐项回执质疑清单。清单由系统派生并注入提示词，模型必须对**每一项**表态；
    # 这里把上面的三类结果映射成回执，缺一项就会被 adjudicate 判为质疑未完成。
    by_target: dict[str, dict[str, Any]] = {}
    for r in rebuttals:
        by_target[r["target"]] = {"status": "REFUTED", "resolution": r["argument"],
                                  "evidence_ids": r.get("counter_evidence_ids", [])}
    for a in attempted:
        by_target.setdefault(a["target"], {
            "status": "ATTEMPTED_FAILED",
            "resolution": f"{a['tried']}；{a['failed_because']}", "evidence_ids": [],
        })
    resolutions = []
    for item in payload.get("checklist", []):
        got = by_target.get(item["target"])
        if got is None:
            # 清单上有、但上面三类质疑都没覆盖到 → 如实回执 INSUFFICIENT，
            # 而不是沉默。沉默会让「没看」伪装成「看过且没问题」
            got = {"status": "INSUFFICIENT",
                   "resolution": "现有证据不足以对该主因作出质疑判断",
                   "evidence_ids": []}
            gaps.append(f"{item['target']}：质疑方无法基于现有证据判断该主因")
        resolutions.append({"item_id": item["item_id"], "target": item["target"], **got})

    if asserted and asserted <= refuted:
        verdict = "REFUTED"
    elif gaps and not (asserted - refuted):
        verdict = "INSUFFICIENT_EVIDENCE"
    elif rebuttals:
        verdict = "PARTIALLY_REFUTED"
    else:
        verdict = "SUPPORTED"

    return {
        "verdict": verdict,
        "rebuttals": rebuttals,
        "attempted_but_failed": attempted,
        "evidence_gaps": gaps,
        "surviving_causes": sorted(asserted - refuted),
        "checklist_resolutions": resolutions,
        "summary": {
            "REFUTED": "定性结论的全部主因均被反证推翻，不应进入处置",
            "PARTIALLY_REFUTED": "部分主因被推翻，但仍存在未被反驳的实质性风险",
            "INSUFFICIENT_EVIDENCE": "现有证据不足以支撑定性，应回退补证",
            "SUPPORTED": "已尝试逐条反驳但均不成立，定性结论成立",
        }[verdict],
    }


_STUB_REASONERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "risk_root_cause": _reason_root_cause,
    "devils_advocate": _reason_devils_advocate,
}

#: 输出不合规时允许的修复轮数。
#: 只给一轮是刻意的：修复轮是给**表述失误**的补救机会，不是给模型反复试错的机会。
#: 两轮仍不合规通常意味着模型不理解该任务，继续重试只是把失败推后并放大成本。
MAX_REPAIR_ROUNDS = 1


# ---------------------------------------------------------------------------
# 失败与结果类型
# ---------------------------------------------------------------------------

class InferenceError(RuntimeError):
    """推理最终失败：传输重试用尽，或输出经修复轮后仍不满足契约。

    刻意与普通异常区分开：调用方要据此执行 ``docs/接口与实验方案.md`` §1.5 的
    失败策略表，而失败策略对每个 task 都不同（定性失败→判 INSUFFICIENT 回退补证；
    质疑失败→**阻断**）。分不清「模型没给出合规输出」与「代码写错了」，
    就只能一律崩掉或一律吞掉，两者都不可接受。
    """

    def __init__(self, task: str, who: str, reason: str, *,
                 attempts: int = 0, errors: list[str] | None = None) -> None:
        self.task, self.who, self.reason = task, who, reason
        self.attempts, self.errors = attempts, list(errors or [])
        detail = ("；".join(self.errors[:4]) + ("…" if len(self.errors) > 4 else "")) \
            if self.errors else ""
        super().__init__(
            f"{who} 的 {task} 推理失败（尝试 {attempts} 次）：{reason}"
            + (f"｜{detail}" if detail else "")
        )


class OutputParseError(ValueError):
    """模型输出连 JSON 都不是。携带原文，供修复轮回喂。"""

    def __init__(self, raw: str, detail: str) -> None:
        self.raw = raw
        super().__init__(detail)


@dataclass
class BackendResult:
    """后端单次调用的原始产物。

    ``raw`` 必须保留：修复轮要把模型上一次的原文作为 assistant 轮回喂，
    只回喂「你错了」而不回喂「你说了什么」，模型无从对照修改。
    """
    data: Any
    tokens_in: int
    tokens_out: int
    raw: str = ""


# 从模型输出里捞 JSON。真实端点常见三种偏差：套 ```json 围栏、
# JSON 前后带一句寒暄、以及（推理类模型）先输出思考再给结果。
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)


def _extract_json(text: str) -> Any:
    """尽最大努力从文本中解析出 JSON 对象。

    这里的宽容是有边界的：只处理**包装**问题，不猜测缺失内容。
    实在解析不出就抛错，交给修复轮——让模型重写一遍，比让代码猜它想说什么安全。
    """
    if not text or not text.strip():
        raise ValueError("模型返回空内容")
    s = text.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    if m := _FENCE_RE.search(s):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            s = m.group(1)
    # 退而求其次：截取最外层花括号之间的内容
    start, end = s.find("{"), s.rfind("}")
    if start >= 0 and end > start:
        return json.loads(s[start:end + 1])
    raise ValueError(f"模型输出中找不到 JSON 对象：{s[:120]}…")


# ---------------------------------------------------------------------------
# 后端实现
# ---------------------------------------------------------------------------

class DeterministicBackend:
    """确定性规则推理器。同输入恒同输出，可复现。"""

    kind = "deterministic"

    def call(self, profile: Profile, task: str, system: str,
             payload: dict[str, Any], *,
             turns: list[dict[str, str]] | None = None) -> BackendResult:
        if task not in _STUB_REASONERS:
            raise KeyError(f"确定性推理器未实现任务：{task}")
        result = _STUB_REASONERS[task](payload)
        # 便于成本口径对齐，按字符数粗估 token
        raw = json.dumps(result, ensure_ascii=False)
        tin = len(json.dumps(payload, ensure_ascii=False)) // 2
        return BackendResult(result, tin, len(raw) // 2, raw)


class OpenAICompatBackend:
    """OpenAI 兼容端点客户端。

    端点、模型、解码参数、超时**全部来自 Profile**，不再从进程级环境变量读取——
    否则同一进程内的不同 Agent 无法用不同模型，异构对抗也就无从谈起。
    仅用标准库 urllib，不引入 SDK 依赖。

    真机上必须处理而 stub 模式下不存在的三件事：

    - **瞬时故障**：限流与 5xx 要退避重试，鉴权与参数错误要立刻上抛。
      对必然失败的请求重试只是在烧配额，还会把真正的错因埋在重试日志里。
    - **``response_format`` 兼容性**：本地 Ollama 与部分自建兼容层不支持该参数，
      写死会让真机第一步就 400。``json_mode: auto`` 时先试、被拒即降级并记住。
    - **输出包装**：见 :func:`_extract_json`。
    """

    kind = "http"

    #: 值得重试的状态码：限流与网关类瞬时故障。401/403/404/422 一律不重试
    RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

    def __init__(self, cfg: "ModelConfig") -> None:
        self.cfg = cfg
        # 探测到不支持 json_object 的 profile，本进程内不再重复尝试
        self._json_mode_disabled: set[str] = set()

    # ---- 请求构造 ---------------------------------------------------
    def _build_body(self, profile: Profile, system: str, payload: dict[str, Any],
                    turns: list[dict[str, str]]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": profile.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                *turns,
            ],
            "temperature": profile.temperature,
        }
        if self._use_json_mode(profile):
            body["response_format"] = {"type": "json_object"}
        if profile.top_p is not None:
            body["top_p"] = profile.top_p
        if profile.seed is not None:
            body["seed"] = profile.seed
        if profile.max_output_tokens is not None:
            body["max_tokens"] = profile.max_output_tokens
        return body

    def _use_json_mode(self, profile: Profile) -> bool:
        if profile.json_mode == "off" or profile.name in self._json_mode_disabled:
            return False
        return True

    # ---- 调用 -------------------------------------------------------
    def call(self, profile: Profile, task: str, system: str,
             payload: dict[str, Any], *,
             turns: list[dict[str, str]] | None = None) -> BackendResult:
        api_key = self.cfg.resolve_credential(profile.provider)
        url = f"{profile.provider.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        attempt = 0
        while True:
            body = self._build_body(profile, system, payload, list(turns or []))
            req = urllib.request.Request(
                url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=profile.timeout_ms / 1000) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                # json_object 不被支持：降级重试一次，且本进程内记住
                if (e.code in (400, 404, 422) and profile.json_mode == "auto"
                        and self._use_json_mode(profile)
                        and ("response_format" in detail or "json_object" in detail
                             or "json_mode" in detail)):
                    self._json_mode_disabled.add(profile.name)
                    continue
                if e.code in self.RETRYABLE_STATUS and attempt < profile.max_retries:
                    attempt += 1
                    time.sleep(min(8.0, 0.8 * (2 ** attempt)))
                    continue
                raise InferenceError(
                    task, profile.name, f"HTTP {e.code} {detail}", attempts=attempt + 1)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < profile.max_retries:
                    attempt += 1
                    time.sleep(min(8.0, 0.8 * (2 ** attempt)))
                    continue
                raise InferenceError(
                    task, profile.name,
                    f"端点不可达或超时（{profile.provider.base_url}）：{e}",
                    attempts=attempt + 1) from e

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise InferenceError(task, profile.name,
                                 f"端点返回结构非 OpenAI 兼容格式：{str(data)[:200]}",
                                 attempts=attempt + 1) from e
        content = message.get("content") or ""
        usage = data.get("usage") or {}
        try:
            obj = _extract_json(content)
        except ValueError as e:
            # 不在此处放弃：解析失败是修复轮最该处理的情形，
            # 带上原文抛出去，由网关决定是重试还是执行失败策略
            raise OutputParseError(content, str(e)) from e
        return BackendResult(
            obj,
            int(usage.get("prompt_tokens", 0) or 0),
            int(usage.get("completion_tokens", 0) or 0),
            raw=content,
        )


class LLMGateway:
    """全系统唯一的推理入口。

    职责是**按调用方解析模型档位**——这正是单一全局配置做不到的事。
    每次调用都记账（档位、族、厂商、token、费用），供 Trace、指标与成本口径使用。

    两种模式：

    - ``stub``（默认）一律走确定性推理器，但仍解析并上报该 Agent **声明绑定**的
      档位。这样 Trace 里既能看到「实际用的是确定性推理器」，也能看到
      「生产上这一步会走哪个模型」，两件事都不含糊。
    - ``live`` 按各 Agent 绑定的档位分别调用真实端点。
    """

    def __init__(self, cfg: "ModelConfig", mode: str = "stub") -> None:
        if mode not in ("stub", "live"):
            raise ValueError(f"未知 LLM 模式：{mode}（可选 stub / live）")
        self.cfg = cfg
        self.mode = mode
        self.name = mode
        self._deterministic = DeterministicBackend()
        self._http = OpenAICompatBackend(cfg) if mode == "live" else None
        self.calls: list[dict[str, Any]] = []
        # 按失败策略降级的记录。空列表是常态，非空必须出现在 metrics 与审计里
        self.degradations: list[dict[str, Any]] = []

    # ---- 档位解析 ---------------------------------------------------
    def profile_for(self, caller: str) -> Profile:
        return self.cfg.profile_for(caller)

    def span_attrs(self, caller: str) -> dict[str, Any]:
        """供调用方补充到 llm Span 上的属性。

        实际用的模型与声明绑定的模型分开上报，避免 stub 模式下把
        「本该用 qwen-max」写成「用了 qwen-max」。
        """
        p = self.profile_for(caller)
        actual = "deterministic-reasoner/v1" if self._is_stubbed(p) else p.model
        return {
            GEN_AI_SYSTEM: self.mode,
            GEN_AI_OPERATION: "chat",
            GEN_AI_REQUEST_MODEL: actual,
            "model.profile": p.name,
            "model.family": p.family,
            "model.provider": p.provider.name,
            "model.declared": p.model,
            "model.stubbed": self._is_stubbed(p),
        }

    def _is_stubbed(self, profile: Profile) -> bool:
        return self.mode == "stub" or profile.is_deterministic

    # ---- 调用 -------------------------------------------------------
    def complete_json(self, task: str, system: str, payload: dict[str, Any],
                      *, caller: str,
                      validator: Callable[[dict[str, Any]], list[str]] | None = None,
                      ) -> tuple[dict[str, Any], int, int]:
        """执行一次带契约的推理。

        循环固定为：**调用 → 归一化 → 结构校验 → 语义校验 → （至多一轮）修复**。

        ``validator`` 是语义校验的注入点。结构校验只认识 Schema，判断不了
        「这个 evidence_id 在账本里存不存在」；而账本知识不该反向渗进 Schema 层，
        所以由 Skill 层把校验器传进来（见 ``skills.risk_root_cause``）。

        为什么修复轮只给一轮：修复轮是给**表述失误**的机会，不是给模型反复试错的机会。
        两轮之后仍不合规，通常说明模型确实不理解这个任务，继续重试只是把
        失败推后并放大成本。到点即按失败策略处置——这在金融场景里比「再试试」正确。
        """
        profile = self.profile_for(caller)
        stubbed = self._is_stubbed(profile)
        backend = self._deterministic if stubbed else self._http
        assert backend is not None

        started = time.time()
        turns: list[dict[str, str]] = []
        attempts = 0
        errors: list[str] = []
        result: dict[str, Any] = {}
        tin = tout = 0

        while True:
            attempts += 1
            try:
                res = backend.call(profile, task, system, payload, turns=turns)
                candidate = schemas.normalize(task, res.data)
                errors = schemas.validate(task, candidate)
                if not errors and validator is not None:
                    errors = list(validator(candidate))
                raw = res.raw
            except OutputParseError as e:
                candidate, raw = {}, e.raw
                errors = [f"输出不是合法 JSON：{e}"]
                res = BackendResult({}, 0, 0, raw)
            except InferenceError:
                # 传输层已重试用尽（超时 / 5xx / 限流），或鉴权失败不予重试。
                # 同样记一笔账再上抛：真机排障时「调用了几次、耗时多久」是关键线索
                self._record(caller, task, profile, stubbed, tin, tout,
                             attempts, started, failed=True)
                raise

            tin, tout = tin + res.tokens_in, tout + res.tokens_out
            if not errors:
                result = candidate
                break

            # 确定性推理器输出不合规是**代码缺陷**，不是模型发挥问题：
            # 立刻失败，绝不进入修复轮——修复轮会把代码 bug 伪装成偶发抖动
            if stubbed:
                raise InferenceError(task, caller, "确定性推理器输出违反自身输出契约",
                                     attempts=attempts, errors=errors)
            if attempts > MAX_REPAIR_ROUNDS:
                # 失败的调用同样消耗了 token，也同样要计费。不记账会让成本口径
                # 在「模型表现不好」时系统性偏低——恰恰是最需要如实统计的那种情况
                self._record(caller, task, profile, stubbed, tin, tout,
                             attempts, started, failed=True)
                raise InferenceError(task, caller, "输出契约校验未通过且修复轮已用尽",
                                     attempts=attempts, errors=errors)
            turns = [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": schemas.repair_note(task, errors)},
            ]

        self._record(caller, task, profile, stubbed, tin, tout, attempts, started)
        return result, tin, tout

    def _record(self, caller: str, task: str, profile: Profile, stubbed: bool,
                tin: int, tout: int, attempts: int, started: float,
                *, failed: bool = False) -> None:
        self.calls.append({
            "caller": caller, "task": task, "profile": profile.name,
            "family": profile.family, "provider": profile.provider.name,
            # declared = 权限矩阵声明的绑定；actual = 本次真正跑了什么。
            # 两者分开记，避免 stub 模式下把「本该用 qwen-max」写成「用了 qwen-max」
            "declared_model": profile.model,
            "actual_model": "deterministic-reasoner/v1" if stubbed else profile.model,
            "stubbed": stubbed,
            "tokens_in": tin, "tokens_out": tout,
            "cost_cny": 0.0 if stubbed else profile.cost(tin, tout),
            # 「模型第几次才给出合规输出」本身是质量信号，不该被吞掉
            "attempts": attempts,
            "repaired": attempts > 1 and not failed,
            "failed": failed,
            "latency_ms": round((time.time() - started) * 1000, 1),
        })

    # ---- 失败策略 ---------------------------------------------------
    def record_degradation(self, *, caller: str, task: str, err: "InferenceError",
                           fallback: str) -> dict[str, Any]:
        """登记一次按失败策略的降级，并返回可写进 Span 的属性。

        降级**必须留痕**。一个悄悄降级的系统和一个没有降级机制的系统，
        在事后复核时是一样的——都无法回答「这个结论当时是怎么来的」。
        """
        rec = {
            "caller": caller, "task": task, "reason": err.reason,
            "attempts": err.attempts, "errors": err.errors[:6], "fallback": fallback,
        }
        self.degradations.append(rec)
        return {
            "llm.degraded": True,
            "llm.degrade_reason": err.reason,
            "llm.attempts": err.attempts,
            "llm.schema_errors": err.errors[:6],
            "llm.fallback": fallback,
        }

    # ---- 汇总 -------------------------------------------------------
    def usage(self) -> dict[str, Any]:
        """按 Agent 分解的用量与成本。实验 E5 的数据来源。"""
        by_caller: dict[str, dict[str, Any]] = {}
        for c in self.calls:
            slot = by_caller.setdefault(c["caller"], {
                "profile": c["profile"], "family": c["family"],
                "provider": c["provider"], "declared_model": c["declared_model"],
                "actual_model": c["actual_model"], "stubbed": c["stubbed"],
                "calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_cny": 0.0,
                # 修复轮次数是质量信号：某个 Agent 频繁触发修复，
                # 说明它的提示词或模型档位需要调整，而不是「偶尔抖一下」
                "repairs": 0, "failures": 0, "latency_ms": 0.0,
            })
            slot["calls"] += 1
            slot["tokens_in"] += c["tokens_in"]
            slot["tokens_out"] += c["tokens_out"]
            slot["cost_cny"] = round(slot["cost_cny"] + c["cost_cny"], 6)
            slot["repairs"] += 1 if c.get("repaired") else 0
            slot["failures"] += 1 if c.get("failed") else 0
            slot["latency_ms"] = round(slot["latency_ms"] + c.get("latency_ms", 0.0), 1)
        return {
            "mode": self.mode,
            "by_caller": by_caller,
            "total_cost_cny": round(sum(c["cost_cny"] for c in self.calls), 6),
            "repair_rounds": sum(1 for c in self.calls if c.get("repaired")),
            "failed_calls": sum(1 for c in self.calls if c.get("failed")),
            # 降级必须出现在指标里。降级了却指标全绿，等于系统在替自己遮掩
            "degradations": list(self.degradations),
        }


def get_llm(mode: str, *, cfg: "ModelConfig | None" = None,
            overrides: dict[str, str] | None = None) -> LLMGateway:
    """构造网关。cfg 缺省时从 ``config/`` 加载并校验全部不变量。"""
    return LLMGateway(cfg or modelconfig.load(overrides=overrides), mode)
