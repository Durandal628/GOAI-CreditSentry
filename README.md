# 信衡 CreditSentry

**贷后风险预警到授信处置的多 Agent 可举证自主闭环系统**

GOAI 赛道一【新智基座｜Agent Infra】· 选题方向四：金融风控与理赔自动化（信贷风控与贷后处置）

队伍：`Durandal` ｜ 成员：`李家宝` / `刘泽苑`

---

## 这是什么

面向商业银行小微与对公信贷的**贷后管理**。不做「AI 预警」——银行不缺预警系统，缺的是
**敢自动执行、又能对监管举证**的处置闭环。

系统以 AgentTeams 为协同基点，由 1 个 Manager + 1 个 Team Leader + 6 个职能 Worker
完成端到端闭环：信号聚合 → 尽调取证 → 风险定性 ⇄ 对抗质疑 → 分级处置 → 合规审计与经验回流。

### 三个设计主张

| | 主张 | 落地位置 |
|---|---|---|
| 一 | **内控即拓扑**　Agent 数量由权限等价类推导，不是拍脑袋 | [`permissions.py`](poc/creditsentry/permissions.py) · [拆分法则](docs/agent-decomposition-law.md) |
| 二 | **无证据不决策**　结论必须挂载可溯源证据，质疑官不可跳过 | [`ledger.py`](poc/creditsentry/ledger.py) · [`routing.py`](poc/creditsentry/routing.py) |
| 三 | **不可逆动作永不自动执行**　四维闸门 L0–L3，fail-safe 而非 fail-open | [`gate.py`](poc/creditsentry/gate.py) |

---

## 快速开始

**环境要求**：Python 3.9+。**无需任何第三方依赖，无需云账号，无需网络。**

### 最快的方式：打开工作台

```bash
python3 poc/serve.py --open
```

浏览器里选一笔预警案件点「开始处置」，看它跑完取证与定性——
**并在需要人拍板的地方真的停下来等你点按钮**（不是动画，后端线程确实阻塞在那里）。

六个可现场操作的动作：点开证据看哈希与来源、并排看定性⇄质疑的分歧、
驳回审批看它降级为只读且额度未被修改、改一个决定性因子当场翻案、
把敞口改成 2 亿看同一个动作从 L2 升到 L3、让零权限的 Agent 去调写接口看它被拒且留痕。
详见 [**工作台使用说明**](docs/工作台使用说明.md)。

> 工作台与命令行是**同一套代码、同一份产出**，没有为演示另造一条链路。
> 新增的只有三个旁路钩子（Span 观察者、日志出口、审批回调），
> 且它们抛错一律吞掉——可视化坏了不能反过来弄坏业务链路，这条由回归测试断言。

### 命令行

```bash
python3 poc/run_demo.py --all          # 跑通三条完整链路
python3 poc/test_safety.py             # 74 项安全边界回归
python3 tools/run_evals.py             # 906 组 Skill 回归评估
python3 tools/check_pit.py             # 回溯案例的时点冻结校验（无前视信息）
python3 tools/live_conformance.py      # live 代码路径的 13 个故障模式（离线，无需 key）
```

五条命令都应在数秒内完成且全绿。产出落在 [`poc/out/`](poc/out/)。

想接真实模型跑一次，见 [**真机部署与 API 方案**](docs/真机部署与API方案.md)——
一个百炼 key 即可跑通异构对抗：

```bash
export DASHSCOPE_API_KEY=...
python3 tools/preflight.py --preset dashscope-only                  # 先自检再花钱
python3 poc/run_demo.py --all --llm live --preset dashscope-only
```

### 三条演示链路 —— 覆盖三种不同的闸门结局

