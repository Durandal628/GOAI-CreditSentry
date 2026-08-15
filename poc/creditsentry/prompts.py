"""版本化提示词注册表。

提示词此前硬编码在函数体里，有两个后果：改监管口径等于改代码；Trace 里没有
``prompt_version``，事后无法回答「当时用的是哪一版提示词」。在金融场景这是硬伤——
复核一个三个月前的处置结论时，你必须能重建当时的全部输入。

本模块把提示词变成**带版本的资产**：与 Skill 同构，可经 Nacos 灰度发布与回滚，
版本号进 Span，改动留 changelog。

三条约定：

1. **缺槽位即抛错**，不静默留下 ``{slot}`` 字面量——渲染失败比渲染出半成品安全；
2. **多余槽位也抛错**——模板改了但调用方没跟上，必须当场发现；
3. **目标函数单独成字段**（``objective``）而非埋在正文里。对抗双方的差异就是这一句，
   把它拎出来才能证明「两个角色确实在做不同的事」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_SLOT_RE = re.compile(r"\{([a-z_]+)\}")


class PromptError(Exception):
    """模板渲染失败：槽位缺失、槽位多余，或引用了不存在的模板。"""


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: str
    task: str          # 对应 llm.complete_json 的 task
    role: str          # 供哪个 Agent 使用
    objective: str     # 目标函数，一句话。对抗设计的核心差异在此
    body: str
    changelog: str

    @property
    def slots(self) -> tuple[str, ...]:
        return tuple(sorted(set(_SLOT_RE.findall(self.body))))

    def render(self, **values: Any) -> str:
        want = set(self.slots)
        got = set(values)
        if missing := want - got:
            raise PromptError(f"{self.name}@{self.version} 缺少槽位：{sorted(missing)}")
        if extra := got - want:
            raise PromptError(
                f"{self.name}@{self.version} 传入了模板中不存在的槽位：{sorted(extra)}"
                f"（模板可能已升级，请同步调用方）"
            )
        return self.body.format(**values)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

REGISTRY: dict[str, PromptTemplate] = {}


def _reg(t: PromptTemplate) -> PromptTemplate:
    REGISTRY[t.name] = t
    return t


_reg(PromptTemplate(
    name="risk_root_cause",
    version="2.0.0",
    task="risk_root_cause",
    role="risk-analyst",
    objective="求证「风险成立」：从已登记证据中归因，宁可判 INSUFFICIENT 也不推测",
    body=(
        "你是银行贷后风险定性官。\n"
        "\n"
        "## 你的目标\n"
        "{objective}\n"
        "\n"
        "## 硬约束\n"
        "- 只能使用下方 `facts` 中已登记的证据，**不得引入任何外部知识作为事实**；\n"
        "- 每一条根因都必须给出 `evidence_ids`，引用不存在的证据会被系统直接拒绝；\n"
        "- 证据不足时输出 `conclusion=INSUFFICIENT` 与证据缺口清单，"
        "严禁以推测填补缺失证据；\n"
        "- 你**没有任何取证工具**。这是刻意的能力剥夺——边查边下结论会产生确认偏差。\n"
        "\n"
        "## 你看到的政策依据已按案件时点过滤\n"
        "{policy_note}\n"
        "\n"
        "## 输出\n"
        "严格输出 JSON：`{{conclusion, root_causes[{{type, confidence, evidence_ids, "
        "rationale}}], suggested_grade, summary}}`。"
    ),
    changelog="2.0.0 抽出目标函数字段、加入时点过滤说明；1.x 为内联硬编码版本",
))


_reg(PromptTemplate(
    name="devils_advocate",
    version="2.0.0",
    task="devils_advocate",
    role="devils-advocate",
    objective="求证「风险不成立」：逐条证伪定性方的每一个主因，宁可漏杀不可误杀",
    body=(
        "你是银行风险评审中的对抗质疑官。\n"
        "\n"
        "## 你的目标\n"
        "{objective}\n"
        "\n"
        "## 你必须逐条处理的质疑清单（共 {item_count} 项，缺一不可）\n"
        "\n"
        "{checklist}\n"
        "\n"
        "每一项必须返回下列结论之一：\n"
        "- `REFUTED` —— 找到了成立的反证或替代解释，该主因不成立；\n"
        "- `ATTEMPTED_FAILED` —— 尝试过反驳但不成立，须写明**试了什么、为何失败**；\n"
        "- `INSUFFICIENT` —— 现有证据不足以判断该主因，须写明缺什么。\n"
        "\n"
        "**不得跳过任何一项。** 系统会逐项核对覆盖情况；"
        "存在未处理项即判定为「质疑未完成」，案件被阻断而非放行。\n"
        "\n"
        "## 硬约束\n"
        "- 下钻到**个案层面**：逐笔看案由、标的额占比、结案状态、诉讼地位；"
        "逐个对手方看是否关联方、是否落在历史波动区间；\n"
        "- 反驳不成立也必须完整记录尝试过程——"
        "「我们试过反驳但没能反驳掉」本身是支撑处置结论的重要证据；\n"
        "- 你**不得提出任何处置方案**。既当质疑者又当决策者会让质疑失去意义；\n"
        "- 你**没有任何取证工具**，与定性方拿到的是完全相同的输入。\n"
        "\n"
        "## 你看到的政策依据已按案件时点过滤\n"
        "{policy_note}\n"
        "\n"
        "## 输出\n"
        "严格输出 JSON：`{{verdict, rebuttals[], attempted_but_failed[], "
        "evidence_gaps[], surviving_causes[], checklist_resolutions[{{item_id, "
        "status, resolution}}]}}`。"
    ),
    changelog="2.0.0 引入质疑清单块与逐项覆盖契约；1.x 仅有「尽力证伪」这类无法核验的软要求",
))


def get(name: str) -> PromptTemplate:
    if name not in REGISTRY:
        raise PromptError(f"未注册的提示词模板：{name}（可用：{sorted(REGISTRY)}）")
    return REGISTRY[name]


def render(name: str, **values: Any) -> tuple[str, str]:
    """渲染模板，返回 (文本, 版本号)。版本号必须写进 Span。"""
    t = get(name)
    return t.render(objective=t.objective, **values), t.version
