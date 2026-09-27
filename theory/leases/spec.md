# theory.leases spec（治理语义）

> theory 圈文档：纯 Markdown，不 import 任何圈的代码（见 `theory/README.md`）。
> 起草：grok-4.7-build 单轮生成草稿（2026-09-27），主代理逐条修订并复核全部行号。
> 行号基线：main=7895c23 工作树（含并行 W-01/W-02 修订）；行号位移后以函数名 + git blame 复核。

## §0 元数据

| 项 | 值 |
| --- | --- |
| 对象名 | leases |
| 圈别 | theory |
| 版本 | 0.1.0-draft |
| 状态 | draft，待 owner Approval（蓝图 v2.0 §7：唯一人工写入点 = theory.Approval） |
| 来源清单 | 蓝图 v2.0 §2 租约术语、§3.3 级联语义（缺陷修复）、§4.4 网关断流、§7 人工写入点；蓝图 v1.7 §12.5；AI Agent Handbook 第 30 章（`repos/ai-agent-handbook/07-conclusion/第 30 章 从 Agentic Application 到 Agentic OS.md`：L110、L120、L241）及其原则二；参考实现 `src/jiuwen_glue/leases.py` |

## §1 治理对象定义

租约（Lease）是由资源、操作、期限、撤销方式构成的授权对象，可派生，可级联撤销。（蓝图 v2.0 §2）

凭证 ≠ 租约。凭证是租约的兑现物：租约规定可动用的资源与操作、有效期限和撤销方式；凭证是该授权在运行时被兑付的载体。（蓝图 v2.0 §2）

预算租约（Budget Lease）是有上限、有期限、可收回的资源与调用额度；生命周期随 Run 派生，可级联撤销；既有近似物是配额与 cgroup 限额；缺失后果是预算失控，且子任务无法被约束。（Handbook 第 30 章表 30-2，`第 30 章….md:110`）

租约是可控自治最关键的对象：它把配额（第 9 章）与最小权限（第 14 章）合并为同一次授权，同时限定可用资源、可执行操作、有效期限与撤销方式，并可随子任务派生。缺少这个对象时，「终止任务」与「收回该任务的全部权限」只能靠多处配置协同保证。（Handbook 第 30 章 `:120`；`src/jiuwen_glue/leases.py:4-6`）

L3 有界自主 = 预算租约 + 工作目录限定 + 完整调用轨迹与预算消耗。（Handbook 第 30 章 `:241`）

## §2 治理语义

### 2.1 状态机与额度单位

合法状态为 ACTIVE、EXHAUSTED、EXPIRED、REVOKED；后三者为终态。（`src/jiuwen_glue/leases.py:47-51`）

额度为抽象整数单位。治理层不做单位换算，换算由调用方约定。（`src/jiuwen_glue/leases.py:29`）

### 2.2 发放与派生

- grant 发放一笔 ACTIVE 租约，可指定期限（ttl → `expires_at`）。（`src/jiuwen_glue/leases.py:8-10,121-217`）
- 派生 = 额度从父租约剩余中划出，父 `remaining` 相应扣减；划出部分已承诺给子租约。（`src/jiuwen_glue/leases.py:9-10,191-196`）
- 父租约须为 ACTIVE，派生额不得超过父剩余；否则拒绝并记 GRANT_REJECTED。（`src/jiuwen_glue/leases.py:150-162`）
- 划扣发生在权限收敛校验之后，拒绝路径对父剩余零副作用。（`src/jiuwen_glue/leases.py:163-165,191-193`）
- `amount` 须为非负整数，否则 GRANT_REJECTED。（`src/jiuwen_glue/leases.py:145-147`）

### 2.3 逐级收敛不变式（含空集快照）

- 子租约权限快照 ⊆ 父租约快照；越界派生拒绝，并记 GRANT_REJECTED。（Handbook 原则二「权限只能逐级收敛」，`01-architecture` 手册册系 §3.3.1 五原则；蓝图 v1.7 §12.5；`src/jiuwen_glue/leases.py:22-23,178-186`）
- 派生未显式给出快照时继承父快照，包含关系由构造保证。（`src/jiuwen_glue/leases.py:187-189`）
- 空集快照也是快照：父快照求值为空（什么都不允许）时，携带非空快照的派生一律拒绝（W-02 复核裁定）。（`src/jiuwen_glue/leases.py:24-25,176-186`）
- `perms_frozen` 区分「求值为空」与「从未求值」。父从未传入 `effective_perms`（`perms_frozen=False`）时无收敛基准，子可声明自己的快照并成为后代收敛基线；该情形不做收敛校验（如实边界）。（`src/jiuwen_glue/leases.py:25-27,85-87,169`）

### 2.4 占用 acquire