| 命令 | 场景 | 期望结论 |
|---|---|---|
| `--case CASE-001` | 三类信号相互印证，质疑均不成立 | `RISK_CONFIRMED` · **L2** · 压降 30% + 追加担保（经审批后执行） |
| `--case CASE-002` | 信号类型完全相同，但个案层面均不具实质性 | `RISK_REFUTED` · **L0** · 维持原状 + 加强监测 |
| `--case CASE-003` | **真实历史回溯**：担保圈传染，直接敞口 6.5 亿 | `RISK_CONFIRMED` · **L3** · 只出方案，系统不执行 |
| `--case CASE-001 --deny-approval` | 人工审批被拒 | 降级为 **L0**，额度未被修改 |

> **链路 B 才是技术难点。** 001 与 002 是同一主体、完全相同的信号类型（涉诉 + 集中转出 +
> 法代变更），差别只在个案细节。让 Agent 敢说「这不是风险」，比让它报警难得多——
> 误杀正常客户会直接抽贷断贷、引发投诉与监管问责。
>
> **链路 C 是真实历史案例的时点冻结回测。** 详见下节。三条链路合起来证明四维闸门真的在分级：
> 同一个「压降 30%」动作，620 万敞口时是 L2（审批后执行），6.5 亿敞口时升为 L3（只出方案）。

### 链路 C：真实历史案例回溯

`CASE-003` 不是合成数据，是**已公开定论的历史事件回溯**——2017 年山东某担保圈风险传染案。
把时钟冻结在 `as_of = 2017-04-01`，**只喂入该日之前的公开信息**，检验系统当时能否识别风险。

- 系统判定：`RISK_CONFIRMED` · 关注类 · 净未覆盖代偿敞口达直接敞口 **2.02 倍**
- 历史结局：自 as_of 起 **31 个月**后发生债券违约（该结局仅用于事后评分）

三条纪律，都由代码而非承诺保证：

| 纪律 | 落地方式 |
|---|---|
| 只回溯**已公开定论**的历史事件，不对存续未定论主体输出风险结论 | 见 fixture 的 `_ethics_boundary` |
| **无前视信息**：每条证据的 `first_public_date` 不晚于 `as_of_date` | `tools/check_pit.py` 逐条校验 |
| **历史结局不可达**：`retrospective_outcome` 在 `World` 构造时即被摘出 | 任何 Server / Skill / Agent 都取不到，是结构约束不是约定 |

数据来源逐字段标注 `provenance`：`real_public`（担保关系、金额、缓释措施、危机爆发时点）、
`derived`（单家银行敞口——从不公开，按公开债务规模推导）、
`synthetic`（征信与流水——依法不可公开，只能合成）。
**新闻报道只用于定位案件与时间线，不作为证据账本中的证据**——这恰好符合我们自己的证据等级规则。

### 每次运行的产出

```
poc/out/<case>/
├── trace.json            全链路 Span（含 routing / adjudication 两类本方案特有 Span）
├── trace.txt             人可读的 Span 树，可直接截图
├── evidence_ledger.json  证据账本（append-only，含哈希与证据等级）
├── case_state.json       共享状态终态与阶段迁移轨迹
├── logs.jsonl            结构化日志（trace_id 关联决策依据与失败原因）
├── mcp_audit.jsonl       MCP 调用审计日志（含被拒调用）
├── 处置意见书.md          正文每处结论带 [EV-xxxx-xxxx] 证据角标
├── 审计报告.md            合规项逐条举证 + 经验沉淀
└── metrics.json          本次运行的指标
```

---

## 工程结构

