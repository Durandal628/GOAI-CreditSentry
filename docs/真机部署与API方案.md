# 真机部署 · API 调用方案 · 接口说明

> 本文覆盖三件事：**怎么在真机上把 `--llm live` 跑通**、**系统怎么调外部模型 API**、
> **系统自身对外与对内的接口契约**。
>
> 与 [`接口与实验方案.md`](接口与实验方案.md) 的分工：那篇写「接口应该长什么样」（设计），
> 本篇写「现在长什么样、怎么用、跑不通怎么查」（运行）。**本文所有命令都可复现，
> 未实现的部分在第七章明确列出，不混在正文里。**

---

## 〇、先看这张图：一次 live 运行到底发生了什么

```
poc/run_demo.py --case CASE-001 --llm live --preset dashscope-only
   │
   ├─① 加载 config/ 并校验四条不变量 ──────── 违反即拒绝启动（不是跑到一半才崩）
   │
   ├─② 状态机五阶段 INTAKE → EVIDENCE → ADJUDICATION → DISPOSITION → AUDIT
   │     流程走向 100% 由 routing.py 决定，模型不参与编排
   │
   ├─③ 取证：7 个 Agent 经 MCPClient 调 4 个 MCP Server
   │     每次调用前查权限矩阵，被拒的调用也进审计日志
   │
   ├─④ 推理：全系统只有两处调模型 ────────── 这是刻意压到最小的暴露面
   │     risk-analyst  ──→ task=risk_root_cause  ──→ qwen-max     (族 qwen)
   │     devils-advocate ─→ task=devils_advocate ──→ deepseek-v3  (族 deepseek)
   │        每次调用都走：HTTP → 抽 JSON → 归一化 → Schema 校验 → 至多一轮修复
   │        失败则按失败策略降级，降级方向恒为「阻断」而非「放行」
   │
   ├─⑤ 闸门：RiskGate 纯规则四维定级 L0–L3，不联网、不问模型
   │
   └─⑥ 产出 9 个文件到 poc/out/<case>/
```

**只有第 ④ 步联网。** 编排、路由、闸门、证据账本、审计全部是本地确定性代码——
这决定了真机部署的复杂度上限：你要准备的只有**模型 API 凭证**，没有别的。

---

# 一、真机部署（本机）

## 1.1 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | 3.9+ | 已在 3.9.6 上验证 |
| 第三方依赖 | **无** | 只用标准库；`urllib` 直连 OpenAI 兼容端点，不引入 SDK |
| 网络 | 仅 `--llm live` 需要 | 默认 `stub` 与离线验证全程不联网 |
| 磁盘 | < 50 MB | 产出文件为主 |

```bash
git clone <repo> && cd GOAI_competation
python3 poc/run_demo.py --all        # 先确认 stub 基线是绿的
python3 poc/test_safety.py           # 65 项安全回归
```

**stub 基线不绿就不要往下走。** 它不依赖任何外部条件，跑不通说明是环境问题
（Python 版本、文件编码），此时接 API 只会把问题变复杂。

## 1.2 选一条路线：你手上有什么 key，决定跑哪个预设

异构对抗要求定性官与质疑官分属**不同模型族**（`family`），这是硬约束，配错直接拒绝启动。
但注意——**约束的是族，不是厂商**。同一个端点上挂着两个族也满足约束。
这一点决定了单 key 也能跑通：

| 你有的 key | 预设 | 定性 ⇄ 质疑 | 需要的环境变量 |
|---|---|---|---|
| **只有百炼** | `--preset dashscope-only` | qwen-max ⇄ deepseek-v3（同端点，异族） | `DASHSCOPE_API_KEY` |
| 百炼 + 火山 | *（无需预设，权限矩阵默认绑定）* | qwen-max ⇄ doubao-1-5-pro-32k | `DASHSCOPE_API_KEY`、`ARK_API_KEY` |
| DeepSeek + 智谱 | `--preset deepseek-zhipu` | deepseek-chat ⇄ glm-4-plus | `DEEPSEEK_API_KEY`、`ZHIPU_API_KEY` |
| 只有本地 Ollama | `--preset local-ollama` | qwen2.5:7b ⇄ llama3.1:8b | 无（需先起 ollama） |
| **什么都没有** | `--preset offline-mock` | 伪端点，见 §1.6 | 无 |

```bash
python3 poc/run_demo.py --list-presets     # 看全部预设及其绑定
```

> **预设不是绕过约束的后门。** 它只是「一组 `--profile` 覆盖」的别名，
> 同样要过四条不变量校验；`poc/test_safety.py` 会逐个预设断言这件事。

