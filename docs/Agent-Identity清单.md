# Agent Identity 清单

> 对应参赛说明 **附录 A**。项目：信衡 CreditSentry。
> 拓扑：`Manager`（框架原生）→ `RiskCommander`（Team Leader）→ 6 个职能 Worker。
> 设计法则见 [agent-decomposition-law.md](./agent-decomposition-law.md)：**Agent 边界 = 工具权限边界**。

---

## 0. 权限矩阵（Agent 数量的推导依据）

● = 拥有该权限；空 = 显式无权限。此矩阵**即 Worker CR 配置**，可 diff、可审计。

| 工具 / 权限 | SignalHub | DueDiligence | RiskAnalyst | DevilsAdvocate | Executor | Auditor |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| 内部预警池（读） | ● | | | | | |
| 敞口 / 担保圈图谱（读） | ● | | | | | |
| 征信 `bureau-mcp`（读 · PII） | | ● | | | | |
| 司法工商 `judicial-mcp`（读） | | ● | | | | |
| 交易流水 `txn-mcp`（读 · PII） | | ● | | | | |
| 信贷核心 `credit-core-mcp`（读） | ● | ● | | | ● | ● |
| 信贷核心（**写 · 额度变更**） | | | | | ● | |
| Case State（读） | ● | ● | ● | ● | ● | ● |
| 证据账本（写） | | ● | | | | |
| 政策 / 案例 RAG（读） | | | ● | ● | | ● |
| 全链路 Trace（读） | | | | | | ● |
| 知识库 RiskPattern（写） | | | | | | ● |
| 对外担保台账（读） | | ● | | | | |
| LLM 自由推理 | ● | ● | ● | ● | ✗ 仅规则 | ● |
| **模型档位绑定** | `worker-light` | `worker-light` | `analyst-primary` | `advocate-primary` | ✗ **无** | `auditor-narrate` |
| 模型族 | qwen | qwen | **qwen** | **doubao** | — | qwen |

**6 = 5 个权限等价类 + 1 个目标函数对立拆分**（RiskAnalyst 与 DevilsAdvocate 权限相同但目标对立，必须隔离上下文）。

### 模型绑定也是受审计的权限

最后两行不是补充说明，是矩阵的一部分。**「哪个 Agent 用哪个模型」与「哪个 Agent 有哪些工具」
同处一份真源**（`permissions.py` 的 `model_profile`），由同一个生成器写进 Worker CR，
受同一套回归断言约束。三条不变量：

1. **异构对抗** —— RiskAnalyst 与 DevilsAdvocate 必须分属不同模型族。
   把它们拆成两个 Agent 是为了避免自洽坍缩；若共用同一批权重，坍缩只是从上下文层面
   搬到了权重层面——同一个模型的两个实例共享相同先验与相同盲区，不构成两个独立观点。
2. **PII 围栏** —— DueDiligence 是唯一 PII 触点，其绑定档位的 provider 数据驻留区
   必须落在白名单内。由于触点唯一，这一条一旦成立就**静态证明**了 PII 的流向。
3. **写触点无模型入口** —— Executor 的 `model_profile` 刻意为空，配置校验会拒绝为它绑定模型。

违反任一条，进程拒绝启动（见 `config/models.yaml` 与 `poc/test_safety.py`）。

---

## 1. RiskCommander（Team Leader）

| 字段 | 内容 |
|---|---|
| **Name** | `risk-commander` |
| **Role** | Team Leader。贷后处置 SOP 的编排者与裁决者，不亲自取证、不亲自执行。 |
| **Capabilities** | **能**：阶段状态机路由、任务 fan-out、冲突裁决、风险分级、发起审批、升级人工。<br>**不能**：调用任何取证类或处置类工具；不能修改证据账本；不能跳过 DevilsAdvocate。 |
| **Inputs** | 归并后的 RiskEvent、各 Worker 的结构化产出、Case State、审批回调。 |
| **Outputs** | `RoutingDecision`（routing_key、命中规则 ID、规则版本、下一阶段）、`Adjudication`（裁决结论、依据证据 ID、分歧记录）。JSON Schema 强校验。 |
| **Dependencies** | Skill：`RiskGate`；Worker：全部 6 个；Nacos 路由表。 |
| **Decision Boundary** | L0/L1 自主推进；L2 必须发起审批并等待人工回调；L3 只产出方案、不派发执行。裁决遵循**证据等级优先于置信度**。 |
| **Trace** | 每次路由决策落一条 `routing` Span（含 routing_key／规则 ID／规则版本）；每次裁决落一条 `adjudication` Span（含双方结论与采信理由）。全部可回放。 |

