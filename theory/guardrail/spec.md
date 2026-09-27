# theory.guardrail spec（治理语义）

> theory 圈文档：纯 Markdown，不 import 任何圈的代码（见 `theory/README.md`）。
> 起草：grok-4.7-build 单轮生成草稿（2026-09-27），主代理逐条修订并复核全部行号。
> 行号基线：main=7895c23 工作树（2026-09-27）；行号位移后以函数名 + git blame 复核。

## §0 元数据

| 项 | 值 |
| --- | --- |
| 对象名 | guardrail |
| 圈别 | theory |
| 版本 | 0.1.0-draft |
| 状态 | draft，待 owner Approval（蓝图 v2.0 §7：唯一人工写入点 = theory.Approval） |
| 来源 | 蓝图 v2.0 §0 原则 3、§1.2、§2、§3.2、§7；Handbook §3.3.1、§3.3.2（`build/refs/handbook-3.3-extract.md`）；glue `src/jiuwen_glue/guardrail.py`；openJiuwen `core/security/guardrail/`（`repos/openjiuwen/agent-core/openjiuwen/`） |

本文件只规定治理语义；实现细节以 §3 所指参考实现对照，不在此重述代码。

## §1 治理对象定义

**门控（Gate）** 是 `GuardrailRun` 的一次运行：绑定 Spec 版本、ChangeSet 摘要、递增的 Submission，并给出一次聚合结论。门控不与审批、评审混用。（蓝图 v2.0 §2）

**校验（Check）** 是门控内的单项检查，结论三态为 `PASS` / `UNKNOWN` / `BLOCKED`。LLM 意见不是 Check，只作为 Submission 的证据附件。（蓝图 v2.0 §2）

**运行（Run）** 的生命周期状态为 `OPEN` → `FINALIZED`，或进入 `VOID`。`VOID` 为终态。（`src/jiuwen_glue/guardrail.py:68-72`）

**规格（GuardrailSpec）** 必填维度为 `action`、`resource`、`agent_identity_ref`、`checks`、`spec_version`。同一 Spec 内 `check_id` 不可重复；创建时 deepcopy 冻结，之后不随外部对象漂移。（`src/jiuwen_glue/guardrail.py:117-139,210-217`）

**结果（GuardrailResult）** 的 `executable` 仅当聚合结论为 `PASS` 时为真；`UNKNOWN` 与 `BLOCKED` 均不可执行。（`src/jiuwen_glue/guardrail.py:165-168`）

**原生检查器** openJiuwen `BaseGuardrail` 是检查执行本体，事件驱动 `detect`；无后端时抛出 `ValueError`。风险分级为 `SAFE`/`LOW`/`MEDIUM`/`HIGH`/`CRITICAL` 五级；原生 `GuardrailResult` 表达 `is_safe` 与 pass/block，不替代本对象的聚合门控结论。（`repos/openjiuwen/agent-core/openjiuwen/core/security/guardrail/guardrail.py:42,195-260`，ValueError 见 221-224；`enums.py:13-27`；`models.py:21-57`）

## §2 治理语义逐条

### 2.1 五步链路

1. 发布系统创建或复用运行，并提交动作上下文。
2. Guardrail 固化规则与上下文。
3. Agent 查询事实，提交判断与 Evidence（原始快照）。
4. Guardrail 验收协议并固化结果。
5. 发布系统在执行前主动查询门控结果，做终检后再执行。终检前，最新状态、权限、目标和动作均重置。

信息不足，或生产现场已经变化，系统拒绝自动执行。任一关键状态变化，原有结论失效并重新检查。（Handbook §3.3.2，`build/refs/handbook-3.3-extract.md:66-83`；五步与实现的映射见 `src/jiuwen_glue/guardrail.py:1-33`——create_run:210 / submit_check:221 / finalize:274 / gate:300）

### 2.2 聚合 fail-closed

`aggregate` 按下述顺序判定，空集与缺失永不升为 `PASS`：

- 检查集为空、检查器缺失或凭证缺失 → `UNKNOWN`（永不 PASS——W-01 缺陷 #2 修复）。
- 任一 Check 为 `BLOCKED` → `BLOCKED`。
- 否则任一 Check 为 `UNKNOWN` → `UNKNOWN`。
- 否则 → `PASS`。

（蓝图 v2.0 §3.2；`src/jiuwen_glue/guardrail.py:75-91`）

### 2.3 必填缺失直判 UNKNOWN

