"""LLM 输出的 Schema 契约、归一化与校验。

`stub` 模式下模型输出由代码构造，字段必然齐全；`live` 模式下它由模型生成，
**没有任何东西保证它齐全**。这两者之间的落差正是 `docs/接口与实验方案.md`
记的缺口 3：「live 模式模型返回不合规 JSON 时直接崩到下游」。

本模块是那道缺口的执行点，做三件事，顺序不能颠倒：

1. :func:`normalize` —— 吸收**表述差异**。模型把 ``confidence`` 写成 ``"0.62"``
   或 ``62``、把单元素列表写成裸值、把结果套一层 ``{"result": ...}``——
   这些是形式偏差不是语义错误，纠正它们不改变模型的判断，不该浪费一轮修复。
2. :func:`validate` —— 拒绝**语义违规**。枚举越界、置信度越界、根因无证据引用、
   质疑清单漏项。这些不能替模型「猜一个」，只能回喂错误让它自己改。
3. :func:`repair_note` —— 把校验错误组装成给模型的修复指令。

一条刻意的边界：**本模块只认识结构，不认识业务**。「证据 ID 是否真实存在」
需要账本才能判断，因此以 ``validator`` 回调的形式由 Skill 层注入
（见 ``skills.risk_root_cause``），不写进这里——否则 Schema 层就要反向依赖账本。

为什么不用 jsonschema 库：全项目零第三方依赖是硬约束（见 README 依赖披露），
且我们需要的校验只有两个 task、十余个字段，自己写反而能给出**面向模型的中文错误信息**——
这些错误是要回喂给模型做修复的，可读性直接决定修复成功率。
"""

from __future__ import annotations

from typing import Any, Callable

# ---------------------------------------------------------------------------
# 枚举：与下游消费方一一对应，改这里必须同步改下游
# ---------------------------------------------------------------------------

#: 定性结论。下游 ``routing.adjudicate`` 只识别这两个值
CONCLUSIONS = ("RISK_CONFIRMED", "INSUFFICIENT")

#: 质疑裁定。下游 ``routing.adjudicate`` 的 ``verdict_map`` 键集合，
#: 多一个值就会 KeyError——这正是必须在此拦住的原因
VERDICTS = ("REFUTED", "PARTIALLY_REFUTED", "INSUFFICIENT_EVIDENCE", "SUPPORTED")

#: 五级分类。``gate.evaluate`` 的 risk_grade 入参
GRADES = ("正常", "关注", "次级", "可疑", "损失")

#: 质疑清单逐项回执状态，与 ``checklist.ADDRESSED`` 中质疑类的三个状态一致
RESOLUTION_STATUS = ("REFUTED", "ATTEMPTED_FAILED", "INSUFFICIENT")


class SchemaError(Exception):
    """输出不满足契约，且修复轮后仍不满足。"""


# ---------------------------------------------------------------------------
# 极简 Schema DSL
# ---------------------------------------------------------------------------
# 字段声明支持的键：
#   type      str / num / bool / list / obj
#   required  缺失是否算错（默认 True）
#   nullable  是否允许 None（默认 False）
#   enum      取值白名单
#   min/max   数值区间
#   item      list 元素为对象时的子 Schema
#   item_type list 元素为标量时的类型
#   min_len   list 最小长度
#   any_of    该对象内至少要出现其中一个键

_TYPES: dict[str, tuple] = {
    "str": (str,),
    "num": (int, float),
    "bool": (bool,),
    "list": (list,),
    "obj": (dict,),
}

_ROOT_CAUSE = {
    "type": {"type": "str"},
    "confidence": {"type": "num", "min": 0.0, "max": 1.0},
    # 「无证据不决策」在 Schema 层的第一道执行点：空列表即违规。
    # 账本层的 assert_supported 是第二道，两道都要有——
    # 第一道能把错误回喂给模型修复，第二道是不可绕过的硬拒绝。
    "evidence_ids": {"type": "list", "item_type": "str", "min_len": 1},
    "rationale": {"type": "str", "required": False},
}

_REBUTTAL = {
    "target": {"type": "str"},
    "argument": {"type": "str"},
    "counter_evidence_ids": {"type": "list", "item_type": "str", "required": False},
}

_ATTEMPTED = {
    "target": {"type": "str"},
    "tried": {"type": "str", "required": False},
    "failed_because": {"type": "str", "required": False},
}

