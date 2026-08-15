"""被 4 个 MCP Server 共同查询的「系统之记录」。

Mock 与真实接入的关键约定：**Server 对外的 Tool 定义与 Schema 完全一致，
只有本模块（数据来源）不同**。真实部署时把 ``World`` 换成行内数据库/接口客户端即可，
Agent、Skill、路由表与契约测试均无需改动。
"""

from __future__ import annotations

import json
import os
from typing import Any

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "poc", "fixtures")


class World:
    """一次案件对应的全量业务数据快照。

    **时点冻结**：回溯案例的历史结局（``retrospective_outcome``）在构造时就被
    摘出到 :attr:`retrospective`，不留在 ``self.data`` 里。因此任何 Server、Skill 或
    Agent 都**够不到**它——前视信息污染在结构上被杜绝，而不是靠约定或事后检查。
    该字段只供评分程序在案件跑完之后读取。
    """

    def __init__(self, data: dict[str, Any]) -> None:
        data = dict(data)
        self.retrospective: dict[str, Any] | None = data.pop("retrospective_outcome", None)
        self.data = data

    @property
    def as_of(self) -> str | None:
        """案件的决策时点。所有证据的首次公开日期都必须不晚于它。"""
        return self.data.get("as_of_date")

    @classmethod
    def load(cls, case_key: str, overrides: dict[str, Any] | None = None) -> "World":
        """加载案件数据，可按点号路径覆盖若干字段。

        ``overrides`` 存在的理由不是「方便调参」，而是让**结论由证据驱动**这件事
        可以被当场检验：改掉一个决定性因子（如把涉诉标的占敞口从 30% 改成 1%），
        重跑应当翻案。做不到，说明结论其实是硬编码的。

        刻意只支持点号路径与 ``[i]`` 下标这种朴素写法，不做通配：
        可改的字段必须是**能被逐个说清楚**的那几个，而不是任意改写案件数据。
        可改字段清单见 :data:`FACTORS`。
        """
        path = os.path.join(FIXTURE_DIR, f"{case_key}.json")
        if not os.path.exists(path):
            raise FileNotFoundError(f"未找到案件数据：{path}")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for dotted, value in (overrides or {}).items():
            _set_path(data, dotted, value)
        return cls(data)

    # 便捷访问器
    @property
    def case_id(self) -> str:
        return self.data["case_id"]

    @property
    def subject(self) -> dict[str, Any]:
        return self.data["subject"]

    def section(self, name: str) -> dict[str, Any]:
        return self.data.get(name, {})


def _set_path(obj: Any, dotted: str, value: Any) -> None:
    """按 ``a.b[0].c`` 形式的路径写值。路径不存在即抛错，不静默创建。

    静默创建会让一个拼错的路径表现为「改了但没效果」——在演示现场，
    这比直接报错糟糕得多。
    """
    cur = obj
    # 同时接受 a.b[0].c 与 a.b.[0].c 两种写法：前者是习惯，后者更好读
    parts = [p for p in dotted.replace("[", ".[").split(".") if p]
    for i, raw in enumerate(parts):
        last = i == len(parts) - 1
        if raw.startswith("[") and raw.endswith("]"):
            idx = int(raw[1:-1])
            if not isinstance(cur, list) or not -len(cur) <= idx < len(cur):
                raise KeyError(f"路径 {dotted!r} 的下标 {raw} 越界或对象不是列表")
            if last:
                cur[idx] = value
            else:
                cur = cur[idx]
            continue
        if not isinstance(cur, dict) or raw not in cur:
            raise KeyError(f"路径 {dotted!r} 中的 {raw!r} 不存在")
        if last:
            cur[raw] = value
        else:
            cur = cur[raw]


