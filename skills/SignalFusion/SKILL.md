# Skill · SignalFusion

> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。
> 真源是实现本身，因此本文档不会与代码漂移。

| 字段 | 内容 |
|---|---|
| Skill 名称 | `SignalFusion` |
| Skill 类型 | 自定义 Skill（L3 · 诊断类） |
| 版本 | `1.2.0` |
| 使用场景 | 把多源零散预警信号归并为可处置的风险事件，并压降无效预警 |
| 输入参数 | `signals[]{source, subject_id, signal_type, ts, detail}, window, dedup_policy` |
| 输出结果 | `RiskEvent{event_id, subject, signal_types[], first_seen, priority, dropped[]}` |
| 调用条件 | 预警池新增信号，或定时批量归并触发 |
| 依赖工具 / 系统 | 内部预警池（只读）；无外部依赖 |
| 失败处理 | 单源不可用 → 降级为可用源归并并标注 degraded_sources；全源失败 → 抛错升级人工，不产出空事件 |
| 权限与安全 | 只读，无 PII；仅 signal-hub 可调用 |
| 可调用的 Agent | `signal-hub` |
| **绑定监管条款** | 《商业银行贷后管理指引》风险监测与预警要求 |
| **回归评估集** | 30 组信号流样例，校验降噪率与零高危漏丢（被丢弃信号必须可回溯） |
| 复用价值 | 场景无关的信号归并内核，可直接复用于保险报案归并、告警降噪、工单去重 |

## 接口 Schema

完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。

## 回归评估集

- 声明覆盖目标：**30** 组
- 当前已实现：**3** 组

> 初赛阶段为种子集，差额 27 组在复赛补齐。我们如实记录覆盖度而非声称已完备——见 `eval/manifest.json`。

评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，
异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。

## 与多 Agent 协同流程的关系

本 Skill 由 `signal-hub` 调用。
调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，
越权调用在运行时被拒绝（见 `poc/test_safety.py`）。