### 各路线的准备动作

**百炼（推荐，一个 key 就够）**

1. 到阿里云百炼控制台开通服务并创建 API-KEY；
2. 确认 `qwen-max` 与 `deepseek-v3` 两个模型都已开通（后者在「DeepSeek 系列」下）；
3. `export DASHSCOPE_API_KEY=你的key`

**本地 Ollama（零成本、零外发）**

```bash
ollama pull qwen2.5:7b && ollama pull llama3.1:8b
ollama serve                      # 默认 11434 端口
```
模型标签必须与 `config/models.yaml` 中 `local-qwen` / `local-llama` 的 `model` 字段一致；
不一致就改配置，不要改代码。

> 本地 7B/8B 模型返回合规 JSON 的稳定性明显差于云端。这不是问题而是**特性**——
> 它会真实地触发修复轮，让你看到 §3.3 那套机制在干活。跑完看 `metrics.json`
> 的 `llm_repair_rounds`，这个数就是模型稳定性的直接读数。

## 1.3 配环境变量

凭证在配置里**只以引用形式出现**（`env:` / `k8s-secret:` / `higress-consumer:` / `none:`），
因此 `config/` 可以安全进版本库。本机部署一律用 `env:`：

```bash
export DASHSCOPE_API_KEY=...      # 对应 providers.yaml 的 credential_ref: env:DASHSCOPE_API_KEY
export ARK_API_KEY=...            # 用火山才需要
export DEEPSEEK_API_KEY=...
export ZHIPU_API_KEY=...
```

想临时换某一个 Agent 的档位而不改任何文件，两种方式等价：

```bash
python3 poc/run_demo.py --case CASE-001 --llm live --profile devils-advocate=advocate-zhipu
export CREDITSENTRY_PROFILE_DEVILS_ADVOCATE=advocate-zhipu    # 环境变量形式
```

## 1.4 自检：在花第一分钱之前

```bash
python3 tools/preflight.py --preset dashscope-only
```

它用**一次几十 token 的探针**一次性问清五件事，并给出单案件成本预估：

```
  ✓ 模型配置加载并通过四条不变量校验
  ✓ 异构对抗：定性与质疑分属不同模型族
      定性 analyst-primary（dashscope/qwen-max·qwen）
      质疑 advocate-dashscope-ds（dashscope/deepseek-v3·deepseek）
  ✓ PII 围栏：due-diligence 的模型驻留区在白名单内
  ✓ 凭证：dashscope   env:DASHSCOPE_API_KEY → 已解析，长度 35，尾 4 位 …a1b2
  ✓ 端点探针：analyst-primary
      dashscope/qwen-max　时延 812 ms　json_mode=启用　返回可解析为 JSON：是
  ✓ 单案件成本预估（按当前绑定的单价）
      三案件均值 ￥0.0153 / 件；按日均 2000 件推算约 ￥31 / 天、￥7,628 / 年
```

之所以值得单独做这一步：接真机时会失败的五个地方，**每一处的报错都长得不像它真正的病因**。
凭证没设会跑到一半才崩、端点写错表现为超时而不是拒绝、模型名拼错返回 404 而不是
「没这个模型」、不支持 `response_format` 返回一个语焉不详的 400。自检把这些前移并翻译成人话。

常用参数：`--no-probe`（只做本地检查，不联网）、`--all-profiles`（探测全部绑定档位）。

## 1.5 跑

```bash
python3 poc/run_demo.py --case CASE-001 --llm live --preset dashscope-only
python3 poc/run_demo.py --all         --llm live --preset dashscope-only
```

**怎么确认真的跑通了**，看输出末尾这一段：

```
【模型绑定】模式 live
  · risk-analyst      analyst-primary        声明 dashscope/qwen-max（族 qwen）      实际 qwen-max
  · devils-advocate   advocate-dashscope-ds  声明 dashscope/deepseek-v3（族 deepseek） 实际 deepseek-v3
  本次成本 ￥0.0142　涉及模型族 ['deepseek', 'qwen']　修复轮 0 次　失败调用 0 次
```

`实际` 一列显示模型名而不是「确定性推理器」，就是真的走了模型。
（stub 模式下这一列恒为「确定性推理器」，而 `声明` 一列照样显示生产上会用什么——
**「实际用了什么」与「生产上会用什么」永远分开记录**。）

`--llm live` 与 stub 的结论**不保证一致**，这是正常的：定性与质疑本来就是自然语言判断。
真正要保证一致的是 Schema 与流程骨架，这两样由 §3.3 的契约层与状态机保证。