_RESOLUTION = {
    # item_id 与 target 至少给一个——模型有时只记得住目标名，有时只记得住编号。
    # 两者都缺才是真问题：那样系统无法把回执对上任何一条清单项。
    "item_id": {"type": "str", "required": False},
    "target": {"type": "str", "required": False},
    "status": {"type": "str", "enum": RESOLUTION_STATUS},
    "resolution": {"type": "str", "required": False},
    "evidence_ids": {"type": "list", "item_type": "str", "required": False},
    "any_of": ["item_id", "target"],
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "risk_root_cause": {
        "conclusion": {"type": "str", "enum": CONCLUSIONS},
        "root_causes": {"type": "list", "item": _ROOT_CAUSE},
        "suggested_grade": {"type": "str", "enum": GRADES, "nullable": True},
        "summary": {"type": "str", "required": False},
    },
    "devils_advocate": {
        "verdict": {"type": "str", "enum": VERDICTS},
        "rebuttals": {"type": "list", "item": _REBUTTAL, "required": False},
        "attempted_but_failed": {"type": "list", "item": _ATTEMPTED, "required": False},
        "evidence_gaps": {"type": "list", "item_type": "str", "required": False},
        "surviving_causes": {"type": "list", "item_type": "str", "required": False},
        "checklist_resolutions": {"type": "list", "item": _RESOLUTION},
    },
}

#: 输出契约的骨架，修复轮会把它连同错误一起回喂给模型
SKELETONS: dict[str, str] = {
    "risk_root_cause": (
        '{"conclusion": "RISK_CONFIRMED|INSUFFICIENT", '
        '"root_causes": [{"type": "<主因名>", "confidence": <0~1 小数>, '
        '"evidence_ids": ["<必须来自输入 facts 中出现过的 EV- 编号>"], '
        '"rationale": "<依据>"}], '
        '"suggested_grade": "正常|关注|次级|可疑|损失|null", "summary": "<一句话>"}'
    ),
    "devils_advocate": (
        '{"verdict": "REFUTED|PARTIALLY_REFUTED|INSUFFICIENT_EVIDENCE|SUPPORTED", '
        '"rebuttals": [{"target": "<主因名>", "argument": "<反证>", '
        '"counter_evidence_ids": []}], '
        '"attempted_but_failed": [{"target": "<主因名>", "tried": "<试了什么>", '
        '"failed_because": "<为何不成立>"}], '
        '"evidence_gaps": [], "surviving_causes": ["<未被推翻的主因名>"], '
        '"checklist_resolutions": [{"item_id": "<清单项编号 R1/R2/...>", '
        '"target": "<该项的主因名>", '
        '"status": "REFUTED|ATTEMPTED_FAILED|INSUFFICIENT", "resolution": "<结论说明>"}]}'
    ),
}


# ---------------------------------------------------------------------------
# 归一化：吸收表述差异，不改变语义
# ---------------------------------------------------------------------------

#: 模型爱套的外层包装键。见到这些且内部像目标对象就拆掉
_WRAPPERS = ("result", "output", "data", "response", "answer", "json")


def _unwrap(obj: Any) -> Any:
    """拆掉 ``{"result": {...}}`` 这类外层包装。

    只在「外层恰好一个键、且该键是已知包装名、且内层是 dict」时才拆，
    避免把模型真正想表达的单字段对象误拆。
    """
    for _ in range(3):  # 允许嵌套包装，但设上限防病态输入
        if not isinstance(obj, dict) or len(obj) != 1:
            break
        (only_key, inner), = obj.items()
        if only_key.lower() not in _WRAPPERS or not isinstance(inner, dict):
            break
        obj = inner
    return obj


def _to_number(v: Any) -> Any:
    """``"0.62"`` → 0.62；``"62%"`` → 0.62；``62`` → 0.62。

    百分数还原是有风险的猜测，因此**只在值 > 1 且 ≤ 100 时**才做——
    置信度的合法域是 [0,1]，落在这个区间的数只可能是百分数写法。
    超出 100 的值不猜，让它去撞 max 校验，由模型自己改。
    """
    if isinstance(v, str):
        s = v.strip()
        is_pct = s.endswith("%")
        try:
            num = float(s.rstrip("%").strip())
        except ValueError:
            return v
        v = num / 100.0 if is_pct else num
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 1 < v <= 100:
        return round(v / 100.0, 4)
    return v


