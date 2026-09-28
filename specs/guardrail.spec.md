# Spec: jiuwen_glue.guardrail.aggregate（聚合语义 + 逐条可判定 rubric）

- ref: jiuwen_glue.guardrail.aggregate
- semver: 1.0.1
- 实现文件: src/jiuwen_glue/guardrail.py
- 来源: 建设方案 v2.0 §5.2（资产层：spec = 逐条可判定 rubric，REQ-xxx 带正反例）
- 基线: main（含 W-01 缺陷#2 修复：聚合空集 fail-closed；**1.0.0 = R5/D1 修复：非规范判定值收敛 UNKNOWN**——0.3.0 的 `aggregate(["junk"]) == PASS` 是 fail-open 边界，违反原则 2，breaking bump）

## 机器可读例格式约定

- REQ-G-01 至 REQ-G-04 与 REQ-G-14 的每条例行必须是单行，且全等匹配：`- 例: aggregate(<列表字面量>) -> <裁决>`。`<裁决>` 只能是 `PASS`、`BLOCKED`、`UNKNOWN` 三者之一。`<列表字面量>` 只能是 `[]`，或由 `PASS`、`BLOCKED`、`UNKNOWN` 组成的列表，或（仅 REQ-G-14）由带引号的任意字符串组成的列表；多个元素之间用逗号加一个空格分隔。
- 上述正例：该列表入参的实际返回值必须与箭头右侧全等。上述反例：该列表入参的实际返回值不得与箭头右侧全等。判定深度 = 机器执行（spec-gate 与 CI 环节9 对冻结契约实现逐条断言）。
- REQ-G-05 至 REQ-G-13 的每条例行必须是单行，且匹配：`- 例: <场景> -> <期望>`。场景与期望均非空，两侧内部不得再出现 ` -> `。判定深度 = 结构校验 + tests/test_guardrail.py 对应断言。
- 裁决词只使用大写的 `PASS`、`BLOCKED`、`UNKNOWN`。正例给出实现必须达到的终态，反例给出实现不得产生的终态。

## REQ 目录（逐条可判定）

### REQ-G-01 空列表聚合为 UNKNOWN
- 规则: `aggregate` 的入参为空列表时，返回值必须为 UNKNOWN。空列表的返回值不得为 PASS。
- 依据: guardrail.py:75-93；建设方案 v2.0 §3.2；W-01 缺陷#2
- 正例:
  - 例: aggregate([]) -> UNKNOWN
- 反例:
  - 例: aggregate([]) -> PASS
- 判定: 调用 `aggregate([])`。实际返回值必须为 UNKNOWN。实际返回值不得为 PASS，不得为 BLOCKED。

### REQ-G-02 任一 BLOCKED 聚合为 BLOCKED
- 规则: 入参列表中只要有一个元素为 BLOCKED，`aggregate` 必须返回 BLOCKED。列表中同时存在 UNKNOWN 或 PASS 时，返回值仍必须为 BLOCKED。
- 依据: guardrail.py:75-93
- 正例:
  - 例: aggregate([BLOCKED]) -> BLOCKED
  - 例: aggregate([BLOCKED, UNKNOWN]) -> BLOCKED
  - 例: aggregate([UNKNOWN, BLOCKED]) -> BLOCKED
  - 例: aggregate([PASS, BLOCKED]) -> BLOCKED
  - 例: aggregate([PASS, UNKNOWN, BLOCKED]) -> BLOCKED
- 反例:
  - 例: aggregate([BLOCKED, UNKNOWN]) -> UNKNOWN
  - 例: aggregate([BLOCKED]) -> PASS
  - 例: aggregate([BLOCKED, UNKNOWN]) -> PASS
  - 例: aggregate([PASS, UNKNOWN, BLOCKED]) -> UNKNOWN
