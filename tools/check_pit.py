#!/usr/bin/env python3
"""时点冻结（point-in-time）校验器 —— 回溯案例可信度的地基。

    python tools/check_pit.py            # 校验全部回溯案例
    python tools/check_pit.py case_003   # 只校验指定 fixture

用真实历史案例做回测，最容易也最致命的错误是**前视信息污染**（look-ahead bias）：
整理案例时把「后来才披露的信息」放进了决策时点的证据里。系统当然能做对，
但那是作弊，而且外行看不出来。所以「本回测无前视信息」这句话必须能被执行验证，
否则只是一句自我声明。

五条检查：

1. **首次公开日不得晚于 as_of** —— 逐条比对 ``first_public_date <= as_of_date``。
   这是量化研究里 point-in-time correctness 的标准做法。
2. **历史结局不可达** —— ``retrospective_outcome`` 在 ``World`` 构造时即被摘出，
   此处断言它不在 ``world.data`` 中，任何 Server / Skill / Agent 都取不到。
3. **结局内容不得泄漏进产出** —— 把结局里的关键词与年份在 trace / 证据账本 /
   MCP 审计日志中做反向搜索，确认没有从别的路径漏进来。
4. **知识维不得前视** —— 召回的政策条款生效日不得晚于 as_of。
   前三条只管「事实」，管不到「知识」；用后生效的条款评价历史案件是同一类错误，
   而且更隐蔽，因为条款看起来「一直都在」。这一条是跑真实回溯案例才暴露出来的。
5. **provenance 必须标注** —— 每个数据节点都要说明自己是真实公开、推导、还是合成。
   没标注的一律视为未审查，直接失败。

前四条是硬约束；第五条是可信度约束——一份说不清哪里是真的的数据集，
在金融场景里没有使用价值。
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.world import World  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(ROOT, "poc", "fixtures")
OUT_ROOT = os.path.join(ROOT, "poc", "out")

VALID_PROVENANCE = {"real_public", "derived", "synthetic"}
DATE_RE = re.compile(r"^\d{4}(-\d{2}){0,2}$")


def _walk(node, path="$"):
    """深度遍历，产出 (路径, 节点)。只在 dict / list 上递归。"""
    yield path, node
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


def _norm(d: str) -> str:
    """把 YYYY / YYYY-MM 补齐为可比较的 YYYY-MM-DD，缺位按当期最早日补。"""
    parts = d.split("-")
    while len(parts) < 3:
        parts.append("01")
    return "-".join(p.zfill(2) if i else p for i, p in enumerate(parts))


def check_fixture(name: str) -> list[str]:
    path = os.path.join(FIXTURE_DIR, f"{name}.json")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    problems: list[str] = []
    as_of = raw.get("as_of_date")
    if not as_of:
        return []  # 非回溯案例，本校验器不适用

    world = World.load(name)

    # ---- 检查 1：首次公开日不得晚于决策时点 ----------------------------
    seen_dates = 0
    for p, node in _walk(raw):
        if not isinstance(node, dict):
            continue
        fpd = node.get("first_public_date")
        if fpd is None:
            continue
        if not DATE_RE.match(str(fpd)):
            problems.append(f"[日期格式] {p}.first_public_date = {fpd!r} 非法")
            continue
        seen_dates += 1
        if _norm(str(fpd)) > _norm(as_of):
            problems.append(
                f"[前视信息] {p}.first_public_date = {fpd} 晚于 as_of {as_of}，"
                f"该证据在决策时点尚未公开"
            )
    if seen_dates == 0:
        problems.append("[覆盖不足] 回溯案例中没有任何 first_public_date 标注，无法验证时点")

    # ---- 检查 2：历史结局在结构上不可达 --------------------------------
    if "retrospective_outcome" in world.data:
        problems.append(
            "[结局可达] retrospective_outcome 仍留在 world.data 中，"
            "Agent 可以取到历史结局，回测结论不可信"
        )
    if world.retrospective is None:
        problems.append("[缺少结局] 回溯案例应提供 retrospective_outcome 供事后评分")

    # ---- 检查 3：结局内容未从别的路径泄漏进产出 ------------------------
    if world.retrospective:
        # 取结局中的年份与判定词作为泄漏指纹
        blob = json.dumps(world.retrospective, ensure_ascii=False)
        fingerprints = set(re.findall(r"20\d{2}", blob)) - set(re.findall(r"20\d{2}", as_of))
        fingerprints |= {world.retrospective.get("outcome", "")} - {""}
        # 产出目录以 run_demo 的 case_key 命名（case_003 → CASE-003）
        case_dir = os.path.join(OUT_ROOT, "CASE-" + name.split("_")[-1])
        for fname in ("trace.json", "evidence_ledger.json", "mcp_audit.jsonl"):
            fpath = os.path.join(case_dir, fname)
            if not os.path.exists(fpath):
                continue
            text = open(fpath, encoding="utf-8").read()
            for fp in sorted(fingerprints):
                if fp and fp in text:
                    problems.append(
                        f"[结局泄漏] {fname} 中出现历史结局指纹 {fp!r}，"
                        f"结局信息可能经其他路径进入了决策链路"
                    )

    # ---- 检查 4：知识维前视 ---------------------------------------------
    # 证据层的时点冻结只管「事实」，管不到「知识」。用后生效的条款或后沉淀的
    # 案例经验去评价历史案件，是同一类错误，而且更隐蔽——条款看起来「一直都在」。
    led_path = os.path.join(OUT_ROOT, "CASE-" + name.split("_")[-1],
                            "evidence_ledger.json")
    if os.path.exists(led_path):
        with open(led_path, encoding="utf-8") as f:
            led = json.load(f)
        policy_items = [e for e in led["items"] if e["source_system"] == "policy-kb"]
        for e in policy_items:
            eff = e["extracted"].get("effective_date")
            if eff and _norm(eff) > _norm(as_of):
                problems.append(
                    f"[知识维前视] 召回了 {eff} 才生效的条款"
                    f"「{e['extracted'].get('title')}」，晚于案件时点 {as_of}"
                )
        if not policy_items:
            problems.append(
                "[覆盖不足] 未召回任何政策条款——可能是时效过滤过度，"
                "需确认该时点确实无可用条款"
            )

    # ---- 检查 5：provenance 标注覆盖 ------------------------------------
    #  只检查承载事实的顶层数据节点，避免对纯结构字段提出无意义要求
    for section in ("subject", "credit_core", "bureau", "judicial", "txn"):
        node = raw.get(section)
        if not isinstance(node, dict):
            continue
        has = any(
            isinstance(n, dict) and n.get("provenance") in VALID_PROVENANCE
            for _, n in _walk(node)
        ) or node.get("_provenance_note")
        if not has:
            problems.append(
                f"[未标注来源] {section} 既无 provenance 字段也无 _provenance_note，"
                f"无法判断其中哪些是真实公开事实"
            )
    for p, node in _walk(raw):
        if isinstance(node, dict) and "provenance" in node:
            if node["provenance"] not in VALID_PROVENANCE:
                problems.append(
                    f"[来源非法] {p}.provenance = {node['provenance']!r}，"
                    f"应为 {sorted(VALID_PROVENANCE)} 之一"
                )

    return problems


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    names = sorted(
        f[:-5] for f in os.listdir(FIXTURE_DIR)
        if re.fullmatch(r"case_\d+\.json", f)      # 只取案件 fixture，排除政策库与案例库
    )
    if only:
        names = [only]

    print("信衡 CreditSentry · 时点冻结校验")
    print("=" * 76)
    total_problems = 0
    checked = 0

    for name in names:
        with open(os.path.join(FIXTURE_DIR, f"{name}.json"), encoding="utf-8") as f:
            raw = json.load(f)
        if not raw.get("as_of_date"):
            print(f"  跳过    {name:<12} 非回溯案例（无 as_of_date），本校验不适用")
            continue
        checked += 1
        problems = check_fixture(name)
        total_problems += len(problems)
        status = "通过" if not problems else "失败"
        print(f"  {status}    {name:<12} as_of {raw['as_of_date']}"
              f"　{'无前视信息污染' if not problems else f'{len(problems)} 项问题'}")
        for p in problems:
            print(f"          → {p}")

    print("=" * 76)
    print(f"校验 {checked} 个回溯案例，{total_problems} 项问题")
    if checked and not total_problems:
        print("\n结论：全部证据的首次公开日均不晚于决策时点；历史结局在结构上不可达，"
              "亦未从其他路径泄漏进决策链路。回测结论不含前视信息。")
    return 1 if total_problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