## 1.6 没有 key 也能验证 live 代码路径

```bash
python3 tools/live_conformance.py
```

这不是「假装跑通」。`--llm live` 新增的三段代码——传输重试、Schema 修复轮、失败策略降级——
有个共同特点：**只在模型表现不好时才会被执行到**。拿真实端点测不了它们，
因为你没法要求 qwen「请这次返回一个非法枚举值」。

于是用 `tools/mock_llm_server.py` 把「模型表现不好」变成可点播的输入，
逐个故障模式跑完整链路。当前 13 个模式全绿：

```
故障模式                    修复轮     降级      阻断      结果
none                    0       否       否       ✓ 通过
prose                   0       否       否       ✓ 通过     JSON 外包围栏与寒暄
bad-enum                2       否       否       ✓ 通过     自造枚举值 → 修复轮救回
loose-format            0       否       否       ✓ 通过     "62%" 等表述偏差被归一化吸收
no-evidence             1       否       否       ✓ 通过     根因无证据引用
fake-evidence           1       否       否       ✓ 通过     引用不存在的证据编号
partial-checklist       1       否       否       ✓ 通过     质疑清单漏项
resolution-by-target    0       否       否       ✓ 通过     回执只给 target 不给 item_id
always-bad              0       是       是       ✓ 通过     持续违约 → 阻断，写操作 0 次
http500                 0       否       否       ✓ 通过     退避重试
ratelimit               0       否       否       ✓ 通过     限流重试
auth-fail               0       是       是       ✓ 通过     鉴权失败不重试、立即降级
no-json-mode            0       否       否       ✓ 通过     端点不支持 response_format → 自动降级
合计 13 个故障模式：13 通过，0 失败
```

伪端点内部调用的就是确定性推理器，因此 `--fault none` 时 live 路径的产出必须与 stub
**逐字节一致**——这证明新增的 HTTP → 解析 → 归一化 → 校验各层**不扭曲内容**。
而 `always-bad` 与 `auth-fail` 证明降级方向恒为阻断：案件停在 `EVIDENCE_GAP` 转人工，
**写类 MCP 调用为 0**。

---

# 二、部署形态：本机之外的两层

本次交付只把**第一层**做到了可复现运行，后两层给的是落地路径而非既成事实。这里如实标注。

| 层 | 形态 | 状态 | 说明 |
|---|---|:--:|---|
| L1 | 本机进程 | **已验证** | 见第一章。MCP 为进程内直连，凭证走 `env:` |
| L2 | 容器 | 方案 | 见 §2.1 |
| L3 | K8s + AgentTeams + Higress | 方案 | 见 §2.2 |

## 2.1 容器（L2）

零依赖带来的直接好处是 Dockerfile 只有四行：

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . .
ENTRYPOINT ["python3", "poc/run_demo.py"]
```

```bash
docker build -t creditsentry:poc .
docker run --rm -e DASHSCOPE_API_KEY=$DASHSCOPE_API_KEY \
  -v "$PWD/poc/out:/app/poc/out" \
  creditsentry:poc --case CASE-001 --llm live --preset dashscope-only
