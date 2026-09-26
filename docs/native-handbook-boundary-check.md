# 研发手册四边界核对表（WO-0003 返工交付物）

- 核对日期：2026-09-26（WO-0003 返工，M0.5 前置）
- 验收原文：`handbook-3.3-extract.md`（《AI Native 研发范式实践手册》§3.3.1 Agent Identity & Policy
  印刷页 51–54 + §3.3.2 Guardrail 印刷页 55 的逐字转写）
- 核对对象：jiuwen-glue（本仓 `src/jiuwen_glue/`）实际实现
- 方法：手册原文逐条 ↔ glue 模块/函数逐一对照；**偏差如实标注，不造假**。
- 结论标记：**对齐** = 手册要求在 glue 有可运行的落实点（含测试）；**偏差** = 手册要求
  glue 未承载或由别处承载（写明承载方与待办）；**待办** = 后续工单动作。

---

## 一、四个对象（手册："需要区分四个对象……它们相互关联，但不能合并成一张'万能 Token'"）

### 1. 身份认证 —— "证明谁在请求"

> 手册原文锚点："一次调用可能同时涉及：发起任务的用户、稳定的 Agent、实际运行的设备或
> 沙箱、工具服务、最终被改变状态的资源服务。只记录一个用户 ID 或共享应用账号，无法回答
> '哪个程序、以哪个 Agent 的名义、基于谁的委托、操作了什么'。"

| 手册要求 | glue 落实点 | 结论 |
| --- | --- | --- |
| 区分"哪个 Agent、哪个运行实例" | `identity.AgentIdentity`（稳定 agent id）/ `identity.RunInstance`（instance id、node、ttl）；`identity.composite_ref()` 生成三层身份单串引用 | **对齐**（`tests/test_identity.py::test_three_layers_and_composite_ref`） |
| "这次为何执行" | `identity.TaskContext`（task id、workspace_uri、pipeline_label、env_ref、delegation_ref），任务上下文贯穿：`BudgetLease.task_ref`、`GuardrailSpec.agent_identity_ref`、`DecisionRecord.agent_ref` 全部携带三层引用 | **对齐** |
| 身份的**密码学验证**（签名、签发方、时效、目标资源绑定） | glue **不做**。参照系为阿里 RRSA OIDC + STS（v1.7 §12.5"不发明新东西"）；验证由可信 Runtime（jiuwenswarm AgentServer）与 OpenBao JWT auth（WO-0005 骨架：ci-builder role_type=jwt、ttl=15m）承担；glue 只承载对象与引用 | **偏差（如实）**：glue 是对象/协议层，不是验证执行层。待办：OpenBao JWT 绑定三层身份（WO-0005 扩展，M1） |
| 实例短时性 | `RunInstance.ttl_seconds` 字段 + `BudgetLease.expires_at` 惰性过期（`leases.py`） | **对齐**（字段级；实际 TTL 签发在租约发放时执行） |

### 2. 委托 —— "Agent 为什么可以代表用户"

> 手册原文锚点："Agent 是在代表哪个用户行动、委托范围是什么？"；
> "多 Agent 协作时，每一跳都要建立自己的调用者身份；子委托的范围只能比上游更小。"

| 手册要求 | glue 落实点 | 结论 |
| --- | --- | --- |
| 委托依据对象化 | `identity.Delegation`（delegation_id、subject、scopes、parent_delegation_id、tenant_id）；`TaskContext.delegation_ref` 携带委托引用 | **对齐** |
| 逐级收敛不变式（子委托 ⊆ 上游） | `identity.narrow_delegation()`：子委托 scopes ⊄ 上游即抛 `DelegationScopeError`（`tests/test_identity.py::test_sub_delegation_must_be_subset_of_upstream`） | **对齐** |
| 越权尝试检测（"模型把操作目标从当前服务改成另一个服务……目标超出本次委托范围时拒绝执行"） | 交集公式中 delegation 作为五集之一强制收敛；`tests/test_identity.py::test_target_service_change_is_privilege_escalation_not_new_phase` 写死该案例 | **对齐**（判定公式层；实际拒绝由各 PEP 用 `EffectivePerms.allows()` 落实） |
| 委托的**签署/授权链**（用户→Agent 的授权凭据，谁在何时授权了谁） | glue 只有 `Delegation` 对象与引用，**没有**用户签署链/授权凭据本体 | **偏差（如实）**：签署链属 Credential Broker 域（OpenBao）。待办：delegation_ref ↔ OpenBao JWT claim 的映射（M1） |