- 判定: 逐条执行本条正例与反例的 `aggregate` 调用。正例的实际返回值必须与箭头右侧全等。反例的实际返回值不得与箭头右侧全等。

### REQ-G-03 无 BLOCKED 且存在 UNKNOWN 时聚合为 UNKNOWN
- 规则: 入参列表中不存在 BLOCKED，且至少存在一个 UNKNOWN 时，`aggregate` 必须返回 UNKNOWN。
- 依据: guardrail.py:75-93
- 正例:
  - 例: aggregate([UNKNOWN]) -> UNKNOWN
  - 例: aggregate([PASS, UNKNOWN]) -> UNKNOWN
  - 例: aggregate([UNKNOWN, PASS]) -> UNKNOWN
  - 例: aggregate([UNKNOWN, PASS, UNKNOWN]) -> UNKNOWN
- 反例:
  - 例: aggregate([PASS, UNKNOWN]) -> PASS
  - 例: aggregate([UNKNOWN]) -> PASS
  - 例: aggregate([PASS, UNKNOWN]) -> BLOCKED
  - 例: aggregate([UNKNOWN]) -> BLOCKED
- 判定: 逐条执行本条正例与反例的 `aggregate` 调用。正例的实际返回值必须与箭头右侧全等。反例的实际返回值不得与箭头右侧全等。

### REQ-G-04 全部元素为 PASS 时聚合为 PASS
- 规则: 入参列表非空，且每一个元素都是 PASS 时，`aggregate` 必须返回 PASS。
- 依据: guardrail.py:75-93
- 正例:
  - 例: aggregate([PASS]) -> PASS
  - 例: aggregate([PASS, PASS]) -> PASS
  - 例: aggregate([PASS, PASS, PASS]) -> PASS
- 反例:
  - 例: aggregate([PASS, PASS]) -> UNKNOWN
  - 例: aggregate([PASS]) -> UNKNOWN
  - 例: aggregate([PASS, PASS]) -> BLOCKED
  - 例: aggregate([PASS]) -> BLOCKED
- 判定: 逐条执行本条正例与反例的 `aggregate` 调用。正例的实际返回值必须与箭头右侧全等。反例的实际返回值不得与箭头右侧全等。

### REQ-G-05 executable 仅在裁决为 PASS 时为 True
- 规则: `GuardrailResult.executable` 为 True 当且仅当该结果的裁决值为 PASS。裁决值为 BLOCKED 时 `executable` 必须为 False。裁决值为 UNKNOWN 时 `executable` 必须为 False。
- 依据: guardrail.py:165-168
- 正例:
  - 例: gate 裁决为 PASS 时读取 executable -> True
  - 例: gate 裁决为 UNKNOWN 时读取 executable -> False
  - 例: gate 裁决为 BLOCKED 时读取 executable -> False
- 反例:
  - 例: gate 裁决为 UNKNOWN 时读取 executable -> True
  - 例: gate 裁决为 BLOCKED 时读取 executable -> True
- 判定: 结构校验 + tests/test_guardrail.py 对应断言

### REQ-G-06 OPEN 且未固化的 run 上 gate 返回 UNKNOWN
- 规则: run 状态为 OPEN 且尚未完成携带 seal_ref 的 finalize 时，`gate` 的裁决必须为 UNKNOWN。此时已提交 check 的取值不论为 PASS、BLOCKED 还是 UNKNOWN，`gate` 的裁决仍必须为 UNKNOWN。
- 依据: guardrail.py:311-317；guardrail.py:300-323
- 正例:
  - 例: run 状态为 OPEN 且未完成携带 seal_ref 的 finalize 时调用 gate -> UNKNOWN
  - 例: run 状态为 OPEN、未固化、且已提交 check 全为 PASS 时调用 gate -> UNKNOWN
- 反例:
  - 例: run 状态为 OPEN 且未完成携带 seal_ref 的 finalize 时调用 gate -> PASS
  - 例: run 状态为 OPEN、未固化、且已提交 check 全为 PASS 时调用 gate -> PASS