#: 可当场调节的决定性因子。**这份清单本身就是边界**——
#: 递给评委的是「可改字段卡片」，既显得开放，又不至于让案件数据被改成任意形状。
#: 每个因子都绑定一条业务口径，而不是随便挑的字段（见 docs/接口与实验方案.md §2.2）。
FACTORS: dict[str, list[dict[str, Any]]] = {
    "case_001": [
        {
            "key": "litigation_material",
            "label": "涉诉实质性",
            "why": "未结案 + 我方被告 + 标的占敞口 ≥5% 才构成偿债能力信号",
            "binding": "MATERIAL_RATIO = 5%（《贷后风险信号认定标准》）",
            "options": [
                {"name": "实质性成立（原值）", "expect": "该主因不被推翻",
                 "patch": {"judicial.total_amount_ratio": 0.3,
                           "judicial.cases.[0].amount_ratio": 0.155,
                           "judicial.cases.[0].closed": False,
                           "judicial.cases.[0].our_role": "被告",
                           "judicial.cases.[1].amount_ratio": 0.097,
                           "judicial.cases.[1].closed": False,
                           "judicial.cases.[1].our_role": "被告",
                           "judicial.cases.[2].amount_ratio": 0.048,
                           "judicial.cases.[2].closed": False,
                           "judicial.cases.[2].our_role": "被告"}},
                {"name": "不具实质性（已结案 · 我方原告 · 占比 <5%）",
                 "expect": "质疑方推翻「偿债能力恶化」",
                 "patch": {"judicial.total_amount_ratio": 0.012,
                           "judicial.cases.[0].amount_ratio": 0.006,
                           "judicial.cases.[0].closed": True,
                           "judicial.cases.[0].our_role": "原告",
                           "judicial.cases.[1].amount_ratio": 0.004,
                           "judicial.cases.[1].closed": True,
                           "judicial.cases.[1].our_role": "原告",
                           "judicial.cases.[2].amount_ratio": 0.002,
                           "judicial.cases.[2].closed": True,
                           "judicial.cases.[2].our_role": "原告"}},
            ],
        },
        {
            "key": "txn_counterparty",
            "label": "资金对手方性质",
            "why": "穿透后为关联方则资金流向体外；稳定供应商则可用正常采购解释",
            "binding": "《贷后资金用途监控》",
            # 注意 patch 打在**对手方明细**上而不是 txn.counterparty_related_party：
            # 后者是 fixture 里的冗余声明，真正驱动判定的是 TxnFlowAnalyze 从
            # 每个对手方的 related_party 逐条推导出来的结果。改错位置会表现为
            # 「改了但没效果」——这恰好也说明系统认的是明细，不是那个总结性标签。
            "options": [
                {"name": "关联方（原值）", "expect": "该主因不被推翻",
                 "patch": {"txn.counterparties.[0].related_party": True,
                           "txn.counterparties.[1].related_party": True,
                           "txn.counterparty_related_party": True,
                           "txn.within_baseline_band": False}},
                {"name": "历史稳定供应商且落在波动区间内",
                 "expect": "质疑方推翻「资金用途异常」",
                 "patch": {"txn.counterparties.[0].related_party": False,
                           "txn.counterparties.[1].related_party": False,
                           "txn.counterparty_related_party": False,
                           "txn.within_baseline_band": True}},
            ],
        },
        {
            "key": "registration_change",
            "label": "法代变更时点",
            "why": "与风险窗口重合且同期股权变动，才构成逃废债信号",
            "binding": "公司治理信号口径",
            "options": [
                {"name": "与风险窗口重合（原值）", "expect": "该主因不被推翻",
                 "patch": {"judicial.change_history.change_overlaps_risk_window": True,
                           "judicial.change_history.equity_changed": True}},
                {"name": "早于风险窗口且股权未变动",
                 "expect": "质疑方推翻「实际控制人风险」",
                 "patch": {"judicial.change_history.change_overlaps_risk_window": False,
                           "judicial.change_history.equity_changed": False}},
            ],
        },
        {
            "key": "exposure",
            "label": "敞口金额",
            "why": "同一个「压降 30%」动作，620 万时是 L2，超 2000 万升为 L3",
            "binding": "RiskGate G-06 / G-07 敞口升档阈值",
            "options": [
                {"name": "620 万（原值）", "expect": "L2 · 审批后执行",
                 "patch": {"credit_core.total_exposure": 6200000}},
                {"name": "2 亿（大型集团客户）", "expect": "升档 L3 · 只出方案不执行",
                 "patch": {"credit_core.total_exposure": 200000000}},
            ],
        },
    ],
}


class MCPError(Exception):
    """MCP 工具调用失败。错误码用于驱动 Skill 侧的 failure policy。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
