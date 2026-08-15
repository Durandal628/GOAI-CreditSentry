# Skill · CreditReportProbe

> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。
> 真源是实现本身，因此本文档不会与代码漂移。

| 字段 | 内容 |
|---|---|
| Skill 名称 | `CreditReportProbe` |
| Skill 类型 | 自定义 Skill（L3 · 取证类） |
| 版本 | `1.3.0` |
| 使用场景 | 解析征信报告并比对期间变动 |
| 输入参数 | `subject_id, report_date, baseline_date（比对基准）` |
| 输出结果 | `CreditFacts{查询次数变动, 新增负债, 逾期记录, 对外担保变动, 关注类标记} + evidence_ids[]` |
| 调用条件 | RiskCommander 派发含 credit 类型的取证任务 |
| 依赖工具 / 系统 | bureau-mcp（只读 · PII） |
| 失败处理 | 征信源限流 → 指数退避重试 3 次；无授权查询 → 立即失败并记录合规事件，绝不降级绕过 |
| 权限与安全 | 高敏 PII；仅 due-diligence 可调用；出站强制经网关脱敏；每次查询写授权审计 |
| 可调用的 Agent | `due-diligence` |
| **绑定监管条款** | 《征信业管理条例》查询授权与用途限制 |
| **回归评估集** | 20 组合成征信报告，校验字段抽取准确率与变动比对正确性 |
| 复用价值 | 征信解析与变动比对可复用于贷前审批、授信年检 |

## 接口 Schema

完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。

## 回归评估集

- 声明覆盖目标：**20** 组
- 当前已实现：**0** 组

> 初赛阶段为种子集，差额 20 组在复赛补齐。我们如实记录覆盖度而非声称已完备——见 `eval/manifest.json`。

评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，
异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。

## 与多 Agent 协同流程的关系

本 Skill 由 `due-diligence` 调用。
调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，
越权调用在运行时被拒绝（见 `poc/test_safety.py`）。