- 判定: 结构校验 + tests/test_guardrail.py 对应断言

### REQ-G-07 VOID run 的 gate 恒为 UNKNOWN 且 void 幂等
- 规则: run 状态为 VOID 时，`gate` 的裁决必须为 UNKNOWN。对状态已是 VOID 的 run 再次调用 void，不得抛出 GuardrailStateError，且调用结束后状态仍为 VOID。
- 依据: guardrail.py:289-296
- 正例:
  - 例: run 状态为 VOID 时调用 gate -> UNKNOWN
  - 例: 对状态已是 VOID 的 run 再次调用 void -> 不抛出 GuardrailStateError 且状态仍为 VOID
- 反例:
  - 例: run 状态为 VOID 时调用 gate -> PASS
  - 例: run 状态为 VOID 时调用 gate -> BLOCKED
  - 例: 对状态已是 VOID 的 run 再次调用 void -> GuardrailStateError
- 判定: 结构校验 + tests/test_guardrail.py 对应断言

### REQ-G-08 必填 check 未提交时 missing_required 非空，无已提交 BLOCKED 则 gate 为 UNKNOWN
- 规则: run 上存在至少一个 `required` 为 True 且尚未提交的 check 时，`missing_required` 必须包含每一个此类 check_id，因此 `missing_required` 非空。缺交折算为 UNKNOWN **参与同一聚合**而非短路（R6/D1 与 aggregate() 一票否决同序）：已提交 check 中不存在 BLOCKED 时 `gate` 裁决必须为 UNKNOWN；已存在至少一个已提交 check 裁决为 BLOCKED 时 `gate` 裁决必须为 BLOCKED（BLOCKED 吸收——补齐缺交不可能翻转结论）。
- 依据: guardrail.py:320-328（gate 聚合：missing 折算 UNKNOWN 入参）；guardrail.py:75-93
- 正例:
  - 例: 存在 check_id 为 c1 且 required 为 True 的 check 未提交且已提交 check 无 BLOCKED 时调用 gate -> UNKNOWN 且 missing_required 含 c1
  - 例: 存在 check_id 为 c1 且 required 为 True 的 check 未提交且另有已提交 check 裁决为 BLOCKED 时调用 gate -> BLOCKED 且 missing_required 含 c1
- 反例:
  - 例: 存在 required 为 True 的 check 未提交时调用 gate -> PASS
  - 例: 存在 check_id 为 c1 且 required 为 True 的 check 未提交时调用 gate -> UNKNOWN 且 missing_required 为空
  - 例: 存在 check_id 为 c1 且 required 为 True 的 check 未提交时调用 gate -> UNKNOWN 且 missing_required 不含 c1
  - 例: 存在 required 为 True 的 check 未提交且另有已提交 check 裁决为 BLOCKED 时调用 gate -> UNKNOWN
- 判定: 结构校验 + tests/test_guardrail.py 对应断言

### REQ-G-09 required 为 False 的未提交 check 不阻断聚合
- 规则: `required` 为 False 且未提交的 check，其 check_id 不得出现在 `missing_required` 中。全部 `required` 为 True 的 check 均已提交且裁决为 PASS 时，仅因另有 `required` 为 False 的 check 未提交，`gate` 的裁决必须为 PASS，且 `missing_required` 必须为空。
- 依据: guardrail.py:327-330
- 正例:
  - 例: 全部 required 为 True 的 check 已提交且裁决为 PASS，另有 check_id 为 opt 且 required 为 False 的 check 未提交时调用 gate -> PASS 且 missing_required 为空
- 反例:
  - 例: 全部 required 为 True 的 check 已提交且裁决为 PASS，另有 required 为 False 的 check 未提交时调用 gate -> UNKNOWN
  - 例: 全部 required 为 True 的 check 已提交且裁决为 PASS，另有 check_id 为 opt 且 required 为 False 的 check 未提交时调用 gate -> PASS 且 missing_required 含 opt