### 3. 策略授权 —— "当前条件下允许做什么"

> 手册原文锚点："策略授权决定当前条件下允许做什么"；
> "有效权限 = 用户权限 ∩ Agent 能力上限 ∩ 平台策略 ∩ 本次委托范围 ∩ 运行时约束"。

| 手册要求 | glue 落实点 | 结论 |
| --- | --- | --- |
| 交集公式显式化（不是口头原则） | `identity.effective_permissions()` 五集交集 + `EffectivePerms`（结果集 + 每一分量出处）；**在租约签发路径上求值并固化**：`BudgetLedger.grant(..., effective_perms=...)` 把快照冻进 `BudgetLease.effective_perms/perms_provenance`（`tests/test_identity.py::test_lease_freezes_effective_perms_at_grant`） | **对齐**（v1.7 §12.5"在 glue 租约签发时求值"逐字落实） |
| "用户有权 ≠ Agent 自动有权" | 交集缺一不可：`tests/test_identity.py::test_user_permission_alone_is_never_enough`（用户全权、其余四集为空 → 交集为空） | **对齐** |
| 派生权限逐级收敛 | 派生租约时强制子快照 ⊆ 父快照，越界派生抛 `LeaseDerivationError` + GRANT_REJECTED 留痕（`tests/test_identity.py::test_derived_lease_perms_must_be_subset_of_parent`） | **对齐** |
| 授权三态（允许/拒绝/Challenge） | 允许/拒绝由原生 TeamPermissionRail（4.9 #3，glue 不替代）；**第三态 = `challenge.Challenge` 结构化对象**（见下）；glue `guardrail` 的 permission_rail 型 check 可提交 ASK 并 emit Challenge | **对齐**（分工：原生决策，glue 结构化协议聚合） |
| PDP/PEP 分工（"允不允许"必须由确定性系统判断） | glue **不新增第二个决策点**（写死于 `guardrail.py` 模块 docstring + `identity.py` 模块 docstring；`tests/test_guardrail.py::test_decision_point_unicity` 断言 GuardrailRunStore 公开方法集合） | **对齐**（决策点唯一：GuardrailRun 聚合 verdict 是唯一门控输出） |
| 授权缓存/撤销扩散（"控制台显示已撤销，不代表每个执行点都拒绝"） | glue 不做授权缓存——租约级快照随租约撤销级联失效（`leases.revoke` 级联 + 留痕）；执行点侧撤销由原生 Rail/OPA 承担 | **偏差（如实）**：跨执行点的撤销广播不在 glue 能力范围。待办：M1 撤销事件接原生 reliability/监控通道 |

### 4. 凭证 —— "让具体下游能够验证这次访问已获准"

> 手册原文锚点："凭证让具体下游能够验证这次访问已获准"；原则四："长期凭证不进入 Agent"，
> "优先级是：代理调用 > 注入短期凭证 > 注入长期凭证"。

| 手册要求 | glue 落实点 | 结论 |
| --- | --- | --- |
| 短期凭证兑换 | glue **不签发凭证**。OpenBao = Credential Broker（v1.6 §7 / WO-0005：execution-worker policy 只读 `kv/data/company/gpu/*`、JWT ttl=15m）；执行面零长期密钥 | **偏差（如实，设计如此）**：凭证对象本体不在 glue——glue 只持有租约级**权限快照**（`effective_perms`）与 TTL，凭证兑换经 OpenBao。待办：无（边界即设计；四边界核对的意义就是确认 glue 越界会做错） |
| "影子凭证"剥离（响应中的 Cookie/刷新令牌在返回 Agent 前剥离） | 不在 glue。原生 tracer_otel redaction（观测侧脱敏）+ 原生 harness/security 承担 | **偏差（如实）**：glue 无响应路径，无从剥离。归原生（4.9 #2/#6） |
| 凭证绑定资源/时效 | 租约绑定：`task_ref`（工作区 URI 经 TaskContext）+ `expires_at` + 权限快照——签发后快照不可变，派生只可收窄 | **对齐**（租约即"访问已获准"的 glue 侧承载；下游验证靠快照引用） |

