# Spec: jiuwen_glue.leases（预算租约语义 + 逐条可判定 rubric）
- ref: jiuwen_glue.leases
- semver: 0.3.0
- 实现文件: src/jiuwen_glue/leases.py
- 来源: 建设方案 v2.0 §5.2（资产层：spec = 逐条可判定 rubric，REQ-xxx 带正反例）
- 基线: main（含 W-01 缺陷#1 修复：级联独立于本节点状态）；不含 W-02 在建语义（见文末非目标）

## 机器可读例格式约定

- 每条例行必须是单行，且匹配：`- 例: <场景> -> <期望>`。场景与期望均非空，两侧内部不得再出现 ` -> `。
- 正例的期望是实现必须达到的终态。反例的期望是实现不得出现的终态。终态只使用本条写出的状态值、数值、异常类型、返回列表归属、审计原因码。
- 状态值只使用大写的 `ACTIVE`、`REVOKED`、`EXPIRED`、`EXHAUSTED`。
- 判定深度：首跑只做结构校验。结构校验核对每条 REQ 的「规则」「依据」「正例」「反例」「判定」五字段齐全，正例与反例各至少一条，且每条例行匹配上一文法。首跑不执行 `src/jiuwen_glue/leases.py`，不把例行期望记为已运行通过的测试结论。行为对拍留待首跑之后的运行时轮次，对拍时正例必须达到、反例不得出现。

## REQ 目录（逐条可判定）

### REQ-L-01 EXPIRED 父节点上 revoke 仍把 ACTIVE 子节点变为 REVOKED
- 规则: 父租约状态为 EXPIRED、其直接子租约状态为 ACTIVE 时，对父租约调用 `revoke` 后，该直接子租约状态必须为 REVOKED，父租约状态必须仍为 EXPIRED。级联沿父子关系发生，不依赖父租约处于 ACTIVE。
- 依据: leases.py:272-298；建设方案 v2.0 §3.3；W-01 缺陷#1
- 正例:
  - 例: 父状态为 EXPIRED 且直接子状态为 ACTIVE 时 revoke(父) -> 直接子状态为 REVOKED 且父状态仍为 EXPIRED
- 反例:
  - 例: 父状态为 EXPIRED 且直接子状态为 ACTIVE 时 revoke(父) -> 直接子状态为 ACTIVE
- 判定: 结构校验（CI 环节9）+ tests/test_lease.py 对应断言；行为级对拍待运行时轮次（运行时对拍时正例必须达到、反例不得出现）。

### REQ-L-02 终态节点再次 revoke 不改本节点、不计入返回列表，并继续遍历后代
- 规则: 节点状态已是 REVOKED、EXPIRED 或 EXHAUSTED 时，对其调用 `revoke`：该节点状态必须与调用前全等；该节点不得出现在本次 `revoke` 的返回列表中；遍历不得停在该节点。若其直接子节点在调用前状态为 ACTIVE，则该直接子节点状态必须变为 REVOKED。
- 依据: leases.py:287-297
- 正例:
  - 例: 节点状态为 REVOKED 时再次 revoke -> 该节点状态仍为 REVOKED 且该节点不在本次返回列表中
  - 例: 节点状态为 EXPIRED 时 revoke -> 该节点状态仍为 EXPIRED 且该节点不在本次返回列表中
  - 例: 节点状态为 EXHAUSTED 时 revoke -> 该节点状态仍为 EXHAUSTED 且该节点不在本次返回列表中
  - 例: 节点状态为 REVOKED 且其直接子状态为 ACTIVE 时 revoke -> 该节点不在本次返回列表中且该直接子状态为 REVOKED
  - 例: 节点状态为 EXPIRED 且其直接子状态为 ACTIVE 时 revoke -> 该节点不在本次返回列表中且该直接子状态为 REVOKED
  - 例: 节点状态为 EXHAUSTED 且其直接子状态为 ACTIVE 时 revoke -> 该节点不在本次返回列表中且该直接子状态为 REVOKED
