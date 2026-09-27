# theory.leases contract（契约 / 接口冻结声明）

> theory 圈文档：纯 Markdown，不 import 任何圈的代码（见 `theory/README.md` 边界声明）。
> 契约语义依蓝图 v2.0 §5.2：**契约 = `(ref, semver) → sha256` 注册表，冻结接口不冻结实现**。

## §0 元数据

| 项 | 值 |
| --- | --- |
| ref | `theory.jiuwen_glue.leases` |
| semver | **0.1.0-draft**（owner Approval 定稿后转 0.1.0，进入契约注册表） |
| 状态 | draft【待 owner Approval——蓝图 v2.0 §7：唯一人工写入点 = theory.Approval】 |
| 冻结对象 | `src/jiuwen_glue/leases.py` 的公开 API 面 + 三条写死不变式 |
| 不冻结 | 实现内部（进程内 dict 台账可换 Postgres，私有方法、审计事件 detail 字段） |
| 实现基线 | main=7895c23，blob `24bf466e7f74deb24053e1fd62657487e12d7d64`（leases.py，2026-09-27 登记；该文件另有并行 W-02 修订待落，定稿时以定稿 commit 重算）【待：契约注册表建成后迁入，蓝图 §5.2】 |

## §1 冻结的接口面（BudgetLedger）

以下签名即冻结契约；参数名、语义与抛错行为均属接口面：

```python
BudgetLedger.grant(task_ref: str, amount: int, *,
                   parent_lease_id=None, ttl_seconds=None,
                   tenant_id="t0", agent_ref=None,
                   effective_perms: Optional[EffectivePerms] = None) -> BudgetLease
# 发放/派生。parent_lease_id 给出时为"随子任务派生"：额度从父剩余划出，
# 且划扣发生在全部校验通过之后（拒绝路径零副作用）。
# 失败：LeaseDerivationError（amount 非法 / 父非 ACTIVE / 超父剩余 / 权限越界），
# 同时 GRANT_REJECTED 留痕。

BudgetLedger.acquire(lease_id: str, cost: int, *, purpose: str = "") -> int
# 占用：返回扣减后剩余。租约必须 ACTIVE 且未过期；耗尽转 EXHAUSTED。
# 失败：BudgetExceededError（超额/负数）、LeaseExpiredError、LeaseRevokedError、
# LeaseExhaustedError；违规占用一律 ACQUIRE_REJECTED 留痕。

BudgetLedger.expire(lease_id: str) -> BudgetLease
# 显式过期（幂等：已终态保持原状态）。任何触碰路径上的惰性过期行为不变。

BudgetLedger.revoke(lease_id: str, *, reason: str = "") -> List[str]
# 撤销并级联：返回被撤销的 lease_id 列表。冻结不变式见 §2-I2。

BudgetLedger.status_of(lease_id: str) -> str
# 查询状态（含惰性过期判定）。get/children_of 同属查询面。
```

数据对象（冻结字段）：

- `BudgetLease`：`lease_id, task_ref, amount, remaining, status, parent_lease_id, granted_at, expires_at, revoked_at, revoke_reason, tenant_id, agent_ref, effective_perms, perms_frozen, perms_provenance`
- `LeaseEvent`：`lease_id, event, occurred_at, detail, tenant_id`；`event ∈ {GRANT, ACQUIRE, EXPIRE, REVOKE, ACQUIRE_REJECTED, GRANT_REJECTED}`
- 状态机：`ACTIVE → {EXHAUSTED, EXPIRED, REVOKED}`（均为终态）；额度为抽象整数单位，本层不做换算。

## §2 写死不变式（任何实现不得违背）

- **I1 派生划扣后置**：派生的额度划扣必须发生在权限收敛校验全部通过之后——拒绝路径零副作用（父剩余不因被拒派生而减少）。
- **I2 级联独立于本节点状态**：`revoke` 的级联遍历不因本节点已终态（EXPIRED/EXHAUSTED/REVOKED）而中断——已死父租约下的 ACTIVE 后代必须被撤销；终态节点不重复落 REVOKED 但继续向下遍历；本租约惰性过期在撤销路径上同步生效。（蓝图 v2.0 §3.3，W-01 缺陷 #1 修复）
- **I3 逐级收敛不变式**：子租约权限快照 ⊆ 父租约快照；越界派生拒绝并留痕；空集快照也是快照（父快照求值为空时，携带非空快照的派生一律拒绝）；`perms_frozen=False`（父从未传入 effective_perms）时不做收敛校验——如实边界，子快照成为其后代的收敛基线。（Handbook 原则二"权限只能逐级收敛"；W-02 复核）
- **I4 检测=拒绝+留痕**：所有违规（超额占用、越界派生、非 ACTIVE 派生）必须被拒绝且进 audit 日志。
- **I5 凭证≠租约**：本对象只管额度生命周期，不签发凭证；凭证是租约的兑现物（蓝图 §2），兑现路径在 Credential Broker/OpenBao。

## §3 兼容性承诺

- 0.x 期间接口可变，但任何变更必须：改本文件 semver + 走变更流程（蓝图 §11）+ spec.md/eval.md 同票评审（蓝图 §0 原则 11）。
- 1.0.0 起仅增不破：新增带默认值参数、新增 LeaseEvent 枚举值允许；删除/改名/改状态机语义为 breaking，禁止。

## §4 一致性验收归属

接口面与不变式的语义一致性对照（oracle）见同目录 `eval.md`；治理语义定义见 `spec.md`。
