# jiuwen-glue

openJiuwen 胶水层（glue layer）。一句话：**只做 openJiuwen 原生没有的那几件事，其余一律用原生。**

## 定位

依据《一人公司多 Agent 系统建设方案》（PROP-0001 v1.6/v1.7，存 CNB `proposals` 仓）§0 总则 4、§4.1 与 §4.9 边界总表，glue 只承载原生没有的能力。本包（WO-0003 + M0.5 返工补齐）实现九个模块：

| 模块 | 说明 | 依据 |
| --- | --- | --- |
| `leases` **Budget Lease**（预算租约） | 发放 / 占用 / 过期；子任务派生即从父租约划出额度；撤销**级联**到全部后代；超额占用被拒并留痕；**签发时求值并固化权限交集快照**（v1.7 §12.5），派生强制"子快照 ⊆ 父快照" | v1.6 §4.1 |
| `evidence` **Evidence 三态** | `draft → verified → finalized` 只进不退；内容仅 draft 可编辑；finalized 封存；能力准入只认 verified/finalized 证据 | v1.6 §4.1 |
| `capabilities` **能力元数据注册** | 五类机读声明（副作用/幂等/可安全重试/风险等级/前置条件）+ 版本准入（必须挂已验证证据）+ 指标**单向回流**（只增不改，无手改接口）；台账按租户键控 | v1.6 §4.1/4.9#1 |
| `rules` **协同三铁律** | Task 台账 = Team State 唯一承载；消息回执不承载状态语义；拆分任务校验 | v1.6 §4.3 |
| `guardrail` **GuardrailRun 协议聚合薄层** | 手册 §3.3.2 五步链路（提交上下文→固化→提交判断与 Evidence→验收固化→执行前门控查询）；聚合三态 PASS/BLOCKED/UNKNOWN，**fail-closed：UNKNOWN 必须拒绝**；可 emit Challenge（ask 语义） | 手册 §3.3.2；M0 审计发现 1 |
| `identity` **三层复合身份 + 权限交集公式** | 稳定 Agent / 运行实例 / 任务上下文；有效权限 = 用户 ∩ Agent 能力 ∩ 平台策略 ∩ 委托范围 ∩ 运行时约束（含每分量出处）；子委托 ⊆ 上游不变式 | 手册 §3.3.1 原则一/二；v1.7 §12.5 |
| `challenge` **Challenge 结构化对象** | 授权三态第三态：谁确认/什么资源/什么动作/什么方式/有效期；过期即 expired（fail-closed）；模型只见高层状态，拿不到授权码 | 手册 §3.3.1；v1.7 §12.5 |
| `promotion` **记忆晋升管线** | 只做"个人→组织"资产晋升（working→shortlist→promoted + rejected/withdrawn）：质量门（eval 指标 append-only）+ 脱敏门（未注入 checker 即 UNKNOWN 拒绝）+ 可选 `score_hook`（决策层 §4.7 第六植入点，唯一交叉点）+ 单调版本化 + tombstone 撤回；**不回写执行面记忆** | v1.6 §4.9#4；M0 审计发现 1 |
| `decisions` **决策记录（append-only）** | agent_ref（三层身份）/context_hash/options/chosen/rationale_ref/guardrail_run_ref/tenant_id；只增不改——无任何 update/delete 接口（DDL 层另有拒绝触发器兜底） | M0 审计发现 1 |
| `routes` **产物路由表 + 节点容量**（便宜三件 Python 侧） | `ArtifactRouteTable`：pipeline→sink 声明（基线 code→git / video→minio / eval→eval-assets），**未声明路由 = 显式错误（防产物误入 git）**；`NodeCapacity`：cpu/gpu_frac（0.0–1.0 份额）/tools/trust_level/max_parallel/online_window，不可信节点只派沙箱任务类 | v1.7 §13/§12.6 |

三条铁律（PROP-0001 §4.3；母本为 Handbook 第 11 章 + openJiuwen agent_teams specs）：