- 反例:
  - 例: 节点状态为 REVOKED 时再次 revoke -> 该节点出现在本次返回列表中
  - 例: 节点状态为 EXPIRED 时 revoke -> 该节点状态为 REVOKED
  - 例: 节点状态为 EXHAUSTED 时 revoke -> 该节点状态为 REVOKED
  - 例: 节点状态为 EXPIRED 且其直接子状态为 ACTIVE 时 revoke -> 该直接子状态为 ACTIVE
- 判定: 结构校验（CI 环节9）+ tests/test_lease.py 对应断言；行为级对拍待运行时轮次（运行时对拍时正例必须达到、反例不得出现）。

### REQ-L-03 已过 expires_at 的 ACTIVE 租约在 revoke 时先落 EXPIRED 再级联
- 规则: 租约状态为 ACTIVE，且 now 大于 `expires_at` 时，对其调用 `revoke` 必须先把该租约状态写成 EXPIRED，再按终态节点的级联规则遍历后代。调用结束后该租约状态必须为 EXPIRED，该租约不得出现在本次返回列表中；其调用前状态为 ACTIVE 的直接子租约状态必须为 REVOKED。
- 依据: leases.py:282-283
- 正例:
  - 例: 状态为 ACTIVE 且 now 大于 expires_at 的租约调用 revoke -> 该租约状态为 EXPIRED 且该租约不在本次返回列表中且其调用前为 ACTIVE 的直接子状态为 REVOKED
- 反例:
  - 例: 状态为 ACTIVE 且 now 大于 expires_at 的租约调用 revoke -> 该租约状态为 REVOKED
  - 例: 状态为 ACTIVE 且 now 大于 expires_at 的租约调用 revoke -> 该租约状态仍为 ACTIVE
  - 例: 状态为 ACTIVE 且 now 大于 expires_at、其直接子状态为 ACTIVE 的租约调用 revoke -> 该直接子状态为 ACTIVE
- 判定: 结构校验（CI 环节9）+ tests/test_lease.py 对应断言；行为级对拍待运行时轮次（运行时对拍时正例必须达到、反例不得出现）。

### REQ-L-04 派生被接受时父 remaining 减少 amount
- 规则: 从父租约派生子租约且该次派生被接受时，父 `remaining` 的新值必须等于派生前父 `remaining` 减去本次 `amount`。
- 依据: leases.py:150-196；leases.py:121-217
- 正例:
  - 例: 父 remaining 为 10 且派生 amount 为 3 且派生被接受 -> 父 remaining 为 7
  - 例: 父 remaining 为 10 且派生 amount 为 10 且派生被接受 -> 父 remaining 为 0
- 反例:
  - 例: 父 remaining 为 10 且派生 amount 为 3 且派生被接受 -> 父 remaining 为 10
  - 例: 父 remaining 为 10 且派生 amount 为 3 且派生被接受 -> 父 remaining 为 3
- 判定: 结构校验（CI 环节9）+ tests/test_lease.py 对应断言；行为级对拍待运行时轮次（运行时对拍时正例必须达到、反例不得出现）。

### REQ-L-05 amount 大于父 remaining 时派生失败
- 规则: 派生的 `amount` 大于父租约的 `remaining` 时，必须抛出 LeaseDerivationError。该次调用不得产生新的子租约。父租约状态必须保持 ACTIVE。
- 依据: leases.py:157-162
- 正例:
  - 例: 父状态为 ACTIVE 且父 remaining 为 5 且派生 amount 为 6 -> LeaseDerivationError 且子租约条数不增加且父状态为 ACTIVE
- 反例:
  - 例: 父状态为 ACTIVE 且父 remaining 为 5 且派生 amount 为 6 -> 子租约条数增加
  - 例: 父状态为 ACTIVE 且父 remaining 为 5 且派生 amount 为 6 -> 不抛出 LeaseDerivationError
  - 例: 父状态为 ACTIVE 且父 remaining 为 5 且派生 amount 为 6 -> 父状态不为 ACTIVE