- 判定: 结构校验 + tests/test_guardrail.py 对应断言

### REQ-G-10 ASK 仅 permission_rail 可提交
- 规则: 提交裁决 ASK 时，该 check 的 backend 必须为 permission_rail。backend 不是 permission_rail 时，该次提交必须抛出 GuardrailSchemaError，ASK 不得写入该 check 的提交记录，Challenge 条数不得增加。backend 为 permission_rail、且 run 已绑定 ChallengeBoard 时，该次提交写入的裁决必须为 UNKNOWN，并且 Challenge 条数必须比提交前增加 1。backend 为 permission_rail 但 run 未绑定 ChallengeBoard 时，该次提交必须抛出 GuardrailStateError，该 check 不得写入提交记录，Challenge 条数不得增加。
- 依据: guardrail.py:246-264；guardrail.py:221-270
- 正例:
  - 例: backend 为 permission_rail 且 run 已绑定 ChallengeBoard 时提交 ASK -> 该 check 写入的裁决为 UNKNOWN 且 Challenge 条数增加 1
  - 例: backend 为 permission_rail 且 run 未绑定 ChallengeBoard 时提交 ASK -> GuardrailStateError 且该 check 无提交记录且 Challenge 条数不增加
  - 例: backend 不是 permission_rail 时提交 ASK -> GuardrailSchemaError 且该 check 无提交记录且 Challenge 条数不增加
- 反例:
  - 例: backend 不是 permission_rail 时提交 ASK -> 该 check 存在提交记录
  - 例: backend 为 permission_rail 且 run 未绑定 ChallengeBoard 时提交 ASK -> 该 check 存在提交记录
  - 例: backend 为 permission_rail 且 run 已绑定 ChallengeBoard 时提交 ASK -> 该 check 写入的裁决为 PASS
  - 例: backend 为 permission_rail 且 run 已绑定 ChallengeBoard 时提交 ASK -> Challenge 条数不增加
- 判定: 结构校验 + tests/test_guardrail.py 对应断言

### REQ-G-11 FINALIZED 后拒绝 submit_check 与重复 finalize
- 规则: run 状态变为 FINALIZED 之后，`submit_check` 必须抛出 GuardrailStateError。对已是 FINALIZED 的 run 再次调用 finalize 必须抛出 GuardrailStateError。finalize 必须携带 seal_ref。对状态为 OPEN 的 run，携带 seal_ref 调用 finalize 后，状态必须为 FINALIZED，且 run 记录的 seal_ref 必须与入参全等。对状态为 OPEN 的 run，调用 finalize 时未提供 seal_ref，结束后状态不得为 FINALIZED。
- 依据: guardrail.py:232-236,274-285
- 正例:
  - 例: 状态为 OPEN 的 run 以 seal_ref 值 s1 调用 finalize -> 状态为 FINALIZED 且 run 记录的 seal_ref 为 s1
  - 例: 状态为 FINALIZED 的 run 调用 submit_check -> GuardrailStateError
  - 例: 状态为 FINALIZED 的 run 再次调用 finalize -> GuardrailStateError
  - 例: 状态为 OPEN 的 run 调用 finalize 时未提供 seal_ref -> 状态不为 FINALIZED
- 反例:
  - 例: 状态为 FINALIZED 的 run 调用 submit_check -> 该 check 存在提交记录
  - 例: 状态为 FINALIZED 的 run 再次调用 finalize -> 不抛出 GuardrailStateError
  - 例: 状态为 OPEN 的 run 调用 finalize 时未提供 seal_ref -> 状态为 FINALIZED
- 判定: 结构校验 + tests/test_guardrail.py 对应断言

