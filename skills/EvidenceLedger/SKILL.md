# Skill · EvidenceLedger

> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。
> 真源是实现本身，因此本文档不会与代码漂移。

| 字段 | 内容 |
|---|---|
| Skill 名称 | `EvidenceLedger` |
| Skill 类型 | 自定义 Skill（L2 · 取证类） |
| 版本 | `2.0.0` |
| 使用场景 | 登记证据、哈希存证并评定证据等级 |
| 输入参数 | `source_system, raw_content, extracted_fields, collected_at, subject_id` |
| 输出结果 | `Evidence{evidence_id, snapshot_uri, content_hash, level, level_reason}` |
| 调用条件 | 任何外部原文取回时强制调用——不经账本的数据不得进入决策 |
| 依赖工具 / 系统 | 自研：证据等级评分、哈希存证语义、引用约束校验。复用官方：对象存储（快照）+ 数据库写入 |
| 复用官方云能力 | 对象存储 OSS（可替换 MinIO）+ 数据库写入（可替换 PostgreSQL） |
| 失败处理 | 存储不可用 → 本地暂存队列 + 后台补偿；哈希冲突 → 拒绝写入并告警 |
| 权限与安全 | 仅 due-diligence 可写；账本 append-only 不可篡改；快照加密存储 |
| 可调用的 Agent | `due-diligence` |
| **绑定监管条款** | 《商业银行内部控制指引》记录留存与可追溯要求 |
| **回归评估集** | 定级规则全覆盖 + 幂等写入 + 哈希校验，共 14 组 |
| 复用价值 | 本项目最通用的开源组件——任何需要「结论可举证」的 Agent 系统均可直接接入 |

## 接口 Schema

完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。

> **设计要点**：content_hash 由原文计算，账本 append-only，改写会产生不同 evidence_id

## 回归评估集

- 声明覆盖目标：**14** 组
- 当前已实现：**4** 组

> 初赛阶段为种子集，差额 10 组在复赛补齐。我们如实记录覆盖度而非声称已完备——见 `eval/manifest.json`。

评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，
异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。

## 与多 Agent 协同流程的关系

本 Skill 由 `due-diligence` 调用。
调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，
越权调用在运行时被拒绝（见 `poc/test_safety.py`）。