def _as_list(v: Any) -> Any:
    """裸值 → 单元素列表；dict → 单元素列表。None 归一为空列表。"""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]


def normalize(task: str, obj: Any) -> Any:
    """把常见的表述偏差纠正到契约形状。不做任何语义补全。

    区别很关键：把 ``"0.62"`` 变成 ``0.62`` 是纠正表述；
    给一条没有 ``evidence_ids`` 的根因**编一个** ID 是伪造证据。前者做，后者绝不做。
    """
    obj = _unwrap(obj)
    if not isinstance(obj, dict):
        return obj

    out = dict(obj)

    if task == "risk_root_cause":
        if "conclusion" in out and isinstance(out["conclusion"], str):
            out["conclusion"] = out["conclusion"].strip().upper()
        causes = _as_list(out.get("root_causes"))
        fixed = []
        for c in causes:
            if not isinstance(c, dict):
                fixed.append(c)
                continue
            c = dict(c)
            if "confidence" in c:
                c["confidence"] = _to_number(c["confidence"])
            c["evidence_ids"] = [str(e) for e in _as_list(c.get("evidence_ids"))]
            fixed.append(c)
        out["root_causes"] = fixed
        g = out.get("suggested_grade")
        if isinstance(g, str) and g.strip().lower() in ("null", "none", "", "-"):
            out["suggested_grade"] = None
        # 「无根因」与「结论为 INSUFFICIENT」互为因果，模型常只写对一半。
        # 只在**两者矛盾且方向安全**时补齐：没有根因就不可能是 RISK_CONFIRMED。
        if not out["root_causes"] and out.get("conclusion") == "RISK_CONFIRMED":
            out["conclusion"] = "INSUFFICIENT"
            out["suggested_grade"] = None

    elif task == "devils_advocate":
        if "verdict" in out and isinstance(out["verdict"], str):
            out["verdict"] = out["verdict"].strip().upper().replace(" ", "_")
        for key in ("rebuttals", "attempted_but_failed", "evidence_gaps",
                    "surviving_causes", "checklist_resolutions"):
            out[key] = _as_list(out.get(key))
        out["evidence_gaps"] = [str(g) for g in out["evidence_gaps"]]
        out["surviving_causes"] = [str(s) for s in out["surviving_causes"]]
        fixed = []
        for r in out["checklist_resolutions"]:
            if not isinstance(r, dict):
                fixed.append(r)
                continue
            r = dict(r)
            if isinstance(r.get("status"), str):
                r["status"] = r["status"].strip().upper().replace(" ", "_")
            if "evidence_ids" in r:
                r["evidence_ids"] = [str(e) for e in _as_list(r["evidence_ids"])]
            fixed.append(r)
        out["checklist_resolutions"] = fixed

    return out


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _check_field(name: str, decl: dict[str, Any], holder: dict[str, Any],
                 path: str, errs: list[str]) -> None:
    where = f"{path}{name}"
    if name not in holder or holder[name] is None:
        if holder.get(name, "__absent__") is None and decl.get("nullable"):
            return
        if decl.get("required", True):
            errs.append(f"缺少必填字段 `{where}`")
        return

    val = holder[name]
    kinds = _TYPES[decl["type"]]
    # bool 是 int 的子类，数值字段收到 True 必须判错而不是当作 1
    if isinstance(val, bool) and decl["type"] == "num":
        errs.append(f"`{where}` 应为数值，收到布尔值")
        return
    if not isinstance(val, kinds):
        errs.append(f"`{where}` 类型应为 {decl['type']}，收到 {type(val).__name__}")
        return

    if (enum := decl.get("enum")) and val not in enum:
        errs.append(f"`{where}` 取值 {val!r} 不在允许集合内，只能是：{'、'.join(map(str, enum))}")
    if decl["type"] == "num":
        if "min" in decl and val < decl["min"]:
            errs.append(f"`{where}` = {val}，小于下界 {decl['min']}")
        if "max" in decl and val > decl["max"]:
            errs.append(f"`{where}` = {val}，大于上界 {decl['max']}")
    if decl["type"] == "list":
        if len(val) < decl.get("min_len", 0):
            errs.append(
                f"`{where}` 至少需要 {decl['min_len']} 个元素，收到 {len(val)} 个"
                + ("（无证据的结论一律被拒绝）" if name == "evidence_ids" else "")
            )
        if it := decl.get("item"):
            for i, elem in enumerate(val):
                if not isinstance(elem, dict):
                    errs.append(f"`{where}[{i}]` 应为对象，收到 {type(elem).__name__}")
                    continue
                _check_object(it, elem, f"{where}[{i}].", errs)
        elif (itype := decl.get("item_type")) is not None:
            for i, elem in enumerate(val):
                if not isinstance(elem, _TYPES[itype]):
                    errs.append(f"`{where}[{i}]` 应为 {itype}，收到 {type(elem).__name__}")