### REQ-G-12 重复 check_id、未知 backend、未声明 check_id 抛出 GuardrailSchemaError
- 规则: 下列三种情况必须抛出 GuardrailSchemaError。第一，同一个 spec 中两个 check 的 check_id 相同，构造不得返回可用 spec。第二，check 的 backend 不在模块声明的合法 backend 集合（native_guardrail / permission_rail / eval_gate / scan）中，构造不得返回可用 spec。第三，`submit_check` 的 check_id 不在该 run 的 spec 已声明 check_id 集合中，该 check_id 不得进入已提交集合。
- 依据: guardrail.py:109-114,129-139,237-243；guardrail.py:49-54
- 正例:
  - 例: 构造 spec 时两个 check 的 check_id 相同 -> GuardrailSchemaError
  - 例: check 的 backend 不在合法 backend 集合中 -> GuardrailSchemaError
  - 例: submit_check 的 check_id 不在该 run 的 spec 已声明 check_id 集合中 -> GuardrailSchemaError 且该 check_id 不在已提交集合中
- 反例:
  - 例: 构造 spec 时两个 check 的 check_id 相同 -> 构造返回 spec 对象
  - 例: check 的 backend 不在合法 backend 集合中 -> 构造返回 spec 对象
  - 例: submit_check 的 check_id 不在该 run 的 spec 已声明 check_id 集合中 -> 该 check_id 出现在已提交集合中
- 判定: 结构校验 + tests/test_guardrail.py 对应断言

### REQ-G-13 create_run 深拷贝并冻结 spec
- 规则: `create_run` 必须对入参 spec 做深拷贝，把副本绑定到新 run，并冻结该副本。返回时，run 绑定的 spec 与入参 spec 的对象标识必须不同，且其中 check_id 序列必须与调用时刻的入参全等。返回之后修改入参 spec 的顶层字段，或修改其 checks 中任一 CheckSpec 的字段，run 绑定副本上的对应字段必须仍等于调用时刻的值。
- 依据: guardrail.py:210-217
- 正例:
  - 例: 调用 create_run(spec) -> run 绑定的 spec 与入参 spec 对象标识不同且 check_id 序列与调用时刻全等
  - 例: create_run(spec) 返回后把入参 spec 中首个 check 的 required 从 True 改为 False -> run 绑定 spec 中该 check 的 required 仍为 True
- 反例:
  - 例: 调用 create_run(spec) -> run 绑定的 spec 与入参 spec 对象标识相同
  - 例: create_run(spec) 返回后把入参 spec 中首个 check 的 required 从 True 改为 False -> run 绑定 spec 中该 check 的 required 为 False
- 判定: 结构校验 + tests/test_guardrail.py 对应断言

### REQ-G-14 非规范判定值聚合为 UNKNOWN（不落默认分支放行）
- 规则: 入参列表中不存在 BLOCKED，且至少存在一个既非 PASS 也非 BLOCKED 的值（一切非规范字符串，含小写变体、带空白、空串；R5/D1 修复前此类值落入默认分支返回 PASS——fail-open）时，`aggregate` 必须返回 UNKNOWN。与 BLOCKED 并存时仍必须返回 BLOCKED（一票否决优先）。
- 依据: guardrail.py:75-93；建设方案 v2.0 原则 2（Fail-closed 无例外）；R5/D1 修复（与 eval-gate gate.aggregate coerce→UNKNOWN 语义对齐）
- 正例:
  - 例: aggregate(["junk"]) -> UNKNOWN
  - 例: aggregate(["PASS", "junk"]) -> UNKNOWN
- 反例:
  - 例: aggregate(["junk"]) -> PASS
  - 例: aggregate(["junk", "BLOCKED"]) -> UNKNOWN
- 判定: 逐条执行本条正例与反例的 `aggregate` 调用。正例的实际返回值必须与箭头右侧全等。反例的实际返回值不得与箭头右侧全等。tests/test_guardrail.py 另有大小写/空白/None 变体断言。