- 判定: 结构校验（CI 环节9）+ tests/test_lease.py 对应断言；行为级对拍待运行时轮次（运行时对拍时正例必须达到、反例不得出现）。

### REQ-L-06 非空父权限快照约束子快照
- 规则: 父租约已固化的权限快照 P 非空时，同时满足下面两条。第一，派生显式给出子快照 S，且存在元素属于 S 但不属于 P 时，必须抛出 LeaseDerivationError，并写入一条 GRANT_REJECTED，原因码为 beyond_scope。第二，派生未给出 effective_perms 时，新子租约的权限快照必须与 P 的元素集合全等。S 的每个元素都属于 P 时，显式快照本身不得触发本条的 LeaseDerivationError。
- 依据: leases.py:166-189
- 正例:
  - 例: 父快照 P 非空且子显式快照 S 的每个元素都属于 P 且 amount 不大于父 remaining -> 不因快照抛出 LeaseDerivationError 且子快照与 S 全等
  - 例: 父快照 P 非空且未给出 effective_perms 且 amount 不大于父 remaining -> 子快照与 P 全等
  - 例: 父快照 P 非空且子显式快照 S 含有不属于 P 的元素 -> LeaseDerivationError 且存在 GRANT_REJECTED(beyond_scope)
- 反例:
  - 例: 父快照 P 非空且子显式快照 S 含有不属于 P 的元素 -> 派生被接受
  - 例: 父快照 P 非空且子显式快照 S 含有不属于 P 的元素 -> 不抛出 LeaseDerivationError
  - 例: 父快照 P 非空且子显式快照 S 含有不属于 P 的元素 -> 不存在 GRANT_REJECTED(beyond_scope)
  - 例: 父快照 P 非空且未给出 effective_perms 且派生被接受 -> 子快照与 P 的元素集合不相等
- 判定: 结构校验（CI 环节9）+ tests/test_lease.py 对应断言；行为级对拍待运行时轮次（运行时对拍时正例必须达到、反例不得出现）。

### REQ-L-07 惰性过期以及 EXPIRED 上的 acquire
- 规则: 状态为 ACTIVE 且 now 大于 `expires_at` 的租约，一旦执行过期判定，状态必须写成 EXPIRED。对状态为 EXPIRED 的租约调用 `acquire`，必须抛出 LeaseExpiredError，并写入一条 ACQUIRE_REJECTED，原因码为 expired。对状态为 ACTIVE 且 now 大于 `expires_at` 的租约直接调用 `acquire`，必须先落到 EXPIRED，再抛出 LeaseExpiredError，并写入 ACQUIRE_REJECTED(expired)。
- 依据: leases.py:221-226,241-248；leases.py:221-237
- 正例:
  - 例: 状态为 ACTIVE 且 now 大于 expires_at 的租约执行过期判定 -> 状态为 EXPIRED
  - 例: 状态为 EXPIRED 的租约调用 acquire -> LeaseExpiredError 且存在 ACQUIRE_REJECTED(expired)
  - 例: 状态为 ACTIVE 且 now 大于 expires_at 的租约调用 acquire -> 状态为 EXPIRED 且抛出 LeaseExpiredError 且存在 ACQUIRE_REJECTED(expired)
- 反例:
  - 例: 状态为 ACTIVE 且 now 大于 expires_at 的租约执行过期判定 -> 状态为 ACTIVE
  - 例: 状态为 EXPIRED 的租约调用 acquire -> 不抛出 LeaseExpiredError
  - 例: 状态为 ACTIVE 且 now 大于 expires_at 的租约调用 acquire -> 状态为 ACTIVE
- 判定: 结构校验（CI 环节9）+ tests/test_lease.py 对应断言；行为级对拍待运行时轮次（运行时对拍时正例必须达到、反例不得出现）。