```

密钥走 `-e` 注入而非写进镜像；产出目录挂出来便于查看 Trace。

## 2.2 K8s + AgentTeams（L3）

三处替换，**Tool 名称、入参 Schema、返回结构与错误码完全不变**：

| 当前实现 | 生产替换 | 替换点 |
|---|---|---|
| `MCPClient` 进程内直连 | 经 Higress 的 MCP over stdio/SSE 客户端 | `mcp_servers/registry.py` |
| `env:` 凭证引用 | `k8s-secret:<secret>/<key>` 或 `higress-consumer:<consumer>` | `config/providers.yaml` |
| `routing.py` 常量路由表 | Nacos 下发 `agentteams/routing-table.yaml` | 已是同构声明，由 `tools/gen_agentteams.py` 生成 |

Worker CR 已经由权限矩阵生成，直接可用：

```bash
python3 tools/gen_agentteams.py       # agentteams/workers/*.yaml + souls/*/SOUL.md
kubectl apply -f agentteams/
```

`higress-consumer:` 这条凭证引用值得单说：**Worker 只持 consumer token，真实密钥留在网关侧**，
Worker 拿不到也换不掉。PII 出站脱敏同样在网关侧落地——因为取证触点唯一
（只有 `due-diligence` 能碰 PII），脱敏范围可收敛、可验证。

---

# 三、API 调用方案（出站：系统怎么调模型）

## 3.1 四层配置：为什么不是三个环境变量

早期实现用 `BASE_URL / API_KEY / MODEL` 三个进程级变量指定唯一端点。它无法表达两件必须表达的事：
**不同 Agent 用不同模型**（异构对抗的前提），以及**哪些数据流向哪个厂商**（银行必问）。

| 层 | 位置 | 回答什么 | 换它影响什么 |
|---|---|---|---|
| 1 | `config/providers.yaml` | 端点在哪、什么协议、凭证怎么取、数据落在哪个区 | 换云厂商，不动模型选型 |
| 2 | `config/models.yaml` | 用哪个模型、怎么解码、多少钱、属于哪个 `family` | 换模型，不动 Agent 拓扑 |
| 3 | `permissions.py` 的 `model_profile` | 哪个 Agent 用哪个档位 | 换绑定，自动进 Worker CR |
| 4 | `--preset` / `--profile` / 环境变量 | 运行时覆盖 | 只影响本次运行 |

### 四条不变量，进程启动时校验，违反即拒绝启动

| 不变量 | 为什么 | 反向验证 |
|---|---|---|
| 对抗双方绑定不同 `family` | 同族权重共享先验与盲区，对抗会在权重层面坍缩 | 配成同族 → `ConfigError` |
| PII 触点的模型不越出 `data_residency` 白名单 | 取证触点唯一，故这条一成立就**静态证明**了 PII 流向 | 挪出白名单 → `ConfigError` |
| `llm=False` 的 Agent 不得绑定档位 | 唯一写触点不应有模型入口 | 给 executor 绑模型 → `ConfigError` |
| 凭证只以引用形式出现 | 配置进版本库，密钥不进 | 配置中出现密钥形态 → 测试失败 |

```bash
# 亲手撞一次，看它是不是真的拦：
python3 poc/run_demo.py --case CASE-001 --profile devils-advocate=analyst-primary
# → 模型配置校验未通过，拒绝启动：对抗双方绑定了同族模型（qwen）
```

## 3.2 实际发出的 HTTP 请求

协议：**OpenAI Chat Completions 兼容**，`POST {base_url}/chat/completions`。
客户端只用标准库 `urllib`（`poc/creditsentry/llm.py` 的 `OpenAICompatBackend`）。

```http
POST https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions
Authorization: Bearer <由 credential_ref 解析，仅在此处短暂出现>
Content-Type: application/json

{
  "model": "qwen-max",
  "messages": [
    {"role": "system", "content": "<prompts.py 渲染的模板，带版本号>"},
    {"role": "user",   "content": "<上下文装配器产出的 payload JSON>"}
  ],
  "response_format": {"type": "json_object"},
  "temperature": 0.2, "top_p": 0.9, "seed": 42, "max_tokens": 4096
}
```

请求字段全部来自 Profile，**没有任何一项来自进程级环境变量**：

| 字段 | 来源 | 说明 |
|---|---|---|
| `model` | `profile.model` | |
| `temperature` / `top_p` / `seed` | Profile 同名字段 | `seed` 是尽力而为，多数端点不保证 |
| `max_tokens` | `profile.max_output_tokens` | |
| `response_format` | `profile.json_mode` | `on` / `off` / `auto`（默认）；`auto` 被端点拒绝后自动降级并在本进程内记住 |
| 超时 | `profile.timeout_ms` | |
| 重试 | `profile.max_retries` | 只对瞬时故障重试，见下 |

**密钥不落任何持久化结构**：不进配置、不进权限矩阵、不进 Trace、不进日志。
`preflight.py` 打印凭证时也只显示长度与末 4 位。

## 3.3 中间件链：顺序本身是安全设计

一次 `complete_json` 调用固定走这条链，**顺序不能颠倒**：

```
调用 → 抽 JSON → 归一化 → 结构校验 → 语义校验 → （至多一轮）修复 → 记账
```

| 环节 | 做什么 | 为什么在这个位置 |
|---|---|---|
| **传输重试** | 408/425/429/5xx 与超时退避重试；401/403/404/422 **立即上抛** | 重试一个必然失败的请求只是烧配额，还会把真正的错因埋进重试日志 |
| **抽 JSON** | 剥 ```` ```json ```` 围栏、剥前后寒暄、取最外层花括号 | 只处理**包装**问题，解析不出就交给修复轮——让模型重写比让代码猜安全 |
| **归一化** | `"62%"`→`0.62`、裸值→列表、拆 `{"result":…}` 包装、枚举大小写 | 这是表述偏差不是语义错误，纠正它不改变判断，不该浪费一轮修复 |
| **结构校验** | 枚举越界、缺必填、置信度越界、根因无证据引用 | 这些不能替模型「猜一个」，只能回喂让它自己改 |
| **语义校验** | 证据编号是否真实存在（`skills` 注入）、质疑清单是否逐项回执（`agents` 注入） | 需要账本/清单知识，不该反向渗进 Schema 层 |
| **修复轮** | 把错误 + 契约骨架回喂，**只给一轮** | 修复轮是给表述失误的补救，不是给模型反复试错 |
| **失败策略** | 见 §3.4 | |
| **记账** | 档位、族、厂商、token、费用、尝试次数、修复与否、时延 | 失败的调用**同样记账**——不然成本口径会在模型表现最差时系统性偏低 |