1. 消息可以触发任务或补充信息，但**发送成功不代表任务已被承接**；
2. **不能把对话历史当作 Team State**，协作事实由 Task State 与 Artifact 维护（完成任务必须带 TaskRun 与 Artifact 引用）；
3. 只有存在独立交付、不同责任或明确依赖时才拆子任务，**同一成员连续完成的内部步骤不建任务**。

每条铁律各有一条失败用例（`tests/test_iron_rules.py`）：喂入违规输入，断言抛出对应 `IronRuleViolation` 子类 + `violation_log` 留痕 + 任务事实未被篡改——**违规必被检测**。

`tenant_id`（v1.7 §4.1）：全部数据类统一携带，默认 `"t0"`（单租户起步，字段全对象覆盖）。

## 边界（写死）

- 原生层管"怎么做"，glue 管"准不准进"，控制台管"看得见"。
- 凡可能有两个决策点的，必须收敛为一个；冲突记 ADR。
- §4.9 边界总表之外，glue 不新增决策点；**发现与编排一律用原生 Symphony，本包不自建发现机制**（4.9 #1）。
- **glue 不新增第二个决策点**：GuardrailRun 聚合 verdict（PASS/BLOCKED/UNKNOWN）是唯一门控输出；检查执行属原生 `core.security.guardrail` 与各执行点（TeamPermissionRail / OPA / eval-gate / 扫描器）——`guardrail.py` 模块 docstring 写死并有测试断言公开方法集。
- 晋升管线**不回写执行面记忆**；决策记录**不参与决策**；路由表**不做调度**（Wave2 WO-0011）。
- 凭证不在 glue：Credential Broker = OpenBao（执行面零长期密钥）。
- 四对象/五原则/五步链路的逐条核对见 **`docs/native-handbook-boundary-check.md`**（含如实偏差与待办）。
- 协议兼容 ≠ 能力等价（见 `docs/e2b-compat-gap-report.md`）。

## 快速上手

```bash
pip install -e .          # 或 uv pip install -e .
python -m pytest          # 整仓 520 项测试，全离线（无需数据库/网络）：主包 tests/ 383 + providers/e2b_compat 41 + tools/console-tui 87 + tools/spec-gate/tests 9（2026-09-28 D6-R2 @ bc12adf 实测 486/358；D1-R2 补 ablation tenant 用例后 487/359；D1-R3 billing 归集租户隔离修复补 1 例并纳入 spec-gate 语义测试 7 例后 495/360；D4-R3 语义回归钉 7→9 后 497（同提交未同步本行，本轮补记）；D1-R4 租约派生同租户不变式补 2 例后 499/362；D1-R5 aggregate 非规范值收敛 UNKNOWN 补 1 例后 500/363（另有双聚合一致性回归 1 例需 eval-gate 在场，未装则 skip 不计数）；D1-R6 gate 缺交折算 UNKNOWN 补 3 例后 503/366；D1-R7 非有限数值闸收口补 5 例后 508 + 二轮补 1 例后 509 + 三轮费率卡整卡 hash 补 1 例后 510/374（CI 口径与主包收集数；PR #16/#18/#19/#20 合入态 c86bafd，eval-gate 在场 511 passed + 0 skipped）；R7 修复轮（终局补漏）非有限闸推广到 leases/fleet + billing duck 路径两处收口补 7 例后 518 + 二批（grok 抽查落地）promotion score_hook 非有限弃权拒绝与 heartbeat_ttl 构造期闸补 2 例后 520/383（eval-gate 在场 520 passed + 0 skipped / CI 不装 eval-gate 519 passed + 1 skipped）；testpaths 已含四件套，子包亦可各自独立 pytest）
```

