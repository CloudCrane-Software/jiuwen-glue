# theory.leases eval（语义一致性对照）

> theory 圈文档：纯 Markdown，不 import 任何圈的代码（见 `theory/README.md`）。
> 起草：grok-4.7-build 单轮生成草稿（2026-09-27），主代理逐条修订（L-07 措辞纠正、§0 来源层级改为如实引用、openJiuwen 对照事实经源码复核）。
> 行号基线：main=7895c23 工作树；行号位移后以函数名 + git blame 复核。

## §0 元数据

| 项 | 值 |
| --- | --- |
| ref / semver | `theory.jiuwen_glue.leases.eval` / 0.1.0-draft |
| 状态 | draft |
| 对照对象 | `src/jiuwen_glue/leases.py`（BudgetLedger / BudgetLease） |
| oracle 来源 | ① 蓝图 v2.0 裁决（§2 租约行、§3.3、§4.4、§5.3）；② AI Agent Handbook 第 30 章（`repos/ai-agent-handbook/07-conclusion/第 30 章 从 Agentic Application 到 Agentic OS.md`：L49、L110 表 30-2、L120、L241）；③ openJiuwen agent-core 源码（`agent_teams/workflow/engine/budget.py`、`agent_teams/workflow/backends/budget_rail.py`、`harness/schema/interaction.py`） |
| 复检触发 | 蓝图修订、Handbook 第 30 章改写、上述源码行漂移、glue `leases.py` 公开语义变更 |

蓝图 §5.3 五级来源（按权威度）：机器可验证 → 差分共识 → 外部权威（厂商真实实现的差分测试）→ 结局回填 → 人类金标。本表工作约定：蓝图裁决与 Handbook 原文优先于源码实证，源码实证优先于实现注释；同级冲突标【待】，不自行合并。

## §1 对照总表

| 编号 | 治理语义点 | 本体系立场（glue 实现） | 外部权威立场 | 一致性判定 | 证据引用 |
| --- | --- | --- | --- | --- | --- |
| L-01 | 租约四要素：资源、操作、期限、撤销方式 | 一笔租约同时带额度（资源）、`effective_perms`（可操作面）、`ttl_seconds`（期限）、`revoke` 级联（撤销方式） | Handbook L120：租约把配额与最小权限收成同一授权物；蓝图 §2 租约行要求可过期、可撤销 | 一致 | `leases.py` perms 82–87；grant 121–217；expire 228–237；revoke 272–298；Handbook L120；蓝图 §2 |
| L-02 | 随子任务派生 | `grant(..., parent_lease_id=)` 从父剩余划出子额度；划扣在收敛校验通过之后（191–193），拒绝路径不划扣 | Handbook L120 的租约是可再授出的有界授权，不是一次性计数 | 一致 | grant 121–217；Handbook L120 |
| L-03 | 级联撤销 | `revoke` 沿子树撤销，返回被撤 id 列表 | Handbook L120 缺失后果：终止任务必须连带收回该任务的全部权限 | 一致 | revoke 272–298；Handbook L120 |
| L-04 | 级联独立于本节点状态 | 本节点已是终态仍继续遍历子树（295–297）；撤销路径先做惰性过期同步（282–283） | 蓝图 §3.3 修复条款：级联不得因本节点已终态而停步 | 一致 | revoke 295–297、282–283；蓝图 §3.3 |
| L-05 | 惰性过期 | 触碰（acquire / revoke / status_of 路径）时才把超时租约转为过期（221–226）；`expire` 为显式、幂等入口（228–237） | 蓝图要求期限约束；Handbook 未规定观察时机。glue 取触碰时判定，与「期限一到即失效」不冲突 | 一致 | 221–226、228–237 |
| L-06 | 额度抽象单位，本层不换算 | `amount` / `cost` 为整数抽象单位；本模块不把 token、金额、次数互化 | Handbook L241 L3 有界自主只要求上界存在，不规定计量单位 | 一致 | grant/acquire 签名；`leases.py:29`；Handbook L241 |
| L-07 | 权限交集固化与逐级收敛 | 签发时把求值所得的交集快照固化进租约（166–175）；派生强制子快照 ⊆ 父快照，越界拒绝（178–186）——不新增求交逻辑，收敛由包含校验保证 | Handbook L120：「租约模型把配额与最小权限合并为同一件事」；原则二：子委托范围只能比上游更小 | 一致 | 166–190；perms 82–87；Handbook L120 |
| L-08 | 审计留痕，拒绝也留痕 | 审计事件 60–66；发放/占用的拒绝路径同样落事件（112–117、145–147、246–260） | Handbook L49 判据二强制性：该能力必须由被约束方之外的组件执行才能真正生效（预算上限、审计记录属此类） | 一致 | 60–66、112–117；Handbook L49、L110 表 30-2 |
| L-09 | 凭证 ≠ 租约 | 租约是有期限、可撤销的预算与权限快照；身份凭证不充当租约 | 蓝图 §2 租约行明确二者分离（凭证是租约的兑现物） | 一致 | 蓝图 §2；`leases.py` 无凭证字段 |
| L-10 | 父过期不回收已划出子额度 | 额度在派生当时划出；父随后过期或撤销不把已划出的数额冲回父账 | Handbook 未规定过期是否回收子额度 | 有意偏离 | 派生划扣 191–193；见 §2-D1 |
| L-11 | 空集快照仍是快照 | 权限快照允许为空集；空集表示「无操作权」，不是「未冻结、可继承父权限」 | Handbook 未规定空快照边界 | 有意偏离 | 24–27、85–87、176–186；W-02；见 §2-D2 |
| L-12 | openJiuwen 原生对照 | glue 在计数器之上补齐派生、级联撤销、TTL、权限快照、撤销状态机、审计 | 原生 `BudgetLedger` 自述 business-agnostic，只做计数器+上限；`OutputLease` 仅同名 | 有意偏离/互补 | 见 §2-D3；`budget.py:23-69`；`budget_rail.py:27`；`interaction.py:195-201` |
| L-13 | 租约与网关断流绑定 | 蓝图要求租约过期即 Higress consumer key 断流，软提醒不算执行 | glue `leases.py` 未接线网关；原生 rail 只停轮 | 待校准【待】 | 蓝图 §4.4；见 §3 |

