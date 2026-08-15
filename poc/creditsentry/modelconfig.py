"""模型配置的四层分离与不变量校验。

单一全局 ``BASE_URL / API_KEY / MODEL`` 三件套无法表达两件必须表达的事：
**不同 Agent 用不同模型**，以及**哪些数据会流向哪个厂商**。前者是对抗质疑不在
权重层面坍缩的前提，后者是银行必然会问的合规问题。因此拆成四层：

===== =========================== ==============================================
层     位置                         回答什么
===== =========================== ==============================================
1     ``config/providers.yaml``    端点在哪、什么协议、凭证怎么取、数据落在哪个区
2     ``config/models.yaml``       用哪个模型、怎么解码、多少钱、属于哪个 family
3     ``permissions.py`` 的         哪个 Agent 用哪个 profile
      ``model_profile``
4     CLI ``--profile a=p`` /      运行时覆盖，便于本地调试与实验 E4 换档
      环境变量
===== =========================== ==============================================

分层的收益是可组合：换云厂商不动模型选型（改第 1 层），换模型不动 Agent 拓扑
（改第 2 层），换绑定不动任何配置文件（改第 3 层，且自动进 Worker CR）。

四条不变量由 :func:`check_invariants` 校验，并被 ``poc/test_safety.py`` 断言。
其中 **PII 围栏**是单一全局配置根本无法提供的能力：由于 due-diligence 是唯一
PII 触点，只要它绑定的 profile 的 provider 数据驻留区在白名单内，就静态证明了
PII 不会流向白名单外的厂商。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from . import _miniyaml, permissions

CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config"
)

# 运行时覆盖用的环境变量前缀：CREDITSENTRY_PROFILE_RISK_ANALYST=local-open
ENV_PREFIX = "CREDITSENTRY_PROFILE_"

# ---------------------------------------------------------------------------
# 运行时凭证
# ---------------------------------------------------------------------------
# 工作台需要让人在界面上填 API Key——总不能要求演示者先去改 shell 再重启服务。
# 但这不能破坏「密钥不进配置、不进版本库」这条约束，因此运行时凭证：
#
#   · 只存在**本进程内存**里，不写文件、不写配置、不进 Trace 与日志；
#   · 优先级高于环境变量，但**仍以 env: 引用为索引**——配置文件里写的还是
#     ``env:DASHSCOPE_API_KEY``，只是取值时多了一个内存来源；
#   · 进程重启即失效。
#
# 换句话说：改变的是「到哪里取值」，不是「配置里能不能写明文」。后者依然不行。
_RUNTIME_CREDENTIALS: dict[str, str] = {}


def set_runtime_credential(env_name: str, value: str) -> None:
    """登记一个仅存活于本进程内存的凭证。空值表示清除。"""
    if value:
        _RUNTIME_CREDENTIALS[env_name] = value
    else:
        _RUNTIME_CREDENTIALS.pop(env_name, None)


def runtime_credential_names() -> list[str]:
    """已登记的凭证**名字**。刻意不提供取值接口——
    除了 resolve_credential，没有任何地方需要读到明文。"""
    return sorted(_RUNTIME_CREDENTIALS)


def credential_source(env_name: str) -> str:
    """该凭证当前从哪里来：runtime / env / 未设置。用于界面提示，不返回值。"""
    if env_name in _RUNTIME_CREDENTIALS:
        return "runtime"
    return "env" if os.environ.get(env_name) else "none"


# ---------------------------------------------------------------------------
# 运行时自定义端点
# ---------------------------------------------------------------------------
# ``config/providers.yaml`` 里预置的端点覆盖不了所有情况：使用者可能用的是
# 自建网关、某个我们没列的服务商、或者公司内网的兼容层。**只给 API Key 的输入框
# 是不够的**——base_url 决定请求发到哪里，端点对不上，key 填得再对也调不通。
#
# 因此允许在运行时登记一对自定义端点（定性侧与质疑侧各一），同样只存内存。
# 注意它**不豁免任何不变量**：两侧仍必须分属不同模型族，PII 触点的数据驻留区
# 仍必须落在白名单内。自定义的是「连到哪里」，不是「要不要守规矩」。

CUSTOM_ROLES = ("risk-analyst", "devils-advocate")

_RUNTIME_ENDPOINTS: dict[str, dict[str, Any]] = {}


def custom_key_env(role: str) -> str:
    """自定义端点的凭证索引名。仍走 env: 引用这条既有路径，不新开一种凭证形态。"""
    return "CREDITSENTRY_CUSTOM_" + role.replace("-", "_").upper() + "_KEY"


def set_runtime_endpoint(role: str, *, base_url: str, model: str,
                         family: str | None = None, data_residency: str = "cn",
                         json_mode: str = "auto") -> None:
    """登记一个自定义端点。base_url 或 model 为空即视为清除该角色的配置。"""
    if role not in CUSTOM_ROLES:
        raise ConfigError(f"自定义端点只支持 {CUSTOM_ROLES}，收到 {role!r}")
    if not base_url or not model:
        _RUNTIME_ENDPOINTS.pop(role, None)
        return
    _RUNTIME_ENDPOINTS[role] = {
        "base_url": base_url.strip().rstrip("/"),
        "model": model.strip(),
        # 族缺省时按模型名首段推断。推断错了会被异构对抗那条不变量拦下来，
        # 而不是悄悄让两侧坍缩到同一批权重上
        "family": (family or "").strip() or _guess_family(model),
        "data_residency": data_residency,
        "json_mode": json_mode,
    }


def _guess_family(model: str) -> str:
    """从模型名推断族。只做最朴素的切分，推断不准由使用者在界面上改。"""
    m = model.strip().lower()
    for known in ("qwen", "deepseek", "glm", "doubao", "moonshot", "kimi",
                  "llama", "mistral", "gemma", "yi", "baichuan", "gpt", "claude"):
        if known in m:
            return known
    return (m.split(":")[0].split("/")[-1].split("-")[0] or "custom")


def runtime_endpoints() -> dict[str, dict[str, Any]]:
    """已登记的自定义端点。不含凭证，可安全返回给界面。"""
    return {k: dict(v) for k, v in _RUNTIME_ENDPOINTS.items()}


def clear_runtime_endpoints() -> None:
    for role in list(_RUNTIME_ENDPOINTS):
        _RUNTIME_ENDPOINTS.pop(role, None)
        _RUNTIME_CREDENTIALS.pop(custom_key_env(role), None)


class ConfigError(Exception):
    """配置缺失、引用不存在，或违反不变量。"""


class CredentialError(Exception):
    """凭证引用无法解析。刻意与 ConfigError 分开：配置错要改文件，凭证缺要设环境。"""


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    protocol: str
    credential_ref: str
    data_residency: str
    note: str = ""


@dataclass(frozen=True)
class Profile:
    name: str
    provider: Provider
    model: str
    family: str
    temperature: float = 0.2
    top_p: float | None = None
    seed: int | None = None
    max_output_tokens: int | None = None
    timeout_ms: int = 60000
    price_in_per_1k: float = 0.0
    price_out_per_1k: float = 0.0
    # 传输层重试次数。只对超时、限流、5xx 这类**瞬时**故障重试；
    # 鉴权失败、参数非法立即上抛——重试一个必然失败的请求只是在烧配额。
    max_retries: int = 2
    # 是否使用 OpenAI 的 response_format=json_object。取值 on / off / auto：
    # auto 表示先试，端点报不支持就自动降级并记住。本地 Ollama 与部分自建
    # 兼容层不支持这个参数，写死 on 会让真机第一步就 400。
    json_mode: str = "auto"
    note: str = ""

    @property
    def is_deterministic(self) -> bool:
        return self.provider.protocol == "deterministic"

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return round(tokens_in / 1000 * self.price_in_per_1k
                     + tokens_out / 1000 * self.price_out_per_1k, 6)

    def describe(self) -> str:
        return f"{self.name}（{self.provider.name}/{self.model}·{self.family}）"


class ModelConfig:
    """加载后的配置视图。构造即校验，配置非法直接抛错而非带病运行。"""

    def __init__(self, providers: dict[str, Provider], profiles: dict[str, Profile],
                 pii_allowed_residency: list[str], adversarial_pairs: list[list[str]],
                 overrides: dict[str, str] | None = None,
                 presets: dict[str, dict[str, Any]] | None = None) -> None:
        self.providers = providers
        self.profiles = profiles
        self.pii_allowed_residency = pii_allowed_residency
        self.adversarial_pairs = adversarial_pairs
        self.overrides = dict(overrides or {})
        self.presets = dict(presets or {})

        problems = self.check_invariants()
        if problems:
            raise ConfigError("模型配置不变量校验未通过：\n  - " + "\n  - ".join(problems))

    # ---- 绑定解析 ---------------------------------------------------
    def profile_name_for(self, agent: str) -> str | None:
        """返回某 Agent 生效的 profile 名。优先级：CLI/env 覆盖 > 权限矩阵绑定。"""
        if agent in self.overrides:
            return self.overrides[agent]
        env_key = ENV_PREFIX + agent.replace("-", "_").upper()
        if os.environ.get(env_key):
            return os.environ[env_key]
        entry = permissions.PERMISSIONS.get(agent)
        return (entry or {}).get("model_profile")

    def profile_for(self, agent: str) -> Profile:
        name = self.profile_name_for(agent)
        if name is None:
            raise ConfigError(
                f"{agent} 在权限矩阵中未绑定 model_profile"
                f"（若该 Agent 本就不做自由推理，说明它不应走 LLM 网关）"
            )
        if name not in self.profiles:
            raise ConfigError(f"{agent} 绑定了不存在的 profile：{name}")
        return self.profiles[name]

    def bindings(self) -> dict[str, str | None]:
        """全部 Agent 的生效绑定，用于 CR 生成、启动打印与审计。"""
        return {a: self.profile_name_for(a) for a in permissions.PERMISSIONS}

    # ---- 凭证解析 ---------------------------------------------------
    def resolve_credential(self, provider: Provider) -> str:
        """把凭证**引用**解析为实际值。密钥只在此处短暂出现，不落任何持久化结构。"""
        ref = provider.credential_ref or "none:"
        scheme, _, rest = ref.partition(":")
        if scheme == "none":
            return ""
        if scheme == "env":
            # 运行时凭证优先于环境变量：工作台上刚填的那个，应当立刻生效，
            # 而不是被一个陈旧的 shell 变量盖住
            val = _RUNTIME_CREDENTIALS.get(rest) or os.environ.get(rest, "")
            if not val:
                raise CredentialError(
                    f"provider {provider.name} 的凭证引用 {ref} 未解析到值，"
                    f"请设置环境变量 {rest}（或在工作台的「模型凭证」面板中填入）；"
                    f"若只想复现评测结果，用默认的 --llm stub"
                )
            return val
        if scheme == "k8s-secret":
            raise CredentialError(
                f"{ref}：k8s-secret 由集群在 Worker CR 的 spec.env 中注入，"
                f"本地运行请改用 env: 引用"
            )
        if scheme == "higress-consumer":
            val = os.environ.get("CREDITSENTRY_HIGRESS_CONSUMER_TOKEN", "")
            if not val:
                raise CredentialError(
                    f"{ref}：Worker 只持 consumer token，请设置 "
                    f"CREDITSENTRY_HIGRESS_CONSUMER_TOKEN（真实密钥留在网关侧）"
                )
            return val
        raise ConfigError(f"未知的凭证引用协议：{ref}")

    # ---- 不变量 -----------------------------------------------------
    def check_invariants(self) -> list[str]:
        """返回违规说明列表，空列表表示全部通过。"""
        problems: list[str] = []

        for pname, prof in self.profiles.items():
            if prof.provider.name not in self.providers:
                problems.append(f"profile {pname} 引用了不存在的 provider {prof.provider.name}")

        for agent, entry in permissions.PERMISSIONS.items():
            bound = self.profile_name_for(agent)
            if entry.get("llm"):
                if not bound:
                    problems.append(f"{agent} 声明 llm=True 但未绑定 model_profile")
                elif bound not in self.profiles:
                    problems.append(f"{agent} 绑定了不存在的 profile：{bound}")
            elif bound:
                problems.append(
                    f"{agent} 声明 llm=False（仅规则驱动）却绑定了 profile {bound}，"
                    f"执行环节不应有模型入口"
                )

        # 异构对抗：目标函数对立的两方必须落在不同模型族
        for pair in self.adversarial_pairs:
            names = [self.profile_name_for(a) for a in pair]
            if any(n is None or n not in self.profiles for n in names):
                continue  # 上一条已经报过缺失
            fams = [self.profiles[n].family for n in names]  # type: ignore[index]
            if len(set(fams)) != len(fams):
                problems.append(
                    f"对抗双方 {' ⇄ '.join(pair)} 绑定了同族模型（{fams[0]}）："
                    f"拆成两个 Agent 是为了避免自洽坍缩，同族权重会让对抗在权重层面坍缩"
                )

        # PII 围栏：唯一 PII 触点的模型不得流向白名单外的数据驻留区
        for agent in permissions.pii_touchpoints():
            bound = self.profile_name_for(agent)
            if not bound or bound not in self.profiles:
                continue
            residency = self.profiles[bound].provider.data_residency
            if residency not in self.pii_allowed_residency:
                problems.append(
                    f"PII 触点 {agent} 绑定的 profile {bound} 落在数据驻留区 "
                    f"{residency}，不在允许清单 {self.pii_allowed_residency} 内"
                )

        return problems


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def _require(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d or d[key] is None:
        raise ConfigError(f"{where} 缺少必填字段 {key}")
    return d[key]


def load(config_dir: str | None = None, overrides: dict[str, str] | None = None) -> ModelConfig:
    """从 ``config/`` 加载并校验。overrides 来自 CLI ``--profile agent=profile``。"""
    base = config_dir or CONFIG_DIR
    ppath = os.path.join(base, "providers.yaml")
    mpath = os.path.join(base, "models.yaml")
    for p in (ppath, mpath):
        if not os.path.exists(p):
            raise ConfigError(f"未找到配置文件：{p}")

    praw = _miniyaml.load(ppath) or {}
    mraw = _miniyaml.load(mpath) or {}

    providers: dict[str, Provider] = {}
    for name, body in (_require(praw, "providers", "providers.yaml") or {}).items():
        providers[name] = Provider(
            name=name,
            base_url=str(_require(body, "base_url", f"provider {name}")),
            protocol=str(_require(body, "protocol", f"provider {name}")),
            credential_ref=str(body.get("credential_ref") or "none:"),
            data_residency=str(_require(body, "data_residency", f"provider {name}")),
            note=str(body.get("note") or ""),
        )

    profiles: dict[str, Profile] = {}
    for name, body in (_require(mraw, "profiles", "models.yaml") or {}).items():
        pname = str(_require(body, "provider", f"profile {name}"))
        if pname not in providers:
            raise ConfigError(f"profile {name} 引用了不存在的 provider：{pname}")
        profiles[name] = Profile(
            name=name,
            provider=providers[pname],
            model=str(_require(body, "model", f"profile {name}")),
            family=str(_require(body, "family", f"profile {name}")),
            temperature=float(body.get("temperature", 0.2)),
            top_p=body.get("top_p"),
            seed=body.get("seed"),
            max_output_tokens=body.get("max_output_tokens"),
            timeout_ms=int(body.get("timeout_ms", 60000)),
            price_in_per_1k=float(body.get("price_in_per_1k", 0.0)),
            price_out_per_1k=float(body.get("price_out_per_1k", 0.0)),
            max_retries=int(body.get("max_retries", 2)),
            json_mode=str(body.get("json_mode", "auto")),
            note=str(body.get("note") or ""),
        )
        if profiles[name].json_mode not in ("on", "off", "auto"):
            raise ConfigError(
                f"profile {name} 的 json_mode={profiles[name].json_mode!r} 非法，"
                f"只能是 on / off / auto"
            )

    # 运行时自定义端点以合成 provider + profile 的形式并入，
    # 之后走的是与 YAML 里那些端点**完全相同**的校验与调用路径——
    # 自定义不等于走后门，四条不变量照样管着它
    for role, ep in _RUNTIME_ENDPOINTS.items():
        pname = f"custom-{role}"
        providers[pname] = Provider(
            name=pname, base_url=ep["base_url"], protocol="openai-compatible",
            credential_ref=f"env:{custom_key_env(role)}",
            data_residency=ep.get("data_residency", "cn"),
            note="运行时登记的自定义端点（仅存内存）",
        )
        profiles[pname] = Profile(
            name=pname, provider=providers[pname], model=ep["model"],
            family=ep["family"], temperature=0.2, top_p=0.9,
            max_output_tokens=4096, timeout_ms=120000, max_retries=2,
            json_mode=ep.get("json_mode", "auto"),
            note=f"自定义端点 · 供 {role} 使用",
        )

    presets = mraw.get("presets") or {}
    for pname, body in presets.items():
        bindings = (body or {}).get("bindings") or {}
        for agent, prof in bindings.items():
            if agent not in permissions.PERMISSIONS:
                raise ConfigError(f"预设 {pname} 绑定了未知 Agent：{agent}")
            if prof not in profiles:
                raise ConfigError(f"预设 {pname} 引用了不存在的 profile：{prof}")

    return ModelConfig(
        providers=providers,
        profiles=profiles,
        pii_allowed_residency=list(mraw.get("pii_allowed_residency") or []),
        adversarial_pairs=[list(p) for p in (mraw.get("adversarial_pairs") or [])],
        overrides=overrides,
        presets=presets,
    )


def parse_overrides(items: list[str] | None) -> dict[str, str]:
    """把 CLI 的 ``--profile agent=profile`` 解析成映射。"""
    out: dict[str, str] = {}
    for item in items or []:
        agent, sep, profile = item.partition("=")
        if not sep or not agent.strip() or not profile.strip():
            raise ConfigError(f"--profile 需要 agent=profile 形式，收到：{item!r}")
        if agent.strip() not in permissions.PERMISSIONS:
            raise ConfigError(f"--profile 指定了未知 Agent：{agent.strip()}")
        out[agent.strip()] = profile.strip()
    return out


def list_presets(config_dir: str | None = None) -> dict[str, dict[str, Any]]:
    """只读取预设清单，不做不变量校验。供 CLI 打印可选值用。"""
    base = config_dir or CONFIG_DIR
    raw = _miniyaml.load(os.path.join(base, "models.yaml")) or {}
    return raw.get("presets") or {}


def resolve(preset: str | None, cli_overrides: list[str] | None,
            config_dir: str | None = None) -> ModelConfig:
    """按「预设 → CLI 覆盖」的优先级组装配置并校验。

    优先级刻意让 ``--profile`` 压过 ``--preset``：预设是一组默认值，
    单独指定的那一个才是使用者此刻真正想要的。两者都属于第 4 层，
    因此**都要过四条不变量**——预设不是绕过约束的后门。
    """
    presets = list_presets(config_dir)
    overrides: dict[str, str] = {}
    if preset == "custom":
        # 自定义端点：两个对抗角色各用各的；其余需要模型入口的 Agent 一律并到
        # 定性侧那个端点上，免得为了跑通还得再准备第二、第三个 key
        missing = [r for r in CUSTOM_ROLES if r not in _RUNTIME_ENDPOINTS]
        if missing:
            raise ConfigError(
                "自定义端点尚未配置完整，缺少：" + "、".join(missing)
                + "。定性官与质疑官各需要一个端点，且两者必须分属不同模型族")
        for agent, entry in permissions.PERMISSIONS.items():
            if not entry.get("llm"):
                continue
            overrides[agent] = (f"custom-{agent}" if agent in CUSTOM_ROLES
                                else "custom-risk-analyst")
    elif preset:
        if preset not in presets:
            raise ConfigError(
                f"未知预设：{preset}（可选：{'、'.join(sorted(presets)) or '无'}）")
        overrides.update((presets[preset] or {}).get("bindings") or {})
    overrides.update(parse_overrides(cli_overrides))
    return load(config_dir, overrides=overrides)
