# theory.guardrail contract（契约 / 接口冻结声明）

> theory 圈文档：纯 Markdown，不 import 任何圈的代码（见 `theory/README.md` 边界声明）。
> 契约语义依蓝图 v2.0 §5.2：**契约 = `(ref, semver) → sha256` 注册表，冻结接口不冻结实现**。

## §0 元数据

| 项 | 值 |
| --- | --- |
| ref | `theory.jiuwen_glue.guardrail` |
| semver | **0.1.0-draft**（owner Approval 定稿后转 0.1.0，进入契约注册表） |
| 状态 | draft【待 owner Approval——蓝图 v2.0 §7：唯一人工写入点 = theory.Approval】 |
| 冻结对象 | `src/jiuwen_glue/guardrail.py` 的公开 API 面 + 两条写死不变式 |
| 不冻结 | 实现内部（存储结构、私有方法、错误消息文案） |
| 实现基线 | main=7895c23，blob `33ee3a1bdd3561c340ee99470384ca14730edf5f`（guardrail.py，2026-09-27 登记；定稿时以定稿 commit 重算）【待：契约注册表（(ref, semver)→sha256 落库）建成后迁入，蓝图 §5.2】 |

## §1 冻结的接口面（GuardrailRunStore 与 aggregate）

以下签名即冻结契约；参数名、语义与抛错行为均属接口面：

```python
aggregate(verdicts: Iterable[str]) -> str
# 聚合纯函数。冻结不变式见 §2-I1/I2。

GuardrailRunStore.create_run(spec: GuardrailSpec) -> GuardrailRun
# 步骤 1+2：提交动作上下文并固化（spec 深拷贝冻结，之后不可改）。

GuardrailRunStore.submit_check(run_id, check_id, outcome, *, evidence_ref="", note="") -> GuardrailRun
# 步骤 3：outcome ∈ {PASS, BLOCKED, UNKNOWN, ASK}；ASK 仅限 backend=permission_rail
# 且经 ChallengeBoard 生成结构化 Challenge，check 结论落为 UNKNOWN（fail-closed）。

GuardrailRunStore.finalize(run_id, *, seal_ref: str) -> GuardrailRun
# 步骤 4：验收固化。seal_ref 必填；固化后结论只读，重开必须建新 run。

GuardrailRunStore.void(run_id, *, reason: str) -> GuardrailRun
# 关键状态变化 → 原有结论失效；此后 gate 恒 UNKNOWN。幂等（重复 void 返回原 run）。

GuardrailRunStore.gate(run_id) -> GuardrailResult
# 步骤 5：执行前唯一门控查询。GuardrailResult.executable 只在 verdict==PASS 时为 True。

GuardrailRunStore.pending_challenges(run_id) -> List[Challenge]
# 该 run 发出且仍 pending 的 Challenge（治理面 ask 审批队列联动）。
```

数据对象（冻结字段）：

- `CheckSpec(check_id, backend, declaration, required=True)`；`backend ∈ {native_guardrail, permission_rail, eval_gate, scan}`
- `GuardrailSpec(action, resource, agent_identity_ref, env_ref, tenant_id, checks, spec_version)`——check_id 不得重复
- `CheckResult(check_id, verdict, evidence_ref, challenge_id, note, submitted_at)`
- `GuardrailResult(run_id, verdict, state, per_check, missing_required, checked_at)` + `executable` 属性
- run 状态机：`OPEN → FINALIZED`（终态）、`OPEN|FINALIZED → VOID`（终态）；固化后提交 → `GuardrailStateError`

## §2 写死不变式（任何实现不得违背）

- **I1 聚合 fail-closed**：空集 → UNKNOWN（永不 PASS）；任一 BLOCKED → BLOCKED；否则任一 UNKNOWN → UNKNOWN；否则 PASS。（蓝图 v2.0 §3.2；W-01 缺陷 #2 修复）
- **I2 决策点唯一**：本对象不执行任何检查、不新增第二个决策点——聚合 verdict（PASS/BLOCKED/UNKNOWN）是唯一门控输出；`executable` 只在 PASS 为 True，调用方对 UNKNOWN 必须拒绝执行。（蓝图 v2.0 §0 原则 1/3、§4.3；Handbook §3.3.2）
- **I3 固化不可变**：spec 创建时深拷贝冻结；FINALIZED/VOID 后提交结论被拒绝。（Handbook §3.3.2 步骤 2/4）
- **I4 失效不删除**：void 后 run 保留、gate 恒 UNKNOWN（降级不删除，蓝图 §1.2）。

## §3 兼容性承诺

- 0.x 期间接口可变，但任何变更必须：改本文件 semver + 走变更流程（蓝图 §11）+ spec.md 同步修订（理论 bump → instance_registrar 生成实例建议单，蓝图 §7/§8）。
- 1.0.0 起仅增不破：新增带默认值参数、新增 `BACKENDS` 枚举值允许；删除/改名/改语义为 breaking，禁止。

## §4 一致性验收归属

接口面与不变式的语义一致性对照（oracle）见同目录 `eval.md`；治理语义定义见 `spec.md`。三件套版本联动：本契约 bump 时 spec/eval 必须同票评审（递归一致，蓝图 §0 原则 11）。
