# Skill · ReportCompose

> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。
> 真源是实现本身，因此本文档不会与代码漂移。

| 字段 | 内容 |
|---|---|
| Skill 名称 | `ReportCompose` |
| Skill 类型 | 自定义 Skill（L2 · 执行类） |
| 版本 | `1.7.0` |
| 使用场景 | 生成贷后检查报告、风险处置意见书与审计报告，并注入证据引用 |
| 输入参数 | `template_id, case_state, evidence_ids[], adjudication, execution_result` |
| 输出结果 | `结构化报告（Markdown）+ archive_uri；正文每处结论自动注入证据引用角标` |
| 调用条件 | 处置方案生成后、审计完成后 |
| 依赖工具 / 系统 | 自研：报告模板、口径统一、证据引用注入。复用官方：产物归档到对象存储 |
| 复用官方云能力 | 对象存储 OSS 归档（可替换 MinIO） |
| 失败处理 | 引用的 evidence_id 不存在 → 拒绝生成并告警（防止报告出现无源结论）；归档失败 → 本地留存 + 重试 |
| 权限与安全 | 报告含敏感信息，归档加密；仅授权角色可下载 |
| 可调用的 Agent | `risk-commander`、`compliance-auditor` |
| **绑定监管条款** | 《商业银行贷后管理指引》贷后检查报告要求 |
| **回归评估集** | 8 组，校验模板完整性与证据引用完备率 100% |
| 复用价值 | 「模板 + 证据注入」成文引擎解决口径不一与经验难复用，可复用于保险结论书、事故复盘报告 |

## 接口 Schema

完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。

> **设计要点**：正文中出现的每个 EV- 编号都会被校验存在于账本，否则拒绝生成

## 回归评估集

- 声明覆盖目标：**8** 组
- 当前已实现：**0** 组

> 初赛阶段为种子集，差额 8 组在复赛补齐。我们如实记录覆盖度而非声称已完备——见 `eval/manifest.json`。

评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，
异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。

## 与多 Agent 协同流程的关系

本 Skill 由 `risk-commander`、`compliance-auditor` 调用。
调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，
越权调用在运行时被拒绝（见 `poc/test_safety.py`）。