**四对象总结论**：身份/委托/策略授权三对象在 glue 有直接落实（对象、公式、不变式、测试）；
凭证对象**有意不在 glue**（Credential Broker=OpenBao 承担）——这是边界声明而非缺失；
身份的密码学验证与委托签署链为两处如实偏差，均已列入待办（M1）。

---

## 二、五原则（手册 §3.3.1"核心架构和原则"逐条落实点）

| 原则 | 原文要点 | glue 落实点 | 结论 |
| --- | --- | --- | --- |
| **一：建立可验证的复合身份** | 三层主体：稳定 Agent / 运行实例 / 任务上下文；代表用户时带验证过的用户身份与委托依据 | `identity.AgentIdentity` / `RunInstance` / `TaskContext` / `composite_ref()`；所有门控与决策对象（GuardrailSpec、DecisionRecord、BudgetLease.agent_ref、Challenge.agent_identity_ref）统一引用三层身份 | **对齐**（"可验证"的验证执行在 Runtime/OpenBao，见四对象 #1 偏差行） |
| **二：权限只能逐级收敛** | 有效权限 = 五集交集；用户有权 ≠ Agent 有权；子委托只能更小 | `identity.effective_permissions()` + `EffectivePerms.subset_of()/allows()` + `narrow_delegation()` + 租约签发固化与派生收窄（leases 联动） | **对齐** |
| **三：由确定性系统决策，并在靠近资源的地方执行** | 模型只提出意图；PDP 确定性判断；PEP 多层落实；不直接相信模型给的资源字符串 | glue 侧确定性公式只有集合运算（`effective_permissions`/`aggregate`），无模型参与；`guardrail.CheckSpec` 只存声明、**执行在原生与各执行点**（native_guardrail→core.security.guardrail、permission_rail→TeamPermissionRail、eval_gate、scan）；决策点唯一断言见测试 | **对齐**（"靠近资源执行"由原生 PEP 承担，glue 是协议聚合层） |
| **四：长期凭证不进入 Agent** | Credential Broker 代管/兑换；代理调用 > 短期凭证 > 长期凭证；影子凭证剥离 | glue 全仓**无任何凭证/密钥字段**（可 grep 验证）；兑换归 OpenBao（WO-0005） | **对齐（以边界方式）**：glue 结构上不可能持有凭证；代理/兑换/剥离的实现在 OpenBao 与原生层（如实声明，非本仓交付物） |
| **五：权限必须可撤销、可追溯** | 撤权后停止签发、清缓存、断下游；审计把意图→Agent→委托→工具→资源→决策→凭证→结果关联成完整链路 | 可撤销：`BudgetLedger.revoke()` 级联撤销全部后代 + 未花额度作废 + audit 留痕；Challenge 过期即 expired（fail-closed）；可追溯：`evidence` 三态迁移留痕、`decisions.DecisionLog` append-only（含 agent_ref/context_hash/rationale_ref/guardrail_run_ref）、`GuardrailRun` 五步全程留痕、violation_log | **对齐**（全链关联依赖观测侧 ATIF/决策记录引用贯通；引用完整性测试见 test_decisions/test_guardrail） |

---

## 三、Guardrail 工作原理（手册 §3.3.2 五步链路逐字对照）

> 手册原文："现有发布链路不被替换，只在执行 resume 前增加一段可配置、可验收的 Agent
> 检查过程。" 五步：①提交动作上下文 → ②固化规则与上下文 → ③Agent 查询事实并提交
> 判断与 Evidence → ④验收协议并固化结果 → ⑤执行前主动查询门控结果、终检并执行。

| 手册五步 | glue 实现（`guardrail.py`） | 测试 |
| --- | --- | --- |
| ① 创建/复用运行并提交动作上下文 | `GuardrailRunStore.create_run(GuardrailSpec)`——spec 含 action/resource/agent_identity_ref/env_ref/tenant_id + checks | `test_five_step_chain_happy_path` |
| ② 固化规则与上下文 | spec 在创建时 deepcopy 冻结，之后不可改；`spec_version` 字段供准入记录绑定 | 同上 + `GuardrailSpec` frozen dataclass |
| ③ 提交判断与 Evidence | `submit_check(run_id, check_id, outcome, evidence_ref=...)`——逐 check 结论 + 证据引用；ASK 走 ChallengeBoard | `test_ask_emits_challenge_and_gate_stays_unknown` |
| ④ 验收协议并固化结果 | `finalize(run_id, seal_ref=...)`——固化后结论只读，重开必须建新 run | `test_frozen_after_finalize` |
| ⑤ 执行前查询门控、终检 | `gate(run_id)` → `GuardrailResult`（聚合 verdict）；**fail-closed：UNKNOWN 必须拒绝**；`void()` 实现手册"任何关键状态变化使原有结论失效" | `test_gate_before_finalize_is_unknown_fail_closed` / `test_void_invalidates_finalized_conclusion` |