```
├── docs/                     方案文档
│   ├── 作品简介.md            【初赛必交】
│   ├── 工作台使用说明.md        六个可现场操作的演示动作
│   ├── 真机部署与API方案.md    部署步骤 · API 调用方案 · 接口说明 · 排错表
│   ├── Agent-Identity清单.md  附录 A · 7 个身份 + 权限矩阵
│   ├── Skill清单.md           附录 B · 16 个 Skill 全字段
│   ├── agent-decomposition-law.md   Agent 拆分法则（方法论开源物）
│   └── 项目一页纸.md          附录 C
├── config/                   模型配置（providers.yaml / models.yaml）
├── deck/                     方案 PPT（HTML → PDF）
├── agentteams/               AgentTeams 声明式配置【由权限矩阵生成】
│   ├── souls/<agent>/SOUL.md      Worker 包内身份定义
│   ├── workers/<agent>.yaml       Worker CR（声明式 MCP + 权限）
│   ├── team.yaml                  Team CR
│   └── routing-table.yaml         SOP 五阶段路由表
├── skills/<name>/            CreditSkill Spec 三件套【由 @skill 元数据生成】
│   ├── SKILL.md  schema.json  eval/{cases.jsonl,manifest.json}
├── mcp_servers/              4 个 Mock MCP Server（与真实系统零 Schema 差异）
├── web/                      风险处置工作台前端（原生 HTML/CSS/JS，零构建）
├── poc/                      可运行 PoC
│   ├── creditsentry/         核心实现（schemas.py 为 live 模式的输出契约）
│   ├── serve.py              工作台后端：REST + SSE + 阻塞式人工审批
│   ├── fixtures/             案件数据（含 1 例真实历史回溯）+ 政策库 + 案例库
│   ├── run_demo.py           端到端入口
│   └── test_safety.py        安全边界回归
└── tools/                    生成器、评估执行器与时点冻结校验器
    ├── preflight.py          真机自检：配置 / 凭证 / 端点 / 成本
    ├── mock_llm_server.py    故障注入伪端点（OpenAI 兼容）
    └── live_conformance.py   live 代码路径的离线一致性与故障回归
```

### 关于生成物

`agentteams/` 与 `skills/` 下的内容**全部由代码生成，请勿手工编辑**：

```bash
python3 tools/gen_agentteams.py   # 真源：poc/creditsentry/permissions.py
python3 tools/gen_skill_docs.py   # 真源：poc/creditsentry/skills.py 的 @skill 元数据
```

这样做的原因是让「权限矩阵即 Worker CR 配置」成为**可校验的事实而非修辞**：
`poc/test_safety.py` 会断言磁盘上的 CR 与 SOUL.md 逐字节等于由矩阵生成的结果，
两边一旦漂移，测试立即失败。

---

## 验收清单

初赛阶段可逐条复核以下断言，全部由代码强制：