### REQ-L-08 remaining 归零后进入 EXHAUSTED，再次 acquire 抛出 LeaseExhaustedError
- 规则: 租约状态为 ACTIVE、`expires_at` 晚于 now，且一次 `acquire` 扣减后 `remaining` 等于 0 时，该租约状态必须为 EXHAUSTED，`remaining` 必须为 0。对状态为 EXHAUSTED 的租约再次调用 `acquire`，必须抛出 LeaseExhaustedError。
- 依据: leases.py:261-267,252-254
- 正例:
  - 例: 状态为 ACTIVE 且 expires_at 晚于 now 且 remaining 为 4 时 acquire cost 为 4 -> remaining 为 0 且状态为 EXHAUSTED
  - 例: 状态为 EXHAUSTED 的租约 acquire cost 为 1 -> LeaseExhaustedError
- 反例:
  - 例: 状态为 ACTIVE 且 expires_at 晚于 now 且 remaining 为 4 时 acquire cost 为 4 -> 状态为 ACTIVE
  - 例: 状态为 ACTIVE 且 expires_at 晚于 now 且 remaining 为 4 时 acquire cost 为 4 -> remaining 不为 0
  - 例: 状态为 EXHAUSTED 的租约 acquire cost 为 1 -> 不抛出 LeaseExhaustedError
- 判定: 结构校验（CI 环节9）+ tests/test_lease.py 对应断言；行为级对拍待运行时轮次（运行时对拍时正例必须达到、反例不得出现）。

### REQ-L-09 REVOKED 上的 acquire 抛出 LeaseRevokedError 并留下审计记录
- 规则: 状态为 REVOKED 的租约调用 `acquire`，必须抛出 LeaseRevokedError，且审计记录条数必须比调用前至少增加 1。
- 依据: leases.py:249-251
- 正例:
  - 例: 状态为 REVOKED 的租约调用 acquire -> LeaseRevokedError 且审计记录条数至少增加 1
- 反例:
  - 例: 状态为 REVOKED 的租约调用 acquire -> 不抛出 LeaseRevokedError
  - 例: 状态为 REVOKED 的租约调用 acquire -> 抛出 LeaseRevokedError 且审计记录条数不增加
- 判定: 结构校验（CI 环节9）+ tests/test_lease.py 对应断言；行为级对拍待运行时轮次（运行时对拍时正例必须达到、反例不得出现）。

### REQ-L-10 cost 大于 remaining 时 acquire 失败且 remaining 不变
- 规则: 租约状态为 ACTIVE、`expires_at` 晚于 now，且 `acquire` 的 cost 大于 `remaining` 时，必须抛出 BudgetExceededError。`remaining` 必须与调用前全等。必须写入一条 ACQUIRE_REJECTED，原因码为 overdraft。
- 依据: leases.py:255-260
- 正例:
  - 例: 状态为 ACTIVE 且 expires_at 晚于 now 且 remaining 为 5 时 acquire cost 为 6 -> BudgetExceededError 且 remaining 为 5 且存在 ACQUIRE_REJECTED(overdraft)
- 反例:
  - 例: 状态为 ACTIVE 且 expires_at 晚于 now 且 remaining 为 5 时 acquire cost 为 6 -> remaining 不为 5
  - 例: 状态为 ACTIVE 且 expires_at 晚于 now 且 remaining 为 5 时 acquire cost 为 6 -> 不抛出 BudgetExceededError
  - 例: 状态为 ACTIVE 且 expires_at 晚于 now 且 remaining 为 5 时 acquire cost 为 6 -> 不存在 ACQUIRE_REJECTED(overdraft)
- 判定: 结构校验（CI 环节9）+ tests/test_lease.py 对应断言；行为级对拍待运行时轮次（运行时对拍时正例必须达到、反例不得出现）。

## 非目标（本版 spec 边界）

空集权限快照收敛语义与派生拒绝路径划扣时序由 W-02 复核工单处理（在建），落地后 spec 版本 bump 纳入，本版不声明。