聚合语义（写死）：任一 BLOCKED → BLOCKED；否则任一 UNKNOWN（含必填 check 未提交）→
UNKNOWN；否则 PASS。**决策点唯一：聚合 verdict 是唯一门控输出，本模块不新增第二个决策点**
（`guardrail.py` 模块 docstring 逐字写死；`test_decision_point_unicity` 断言公开方法集
= {create_run, submit_check, finalize, void, gate, get, pending_challenges}，无任何"执行检查"方法）。

**偏差（如实）**：手册五步中"发布系统执行前……条件满足后执行 resume"的动作执行体
在发布系统（原生链路），glue 只提供门控查询——这正是 4.9 #2 的分工（原生管执行、
glue 管聚合），非遗漏。

---

## 四、授权三态之 Challenge（手册"缺少权限时怎么办"逐字对照）

| 手册要求 | glue 实现（`challenge.py`） | 测试 |
| --- | --- | --- |
| 第三态是**结构化授权要求**，不是 403 文本 | `Challenge`：who_confirms / resource / action / method / expires_at / state | `test_structured_object_fields` |
| "说明需要谁确认"（用户/负责人/值班） | who_confirms ∈ {user, resource_owner, duty_officer}；Agent 自确认被拒 | `test_open_validation` |
| "哪个 Agent 想对哪个资源做什么"（独立界面展示） | `to_ask_payload()` 映射 TeamPermissionRail ask 路由 | `test_to_ask_payload_shows_who_wants_what` |
| "有效期多久" + 过期失效 | ttl 必须为正；pending 越线惰性转 expired；过期后裁决被拒（fail-closed） | `test_expiry_is_fail_closed` |
| "模型只收到高层状态，接触不到授权码和 Token" | 对象与载荷**均无**授权码/token 字段（测试断言 forbidden 字段集不相交）；兑换在 OpenBao | `test_model_only_sees_high_level_states_never_auth_code` |
| 研发 Agent 逐步获得权限案例（12.5 验收场景） | 修改测试环境配置 → ASK→Challenge→批准→PASS 重提交（`test_ask_emits_challenge_and_gate_stays_unknown`）；重启生产实例 → duty_officer 确认（`test_pending_queue_by_confirmer`） | 见左 |

---

## 五、与 v1.7 §12.5"JIT 身份收口三件"的对照（工单验收线）

| v1.7 §12.5 要求 | 落实 | 结论 |
| --- | --- | --- |
| 三层复合身份写进 glue 对象 | `identity.py` 三 dataclass + composite_ref；引用贯通 lease/guardrail/decision/challenge | 对齐 |
| 权限交集公式在 glue 租约签发时求值，不是口头原则 | `BudgetLedger.grant(..., effective_perms=...)` 固化快照 + 派生收窄强制 | 对齐 |
| Challenge 是结构化对象，映射 TeamPermissionRail ask 路由 | `challenge.py` + `guardrail` ASK 语义 | 对齐 |
| 参照系=阿里体系（RRSA OIDC+STS），不发明新东西 | glue 不自建 OIDC/STS；对象模型与 RRCS/阿里两层交集同构（五集交集为其多 Agent 扩展，出自手册原则二原文） | 对齐 |

## 六、遗留待办（汇总，均不在本工单范围）

1. OpenBao JWT 绑定三层身份（身份验证执行 + 委托签署链映射）——WO-0005 扩展，M1。
2. 撤销事件跨执行点广播——M1 观测/可靠性通道工单。
3. `ops/sql/002_glue_v2.sql` 的 DDL 落生产库（srv-1 pg）——由后续 srv-1 工单执行（本工单只交 DDL）。
4. 短期凭证兑换链路实测（依赖 OpenBao JWT role 与执行面联调）——[待实测]，进 RUNBOOK M1 步骤。