归一化与校验的边界有一条硬线：

> 把 `"62%"` 改成 `0.62` 是**纠正表述**；给一条没有证据的结论**编一个** ID 是**伪造证据**。
> 前者做，后者绝不做。`poc/test_safety.py` 有一条断言专门守这条线。

## 3.4 失败策略：降级方向恒为阻断

| task | caller | 失败后 | 允许降级？ |
|---|---|---|:--:|
| `risk_root_cause` | `risk-analyst` | 判 `conclusion=INSUFFICIENT` → 裁决判 `EVIDENCE_INSUFFICIENT` → 回退补证，重试用尽转人工 | **否** |
| `devils_advocate` | `devils-advocate` | 判 `verdict=INSUFFICIENT_EVIDENCE` → **阻断**，不得进入处置 | **否** |

这张表本身是一个论点：**只有不影响决策的环节才允许降级。**
尤其第二行——质疑器失效时 fail-safe 的方向是**阻断**而不是放行。
质疑器坏了就当作「无法完成质疑」，与 RiskGate 的 fail-safe 是同一条原则在不同层的体现。

降级**必须留痕**：进 Span（`llm.degraded` / `llm.degrade_reason` / `llm.schema_errors`）、
进 `logs.jsonl`、进 `metrics.json` 的 `llm_degradations`，并在终端显式打印。
一次悄悄降级的运行和一次正常运行长得一样，那么「这个结论当时是怎么来的」就永远说不清了。

## 3.5 成本

单价写在 `config/models.yaml`，**只用于成本统计，不参与任何决策**。接入前请按实际计费更新。

当前绑定下的实测 token 量（来自真实的上下文装配结果，非经验值）：

| 案件 | risk-analyst | devils-advocate | 成本 |
|---|---|---|---|
| CASE-001 | 2124 in / 282 out | 2685 in / 476 out | ￥0.0170 |
| CASE-002 | 2038 in / 271 out | 2579 in / 591 out | ￥0.0174 |
| CASE-003 | 1695 in / 190 out | 1568 in / 297 out | ￥0.0114 |

均值 **￥0.0153 / 件**；日均 2000 件推算约 ￥31/天、￥7,628/年（250 工作日）。
**不含修复轮与重试**——修复轮发生率要真机跑过才有实数，跑完看 `metrics.json` 的
`llm_repair_rounds`。`python3 tools/preflight.py` 会按你当前的绑定重算这张表。

---

# 四、API 接口说明

## 4.1 推理网关（系统 → 模型，唯一入口）

```python
LLMGateway.complete_json(
    task: str,                 # 枚举，不接受自由字符串：risk_root_cause | devils_advocate
    system: str,               # prompts.render() 产出，带版本号
    payload: dict,             # 上下文装配器产出
    *, caller: str,            # 调用方 Agent id —— 档位由它解析，这是四层配置的落点
    validator: Callable[[dict], list[str]] | None = None,   # 语义校验注入点
) -> tuple[dict, int, int]     # (结果, tokens_in, tokens_out)
```

抛 `InferenceError`（传输重试用尽 / 修复轮后仍违约）与 `CredentialError`（凭证未解析）。

配套方法：

| 方法 | 用途 |
|---|---|
| `profile_for(caller)` | 解析该 Agent 生效的档位 |
| `span_attrs(caller)` | 供调用方补到 llm Span 上（含 `model.declared` 与实际模型，分开上报） |
| `record_degradation(...)` | 登记一次失败策略降级，返回可写进 Span 的属性 |
| `usage()` | 按 Agent 分解的用量、成本、修复轮、失败调用、降级记录 |

## 4.2 两个 task 的输入 / 输出契约

