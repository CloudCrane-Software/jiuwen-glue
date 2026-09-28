# evals/guardrail-aggregate — guardrail 聚合 eval 集（line. 资产）

- 资产类别：**eval 集**（v2.0 §5.2：资产 = spec / eval 集 / 契约，版本化、**只增不删**）
- 覆盖对象：`jiuwen_glue.guardrail.aggregate`（契约 `contracts/registry.json` 首条，semver 1.0.0（0.3.0→1.0.0 breaking bump = R5/D1：非规范判定值收敛 UNKNOWN，配套 agg-red-006 / REQ-G-14））
- 判定器：`tools/spec-gate/spec_gate.py`（六维元门禁）；同目录 `red_cases.jsonl` 为机读红反例集
- 来源工单：W-05（M3 P0 line. 层 jiuwen-glue 自 dogfood）

## 纪律（写死）

1. **只增不删**：case 失效必须走「失效证明」（在 `decommissioned.jsonl` 记 case id +
   失效证据 + 日期），文件中保留原行并加 `"decommissioned": true` 字段——直接删除即违规。
2. **迁移须留痕**：eval 集迁位（如未来从仓内 evals/ 迁入 eval-assets 服务）必须开
   迁移工单，`from→to` 转换函数入 `eval_migrations/`（v2.0 §5.2）。
3. **红证明绑定**：每条 case 的 `kills_impl` 字段声明它杀死哪类错误实现；
   `kills_impl: "pre-w01-fail-open"` 的 case 必须始终能在旧实现快照上复现断言失败
   （红证明维度，spec-gate 每次运行复检）。旧实现快照存于
   `tools/spec-gate/old_impls.py`（源：git 6f4674c，逐字拷贝，仅作红证明靶子，不可运行于生产）。

## case 模式（red_cases.jsonl，JSON Lines）

| 字段 | 含义 |
| --- | --- |
| `id` | case 稳定 ID（`agg-red-NNN`，只增不复用） |
| `req` | 对应 spec REQ 条目（`specs/guardrail.spec.md`） |
| `input` | aggregate 输入（列表字面量，元素 ∈ PASS/BLOCKED/UNKNOWN） |
| `expect` | 正确聚合输出 |
| `forbidden` | 被禁止的错误输出（红反例语义） |
| `kills_impl` | 本 case 杀死的错误实现族（红证明锚点） |
| `origin` | 语义出处（缺陷实录 / spec 反例的 eval 化） |
| `entered` | 入库日期与工单 |

## 与 spec / 测试的关系（红绿分离，v2.0 §5.5）

- spec（`specs/guardrail.spec.md`）声明语义；本 eval 集是 spec 反例的**可执行载体**；
  `tests/test_guardrail.py` 是其回归形态。三层引用关系由 spec-gate 静态一致性检查维护。
- 红绿流程：eval 先合（新 case 附红证明），实现后合；存量未实现语义以
  `xfail(strict)` 管理（XPASS 即失败，强制摘帽）。复盘演示见 `docs/line-dogfood.md`。
