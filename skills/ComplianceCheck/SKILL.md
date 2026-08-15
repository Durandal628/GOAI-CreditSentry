# Skill · ComplianceCheck

> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。
> 真源是实现本身，因此本文档不会与代码漂移。

| 字段 | 内容 |
|---|---|
| Skill 名称 | `ComplianceCheck` |
| Skill 类型 | 自定义 Skill（L3 · 治理类） |
| 版本 | `1.5.0` |
| 使用场景 | 逐条校验贷后管理合规项并输出可举证结论 |
| 输入参数 | `case_id, trace_id, rule_set_version` |
| 输出结果 | `ComplianceResult{items[]{rule_id, 条款出处, 结论, 证据引用}}, 整改建议[]` |
| 调用条件 | 处置执行完成后自动触发 |
| 依赖工具 / 系统 | 监管规则库；全链路 Trace（只读） |
| 失败处理 | 规则库版本不匹配 → 拒绝执行并告警，不用旧版本静默通过 |
| 权限与安全 | 只读；结果不可篡改，写入 append-only 审计表 |
| 可调用的 Agent | `compliance-auditor` |
| **绑定监管条款** | 《商业银行贷后管理指引》检查频次、双人复核、留痕与时效要求 |
| **回归评估集** | 每条规则正反各 1 例，共 24 组 |
| 复用价值 | 「规则集 + Trace → 逐条举证」范式可复用于任何需合规审计的 Agent 系统 |

## 接口 Schema

完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。

## 回归评估集

- 声明覆盖目标：**24** 组
- 当前已实现：**0** 组

> 初赛阶段为种子集，差额 24 组在复赛补齐。我们如实记录覆盖度而非声称已完备——见 `eval/manifest.json`。

评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，
异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。

## 与多 Agent 协同流程的关系

本 Skill 由 `compliance-auditor` 调用。
调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，
越权调用在运行时被拒绝（见 `poc/test_safety.py`）。