| # | 断言 | 验证方式 |
|---|---|---|
| 1 | 不可逆动作在**任何**入参组合下都不落入 L0/L1 | `tools/run_evals.py RiskGate`（865 组全组合遍历） |
| 2 | 入参缺失时 fail-safe 降级为 L3，而非放行 | `poc/test_safety.py` G-02 |
| 3 | 白名单外动作一律拒绝 | `poc/test_safety.py` G-01 |
| 4 | 写权限唯一：只有 executor 能改额度 | `poc/test_safety.py` 权限矩阵不变量 |
| 5 | PII 触点唯一：只有 due-diligence 能读征信与流水 | 同上 |
| 6 | 定性官零工具权限（防确认偏差） | 同上 |
| 7 | 无证据 / 引用无效证据 / 仅凭缺失证据的结论一律被拒 | `poc/test_safety.py` 证据约束 |
| 8 | 质疑环节不可跳过（硬约束写在路由表，不交给 LLM） | `poc/test_safety.py` R-04 |
| 9 | 全流程仅一条回退边，重试用尽即转人工 | `poc/test_safety.py` R-03/R-05 |
| 10 | 每条 routing Span 都含 routing_key / 规则 ID / 规则版本 | `poc/out/*/trace.json` |
| 11 | 执行方与审计方不是同一 Agent | `poc/test_safety.py` 职责分离 |
| 12 | 幂等重放不重复执行；回滚可恢复原值 | `poc/test_safety.py` 审批与幂等 |
| 13 | 审批被拒即降级 L0，额度不被修改 | `poc/run_demo.py --deny-approval` |
| 14 | Worker CR / SOUL.md / 路由表与真源零漂移 | `poc/test_safety.py` 配置一致性 |
| 15 | 敞口维度真的在分级：同一动作 620 万 → L2，6.5 亿 → L3 | `poc/test_safety.py` 大额敞口升档 |
| 16 | L3 案件全程不派发执行方，写类 MCP 调用为 0 | `poc/test_safety.py` L3 不执行 |
| 17 | 对抗双方必须异构；配成同族即拒绝启动 | `poc/test_safety.py` 异构对抗（含反向验证） |
| 18 | PII 触点的模型不越出数据驻留白名单 | `poc/test_safety.py` PII 围栏（含反向验证） |
| 19 | 回溯案例无前视信息；历史结局结构上不可达 | `tools/check_pit.py` + `poc/test_safety.py` |
| 20 | 配置文件不含任何明文密钥 | `poc/test_safety.py` 凭证引用 |
| 21 | 质疑覆盖不足即阻断：未被质疑的主因不等于质疑通过 | `poc/test_safety.py` 质疑覆盖（含反向验证） |
| 22 | 应取而未取的事实被补登记为显式缺口，不会静悄悄消失 | `poc/test_safety.py` 取证清单 |
| 23 | 求证方不产生否定式子查询，证伪方必须产生 | `poc/test_safety.py` 查询改写 |
| 24 | 知识维无前视：召回条款生效日 ≤ 案件时点 | `tools/check_pit.py` 第 4 项 + 端到端断言 |
| 25 | 原文不得灌入上下文；裁剪必须留痕 | `poc/test_safety.py` 上下文装配 |
| 26 | 澄清能自动则不问人，问人必须给选项 | `poc/test_safety.py` 澄清选路 |
| 27 | 两种推理模式产出同一套 Schema：stub 输出也必须过 live 的校验器 | `poc/test_safety.py` 输出契约 |
| 28 | 归一化只吸收表述差异，绝不伪造证据引用（含反向验证） | `poc/test_safety.py` 归一化边界 |
| 29 | 模型失效时降级方向恒为阻断：定性失败回退补证，质疑失败阻断 | `poc/test_safety.py` 失败策略 |
| 30 | live 路径端点正常时与 stub 逐字节一致；异常时阻断且写调用为 0 | `tools/live_conformance.py`（13 个故障模式） |
| 31 | 结论由证据驱动：翻转决定性因子必须翻转对应主因的质疑结论 | `poc/test_safety.py` 因子驱动 |
| 32 | 人工审批回调被驳回即降级 L0，全程零写操作 | `poc/test_safety.py` 审批回调 |
| 33 | 可视化旁路抛错不影响业务链路，结果逐字段不变 | `poc/test_safety.py` 实时旁路 |
| 34 | 转人工必须产出可派发的取证任务清单：每项含来源系统、授权要求与责任岗位 | `poc/test_safety.py` 转人工交接 |
| 35 | 三种报告互斥：本次没产出的旧报告一律清掉，杜绝「未定论却有处置意见书」 | `poc/test_safety.py` 报告互斥 |
| 36 | 原件可翻开且每次调取都重算哈希；篡改快照当场识破 | `poc/test_safety.py` 原件校验 |

---

## 模型配置：四层分离

**每个 Agent 用哪个模型，是受审计的绑定，不是一个全局环境变量。**

早期实现用三个进程级变量指定唯一端点，所有 Agent 共用。那样做无法表达两件必须表达的事：
不同 Agent 用不同模型（异构对抗的前提），以及哪些数据流向哪个厂商（银行必问的合规问题）。

| 层 | 位置 | 回答什么 |
|---|---|---|
| 1 | [`config/providers.yaml`](config/providers.yaml) | 端点在哪、什么协议、**凭证怎么取**、数据落在哪个区 |
| 2 | [`config/models.yaml`](config/models.yaml) | 用哪个模型、怎么解码、多少钱、属于哪个 `family` |
| 3 | [`permissions.py`](poc/creditsentry/permissions.py) 的 `model_profile` | 哪个 Agent 用哪个档位 |
| 4 | `--profile agent=profile` / 环境变量 | 运行时覆盖，便于调试与换档实验 |

