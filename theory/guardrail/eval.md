# theory.guardrail eval（语义一致性对照）

> theory 圈文档：纯 Markdown，不 import 任何圈的代码（见 `theory/README.md`）。
> 起草：grok-4.7-build 单轮生成草稿（2026-09-27），主代理逐条修订（G-07 判定纠正、证据引用补全路径）。
> 行号基线：main=7895c23 工作树；行号位移后以函数名 + git blame 复核。

## §0 元数据

| 项 | 值 |
| --- | --- |
| 文档 | theory.guardrail eval（语义一致性对照） |
| ref / semver | `theory.jiuwen_glue.guardrail.eval` / 0.1.0-draft |
| 状态 | draft |
| 对照对象 | glue `Guardrail` 聚合层 ↔ 外部权威 |

**Oracle 来源三级清单**

1. **蓝图裁决**：蓝图 v2.0 §0 原则 3、§1.2、§3.2、§5.3。
2. **厂商手册**：`build/refs/handbook-3.3-extract.md` §3.3.1（56–64 行）、§3.3.2（66–83 行）。
3. **openJiuwen 源码**：`repos/openjiuwen/agent-core/openjiuwen/core/security/guardrail/{guardrail,enums,models}.py`；对照实现 `src/jiuwen_glue/guardrail.py`。

## §1 对照总表

| 编号 | 治理语义点 | 本体系立场（glue 实现） | 外部权威立场 | 一致性判定 | 证据引用 |
| --- | --- | --- | --- | --- | --- |
| G-01 | 五步链路完整性 | 一次 RUN：create（deepcopy 固化）→submit_check→finalize（seal_ref 封印）→gate 终检放行；void 使结论失效；非 FINALIZED 不放行 | 手册五步：创建/固化→提交→验收固化→执行前查询终检；信息不足或现场已变则拒绝自动执行，关键状态变化后原结论失效并重查 | 一致 | glue `68-72,210-217,274-285,289-296,300-323`；手册 `handbook-3.3-extract.md:72-78` |
| G-02 | 聚合 fail-closed：空集→UNKNOWN | `aggregate` 空集返回 UNKNOWN（W-01 缺陷 #2 修复），空集永不 PASS | 蓝图 §3.2：空集、检查器缺失、凭证缺失→UNKNOWN（永不 PASS）；手册：信息不足拒绝自动执行 | 一致 | glue `75-91`；蓝图 §3.2；手册 `69` |
| G-03 | 聚合优先级 BLOCKED > UNKNOWN > PASS | 任一 BLOCKED 压过其余；存在 UNKNOWN 且无 BLOCKED 则 UNKNOWN；仅全部 PASS 才 PASS | 蓝图 §3.2 同序 | 一致 | glue `75-91`；蓝图 §3.2 |
| G-04 | 决策点唯一 | glue 只聚合已提交三态结论，不执行检查、不改写业务动作 | 蓝图 §0 原则 3：决策点唯一；openJiuwen `BaseGuardrail` 是检查执行本体（`detect` 出结果） | 有意偏离（职责切分，见 §2-D1） | glue `24-29,186-192`；蓝图 §0 原则 3；`guardrail.py:42,195-260`（openjiwen） |
| G-05 | void 失效语义 | `void` 幂等；VOID 后 gate 恒 UNKNOWN，须重建 run 重查；spec 创建时 deepcopy 冻结 | 手册：「任何关键状态发生变化，原有结论都应失效并重新检查」 | 一致 | glue `210-217,289-296`；手册 `70` |
| G-06 | UNKNOWN 必须拒绝执行 | `executable` 仅当聚合为 PASS 时为真；UNKNOWN 与 BLOCKED 均不可执行 | 手册：信息不足拒绝自动执行；蓝图 §3.2：UNKNOWN 不是放行 | 一致 | glue `165-168,300-323`；手册 `69`；蓝图 §3.2 |
| G-07 | ASK 第三态与 Challenge | ASK 仅 `permission_rail` Check 可提交，经 ChallengeBoard 生成结构化 Challenge（who_confirms/resource/action/method/ttl），门控侧落 UNKNOWN；批准后 PASS+evidence_ref 重提 | 手册 §3.3.1：允许/拒绝/需要补充授权；Challenge=结构化授权要求（谁确认/什么方式/有效期），模型只收高层状态 | 一致（结构化对象已具备；批准的独立界面/通知渠道在 glue 外【待】） | glue `248-264`；`challenge.py:45-51,132-154`；手册 `56-64` |
| G-08 | 检查器缺失：两层互补 | 聚合层无已提交检查→UNKNOWN，不抛错、不进 PASS；必填缺失在 gate 直判 UNKNOWN 不进聚合（320） | `detect` 无后端 `raise ValueError`（221-224）：检查执行层 fail-fast——执行层快速失败，聚合层 fail-closed 降级，两层互补不冲突 | 一致 | glue `75-91,300-323`；`guardrail.py:221-224`（openjiwen） |
| G-09 | 三态与五级风险等级 | glue Check 只收 PASS/UNKNOWN/BLOCKED；无 RiskLevel→三态固定映射表，折算发生在各检查执行点，提交时已是三态 | openJiuwen `RiskLevel` 五级（SAFE/LOW/MEDIUM/HIGH/CRITICAL）；`GuardrailResult` 以 `is_safe`/`pass_`/`block` 表达 | 有意偏离（层边界，见 §2-D2） | glue `60-66`；`enums.py:13-27`、`models.py:21-57`（openjiwen） |

## §2 有意偏离清单

1. **D1：薄层聚合 vs 检查执行（G-04）。** openJiuwen `BaseGuardrail.detect` 是检查执行本体；glue 不内嵌该执行，只消费三态并 fail-closed 聚合。决策点仍唯一：能否执行只由聚合结果与 `executable`/`gate` 决定。偏离的是职责切分，不是第二套放行逻辑——蓝图 §1.2：降级不删除，执行能力留在原生检查器，glue 只在缺失时拒绝放行。
2. **D2：三态 vs 五级、无固定映射表（G-09）。** 原生五级风险由各 Check 执行点自行折成三态后提交。glue 不维护第二张等级表，避免聚合层改写检查结论。这是边界而非漏实现：等级语义留在执行点，聚合层只保证优先级与 fail-closed。
3. **D3（如实备注）：G-07 的残留缺口。** 结构化 Challenge 对象与审批队列已具备（`challenge.py` pending_for/decide），但"独立可信交互界面"本身与"模型只收高层状态"的通道在 glue 层外，接线情况未验收【待】。

## §3 Oracle 常驻与复检方式

蓝图 §5.3：Oracle 生命周期为 **提议 → 校准 → 影子 → 生效 → 退役监控**；来源由强到弱五级：机器可验证 → 差分共识 → 外部权威 → 结局回填 → 人类金标。

- **常驻**：本表为 draft oracle。G-01–G-03、G-05、G-06、G-08 以源码与手册/蓝图引文作机器可验证或外部权威级；G-04、G-09 以设计裁决（蓝图）作权威级"有意偏离"；G-07 的残留缺口停在提议级【待】。
- **复检**：源码行号或手册/蓝图条文变更时重跑本表；影子期只记录判定差，不改 glue 放行语义。退役监控：已封印结论若被 `void` 或现场状态变化，原判定退出有效集并重新取证（手册 70 行；glue `289-296`）。
