# Skill · PolicyRag

> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。
> 真源是实现本身，因此本文档不会与代码漂移。

| 字段 | 内容 |
|---|---|
| Skill 名称 | `PolicyRag` |
| Skill 类型 | 自定义 Skill（L2 · 知识类） |
| 版本 | `2.0.0` |
| 使用场景 | 按多维查询计划召回政策条款、加权融合、并把召回结果证据化 |
| 输入参数 | `plan（QueryRewrite 产出的 QueryPlan）, top_k` |
| 输出结果 | `PolicyChunks[]{条款原文, 出处, 生效日期, 相似度, matched_dimensions[], evidence_id}` |
| 调用条件 | RiskAnalyst / DevilsAdvocate / ComplianceAuditor 需要政策依据时 |
| 依赖工具 / 系统 | 自研：多维融合排序、时效过滤、召回后证据化。复用官方：向量库建库与相似度检索 |
| 复用官方云能力 | 向量检索 DashVector（可替换 pgvector） |
| 失败处理 | 召回为空 → 返回 no_policy_matched 并区分「无匹配」与「全部被时效过滤」，绝不编造条款；向量库不可用 → 降级关键词检索并标注 |
| 权限与安全 | 只读；按产品线做召回范围隔离 |
| 可调用的 Agent | `risk-analyst`、`devils-advocate`、`compliance-auditor` |
| **绑定监管条款** | —（本 Skill 是条款的检索侧） |
| **回归评估集** | 22 组 query，校验 Recall@5、零条款幻觉，以及**生效日晚于案件时点的条款一律不得召回** |
| 复用价值 | 「多维融合 + 时效过滤 + 召回即证据化」可复用于所有合规类 RAG |

## 接口 Schema

完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。

> **设计要点**：召回即证据化：每条召回结果都带 evidence_id，使结论能点回条款原文与生效日期

## 回归评估集

- 声明覆盖目标：**22** 组
- 当前已实现：**6** 组

> 初赛阶段为种子集，差额 16 组在复赛补齐。我们如实记录覆盖度而非声称已完备——见 `eval/manifest.json`。

评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，
异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。

## 与多 Agent 协同流程的关系

本 Skill 由 `risk-analyst`、`devils-advocate`、`compliance-auditor` 调用。
调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，
越权调用在运行时被拒绝（见 `poc/test_safety.py`）。