分层可组合：换云厂商不动模型选型，换模型不动 Agent 拓扑，换绑定不动任何配置文件
（改第 3 层即自动进 Worker CR）。

### 四条不变量，违反即拒绝启动

| 不变量 | 为什么 |
|---|---|
| **异构对抗**：定性官与质疑官必须绑定不同 `family` | 拆成两个 Agent 是为了避免自洽坍缩。若共用同一批权重，坍缩只是从上下文层面搬到权重层面——同一模型的两个实例共享相同先验与盲区，不构成两个独立观点 |
| **PII 围栏**：唯一 PII 触点的模型不得越出数据驻留白名单 | 由于取证触点唯一，这条一成立就**静态证明**了 PII 不会流向白名单外的厂商 |
| **写触点无模型入口**：`llm=False` 的 Agent 不得绑定任何档位 | 执行环节不给模型发挥空间 |
| **凭证只以引用形式出现** | `env:` / `k8s-secret:` / `higress-consumer:` / `none:`，配置可进版本库，密钥不进 |

```bash
python3 poc/run_demo.py --case CASE-001 --profile devils-advocate=analyst-primary
# → 模型配置校验未通过，拒绝启动：对抗双方绑定了同族模型（qwen）
```

### 两种推理模式

默认 `--llm stub`：**确定性规则推理器**，不调用任何模型，同输入恒同输出。

这不是为了掩饰能力，而是因为评测集需要可复现——Skill 的 `eval/` 要作为发布门禁，
门禁就不能依赖模型的当次发挥。编排、路由、闸门、证据账本、审计**全部走真实代码路径**，
被替换的只有「定性」与「质疑」两处自然语言推理，且两种模式产出同一套 JSON Schema。
即便在 stub 模式下，网关仍会解析并上报各 Agent **声明绑定**的档位，
Trace 中「实际用了什么」与「生产上会用什么」分开记录，两件事都不含糊。

接真实模型（`--llm live`）：按 `config/` 中各 Agent 各自的绑定分别调用，
凭证从 `credential_ref` 指向的位置解析。客户端只用标准库 `urllib`，
兼容任何 OpenAI 协议端点，不引入 SDK 依赖。

```bash
export DASHSCOPE_API_KEY=...     # 见 config/providers.yaml 的 credential_ref
python3 tools/preflight.py --preset dashscope-only     # 配置 / 凭证 / 端点 / 成本，一次问清
python3 poc/run_demo.py --case CASE-001 --llm live --preset dashscope-only
```

**live 与 stub 的差别不只是「换个后端」。** stub 模式下模型输出由代码构造，字段必然齐全；
live 模式下它由模型生成，没有任何东西保证它齐全。因此 live 路径上多了一层输出契约：

```
调用 → 抽 JSON → 归一化 → 结构校验 → 语义校验 → （至多一轮）修复 → 失败策略
```

归一化与校验之间有条硬线：把 `"62%"` 改成 `0.62` 是**纠正表述**，
给一条没有证据的结论**编一个** ID 是**伪造证据**。前者做，后者绝不做。

失败策略的方向恒为**阻断**而非放行——定性失败判「证据不足」回退补证，
质疑失败判「质疑未完成」直接阻断。质疑器坏了不等于质疑通过。

这三段代码有个共同麻烦：**只在模型表现不好时才会被执行到**，真实端点没法按需复现。
所以配了故障注入伪端点，13 个故障模式可离线、零成本回归：

```bash
python3 tools/live_conformance.py        # 无需 key、无需联网
```

其中 `--fault none` 断言 live 产出与 stub **逐字节一致**（伪端点内部就是确定性推理器），
证明新增各层不扭曲内容；`always-bad` 与 `auth-fail` 断言案件被阻断在 `EVIDENCE_GAP`
且**写类 MCP 调用为 0**。

只有一个 key 也能跑：异构对抗约束的是**模型族**不是厂商，同一个百炼端点上
qwen 与 deepseek 分属两族，因此满足约束。全部预设见 `--list-presets`。

---

## Prompt 与上下文工程

### 提示词是带版本的资产