---

## 2. SignalHub（Worker）

| 字段 | 内容 |
|---|---|
| **Name** | `signal-hub` |
| **Role** | 多源风险信号聚合、降噪去重与影响面测绘。**只聚合，不定性**。 |
| **Capabilities** | **能**：跨源信号归并去重、噪声压降、敞口与担保圈/集团户/上下游关系测绘。<br>**不能**：判断风险是否成立；不能访问征信/司法/流水等外部 PII 源。 |
| **Inputs** | 内部预警池信号流、信贷核心存量敞口数据。 |
| **Outputs** | `RiskEvent{event_id, 企业主体, 信号类型集合, 首现时间, 敞口金额, 关联主体图, 初筛优先级}`。 |
| **Dependencies** | Skill：`SignalFusion`、`ExposureMapping`；MCP：`credit-core-mcp`（只读）。 |
| **Decision Boundary** | 全自主（纯只读，无外部副作用）。 |
| **Trace** | 记录输入信号条数、归并后条数、降噪率、被丢弃信号及丢弃理由（可回溯，防止误丢高风险信号）。 |

---

## 3. DueDiligence（Worker）

| 字段 | 内容 |
|---|---|
| **Name** | `due-diligence` |
| **Role** | 尽调取证官。系统中**唯一的外部 PII 数据触点**，只摆事实、不下结论。 |
| **Capabilities** | **能**：调取征信报告、裁判文书、工商登记、交易流水、押品状态的原文，做结构化抽取并登记证据。<br>**不能**：给出风险定性结论；不能提出处置方案；不能写信贷核心。 |
| **Inputs** | RiskCommander 派发的取证任务清单（含待证事实与优先级）。 |
| **Outputs** | `Evidence{evidence_id, 来源系统, 原文快照哈希, 采集时间, 抽取字段, 证据等级(强/弱/缺失)}`，原文快照存 MinIO/OSS。 |
| **Dependencies** | Skill：`CreditReportProbe`、`LitigationProbe`、`TxnFlowAnalyze`、`GuaranteeProbe`、`EvidenceLedger`；MCP：`bureau-mcp`、`judicial-mcp`、`txn-mcp`、`credit-core-mcp`。 |
| **Decision Boundary** | 全自主（只读 + 写证据账本）。**PII 出站强制经 Higress 脱敏**——因取证触点唯一，脱敏范围可收敛。 |
| **Trace** | 每次外部调用落 MCP Span（工具名、参数摘要、耗时、成功/失败）；每条证据落 `evidence.write` Span 并关联 evidence_id。 |

---

## 4. RiskAnalyst（Worker）

| 字段 | 内容 |
|---|---|
| **Name** | `risk-analyst` |
| **Role** | 风险根因定位与定性。**零工具权限的纯推理角色**。 |
| **Capabilities** | **能**：基于已登记证据做根因归因（经营性恶化／技术性逾期／实控人风险／行业系统性风险），给出五级分类建议与置信度。<br>**不能**：自行取证（防止边查边下结论的确认偏差）；不能执行处置。 |
| **Inputs** | Case State 中的证据集合、RAG 召回的政策条款与历史案例。 |
| **Outputs** | `RiskAssertion{根因候选[], 置信度, 引用证据 ID[], 建议分类, 建议处置方向}`。**每条结论必须挂载 evidence_id，无证据的断言被 Schema 拒绝**。 |
| **Dependencies** | Skill：`RiskRootCause`、`PolicyRag`、`CaseMemory`。 |
| **Decision Boundary** | 只有提议权，无执行权。证据不足时**必须**输出「证据缺口清单」而非推测结论。 |
| **Trace** | 落 `llm` Span（prompt 版本、token、耗时）+ `assertion` Span（结论与证据引用），供事后复核归因链。 |

---

## 5. DevilsAdvocate（Worker）