契约的**唯一真源**是 `poc/creditsentry/schemas.py`；下表是它的可读呈现。

### `risk_root_cause`（定性官）

**输入** `payload`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `facts` | object | 证据的**抽取字段 + evidence_id**。原文绝不入上下文，靠哈希锚定 |
| `policy_context` | array | 政策条款片段，已按案件时点过滤（生效日 ≤ `as_of`） |
| `similar_cases` | array | 历史案例，优先级最低，超预算时最先被裁剪 |

**输出**：

| 字段 | 类型 | 约束 |
|---|---|---|
| `conclusion` | string | 枚举：`RISK_CONFIRMED` \| `INSUFFICIENT` |
| `root_causes[]` | array | 每项含 `type`(string)、`confidence`(0~1)、`evidence_ids`(**至少 1 个**)、`rationale` |
| `suggested_grade` | string\|null | 枚举：正常/关注/次级/可疑/损失 |
| `summary` | string | 可选 |

`evidence_ids` 非空是**「无证据不决策」在 Schema 层的执行点**；账本的
`assert_supported()` 是第二道不可绕过的硬拒绝。两道都要有：缺前者真机上会频繁硬崩，
缺后者等于把这条原则交给模型自觉。

### `devils_advocate`（质疑官）

**输入** `payload`：在定性官的基础上多两块，且两者都 `required=True`（永不被裁剪）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `assertion` | object | 定性官的完整输出 |
| `checklist[]` | array | **由系统从断言派生，不由模型生成**。每项含 `item_id` / `target` / `why` |

**输出**：

| 字段 | 类型 | 约束 |
|---|---|---|
| `verdict` | string | 枚举：`REFUTED` \| `PARTIALLY_REFUTED` \| `INSUFFICIENT_EVIDENCE` \| `SUPPORTED` |
| `rebuttals[]` | array | `target` / `argument` / `counter_evidence_ids` |
| `attempted_but_failed[]` | array | `target` / `tried` / `failed_because`——反驳不成立也必须留痕 |
| `evidence_gaps[]` | array of string | |
| `surviving_causes[]` | array of string | |
| `checklist_resolutions[]` | array | 每项须含 `status`（`REFUTED`/`ATTEMPTED_FAILED`/`INSUFFICIENT`）与 `item_id` **或** `target` 之一 |

`checklist_resolutions` 必须**逐项覆盖** `checklist`。漏项不是「默认通过」：
先由修复轮给模型一次补齐机会，仍不全则裁决判 `EVIDENCE_INSUFFICIENT` 阻断。

> **未被质疑的主因 ≠ 质疑通过。** 前者只是没人看过。

## 4.3 MCP 工具接口（系统 → 业务系统）

四个 Server 共 17 个工具。**权限矩阵是唯一授权来源，不在表内即无权**，
调用前逐条校验，被拒的调用也进审计日志。

### `credit-core-mcp` 信贷核心

| 工具 | 入参 | 副作用 | 有权调用者 |
|---|---|:--:|---|
| `get_facility` | `subject_id` | 读 | signal-hub / due-diligence / disposition-executor / compliance-auditor |
| `get_exposure` | `subject_id`, `depth`(1–3, 默认 2) | 读 | signal-hub / due-diligence / compliance-auditor |
| `get_collateral` | `subject_id` | 读 | signal-hub / due-diligence |
| `get_guarantee_ledger` | `subject_id` | 读 | due-diligence |
| `adjust_limit` | `subject_id`, `new_limit`, `idempotency_key`, `approval_token` | **写** | **仅 disposition-executor** |
| `add_guarantee` | `subject_id`, `guarantee_type`, `idempotency_key`, `approval_token` | **写** | **仅 disposition-executor** |
| `rollback_adjustment` | `subject_id`, `rollback_point_id`, `idempotency_key` | **写** | **仅 disposition-executor** |

### `bureau-mcp` 征信（**高敏 PII**）

| 工具 | 入参 | 有权调用者 |
|---|---|---|
| `get_credit_report` | `subject_id`, `report_date`, **`authorization_id`（必填）** | **仅 due-diligence** |
| `diff_report` | `subject_id`, `baseline_date`, `report_date`, `authorization_id` | 仅 due-diligence |
| `get_query_history` | `subject_id`, `months`(默认 6), `authorization_id` | 仅 due-diligence |

无授权编号一律 `AUTHORIZATION_MISSING`，**绝不降级绕过**。

### `judicial-mcp` 司法与工商