```python
from jiuwen_glue import (
    BudgetLedger, EvidenceStore, CapabilityRegistry, TaskLedger,
    GuardrailRunStore, GuardrailSpec, CheckSpec, BACKEND_SCAN,
    AgentIdentity, RunInstance, TaskContext, composite_ref,
    effective_permissions, ChallengeBoard, PromotionLedger,
    DecisionLog, ArtifactRouteTable, NodeCapacity,
)

# Budget Lease：随子任务派生 + 级联撤销 + 签发时固化权限交集
agent = composite_ref(AgentIdentity("dev-core"), RunInstance("i-1", "node-0"),
                      TaskContext("wo-0003"))
eps = effective_permissions(user={"svc-a:logs:read"}, agent_caps={"svc-a:logs:read"},
                            platform_policy={"svc-a:logs:read"},
                            delegation={"svc-a:logs:read"},
                            runtime={"svc-a:logs:read"})
led = BudgetLedger()
root = led.grant("task-root", 1000, agent_ref=agent, effective_perms=eps)
child = led.grant("task-child", 300, parent_lease_id=root.lease_id)  # 派生即划出；perms ⊆ 父
led.acquire(child.lease_id, 120, purpose="run-1")
led.revoke(root.lease_id, reason="owner cancelled")                  # 级联撤销

# GuardrailRun：五步链路，聚合 verdict 是唯一门控输出（UNKNOWN 必须拒绝）
store = GuardrailRunStore(challenge_board=ChallengeBoard())
run = store.create_run(GuardrailSpec(
    action="release.resume", resource="batch:cfg-01", agent_identity_ref=agent,
    checks=(CheckSpec(check_id="static-scan", backend=BACKEND_SCAN),)))
store.submit_check(run.run_id, "static-scan", "PASS", evidence_ref="ev-1")
store.finalize(run.run_id, seal_ref="seal-1")
result = store.gate(run.run_id)
assert result.executable          # fail-closed：只有 PASS 为 True

# Evidence 三态 / 能力元数据 / 晋升 / 决策记录 / 路由表——详见各模块 docstring
```

## Wave2 能力（fleet / 决策层 / e2b_compat / console-tui）

四个 Wave2 分支已按 fleet → decision → e2b → tui 顺序合并进 main（各为独立 merge commit，零冲突）：

