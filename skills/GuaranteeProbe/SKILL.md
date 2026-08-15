# Skill · GuaranteeProbe

> 由 `tools/gen_skill_docs.py` 从 `poc/creditsentry/skills.py` 的 `@skill` 元数据生成。
> 真源是实现本身，因此本文档不会与代码漂移。

| 字段 | 内容 |
|---|---|
| Skill 名称 | `GuaranteeProbe` |
| Skill 类型 | 自定义 Skill（L3 · 取证类） |
| 版本 | `1.0.0` |
| 使用场景 | 对外担保台账取证：识别已出险被担保方，测算净代偿敞口与缓释覆盖率 |
| 输入参数 | `subject_id, direct_exposure（用于计算代偿敞口相对倍数）` |
| 输出结果 | `GuaranteeFacts{担保余额, 已出险被担保方[], 缓释措施[], 覆盖率, 净未覆盖敞口} + evidence_ids[]` |
| 调用条件 | 取证任务含 guarantee_contagion 类型，或敞口测绘发现已出险关联主体 |
| 依赖工具 / 系统 | credit-core-mcp（只读） |
| 失败处理 | 台账为空 → 登记为有效负向证据而非取证失败；被担保方状态无公开定论 → 标注 ambiguous 降为弱证据，不自动认定出险 |
| 权限与安全 | 只读；担保关系与金额属敏感数据，出站脱敏 |
| 可调用的 Agent | `due-diligence` |
| **绑定监管条款** | 《商业银行大额风险暴露管理办法》关联客户与担保链认定；《商业银行贷后管理指引》或有负债监测 |
| **回归评估集** | 10 组担保结构样例，含 3 组「缓释措施已全额覆盖，不构成风险」的误报陷阱 |
| 复用价值 | 或有负债 → 净敞口的测算范式可复用于供应链金融确权、融资担保机构代偿测算 |

## 接口 Schema

完整定义见同目录 `schema.json`，运行时对入参与返回值做强校验。

## 回归评估集

- 声明覆盖目标：**10** 组
- 当前已实现：**7** 组

> 初赛阶段为种子集，差额 3 组在复赛补齐。我们如实记录覆盖度而非声称已完备——见 `eval/manifest.json`。

评估集是 Skill 的**发布门禁**：`eval/` 全绿才允许经 Nacos 灰度发布，
异常可一键回滚至上一版本。监管口径变化时升级 Skill 版本，不改 Agent 代码。

## 与多 Agent 协同流程的关系

本 Skill 由 `due-diligence` 调用。
调用权限由 `poc/creditsentry/permissions.py` 的权限矩阵授予，
越权调用在运行时被拒绝（见 `poc/test_safety.py`）。
