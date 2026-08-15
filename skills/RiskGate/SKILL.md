# Skill · RiskGate

> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。
> 真源是实现本身，因此本文档不会与代码漂移。

| 字段 | 内容 |
|---|---|
| Skill 名称 | `RiskGate` |
| Skill 类型 | 自定义 Skill（L3 · 治理类） |
| 版本 | `3.1.0` |
| 使用场景 | 计算处置动作的执行层级 L0–L3（安全核心） |
| 输入参数 | `risk_grade, evidence_level, exposure_amount, action_type, reversibility` |
| 输出结果 | `Gate{action_tier, 需审批, 审批角色[], idempotency_key, rollback_point, 判定理由}` |
| 调用条件 | 裁决完成、进入 DISPOSITION 阶段时强制调用，无旁路 |
| 依赖工具 / 系统 | 纯规则引擎，无外部依赖（安全判定不依赖网络与 LLM） |
| 失败处理 | 任何入参缺失或规则未命中 → 默认降级为 L3（只出方案不执行）。fail-safe 而非 fail-open |
| 权限与安全 | 规则表经 Nacos 版本管控，变更需双人审核；判定结果写审计日志 |
| 可调用的 Agent | `risk-commander` |
| **绑定监管条款** | 《商业银行内部控制指引》授权审批与不相容职务分离 |
| **回归评估集** | 全组合边界用例（含缺失入参、极端金额、未知动作类型），校验不可逆动作永不落入 L0/L1 |
| 复用价值 | 四维闸门模型是场景无关的高风险动作管控内核，可复用于运维自愈、保险赔付、自动化交易 |

## 接口 Schema

完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。

> **设计要点**：入参可为 null 是刻意的——缺失时由 G-02 fail-safe 降级为 L3，而非拒绝调用

## 回归评估集

- 声明覆盖目标：**全组合遍历** 组
- 当前已实现：**865** 组

> 已达成声明的覆盖目标。

评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，
异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。

## 与多 Agent 协同流程的关系

本 Skill 由 `risk-commander` 调用。
调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，
越权调用在运行时被拒绝（见 `poc/test_safety.py`）。
