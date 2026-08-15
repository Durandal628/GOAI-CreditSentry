# Skill · TxnFlowAnalyze

> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。
> 真源是实现本身，因此本文档不会与代码漂移。

| 字段 | 内容 |
|---|---|
| Skill 名称 | `TxnFlowAnalyze` |
| Skill 类型 | 自定义 Skill（L3 · 取证类） |
| 版本 | `1.2.3` |
| 使用场景 | 识别交易流水异常模式：回流 / 空转 / 集中转出 / 整数化 |
| 输入参数 | `account_ids[], date_range, patterns[]` |
| 输出结果 | `FlowFacts{anomalies[]{pattern, 金额, 对手方, 置信度}} + evidence_ids[]` |
| 调用条件 | 取证任务含 transaction 类型 |
| 依赖工具 / 系统 | txn-mcp（只读 · PII） |
| 失败处理 | 数据量超阈值 → 分片处理后合并；采样不足 → 输出弱证据等级而非强断言 |
| 权限与安全 | 高敏 PII；账号与对手方出站脱敏 |
| 可调用的 Agent | `due-diligence` |
| **绑定监管条款** | 《流动资金贷款管理办法》贷款用途真实性核查 |
| **回归评估集** | 18 组合成流水，覆盖 4 类异常模式 + 6 组正常波动负样本 |
| 复用价值 | 异常模式引擎可复用于反洗钱可疑交易识别、受托支付合规核查 |

## 接口 Schema

完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。

## 回归评估集

- 声明覆盖目标：**18** 组
- 当前已实现：**0** 组

> 初赛阶段为种子集，差额 18 组在复赛补齐。我们如实记录覆盖度而非声称已完备——见 `eval/manifest.json`。

评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，
异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。

## 与多 Agent 协同流程的关系

本 Skill 由 `due-diligence` 调用。
调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，
越权调用在运行时被拒绝（见 `poc/test_safety.py`）。
