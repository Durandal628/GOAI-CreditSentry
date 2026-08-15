# Skill · LitigationProbe

> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。
> 真源是实现本身，因此本文档不会与代码漂移。

| 字段 | 内容 |
|---|---|
| Skill 名称 | `LitigationProbe` |
| Skill 类型 | 自定义 Skill（L3 · 取证类） |
| 版本 | `1.4.2` |
| 使用场景 | 涉诉检索并做实质性判定（标的额占比 / 案由性质 / 结案状态 / 诉讼地位） |
| 输入参数 | `subject_name, date_range, exposure_amount（用于计算标的额占比）` |
| 输出结果 | `LitigationFacts{cases[]{案由, 标的额, 占敞口比, 诉讼地位, 结案状态}} + evidence_ids[]` |
| 调用条件 | 取证任务含 judicial 类型 |
| 依赖工具 / 系统 | judicial-mcp（只读） |
| 失败处理 | 检索超时 → 返回部分结果并标注 partial；重名无法消歧 → 输出候选集标注 ambiguous，不自动认定 |
| 权限与安全 | 只读公开司法数据；主体名称出站脱敏 |
| 可调用的 Agent | `due-diligence` |
| **绑定监管条款** | 《贷款风险分类指导原则》关于司法风险的认定口径 |
| **回归评估集** | 25 组涉诉样例，含 8 组「小额买卖合同纠纷不构成实质风险」的误报陷阱 |
| 复用价值 | 实质性判定逻辑可直接复用于保险欺诈调查、供应商准入审查 |

## 接口 Schema

完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。

## 回归评估集

- 声明覆盖目标：**25** 组
- 当前已实现：**3** 组

> 初赛阶段为种子集，差额 22 组在复赛补齐。我们如实记录覆盖度而非声称已完备——见 `eval/manifest.json`。

评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，
异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。

## 与多 Agent 协同流程的关系

本 Skill 由 `due-diligence` 调用。
调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，
越权调用在运行时被拒绝（见 `poc/test_safety.py`）。