`prompts.py` 是注册表，每个模板带 `version` / `objective` / `changelog`，版本号写进 Span。
渲染时**缺槽位与多余槽位都抛错**——渲染失败比渲染出半成品安全。

一个刻意的结构选择：**目标函数单独成字段，不埋在正文里**。对抗双方的差异就是这一句，
拎出来才能在回归里断言「两个角色确实在做不同的事」。

### 上下文装配：三个坑，逐个用代码堵

| 坑 | 堵法 |
|---|---|
| **静默截断** | 裁剪必须留痕，`manifest.dropped` 记录丢了什么、为什么丢，并进 Span |
| **原文灌入** | `add_evidence_refs()` 强制只放 `evidence_id` + 抽取字段，检测到长文本直接抛错 |
| **关键块被挤掉** | `required=True` 的块不参与裁剪；必需块本身超预算则抛错，不硬塞 |

证据原文绝不入上下文（模型要的是抽取字段，原文靠哈希锚定）；
但知识片段的正文就是推理对象，必须入——所以后者只做逐片截断且**截断留痕**，
默默砍掉条款后半段会让模型看不到但书和除外情形。

### 待办清单：不是提示技巧，是完成性契约

to do list 之所以有效，是把长程任务外化成可枚举、可检查的子项，
让模型不必靠注意力在长上下文里维持目标。**但它有个根本弱点**：
清单通常由模型自己维护，模型可以偷偷改、悄悄划掉。

所以我们的清单是：**由系统派生 → 注入提示词告知完成标准 → 模型返回后由代码逐项核对**。
提示词里的清单只是告知契约，真正起作用的是事后那次核对。

这堵住了一个真实漏洞：此前质疑方可以只反驳 3 条主因里的 1 条、对另外 2 条保持沉默，
系统无法区分「反驳失败」与「根本没看」。

> **未被质疑的主因 ≠ 质疑通过。** 前者只是没人看过。

| 清单 | 派生自 | 未覆盖时 |
|---|---|---|
| 质疑清单 | 定性方每条置信度 ≥ 0.5 的主因 | 裁决判 `EVIDENCE_INSUFFICIENT`，回退补证；重试用尽转人工 |
| 取证清单 | 信号类型 → 该类型必须取到的事实 | 补登记为显式证据缺口，进取证清单交人工 |

### 六维查询改写

`stance` 立场 · `terminology` 术语规范化 · `signal_topic` 信号主题 ·
`clause_ref` 条款直查 · `negation` 否定式（仅证伪方）· `recency` 时效。

每条命中记录 `matched_dimensions`，既是可解释性，也是评估改写效果的依据——
某一维从不贡献命中，说明它要么设计错了，要么词表没覆盖到。

**时效维修复了一个真实缺陷**：`as_of` 为 2017-04-01 的回溯案件曾召回 2025-06-01
才生效的行内制度。这是知识维度的前视污染，比证据层更隐蔽——条款看起来「一直都在」。

澄清不默认追问用户，三条渠道按优先级：`AUTO`（有规则可自动决定）→
`SYSTEM_TASK`（派可执行的取证任务）→ `HUMAN_CHOICE`（**选项式**提问，不问开放问题）。
理由很实际：银行场景里补充信息的成本不在用户打字，在于去哪个系统查、有没有授权。

---

## 依赖披露与合规边界