| 字段 | 内容 |
|---|---|
| **Name** | `devils-advocate` |
| **Role** | 对抗质疑官。**目标函数与 RiskAnalyst 刻意对立**：尽力证伪「风险成立」。核心使命是防止误杀正常经营客户。 |
| **Capabilities** | **能**：提出反证与替代解释、指出证据不足与逻辑跳跃、质疑证据时效性与代表性。<br>**不能**：提出处置方案（避免既当质疑者又当决策者）；不能自行取证。 |
| **Inputs** | 与 RiskAnalyst **完全相同**的输入（同证据集、同 RAG 上下文），但独立上下文、对立系统提示词。 |
| **Outputs** | `Rebuttal{反驳点[], 替代解释[], 证据不足项[], 结论(支持/反对/证据不足)}`。 |
| **Dependencies** | Skill：`PolicyRag`、`CaseMemory`。 |
| **Decision Boundary** | 对**自动执行**拥有一票阻断权：一旦判定「证据不足」，Case 强制回退 EVIDENCE 阶段补证，不得进入 DISPOSITION。 |
| **Trace** | 落独立 `llm` Span 与 `rebuttal` Span；与 RiskAnalyst 的结论一并进入 `adjudication` Span，分歧全程留痕。 |

---

## 6. DispositionExecutor（Worker）

| 字段 | 内容 |
|---|---|
| **Name** | `disposition-executor` |
| **Role** | 处置执行官。系统中**唯一的写触点**，且**仅规则驱动、不做自由推理**。 |
| **Capabilities** | **能**：执行白名单内动作（打标关注、发起补充资料、额度压降、追加担保、生成收贷方案）。<br>**不能**：自主决定是否执行；不能执行白名单外动作；不能审计自身结果（不相容职务分离）。 |
| **Inputs** | 已裁决且（L2 时）已审批的 `DispositionOrder{action, params, action_tier, idempotency_key, rollback_point}`。 |
| **Outputs** | `ExecutionResult{执行状态, 系统回执, 生效时间, 回滚点 ID, 审计流水号}`。 |
| **Dependencies** | Skill：`SafeDisposition`；MCP：`credit-core-mcp`（读 + 写）。 |
| **Decision Boundary** | **零自主决策**。L0/L1 凭裁决执行；L2 必须校验审批回调签名后执行；**L3 永不执行**，仅落方案。所有动作幂等，失败自动回滚至 rollback_point。 |
| **Trace** | 落 `execution` Span（含幂等键、动作前后快照、回执）；写操作强制双写审计日志，TraceId 关联。 |

---

## 7. ComplianceAuditor（Worker）

| 字段 | 内容 |
|---|---|
| **Name** | `compliance-auditor` |
| **Role** | 结果核验、合规审计与经验沉淀。**审计者与执行者分离**。 |
| **Capabilities** | **能**：核验处置是否真实生效、校验监管合规项（贷后检查频次／双人复核／留痕／时效）、生成审计报告、把确认的风险模式沉淀为 RiskPattern 与向量案例。<br>**不能**：执行或修改任何处置动作。 |
| **Inputs** | ExecutionResult、全链路 Trace、Case State、监管规则库。 |
| **Outputs** | `AuditReport{处置生效核验, 合规项逐条结论, 证据完备率, 异常项与整改建议}`、`RiskPattern`（回流知识库）。 |
| **Dependencies** | Skill：`ComplianceCheck`、`ReportCompose`、`PostmortemDistill`；MCP：`credit-core-mcp`（只读）。 |
| **Decision Boundary** | 全自主（只读业务系统 + 写知识库）。发现合规缺失时**只报告不修复**，由 RiskCommander 升级人工。 |
| **Trace** | 读取全链路 Trace 生成审计视图；沉淀动作落 `knowledge.write` Span，可追溯"哪次案件产生了哪条规则"。 |

---

## 8. Manager（框架原生，职责取舍说明）

**诚实结论：在单一贷后场景下 Manager 接近透明，我们不为它编造职责。** 保持 AgentTeams 默认的轻量管理指令集，只承担三件不可省的事：

1. **人机会话入口与人工干预** —— 风险经理在 Matrix 房间内下达指令、随时介入；
2. **多案并发下的 case room 生命周期** —— 一案一房间的创建、归档、超时回收；
3. **跨 Team 移交** —— 涉嫌欺诈移交反欺诈 Team、需诉讼保全移交法务 Team。**Manager 是唯一有权跨 Team 的角色**。

若砍掉 Manager，Team Leader 需同时承担人机接口 + 领域调度 + 跨团队移交，SOUL 显著变胖，且跨 Team 必须持有他团队权限，破坏最小权限原则。**取舍**：初赛保持极薄，复赛扩展至「贷后 + 反欺诈 + 法务」三 Team 时才变厚。
