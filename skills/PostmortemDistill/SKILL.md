# Skill · PostmortemDistill

> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。
> 真源是实现本身，因此本文档不会与代码漂移。

| 字段 | 内容 |
|---|---|
| Skill 名称 | `PostmortemDistill` |
| Skill 类型 | 自定义 Skill（L3 · 执行类） |
| 版本 | `1.1.0` |
| 使用场景 | 把已核实的案件沉淀为可复用的 RiskPattern 与向量案例 |
| 输入参数 | `case_id, final_adjudication, execution_result, audit_report` |
| 输出结果 | `RiskPattern{模式描述, 触发条件, 判定要点, 反例说明, 适用边界} + 结构化案例` |
| 调用条件 | 审计完成且案件闭环 |
| 依赖工具 / 系统 | 知识库（写）；L2 CaseMemory 写入通道 |
| 失败处理 | 沉淀内容与既有 RiskPattern 冲突 → 标记 conflict 待人工裁定，不自动覆盖 |
| 权限与安全 | 仅 compliance-auditor 可调用；写入内容脱敏后入库 |
| 可调用的 Agent | `compliance-auditor` |
| **绑定监管条款** | 《商业银行贷后管理指引》风险事件复盘要求 |
| **回归评估集** | 10 组闭环案件，校验沉淀后 CaseMemory 召回命中率提升 |
| 复用价值 | 经验回流闭环可复用于任何「越用越准」的 Agent 系统 |

## 接口 Schema

完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。

## 回归评估集

- 声明覆盖目标：**10** 组
- 当前已实现：**0** 组

> 初赛阶段为种子集，差额 10 组在复赛补齐。我们如实记录覆盖度而非声称已完备——见 `eval/manifest.json`。

评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，
异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。

## 与多 Agent 协同流程的关系

本 Skill 由 `compliance-auditor` 调用。
调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，
越权调用在运行时被拒绝（见 `poc/test_safety.py`）。
