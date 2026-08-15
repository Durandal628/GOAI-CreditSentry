# Skill · QueryRewrite

> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。
> 真源是实现本身，因此本文档不会与代码漂移。

| 字段 | 内容 |
|---|---|
| Skill 名称 | `QueryRewrite` |
| Skill 类型 | 自定义 Skill（L3 · 知识类） |
| 版本 | `1.0.0` |
| 使用场景 | 把一个基础检索意图改写为六维子查询，并识别需要澄清的歧义 |
| 输入参数 | `base_query, stance（PROVE/REFUTE）, signal_types[], facts, as_of` |
| 输出结果 | `QueryPlan{subqueries[]{dimension, text, filters, why, weight}, clarifications[]}` |
| 调用条件 | 任何需要检索政策或历史案例的环节，检索前必调 |
| 依赖工具 / 系统 | 自研术语表与信号主题映射，无外部依赖（纯函数） |
| 失败处理 | 未知立场 → 直接抛错不猜；术语表未覆盖 → 降级为仅立场维与信号主题维，并在 Span 中标注 dimensions_used 以便事后补词表 |
| 权限与安全 | 只读纯函数；不接触任何业务数据 |
| 可调用的 Agent | `risk-analyst`、`devils-advocate`、`compliance-auditor` |
| **绑定监管条款** | 《商业银行贷后管理指引》条款适用性判断——条款须在案件时点已生效 |
| **回归评估集** | 18 组改写样例，覆盖六个维度各自的触发与**不触发**条件（如求证方不得产生否定式子查询），以及澄清三渠道的选路正确性 |
| 复用价值 | 立场维 + 否定式维的组合是对抗式检索的通用范式，可复用于合同审查、尽调抗辩、审计发现的反向验证 |

## 接口 Schema

完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。

## 回归评估集

- 声明覆盖目标：**18** 组
- 当前已实现：**14** 组

> 初赛阶段为种子集，差额 4 组在复赛补齐。我们如实记录覆盖度而非声称已完备——见 `eval/manifest.json`。

评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，
异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。

## 与多 Agent 协同流程的关系

本 Skill 由 `risk-analyst`、`devils-advocate`、`compliance-auditor` 调用。
调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，
越权调用在运行时被拒绝（见 `poc/test_safety.py`）。