状态机锚点（不单列判定）：`leases.py` 47–51。acquire 241–268 是 L-05/L-06/L-08 的消费侧入口。

### L-12 事实摘要（已核实）

openJiuwen `BudgetLedger`（`agent_teams/workflow/engine/budget.py:23-69`）字段为 `total` / `spent` / `phase_tokens`。`add()` 上报用量，`remaining()` 钳到 0，`exhausted` 为耗尽属性。`bind_budget` 按引用把同一账本交给 backend（docstring 4–15 行：引擎只读）。强制点是 `SwarmflowBudgetRail`（`budget_rail.py:27`）：before/after 见 `exhausted` 则停轮。docstring 第 14 行自述 business-agnostic（a counter and a ceiling）。原生没有：父子派生、级联撤销、TTL、权限快照固化、撤销状态机、审计事件。`OutputLease`（`harness/schema/interaction.py:195-201`）是交互输出的单消费者租让（token + closed event），与预算无关，只是同名。Handbook L120 的缺失后果写明：「终止任务」与「收回该任务的全部权限」若靠多处配置协同就会漏。原生 rail 只停轮，不撤销授权。glue 用同一租约对象补上这半边，故判互补，而不是把原生计数器判为错误实现。

## §2 有意偏离清单

相对 Handbook 第 30 章论述的具体裁定。Handbook 未写明之处，glue 给出本体系裁决，不把沉默读成同意。

- **D1 子额度不随父过期回收。** Handbook 未规定父过期后已划给子任务的额度是否冲回。glue 裁定：额度在派生校验通过时已经划出（`leases.py` 191–193），父过期只终止父自身的再授出与再占用，不回溯子账。理由：子任务已在飞，回溯会造成「账上还有、手上被收」的双计；收回靠级联 `revoke`，不靠过期副作用。
- **D2 空集快照也是快照（W-02）。** Handbook 未规定权限快照为空时算「未授权」还是「沿用父集」。glue 裁定：空集是一次成功的固化（24–27、85–87、176–186），语义为零权限；缺快照（perms_frozen=False）才是未冻结。理由：把空集当成继承会让收敛检查被绕开，最小权限合并（L120）失效。
- **D3 相对原生 BudgetLedger 的互补，不替换其计数语义。** 原生账本保留「上限 + 已用 + 停轮」。glue 不把 `total/spent/exhausted` 重写成另一套单位，而是在治理层增加派生、TTL、快照、级联与审计。`OutputLease` 不纳入预算租约。理由：Handbook L49 要求约束落在被约束方之外；停轮满足「停」，不满足「收回全部权限」（L120）。两者叠放，原生 rail 仍可作运行时背压。

不在本清单内的 L-01–L-09 与蓝图或 Handbook 的已写明命题同向，不升格为偏离。

## §3 oracle 常驻与复检方式

蓝图 §5.3：Oracle 生命周期为 **提议 → 校准 → 影子 → 生效 → 退役监控**。本表处于「校准」：草案可引用，不得当已生效契约。

| 阶段 | 本表动作 |
| --- | --- |
| 提议 | 新语义点先入本表，判定默认【待】，不改 `leases.py` |
| 校准 | 按 §0 来源层级填「外部权威立场」；同级冲突保持【待】 |
| 影子 | 用现有 `tests/test_lease.py` 对 L-01–L-12 做只读对照，不新造执行路径 |
| 生效 | owner Approval 后，与 `theory/leases/contract.md` 同号冻结；本表判定改为生效记录 |
| 退役监控 | 源码行号或 Handbook 章节移动时重核证据列；退役项保留编号，不复用 |

当前唯一【待】项是 **L-13**：蓝图 §4.4 要求租约过期即 Higress consumer key 断流，软提醒不算数。glue 未接线，openJiuwen 无对应物。复检条件：出现租约 id 与 consumer key 的绑定实现，且过期/撤销路径能关闭该 key。在此之前 L-13 不得标成一致。

复检不自动改判定。证据行漂移只更新「证据引用」列；语义变化才改「一致性判定」，并在 §2 追加或撤销对应 D 项。