- 占用要求租约为 ACTIVE 且未过期，按 cost 扣减 `remaining`。（`src/jiuwen_glue/leases.py:11-12,241-268`）
- 超额拒绝，租约保持 ACTIVE，额度不动，记 ACQUIRE_REJECTED。（`src/jiuwen_glue/leases.py:255-260`）
- 剩余降为 0 时转为 EXHAUSTED。（`src/jiuwen_glue/leases.py:262-265`）
- 对 EXPIRED、REVOKED、EXHAUSTED 的占用一律拒绝并留痕。（`src/jiuwen_glue/leases.py:246-254`）

### 2.5 惰性过期与显式过期

- 到达 `expires_at` 后，在触碰时惰性转为 EXPIRED，并记 EXPIRE。（`src/jiuwen_glue/leases.py:13,221-226`）
- 显式 expire 幂等：仅 ACTIVE 落入 EXPIRED；已终态保持原状态。（`src/jiuwen_glue/leases.py:228-237`）
- 过期只作用于本租约。子租约额度在派生时已从父划出、已承诺，不随父过期回收。（`src/jiuwen_glue/leases.py:13-14`）

### 2.6 级联撤销

- 撤销父租约时全部后代一并 REVOKED；未花完的额度作废；此后占用被拒。（蓝图 v2.0 §2；`src/jiuwen_glue/leases.py:15-16,272-298`）
- 级联遍历独立于本节点状态：父节点终态不阻止其后代被撤销。EXPIRED 父 + ACTIVE 子，revoke 之后子必为 REVOKED（W-01 缺陷 #1 修复）。（蓝图 v2.0 §3.3；`src/jiuwen_glue/leases.py:16-18,275-279,295-297`）
- 终态节点不重复落 REVOKED，但必须继续遍历后代。（`src/jiuwen_glue/leases.py:295-297`）
- 撤销路径上惰性过期同步生效：父租约惰性过期时仍同步级联。（蓝图 v2.0 §3.3；`src/jiuwen_glue/leases.py:282-283`）

### 2.7 审计留痕

检测 = 拒绝 + 留痕。事件全集追加审计日志：GRANT、ACQUIRE、EXPIRE、REVOKE、ACQUIRE_REJECTED、GRANT_REJECTED。（`src/jiuwen_glue/leases.py:19,60-66,112-117`）

### 2.8 权限交集固化

`EffectivePerms` 在签发时求值，交集快照与三层身份引用固化进租约：`agent_ref`、`effective_perms`、`perms_frozen`、`perms_provenance`——权限交集公式在租约签发路径上被求值，不是口头原则。（蓝图 v1.7 §12.5；`src/jiuwen_glue/leases.py:20-23,82-87,170-175`）

### 2.9 租约与网关断流绑定

租约与 Higress consumer key 绑定：租约过期即网关断流，软提醒不算数。（蓝图 v2.0 §4.4）【待：glue 层未接线，见 §5】

## §3 与参考实现的关系

`src/jiuwen_glue/leases.py` 的 `BudgetLedger` 是本语义的进程内参考实现。grant、acquire、惰性/显式过期、级联 revoke 与审计留痕与 §2 对齐；`BudgetLease` 携带 `agent_ref`、`effective_perms`、`perms_frozen`、`perms_provenance`。（`src/jiuwen_glue/leases.py:69-87,93-94,121-298`）

持久化形态为 `glue.budget_lease`（DDL 见 `sql/`），不改变本节语义。（`src/jiuwen_glue/leases.py:94`）

理论版本 bump 之后，实例注册表为每个实例生成升级建议单。理论圈唯一人工写入点是 `theory.Approval`；本文件在 owner Approval 之前保持 draft。（蓝图 v2.0 §7；骨架见 `src/jiuwen_glue/meta_governance.py` instance_registrar）

## §4 边界声明

本对象**管**额度生命周期与级联撤销语义（发放/派生/占用/过期/撤销/审计）。（`src/jiuwen_glue/leases.py:7-27`）

本对象**不管**额度单位换算——换算由调用方约定。（`src/jiuwen_glue/leases.py:29`）

本对象**不管**凭证签发。凭证签发在 Credential Broker / OpenBao；凭证是租约的兑现物。（蓝图 v2.0 §2）

## §5 开放问题

1. 【待】与 Higress consumer key 的绑定在 glue 层尚未实现（§2.9）；计量四维度接线属后续工单。（对照：蓝图 §4.4 已规定过期即断流）
2. 【待】过期子租约的额度是否回收给父。当前裁定为不回收（派生时已划出）。需要 owner 复核。（`src/jiuwen_glue/leases.py:13-14`）
3. 【待】perms_frozen=False（父从未传入快照）时子可自声明快照成为收敛基线——该如实边界是否要求父租约签发强制传快照，待 owner 裁定。（`src/jiuwen_glue/leases.py:25-27`）
