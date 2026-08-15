# Skill · ExposureMapping

> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。
> 真源是实现本身，因此本文档不会与代码漂移。

| 字段 | 内容 |
|---|---|
| Skill 名称 | `ExposureMapping` |
| Skill 类型 | 自定义 Skill（L3 · 诊断类） |
| 版本 | `1.1.0` |
| 使用场景 | 测绘风险主体的敞口与传染面：担保圈、集团户、上下游 |
| 输入参数 | `subject_id, depth（关系穿透层数，默认 2）, relation_types[]` |
| 输出结果 | `Exposure{total_amount, related_subjects[], guarantee_ring[], truncated_at_depth}` |
| 调用条件 | RiskEvent 生成后立即调用 |
| 依赖工具 / 系统 | credit-core-mcp（只读） |
| 失败处理 | 关系穿透超时 → 返回已穿透层级并标注 truncated_at_depth；核心系统不可用 → 用 T-1 快照并标注数据时点 |
| 权限与安全 | 只读；输出含敞口金额属敏感数据，出站脱敏 |
| 可调用的 Agent | `signal-hub` |
| **绑定监管条款** | 《商业银行大额风险暴露管理办法》关联客户认定 |
| **回归评估集** | 12 组含担保圈与集团户的图结构样例，校验穿透正确性与环路检测 |
| 复用价值 | 图穿透内核可复用于供应链金融核心企业测绘、反洗钱资金网络分析 |

## 接口 Schema

完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。

## 回归评估集

- 声明覆盖目标：**12** 组
- 当前已实现：**0** 组

> 初赛阶段为种子集，差额 12 组在复赛补齐。我们如实记录覆盖度而非声称已完备——见 `eval/manifest.json`。

评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，
异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。

## 与多 Agent 协同流程的关系

本 Skill 由 `signal-hub` 调用。
调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，
越权调用在运行时被拒绝（见 `poc/test_safety.py`）。