`gate` 只聚合 `FINALIZED` 且必填 Check 齐全的运行。非 `FINALIZED` 直接 `UNKNOWN`。`FINALIZED` 但必填缺失时，直接判 `UNKNOWN`，**不进入** `aggregate`：此时即使已有 `BLOCKED` 提交，对外结论仍是 `UNKNOWN`（`src/jiuwen_glue/guardrail.py:320`）。`UNKNOWN` 与 `BLOCKED` 都拒绝执行，治理效果一致；逐项诊断保留在 `per_check`，不因短路而丢弃。（`src/jiuwen_glue/guardrail.py:300-323`；`executable` 165-168；蓝图 §3.2）

### 2.4 决策点唯一

一次门控只有一个对外结论，即聚合 verdict。不允许在聚合之外再设第二个放行或拦截点；本对象只存声明与聚合结果，不执行任何检查。人工写入的唯一入口是 `theory.Approval`，不从检查器或模型侧旁路写入门控结论。（蓝图 v2.0 §0 原则 3、§3.2、§7；`src/jiuwen_glue/guardrail.py:24-29,186-192`）

### 2.5 void 失效

`void` 幂等。进入 `VOID` 后，`gate` 恒为 `UNKNOWN`。失效沿运行传播时一律降级，不删除已固化的检查记录与证据（重新校验后可恢复）。（蓝图 v2.0 §1.2、§3.4；`src/jiuwen_glue/guardrail.py:289-296,300-323`）

`finalize` 要求 `seal_ref` 必填后才封存；未封存的运行不能作为可执行依据。（`src/jiuwen_glue/guardrail.py:274-285`）

### 2.6 ASK 三态与 Challenge

授权三态为：允许、拒绝、需要补充授权（Challenge）。`Challenge` 是结构化授权要求——说明需要谁确认（who_confirms）、以什么方式（method）、有效期多久；模型只收到"等待确认/审批拒绝"这类高层状态，不接触授权码与 Token。（Handbook §3.3.1，`build/refs/handbook-3.3-extract.md:56-64`；结构化对象见 `src/jiuwen_glue/challenge.py:45-51`）

`ASK` 仅允许 `permission_rail` 后端的 Check 提出。提出后经 `ChallengeBoard.open` 生成结构化 `Challenge`，该 Check 在门控侧落为 `UNKNOWN`（fail-closed），直到具备确认权的人在独立界面批准后，调用方以 `PASS` + evidence_ref 重新提交。（Handbook §3.3.1；`src/jiuwen_glue/guardrail.py:247-264`）

## §3 与参考实现的关系

glue `GuardrailRunStore`（`src/jiuwen_glue/guardrail.py`）是本 spec 的参考实现，位于 product 圈。理论层只约束语义；产品层按该模块落地运行存储、聚合与 `gate`。

理论版本 bump 不直接改写已注册实例。实例注册表为每个实例生成升级建议单，由实例侧决定是否采纳。（蓝图 v2.0 §7；骨架见 `src/jiuwen_glue/meta_governance.py` instance_registrar）

`create_run` 对应链路第 1 步的"创建"（`src/jiuwen_glue/guardrail.py:210-217`）；"复用"语义见 §5。

## §4 边界声明

本对象**管**协议聚合与唯一门控输出：Spec 冻结、Check 三态、fail-closed 聚合、`finalize`/`void`/`gate`、以及 `executable` 仅在 `PASS` 时为真。

本对象**不管**检查执行。执行留在 openJiuwen 原生 `core.security.guardrail` 及各执行点（TeamPermissionRail / eval-gate / 扫描器）。（蓝图 v2.0 §2 术语；`src/jiuwen_glue/guardrail.py:48-54,186-192`）

本对象**不管**审批 UI。执行期 `Challenge` 留在权限轨 `TeamPermissionRail` 的 ask；理论定稿审批只走 `theory.Approval`。（蓝图 v2.0 §7；`repos/openjiuwen/agent-core/openjiuwen/agent_teams/rails/team_permission_rail.py:109`）

聚合 verdict 是唯一门控输出，不新增第二个决策点。（蓝图 v2.0 §0 原则 3；`src/jiuwen_glue/guardrail.py:24-29`）

## §5 开放问题

1. 【待】蓝图 §2 将 ChangeSet 摘要与 Submission 递增列为门控组成。当前 `GuardrailSpec` 字段为 `action`/`resource`/`agent_identity_ref`/`checks`/`spec_version`（`src/jiuwen_glue/guardrail.py:117-139`），二者在 Spec 或 Run 上的体现方式尚未定稿。
2. 【待】Handbook §3.3.2 要求"创建或复用运行"。参考实现仅有 `create_run`（210-217）；跨进程按业务键查找 `OPEN` run 的复用未实现。
3. 【待】"终检前最新状态、权限、目标和动作均重置"（Handbook 步骤 5）在 glue 层仅由 `void` + 重新建 run 承担；自动检测"现场已变化"的信号源未接线。