| 子项 | 能力 | 路径 | 测试 | 依据 |
| --- | --- | --- | --- | --- |
| **fleet 调度器**（WO-0011） | 节点池注册协议（OpenBao JWT 声明式注册/心跳/STALE 兜底，不验签）+ P1 贪心调度器（派工制/自取制两模式、份额记账、Budget Lease 派生）+ 自取制 worker 客户端协议（模拟侧）；复用 routes/leases/identity/guardrail，不重造 | `src/jiuwen_glue/fleet/`（registration / scheduler / worker） | 71 项（全离线；P2 bin-packing 后） | PROP-0003 / PROP-0004 P1；v1.7 §12.6/§13 |
| **决策层 MVP**（WO-0010 + WO-0007 件 1/2） | JevProvider 三原语 classify/score/judge（返回值携带 decision_ref，每次高频决策落账）+ RuleBasedBackend 确定性后端 + `make_score_hook`（晋升打分唯一交叉点）；准入前 A/B 消融对照（sign-test 判定）；准入台账（消融+GuardrailRun 硬规则）+ skill-pack 外发 | `src/jiuwen_glue/` 下 `decision.py` / `ablation.py` / `admission.py` | 49 项 | v1.7 §0 总则 6/§12.7；v1.6 §4.7；手册 §3.3.1 原则三 |
| **e2b_compat Provider**（PROP-0008） | openJiuwen SandboxRegistry 的 E2B 兼容三件套——云突发沙箱接成沙箱新 backend，不改 openjiwen 代码，glue 不新增沙箱决策点；核心零依赖，缺 e2b SDK 时优雅降级报错。**当前为 mock 级交付**（测试全用假客户端，未连真实 E2B 云） | `providers/e2b_compat/`（独立子包） | 41 项 | PROP-0008；v1.7 §12.8；`docs/e2b-compat-provider.md` |
| **console-tui 治理驾驶舱**（WO-0012） | 治理面作战室 TUI（Textual）：读 glue 库五面板 + 三级受控干预 s/a/p，全部干预经控制台留痕（GuardrailRun Challenge 语义）；无 DSN 时 mock 演示模式；含第三方 TUI 开源项目评估报告（先评估后引进） | `tools/console-tui/`（独立工具包）；`tools/console-tui/docs/{console-tui,tui-research}.md` | 87 项（2026-09-28 D6-R2 实测，含 timeline 合并与 challenge 回归增量） | v1.7 §12.3；TUI 调研 PROP-0007 |
| **usage 计量**（v2.0 W-04） | usage_events 四维度 append-only 计量 + 租约-Higress consumer 绑定与 cutoff_due/cutoff_plan 断流求值 + TUI usage 面板（pg 读 glue.v_usage 参数化） | `src/jiuwen_glue/usage.py` | 15 项（全离线） | v2.0 §4.4；报告 §W-04 |
| **escalation 升级阶梯 + 裁决卡**（v2.1 W-06） | 六级阶梯单步推进 + 人类就绪包四件套 + L4→L5 硬闸（就绪包签名==blocker 签名）+ 风暴防护三防线（级内总量上限/最小步进间隔/签名去重）+ TUI AdjudicationCard（y=approve fail-closed / e=escalate 打回 L3） | `src/jiuwen_glue/escalation.py`；`tools/console-tui/`（裁决卡） | 23 项（glue 侧） | v2.1 §4.7；报告 §W-06 |
| **Wave2 分支模块（已并 main）** | platform_tokens 铸造器（TTL≤1h/禁缓存禁代签，W-09，22 例）经 3342fc0 并入；billing 结算（W-07，14 例）经 6ec8807 并入；TUI timeline（W-11）经 88e93f9 并入——**三者均已在 main，用例计入上述计数**。〔勘误 2026-09-28 D6-R2〕收口提交 bc12adf 曾沿用合并前 ae23962 树的实测数并误标"均未并入 main"，与本树事实不符，已按 bc12adf 树重测更正 | `src/jiuwen_glue/`（billing.py / platform_tokens.py）；`tools/console-tui/`（timeline） | 已计入（主包 360 / tui 87） | v2.1 报告 ⑤.1/⑥F；三分支并入 main=6ec8807(billing)/3342fc0(w09)/88e93f9(w11)，可 `git merge-base --is-ancestor` 复核（CNB 台账行 8e431b26 的收口 SQL 在 company-ops `ops/sql/009`〔待应用〕，勿以台账现态判断合并与否） |

合并态整仓 `python -m pytest` 520 项全绿（2026-09-28 D6-R2 @ bc12adf 实测 486/358，D1-R2 补 ablation tenant 用例 +1 后复测 487，D1-R3 billing 归集租户隔离修复补 1 例 + 纳入 spec-gate 语义测试 7 例后复测 495 @ D1-R3 修复提交：主包 tests/ 360，含 fleet 71 + usage 15 + escalation 23 + billing 15 + platform_tokens 22 + statebuilder/FEFO + harness 扩展包 + W-01/W-02/W-06 各单回归；D4-R3 语义回归钉 7→9 后 497（同提交未同步本节，本轮补记）；D1-R4 租约派生同租户不变式补 2 例后 499 @ D1-R4 修复提交：主包 tests/ 362；D1-R5 aggregate 非规范值收敛 UNKNOWN 补 1 例后 500 @ D1-R5 修复提交（PR #11 rebase 合并 9d49204）：主包 tests/ 363；D1-R6 gate 缺交折算 UNKNOWN 补 3 例后 503（PR #14）；D1-R7 非有限数值闸收口 +5、二轮 duck 包转 +1、三轮费率卡整卡 hash +1 后 510/374（PR #16/#18/#19/#20 合入态 c86bafd，eval-gate 在场实测 511）；R7 修复轮（终局补漏）非有限闸推广到 leases/fleet（ttl_seconds/gpu_frac/自取制 lease_ttl 三处 fail-open 收口）+ billing duck 路径 quantity 包转与 occurred_at falsy 强转拒算（含 settle 生效窗预检同类扩面）补 7 例 + 二批（promotion score_hook NaN 静默放行收口 + heartbeat_ttl NaN 僵尸节点收口）补 2 例后 520/383：`providers/e2b_compat` 41、`tools/console-tui` 87 含 timeline、`tools/spec-gate/tests` 9，四件套同会话收集，eval-gate 在场 520 passed + 0 skipped / CI 不装 eval-gate 519 passed + 1 skipped）。〔勘误史〕收口提交 bc12adf 曾写 320/76（误用合并前 ae23962 树实测）；v2.1 报告 R1-D3 勘误另记 356/82（与同树复跑不符）；D4-R3 提交 a0f4fd3 漏更本节（497 实测仍写 495）；D1-R6/R7 棘轮只更 ci.yml 头注未同步本行（R7 修复轮补记）——均以本节数字为准。合并态收口=上表三分支 merge commit 已在 main（本仓可独立复核，不依赖台账现态）；CNB 台账行"分支卫生收口"（8e431b26）的收口 SQL 随 company-ops `ops/sql/009`〔待应用，跟踪见其 docs/db-state-registry.md〕。（2026-09-28 D6-R3：原「见 glue 台账行」指针在行收口前会误读为未并，据此改写。）