def _check_object(spec: dict[str, Any], obj: dict[str, Any],
                  path: str, errs: list[str]) -> None:
    for name, decl in spec.items():
        if name == "any_of":
            continue
        _check_field(name, decl, obj, path, errs)
    if any_of := spec.get("any_of"):
        if not any(obj.get(k) for k in any_of):
            errs.append(f"`{path[:-1]}` 至少需要提供 {' 或 '.join(f'`{k}`' for k in any_of)} 之一")


def validate(task: str, obj: Any) -> list[str]:
    """返回违规说明列表，空列表表示通过。错误信息面向模型编写，会被原样回喂。"""
    if task not in SCHEMAS:
        raise SchemaError(f"未登记输出契约的 task：{task}（已登记：{sorted(SCHEMAS)}）")
    if not isinstance(obj, dict):
        return [f"顶层应为 JSON 对象，收到 {type(obj).__name__}"]
    errs: list[str] = []
    _check_object(SCHEMAS[task], obj, "", errs)
    return errs


def checklist_validator(item_ids: list[str], targets: list[str]) -> Callable[[dict], list[str]]:
    """质疑清单覆盖性校验器，由 ``agents`` 注入。

    这是「未被质疑的主因 ≠ 质疑通过」在 LLM 接口层的前哨：**先给模型一次补齐的机会**，
    补齐失败才由裁决环节阻断。两者不冲突——修复轮是提醒，阻断是兜底，
    差别在于阻断永远不会因为「模型这次状态不好」而放行。
    """
    known_ids = set(item_ids)
    known_targets = set(targets)
    id_of_target = dict(zip(targets, item_ids))

    def check(result: dict[str, Any]) -> list[str]:
        covered: set[str] = set()
        errs: list[str] = []
        for r in result.get("checklist_resolutions", []):
            if not isinstance(r, dict):
                continue
            iid, tgt = r.get("item_id"), r.get("target")
            if iid in known_ids:
                covered.add(iid)
            elif tgt in known_targets:
                covered.add(id_of_target[tgt])
            else:
                errs.append(
                    f"回执 item_id={iid!r} / target={tgt!r} 对不上任何清单项，"
                    f"清单项只有：{'、'.join(f'{i}({t})' for i, t in zip(item_ids, targets))}"
                )
        if missing := [f"{i}({t})" for i, t in zip(item_ids, targets) if i not in covered]:
            errs.append(
                f"质疑清单有 {len(missing)} 项未回执：{'、'.join(missing)}。"
                f"每一项都必须给出 REFUTED / ATTEMPTED_FAILED / INSUFFICIENT 之一，不得跳过"
            )
        return errs

    return check


def repair_note(task: str, errors: list[str]) -> str:
    """把校验错误组装成修复指令。

    刻意不重发全部上下文——只发错误与契约骨架。原因有二：省 token，
    以及**让模型的注意力集中在「哪里错了」而不是重读一遍事实**。
    """
    lines = "\n".join(f"{i + 1}. {e}" for i, e in enumerate(errors))
    return (
        "你上一次的输出不满足输出契约，被系统拒绝。请**只输出修正后的完整 JSON**，"
        "不要解释、不要道歉、不要输出 Markdown 代码块。\n\n"
        f"## 校验未通过的项\n{lines}\n\n"
        f"## 必须满足的契约\n```json\n{SKELETONS[task]}\n```\n\n"
        "注意：不得为了通过校验而编造证据编号。若确实没有证据支撑某条结论，"
        "正确做法是删掉该条结论或把结论降级，而不是填一个不存在的 ID。"
    )