| 项 | 说明 |
|---|---|
| **数据来源** | **合成数据 + 公开数据，不含任何真实银行客户数据。** 链路 A/B 为全合成（主体、账号、金额、案号均为构造值）；链路 C 为**已公开定论的历史事件回溯**，逐字段标注 `provenance`：担保关系与金额取自公开披露，单家银行敞口为推导值，征信与流水依法不可公开故为合成 |
| **回溯伦理边界** | 只回溯已有公开定论的历史事件（已违约 / 已重整 / 已结案）。**不对当前存续且未定论的主体输出风险结论。** 新闻报道仅用于定位案件与时间线，不进入证据账本 |
| **前视信息** | 每条证据标注 `first_public_date` 并由 `tools/check_pit.py` 断言不晚于 `as_of_date`；历史结局在 `World` 构造时即被摘出，Agent 结构上不可达 |
| **运行时依赖** | **零第三方依赖**，仅 Python 标准库。开发期校验用到的 PyYAML 不是运行时依赖 |
| **商业 API** | 仅 `--llm live` 模式下调用商业模型 API，用于定性、质疑两个环节；默认 stub 模式完全不联网 |
| **闭源模型** | 可替换为开源模型，Prompt 与 Skill Schema 不变 |
| **生产组件** | AgentTeams、Higress、Nacos、RocketMQ、PostgreSQL + pgvector、MinIO、OpenTelemetry，均为开源组件 |
| **官方云 Skills** | 仅用于 L1 云操作层（对象存储、向量检索、日志、监控），均标注开源等价替代（MinIO / pgvector / Loki / Prometheus），PoC 默认走开源替代 |
| **已有项目基础** | 本项目为全新构建，未基于团队既有项目二次开发 |

---

## 当前边界

我们明确说明做到了什么、没做什么：

**已完成**　6 Worker 的 SOUL 与 CR 声明、SOP 五阶段路由状态机、15 个 Skill 的 Schema 与实现、
4 个 Mock MCP Server、证据账本、模型配置四层分离与四条不变量、
三条链路端到端跑通（含一条真实历史回溯）、OTel 语义 Trace 落盘、
69 项安全回归、906 组 Skill 评估、时点冻结校验器；
提示词版本治理、上下文装配器、质疑与取证双清单、六维查询改写；
`--llm live` 全链路（输出契约 + 归一化 + 双重校验 + 修复轮 + 失败策略执行点）、
故障注入伪端点与 13 个故障模式的离线回归、真机自检工具、4 组绑定预设。

**尚未做**　真实行内系统接入；生产级并发；完整 golden set 规模化标注；
9 个 Skill 的评估集仍为种子集（覆盖度差额在 [`skills/MANIFEST.json`](skills/MANIFEST.json) 中如实记录，
未把「没跑」粉饰为「通过」）；磁带模式与影子比对；容器与 K8s 部署未实机验证。

**一处必须说清的边界**　`--llm live` 已在故障注入伪端点上跑通全部 13 个故障模式与完整链路，
但**尚未在真实商业端点上跑过**——那需要 key，而 key 在使用者手里。
[`tools/preflight.py`](tools/preflight.py) 就是为了让这最后一步尽可能不出意外：
配置、凭证、端点连通性、模型名、JSON 模式与成本，在花第一分钱之前一次问清。

**一处已知的标定局限**　`EXPOSURE_ESCALATE` 阈值（500 万 / 2000 万）目前对所有客户分层
使用同一套绝对值。在链路 C 这类大型集团客户上，它会把几乎所有可逆动作都推到 L3。
机制是对的，标定不对——复赛应把阈值改为按客户分层参数化。这是跑真实案例才暴露出来的问题，
如实记在这里而不是悄悄调参掩盖。

**复赛计划**　接入真实 Nacos 做 Skill 与路由表灰度回滚；Higress 网关落地凭证透传与 PII 出站脱敏；
评估集扩至 100+ 标注案例；回滚与越权拦截的故障注入演练；
Manager 变厚——扩展至「贷后 + 反欺诈 + 法务」三 Team。

---

## 开源计划

| 组件 | 说明 | 协议 |
|---|---|---|
| Agent 拆分法则 | 与业务解耦的方法论 | CC BY 4.0 |
| CreditSkill Spec | Skill 规范三件套 | Apache-2.0 |
| EvidenceLedger | 证据账本（最通用组件） | Apache-2.0 |
| RiskGate | 四维执行闸门内核 | Apache-2.0 |
| 4 个 MCP 接口契约 | 含契约测试用例 | Apache-2.0 |
| 风险案例回放数据集 | 含误报陷阱样例 | CC BY 4.0 |
