# Skill · CaseMemory

> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。
> 真源是实现本身，因此本文档不会与代码漂移。

| 字段 | 内容 |
|---|---|
| Skill 名称 | `CaseMemory` |
| Skill 类型 | 自定义 Skill（L2 · 知识类） |
| 版本 | `1.4.0` |
| 使用场景 | 检索历史相似处置案例与本 Case 的决策上下文 |
| 输入参数 | `subject_features, time_window, top_k, scope: case|session` |
| 输出结果 | `Cases[]{案件摘要, 当时结论, 处置动作, 事后验证结果, 相似度}` |
| 调用条件 | 定性、质疑、复盘阶段 |
| 依赖工具 / 系统 | 自研：案例结构化模板、时间窗召回策略。复用官方：向量检索 + 数据库查询 |
| 复用官方云能力 | 向量检索 + 数据库查询（可替换 pgvector + PostgreSQL） |
| 失败处理 | 无相似案例 → 明确返回空集，不用低相似度结果凑数 |
| 权限与安全 | 只读；跨机构案例默认脱敏隔离 |
| 可调用的 Agent | `risk-analyst`、`devils-advocate`、`compliance-auditor` |
| **绑定监管条款** | — |
| **回归评估集** | 15 组，校验相似案例召回相关性、时间窗过滤，以及**回溯案件中晚于案件时点沉淀的案例一律不得召回** |
| 复用价值 | Agent 记忆存储的通用实现，满足赛题 RAG 要求第 1 项 |

## 接口 Schema

完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。

> **设计要点**：无相似案例时返回空数组，不用低相似度结果凑数

## 回归评估集

- 声明覆盖目标：**15** 组
- 当前已实现：**0** 组

> 初赛阶段为种子集，差额 15 组在复赛补齐。我们如实记录覆盖度而非声称已完备——见 `eval/manifest.json`。

评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，
异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。

## 与多 Agent 协同流程的关系

本 Skill 由 `risk-analyst`、`devils-advocate`、`compliance-auditor` 调用。
调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，
越权调用在运行时被拒绝（见 `poc/test_safety.py`）。