| 工具 | 入参 | 有权调用者 |
|---|---|---|
| `search_litigation` | `subject_name`, `date_from`, `date_to` | 仅 due-diligence |
| `get_judgment_doc` | `case_no` | 仅 due-diligence |
| `get_business_registration` | `subject_id` | 仅 due-diligence |
| `get_change_history` | `subject_id`, `months`(默认 12) | 仅 due-diligence |

### `txn-mcp` 交易流水（**PII**）

| 工具 | 入参 | 有权调用者 |
|---|---|---|
| `query_transactions` | `account_ids[]`, `date_from`, `date_to`, `cursor` | 仅 due-diligence |
| `get_counterparty_summary` | `account_ids[]`, `date_from`, `date_to` | 仅 due-diligence |
| `get_flow_pattern` | `account_ids[]`, `patterns[]` | 仅 due-diligence |

### 错误码

| 错误码 | 含义 | 客户端处置 |
|---|---|---|
| `PERMISSION_DENIED` | 权限矩阵未授予 | **不重试**，进审计日志并升级 |
| `AUTHORIZATION_MISSING` | 缺征信查询授权编号 | **不重试**，记合规事件 |
| `RATE_LIMITED` | 源系统限流 | 指数退避重试（`MCPClient(retries=, backoff=)`） |
| `NOT_FOUND` | 主体 / 案号 / 账户不存在 | 登记为证据缺口 |
| `APPROVAL_REQUIRED` / `APPROVAL_INVALID` | 缺审批令牌 / 令牌无效 | 拒绝执行，降级 L0 |
| `ROLLBACK_POINT_NOT_FOUND` | 回滚点不存在 | 升级人工 |
| `UNKNOWN_SERVER` / `UNKNOWN_TOOL` | 未注册 | 配置错误 |

> **只有限流类错误值得重试。** 权限与授权类错误立即上抛，不重试不降级——
> 这与模型侧「401 不重试」是同一条原则。

## 4.4 CLI 接口

```bash
python3 poc/run_demo.py [选项]
```

| 选项 | 说明 |
|---|---|
| `--case CASE-001\|CASE-002\|CASE-003` | 跑单个案件 |
| `--all` | 跑全部并汇总 |
| `--llm stub\|live` | 推理后端，默认 `stub` |
| `--preset NAME` | 套用绑定预设 |
| `--profile AGENT=PROFILE` | 覆盖单个 Agent 的档位（可重复，优先级高于 `--preset`） |
| `--list-presets` | 列出预设后退出 |
| `--deny-approval` | 模拟审批被拒，演示 L2 → L0 降级 |

退出码：`0` 全部断言通过；`1` 有案件与期望不符；`2` 配置校验未通过（拒绝启动）。

配套工具：

| 命令 | 用途 |
|---|---|
| `python3 tools/preflight.py [--preset X] [--no-probe]` | 真机自检与成本预估 |
| `python3 tools/live_conformance.py [--fault X]` | 离线回归 live 代码路径（13 个故障模式） |
| `python3 tools/mock_llm_server.py --fault X` | 单独起故障注入伪端点 |
| `python3 poc/test_safety.py` | 65 项安全边界回归 |
| `python3 tools/run_evals.py` | Skill 回归评估 |
| `python3 tools/check_pit.py` | 回溯案例时点冻结校验 |
| `python3 tools/gen_agentteams.py` | 由权限矩阵生成 Worker CR 与 SOUL.md |

## 4.5 产出文件契约

每次运行落在 `poc/out/<case>/`：

| 文件 | 内容 | 真机排障时看什么 |
|---|---|---|
| `trace.json` | 全链路 Span | `llm` Span 的 `model.declared` vs `gen_ai.request.model`；`llm.degraded` |
| `trace.txt` | 人可读 Span 树 | 整体链路是否走完五个阶段 |
| `evidence_ledger.json` | 证据账本（append-only，含哈希与等级） | 模型引用的 `EV-` 编号是否都在这里 |
| `case_state.json` | 共享状态终态与阶段迁移轨迹 | 是否停在 `EVIDENCE_GAP`（被阻断） |
| `logs.jsonl` | 结构化日志 | `*.degraded` / `rebuttal_checklist.unmatched_resolution` |
| `mcp_audit.jsonl` | MCP 调用审计（**含被拒调用**） | 阻断路径下写类调用是否真的为 0 |
| `处置意见书.md` | 正文带 `[EV-xxxx]` 证据角标 | |
| `审计报告.md` | 合规项逐条举证 | |
| `metrics.json` | 本次指标 | `llm_repair_rounds` / `llm_failed_calls` / `llm_degradations` |

