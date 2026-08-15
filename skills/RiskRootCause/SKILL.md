# Skill · RiskRootCause

> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。
> 真源是实现本身，因此本文档不会与代码漂移。

| 字段 | 内容 |
|---|---|
| Skill 名称 | `RiskRootCause` |
| Skill 类型 | 自定义 Skill（L3 · 诊断类） |
| 版本 | `2.0.1` |
| 使用场景 | 基于已登记证据做风险根因归因与五级分类建议 |
| 输入参数 | `evidence_ids[], exposure, policy_context（RAG 召回）, historical_cases[]` |
| 输出结果 | `RiskAssertion{root_causes[]{type, confidence, evidence_ids[]}, suggested_grade, evidence_gaps[]}` |
| 调用条件 | EVIDENCE 阶段完成且 evidence_sufficiency ≥ 0.7 |
| 依赖工具 / 系统 | LLM 推理；PolicyRag、CaseMemory 提供上下文 |
| 失败处理 | 证据不足 → 强制输出 evidence_gaps 并将结论置为 INSUFFICIENT，Case 回退 EVIDENCE；LLM 超时 → 重试 2 次后升级人工 |
| 权限与安全 | 零工具权限（刻意剥夺，防边查边下结论的确认偏差）；Schema 拒绝无 evidence_ids 的断言 |
| 可调用的 Agent | `risk-analyst` |
| **绑定监管条款** | 《贷款风险分类指导原则》五级分类标准 |
| **回归评估集** | 40 组已标注案例（含 10 组误报陷阱），校验分类准确率与无证据结论率 = 0 |
| 复用价值 | 「证据 → 归因 → 分级」范式可复用于保险核赔定性、故障根因定位 |

## 接口 Schema

完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。

> **设计要点**：root_causes[].evidence_ids 的 minItems=1 是「无证据不决策」的 Schema 级执行点

## 回归评估集

- 声明覆盖目标：**40** 组
- 当前已实现：**0** 组

> 初赛阶段为种子集，差额 40 组在复赛补齐。我们如实记录覆盖度而非声称已完备——见 `eval/manifest.json`。

评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，
异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。

## 与多 Agent 协同流程的关系

本 Skill 由 `risk-analyst` 调用。
调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，
越权调用在运行时被拒绝（见 `poc/test_safety.py`）。
