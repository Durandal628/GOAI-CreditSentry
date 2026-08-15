# Skill · SafeDisposition

> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。
> 真源是实现本身，因此本文档不会与代码漂移。

| 字段 | 内容 |
|---|---|
| Skill 名称 | `SafeDisposition` |
| Skill 类型 | 自定义 Skill（L3 · 执行类） |
| 版本 | `2.2.0` |
| 使用场景 | 幂等执行已裁决且已审批的处置动作，带回滚点 |
| 输入参数 | `DispositionOrder{action, params, action_tier, idempotency_key, rollback_point, approval_token}` |
| 输出结果 | `ExecutionResult{status, 系统回执, 生效时间, rollback_point_id, 审计流水号}` |
| 调用条件 | action_tier ∈ {L0, L1}，或 L2 且 approval_token 验签通过。L3 永不调用 |
| 依赖工具 / 系统 | credit-core-mcp（读 + 写） |
| 失败处理 | 执行中断 → 按 rollback_point 自动回滚（额度冲正）；重复投递 → 幂等键去重返回首次结果；回滚失败 → 立即冻结 Case 并升级人工 |
| 权限与安全 | 仅 disposition-executor 可调用；动作白名单强校验；审批 token 验签；双写审计日志 |
| 可调用的 Agent | `disposition-executor` |
| **绑定监管条款** | 《商业银行内部控制指引》授权审批；《贷后管理指引》处置留痕 |
| **回归评估集** | 幂等重放、中断回滚、越权动作拦截、无审批 L2 拦截，共 16 组 |
| 复用价值 | 幂等 + 回滚点 + 审批验签的执行封装，是任何有副作用 Agent 的通用安全底座 |

## 接口 Schema

完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。

> **设计要点**：action_tier=L3 时必定返回 PLAN_ONLY——不可逆动作永不执行

## 回归评估集

- 声明覆盖目标：**16** 组
- 当前已实现：**4** 组

> 初赛阶段为种子集，差额 12 组在复赛补齐。我们如实记录覆盖度而非声称已完备——见 `eval/manifest.json`。

评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，
异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。

## 与多 Agent 协同流程的关系

本 Skill 由 `disposition-executor` 调用。
调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，
越权调用在运行时被拒绝（见 `poc/test_safety.py`）。