---

# 五、真机排错对照表

| 现象 | 病因 | 处置 |
|---|---|---|
| `模型配置校验未通过，拒绝启动：对抗双方绑定了同族模型` | 覆盖或预设让定性与质疑落进同一 `family` | 换一个不同族的 profile；这是设计约束不是 bug |
| `凭证引用 env:XXX 未解析到值` | 环境变量没设或没 export 到当前 shell | `export XXX=...`；`tools/preflight.py --no-probe` 复查 |
| 探针报 `HTTP 401` | key 错或已过期 | 换 key。**注意系统不会对 401 重试**——这是刻意的 |
| 探针报 `HTTP 404` | `base_url` 或 `model` 拼错 | 核对 `config/providers.yaml` 与 `models.yaml`；模型名要与厂商控制台完全一致 |
| 探针报 `HTTP 400 … response_format` | 端点不支持 JSON 模式 | 该 profile 的 `json_mode` 设为 `auto`（默认）会自动降级；本地端点建议显式设 `on` 或 `off` |
| 端点不可达 / 超时 | 本地服务没起、端口错、网络受限 | 本地端点先 `curl {base_url}/models`；调大 `timeout_ms` |
| `metrics.json` 的 `llm_repair_rounds` 很高 | 模型稳定性不足或提示词与该模型不匹配 | 换更强档位，或看 `logs.jsonl` 里的 `schema_errors` 针对性改提示词 |
| 案件停在 `EVIDENCE_GAP` 且有 `llm_degradations` | 失败策略生效，链路被 fail-safe 阻断 | 这是**正确行为**不是故障。看 `logs.jsonl` 的 `*.degraded` 找根因 |
| 结论与 stub 不同 | 正常：定性与质疑本就是自然语言判断 | 要一致的是 Schema 与流程骨架，由契约层与状态机保证 |
| 本地小模型频繁触发修复轮 | 7B 级模型 JSON 稳定性有限 | 预期之内。它恰好让 §3.3 的机制可见；要稳定就换云端档位 |

---

# 六、验收：怎么确认这套东西真的立住了

按顺序跑完，全绿即为真机就绪：

```bash
python3 poc/run_demo.py --all                        # ① stub 基线三条链路
python3 poc/test_safety.py                           # ② 65 项安全边界回归
python3 tools/run_evals.py                           # ③ Skill 回归评估
python3 tools/check_pit.py                           # ④ 时点冻结校验
python3 tools/live_conformance.py                    # ⑤ live 路径 13 个故障模式（离线）
python3 tools/preflight.py --preset dashscope-only   # ⑥ 真机自检 + 成本预估
python3 poc/run_demo.py --all --llm live --preset dashscope-only   # ⑦ 真机跑全链路
```

第 ⑤ 步是本次新增的关键一环：它让「live 模式是否可靠」不再依赖「我们跑过一次没崩」这种说法。

---

# 七、边界：本文中哪些是已实现，哪些不是

**已实现并可复现**

- `--llm live` 全链路（OpenAI 兼容端点，标准库直连）
- 输出 Schema 契约、归一化、结构与语义双重校验、单轮修复
- 传输层重试与 `json_mode` 自动降级
- 失败策略执行点：定性失败→回退补证，质疑失败→阻断
- 绑定预设（4 组）与真机自检工具
- 故障注入伪端点与 13 个故障模式的离线回归
- 回退次数上限覆盖第二条入口（R-12），杜绝 R-04 ⇄ R-05 环路

**仍是方案，未实现**

| 项 | 说明 |
|---|---|
| 磁带模式 `--llm replay` | 把 live 输出冻结成磁带，兼得真实模型与可复现（见 `接口与实验方案.md` §1.4b） |
| 影子比对 | live 模式下同时跑 stub，结论分歧打 `divergence` Span |
| 案件级预算上限 | 目前已记账未设限 |
| 容器与 K8s 部署 | §2.1 / §2.2 给的是路径与替换点，未实机验证 |
| Higress 凭证透传与 PII 出站脱敏 | 配置层已预留 `higress-consumer:` 引用，网关侧未落地 |
| 真实行内系统接入 | 4 个 MCP Server 仍为 Mock；Tool 名称与 Schema 已按零差异设计 |

**一处诚实的说明**：`--llm live` 已在故障注入伪端点上跑通全部 13 个模式与完整链路，
但**尚未在真实商业端点上跑过**——那需要 key，而 key 在使用者手里。
第一章与 `tools/preflight.py` 就是为了让这最后一步尽可能不出意外。
