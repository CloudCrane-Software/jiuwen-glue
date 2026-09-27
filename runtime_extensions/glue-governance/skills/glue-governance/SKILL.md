---
name: glue-governance
description: >-
  当 agent 要执行受治理的动作（发布/部署/配置变更/资源操作）时使用：先建 GuardrailRun 提交各项 check，需人工授权时走 Challenge 审批，执行前查 gate，执行后落决策记录；UNKNOWN 一律不得执行。
---

# glue 治理 SOP

加载本包的 agent 执行受治理动作时遵循本流程。三个工具只转发 `jiuwen_glue`，聚合 verdict 是唯一门控输出。

## 何时使用

下列动作先走门控，再执行：

- 发布与恢复（发版、回滚、流量切回）
- 部署变更（上线、扩缩容、环境切换）
- 配置写入（运行时配置、特性开关、路由与权限配置）
- 资金或资源敏感操作（配额、密钥轮转触发、生产资源变更）

只读查询、与上述动作无关的本地草稿，不建 Run。

## 五步门控流程

对应工具 `glue_guardrail_gate` 的 `op`。顺序不可跳。

1. **create_run**。给出 `action`、`resource`、`agent_identity_ref`，并声明 `checks`。每项 check 含 `check_id` 与 `backend`：
   - `native_guardrail`：原生安全护栏检查
   - `eval_gate`：公司级 eval 门禁
   - `scan`：安全扫描
   - `permission_rail`：权限三态
2. **逐 check submit_check**。`outcome` 取 `PASS` / `BLOCKED` / `UNKNOWN` / `ASK`。`ASK` 只有 `permission_rail` check 可用：工具会自动生成 Challenge，该 check 落 `UNKNOWN`。
3. **有 ASK 时把 `ask_payload` 交给人**。裁决人是 `who_confirms` 对应的人（`user` / `resource_owner` / `duty_officer`），在独立界面完成。Agent 不得代批。可用 `glue_challenge_board` 的 `pending_for` / `state_of` 查队列与状态，不得调用 `resolve` 代替人。
4. **全部 check 有结论后 finalize**。`seal_ref` 引用验收凭证。未 finalize 的 Run 不可执行。
5. **执行前 gate**。只认本次 `gate` 返回的聚合 `verdict`：
   - `verdict=PASS` 才可执行
   - `BLOCKED`、`UNKNOWN`、未 finalize，一律拒绝执行（fail-closed）
   - 现场关键状态变化时用 `void`（带 `reason`）使原结论失效，之后必须重跑，不得沿用旧 PASS

`pending_challenges` 只查本 Run 上待审批的 Challenge，不构成放行。

## 决策留痕

动作执行后，用 `glue_decision_log` 的 `append` 落一条决策记录：

- 必填：`agent_ref`、`context`、`options`、`chosen`、`rationale_ref`
- 可绑：`guardrail_run_ref`（指向刚放行的 Run）
- 账面只存 `context_hash`，上下文原文不落账
- 禁止把密钥、凭据、token 写进 `context` 或 `meta`

`get` / `list` 只读账本。账本记录决策，不做决策。

## 边界

- 三个工具只是 `jiuwen_glue` 的薄封装，不复制其源码，不新增第二个决策点。
- 聚合 `verdict` 是唯一门控输出。工具返回的其他字段不得被解释成放行。
- 授权码与 token 永不经这三个工具出入。批准如何换成可执行凭证，由可信 Runtime 与 Credential Broker 兑换。
- `UNKNOWN` 一律不得执行。过期或终态 Challenge 由 glue 拒绝裁决。

## 工具速查表

| 工具 | op | 必填参数 |
| --- | --- | --- |
| `glue_guardrail_gate` | `create_run` | `action`, `resource`, `agent_identity_ref`（并声明 `checks`） |
| `glue_guardrail_gate` | `submit_check` | `run_id`, `check_id`, `outcome` |
| `glue_guardrail_gate` | `finalize` | `run_id`, `seal_ref` |
| `glue_guardrail_gate` | `void` | `run_id`, `reason` |
| `glue_guardrail_gate` | `gate` | `run_id` |
| `glue_guardrail_gate` | `pending_challenges` | `run_id` |
| `glue_challenge_board` | `open` | `who_confirms`, `resource`, `action`, `method`, `ttl_seconds` |
| `glue_challenge_board` | `resolve` | `challenge_id`, `approved`, `by`（仅 `who_confirms` 对应的人在独立界面调用） |
| `glue_challenge_board` | `pending_for` | `who_confirms` |
| `glue_challenge_board` | `state_of` | `challenge_id` |
| `glue_challenge_board` | `ask_payload` | `challenge_id` |
| `glue_decision_log` | `append` | `agent_ref`, `context`, `options`, `chosen`, `rationale_ref` |
| `glue_decision_log` | `get` | `decision_id` |
| `glue_decision_log` | `list` | （无必填；可按 `agent_ref` 或 `context` 过滤） |
