#!/usr/bin/env python3
"""真机自检：在花第一分钱之前，把能查的都查完。

接真实端点时会失败的地方高度集中在五处，且**每一处的报错都长得不像它真正的病因**：
凭证没设会报到一半才崩、端点写错会超时而不是拒绝、模型名拼错会返回 404 而不是
「没这个模型」、不支持 ``response_format`` 会返回一个语焉不详的 400、
而异构对抗配错族要等进程启动才被拒。

本脚本把这五件事挪到跑案件之前，用**一次几十 token 的探针**问清楚，
并在最后给出单案件成本预估——先知道一次多少钱，再决定跑不跑。

用法::

    python3 tools/preflight.py                          # 用权限矩阵的默认绑定
    python3 tools/preflight.py --preset dashscope-only  # 单 key 跑法
    python3 tools/preflight.py --preset local-ollama --all-profiles
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.registry import MCPClient  # noqa: E402
from mcp_servers.world import World  # noqa: E402
from poc.creditsentry import modelconfig, permissions  # noqa: E402
from poc.creditsentry.agents import Orchestrator  # noqa: E402
from poc.creditsentry.llm import get_llm  # noqa: E402
from poc.creditsentry.modelconfig import Profile  # noqa: E402
from poc.creditsentry.tracing import Tracer  # noqa: E402

OK, BAD, WARN = "✓", "✗", "!"

#: 真正会发起 live 调用的两个 Agent。其余 Agent 虽然在权限矩阵里声明 llm=True，
#: 但当前实现中它们的环节是规则驱动的，不经过 LLM 网关——
#: 把它们一并探测会让「必须准备哪些 key」这个问题的答案凭空变复杂。
#: 想全量探测用 --all-profiles。
LIVE_CALLERS = ("risk-analyst", "devils-advocate")


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.failed = 0

    def add(self, mark: str, title: str, detail: str = "") -> None:
        self.rows.append((mark, title, detail))
        if mark == BAD:
            self.failed += 1

    def render(self) -> None:
        for mark, title, detail in self.rows:
            print(f"  {mark} {title}")
            for line in (detail.splitlines() if detail else []):
                print(f"      {line}")


def _mask(ref: str, value: str) -> str:
    """凭证只显示形态，不显示值。自检输出经常被贴进群里排障。"""
    if not value:
        return f"{ref} → （空）"
    return f"{ref} → 已解析，长度 {len(value)}，尾 4 位 …{value[-4:]}"


def check_config(preset: str | None, overrides: list[str] | None,
                 rep: Report) -> modelconfig.ModelConfig | None:
    try:
        cfg = modelconfig.resolve(preset, overrides)
    except modelconfig.ConfigError as e:
        rep.add(BAD, "模型配置与四条不变量", str(e))
        return None
    rep.add(OK, "模型配置加载并通过四条不变量校验",
            f"预设 {preset or '（无，用权限矩阵默认绑定）'}")

    an = cfg.profile_for("risk-analyst")
    ad = cfg.profile_for("devils-advocate")
    rep.add(OK if an.family != ad.family else BAD, "异构对抗：定性与质疑分属不同模型族",
            f"定性 {an.describe()}\n质疑 {ad.describe()}")

    for agent in permissions.pii_touchpoints():
        p = cfg.profile_for(agent)
        ok = p.provider.data_residency in cfg.pii_allowed_residency
        rep.add(OK if ok else BAD, f"PII 围栏：{agent} 的模型驻留区在白名单内",
                f"{p.describe()} 驻留区 {p.provider.data_residency}，"
                f"白名单 {cfg.pii_allowed_residency}")
    return cfg


def check_credentials(cfg: modelconfig.ModelConfig, profiles: list[Profile],
                      rep: Report) -> None:
    seen: set[str] = set()
    for p in profiles:
        prov = p.provider
        if prov.name in seen:
            continue
        seen.add(prov.name)
        if prov.protocol == "deterministic":
            continue
        try:
            val = cfg.resolve_credential(prov)
        except modelconfig.CredentialError as e:
            rep.add(BAD, f"凭证：{prov.name}", str(e))
            continue
        rep.add(OK, f"凭证：{prov.name}", _mask(prov.credential_ref, val))


def _hint(code: int, p: Profile) -> str:
    """把状态码翻成「下一步做什么」。

    401 那条值得单独说：百炼两个地域端点的 Key 不通用，跨地域调用返回的也是
    「Incorrect API key」——照着字面去查 Key 会一直查不出问题。
    """
    base = p.provider.base_url
    intl = "dashscope-intl" in base
    if code == 401:
        if "dashscope" in base:
            return ("百炼的 Key 不能跨地域使用。当前打的是"
                    + ("国际站（新加坡），若账号在中国大陆请改用 --preset dashscope-only"
                       if intl else
                       "中国大陆（北京），若账号在国际站请改用 --preset dashscope-intl-ollama "
                       "或 dashscope-intl-deepseek")
                    + "；其次才是检查 Key 是否复制完整")
        return "鉴权失败：检查 API Key 是否正确、是否已过期"
    if code == 403:
        return "无权访问：检查该 Key 是否开通了这个模型"
    if code == 404:
        return (f"模型 {p.model} 在你的账号或地域下不存在／未开通，核对 "
                f"base_url={base} 与 model={p.model}"
                + ("；百炼国际站默认通常只有 qwen 系列" if intl else ""))
    if code == 429:
        return "触发限流，稍后重试或检查额度"
    return ""


def probe(cfg: modelconfig.ModelConfig, p: Profile, rep: Report) -> dict | None:
    """对一个档位发一次最小探针：连通性 + 鉴权 + 模型名 + JSON 模式，一次问清。"""
    if p.provider.protocol == "deterministic":
        rep.add(OK, f"端点探针：{p.name}", "内置确定性推理器，无需联网")
        return None
    try:
        key = cfg.resolve_credential(p.provider)
    except modelconfig.CredentialError as e:
        rep.add(BAD, f"端点探针：{p.name}", f"凭证未解析，跳过探测：{e}")
        return None

    url = f"{p.provider.base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    def once(json_mode: bool) -> tuple[int, str, float]:
        body = {
            "model": p.model,
            "messages": [
                {"role": "system", "content": "只输出 JSON，不要任何解释。"},
                {"role": "user", "content": '返回 {"ok": true}'},
            ],
            "temperature": 0.0, "max_tokens": 32,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=min(30.0, p.timeout_ms / 1000)) as r:
            return r.status, r.read().decode("utf-8"), (time.time() - t0) * 1000

    want_json = p.json_mode in ("on", "auto")
    try:
        _, text, ms = once(want_json)
        json_mode_ok = want_json
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        if want_json and p.json_mode == "auto" and e.code in (400, 404, 422):
            try:
                _, text, ms = once(False)
                json_mode_ok = False
            except Exception as e2:  # noqa: BLE001
                rep.add(BAD, f"端点探针：{p.name}", f"HTTP {e.code} {detail}｜降级重试仍失败：{e2}")
                return None
        else:
            rep.add(BAD, f"端点探针：{p.name}",
                    f"HTTP {e.code} {detail}\n→ {_hint(e.code, p)}")
            return None
    except Exception as e:  # noqa: BLE001
        rep.add(BAD, f"端点探针：{p.name}",
                f"不可达：{e}\n→ 核对 base_url={p.provider.base_url}（本地端点请确认服务已启动）")
        return None

    data = json.loads(text)
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    usage = data.get("usage") or {}
    parsed = "是" if _looks_json(content) else "否"
    mark = OK if parsed == "是" else WARN
    json_state = ("启用" if json_mode_ok else
                  ("配置为关闭" if p.json_mode == "off" else "端点不支持，已自动降级"))
    rep.add(mark, f"端点探针：{p.name}",
            f"{p.provider.name}/{p.model}　时延 {ms:.0f} ms　"
            f"json_mode={json_state}　"
            f"返回可解析为 JSON：{parsed}\n"
            f"用量 in={usage.get('prompt_tokens', '?')} out={usage.get('completion_tokens', '?')}"
            + ("" if parsed == "是" else
               "\n→ 该端点未直接返回 JSON，系统会走 _extract_json 兜底与修复轮，成本略高"))
    return {"latency_ms": ms, "json_mode": json_mode_ok}


def _looks_json(text: str) -> bool:
    try:
        json.loads((text or "").strip())
        return True
    except Exception:  # noqa: BLE001
        return False


def estimate_cost(cfg: modelconfig.ModelConfig, rep: Report) -> None:
    """用 stub 跑一遍真实链路，拿到每个 Agent 的 token 量，再按 live 单价折算。

    这个估算偏保守也偏诚实：token 量取自真实的上下文装配结果，
    不是拍脑袋的经验值；但它**不含修复轮**——修复轮的发生率要跑过真机才知道。
    """
    lines: list[str] = []
    total = 0.0
    for case in ("case_001", "case_002", "case_003"):
        world = World.load(case)
        tracer = Tracer()
        mcp = MCPClient(world, tracer)
        llm = get_llm("stub", cfg=cfg)
        Orchestrator(world, mcp, llm, auto_approve=True).run()
        cost = 0.0
        detail = []
        for caller, u in llm.usage()["by_caller"].items():
            p = cfg.profile_for(caller)
            c = p.cost(u["tokens_in"], u["tokens_out"])
            cost += c
            detail.append(f"{caller}={u['tokens_in']}+{u['tokens_out']} tok")
        total += cost
        lines.append(f"{world.case_id}　￥{cost:.4f}　（{'，'.join(detail)}）")
    avg = total / 3
    lines.append(f"三案件均值 ￥{avg:.4f} / 件；按日均 2000 件推算约 "
                 f"￥{avg * 2000:,.0f} / 天、￥{avg * 2000 * 250:,.0f} / 年（250 工作日）")
    lines.append("不含修复轮与重试；修复轮发生率需真机跑过才有实数")
    rep.add(OK, "单案件成本预估（按当前绑定的单价）", "\n".join(lines))


def main() -> int:
    p = argparse.ArgumentParser(description="真机自检：配置、凭证、端点、成本")
    p.add_argument("--preset", help="套用绑定预设，如 dashscope-only")
    p.add_argument("--profile", action="append", metavar="AGENT=PROFILE")
    p.add_argument("--all-profiles", action="store_true",
                   help="探测全部已绑定档位（默认只探测真正会发起 live 调用的两个）")
    p.add_argument("--no-probe", action="store_true", help="跳过网络探针，只做本地检查")
    args = p.parse_args()

    rep = Report()
    print("\n信衡 CreditSentry · 真机自检")
    print("═" * 78)

    cfg = check_config(args.preset, args.profile, rep)
    if cfg is None:
        rep.render()
        print("═" * 78)
        print("配置层就没通过，后续检查已跳过。修好配置再跑。")
        return 1

    callers = list(permissions.PERMISSIONS) if args.all_profiles else list(LIVE_CALLERS)
    profiles: list[Profile] = []
    for c in callers:
        if not permissions.PERMISSIONS[c]["llm"]:
            continue
        prof = cfg.profile_for(c)
        if prof.name not in {x.name for x in profiles}:
            profiles.append(prof)

    check_credentials(cfg, profiles, rep)
    if not args.no_probe:
        for prof in profiles:
            probe(cfg, prof, rep)
    else:
        rep.add(WARN, "端点探针", "已按 --no-probe 跳过")

    estimate_cost(cfg, rep)

    rep.render()
    print("═" * 78)
    if rep.failed:
        print(f"{rep.failed} 项未通过。修复后再跑 --llm live，否则会在链路中途失败。")
        return 1
    print("全部通过。可以跑：")
    pre = f" --preset {args.preset}" if args.preset else ""
    print(f"  python3 poc/run_demo.py --case CASE-001 --llm live{pre}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