## Postgres DDL

`sql/001_glue_objects.sql` + `sql/002_glue_v2.sql`（可重复执行，Python 层为第一道闸、DDL 约束/触发器为第二道闸）：

- `glue.budget_lease` / `glue.lease_event`（占用留痕 + 超额触发器）
- `glue.evidence` / `glue.evidence_transition`（前向迁移 + 内容冻结触发器）
- `glue.capability_version` / `glue.capability_metric_event` / `glue.capability_metrics` 视图（准入证据触发器 + 只追加指标）
- `glue.team_task` / `glue.task_transition` / `glue.message_receipt` / `glue.rule_violation`（source 白名单把消息/对话历史挡在任务事实之外）
- `002_glue_v2.sql`：`glue.guardrail_run` / `glue.guardrail_check_result` / `glue.challenge` / `glue.decision_record`（append-only 触发器）/ `glue.promotion_record` / `glue.artifact_route` / `glue.node`；并对 001 既有表幂等 `ALTER ... ADD COLUMN IF NOT EXISTS tenant_id`。消费方：jiuwen-glue 各模块 + Wave2 fleet 调度器 / 控制台 TUI。

运维权威副本在 CNB `company-ops` 仓库 `ops/sql/`（私有）；两处内容一致。DDL 落生产库由 srv-1 工单执行。

## 状态（如实）

- WO-0002：M0 骨架（README / LICENSE / .gitignore）。
- WO-0003 第 3-5 步：leases / evidence / capabilities / rules 四模块 + 三铁律失败用例 + 001 DDL + E2B 兼容差距报告（M0 已交付，独立审计复跑 26 测试通过）。
- **WO-0003 返工（M0.5 前置，本次交付）**：按 M0 独立审计发现 1 补齐 GuardrailRun 协议聚合、记忆晋升管线、决策记录三模块；落地 v1.7 §12.5 JIT 身份三件（三层复合身份 / 权限交集公式进租约签发路径 / Challenge 结构化对象）；tenant_id 全对象覆盖；便宜三件 Python 侧（artifact_routes + nodes 容量模型，environments/ YAML 侧在 CNB company-ops 后续工单）；研发手册四边界核对文档。91 项测试全绿（26 项既有不破坏 + 65 项新增）。
- **Wave2 合并（本次交付）**：四开发分支按 fleet → decision → e2b → tui 顺序 `--no-ff` 合并进 main（远端分支保留评审留档）；主包 196 项测试全绿，e2b_compat 41 项、console-tui 56 项子包测试各自全绿。能力明细见上文「Wave2 能力」节。如实边界：决策层 ModelBackend 只留形状未发真实模型调用；e2b_compat 为 mock 级交付（[待云沙箱实测]）；fleet P2/P3 只做设计（docs/fleet-design.md）。
- 未做 / 不在本包：DDL 落生产库（srv-1 工单）、environments/ 与 pipelines/ YAML（company-ops 流水线工单）、沙箱/网关/编排等一律用 openJiuwen 原生。

## License

Apache-2.0
