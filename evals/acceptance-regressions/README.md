# evals/acceptance-regressions — 验收七轮已修复真问题回归集（line. 资产）

- 资产类别：**eval 集**（v2.0 §5.2：版本化、**只增不删**）
- 来源：2026-09-27~09-30 七天七轮验收（R1-R7，63→41→43→36→28→17→17 条发现）中**已修复真问题**的回归化（RSI 首航·线3 沉淀）
- 覆盖对象：`jiuwen_glue` 主体（src/guardrail|challenge|leases|fleet|billing|promotion|selection|decision|rules|routes|statebuilder|escalation）+ 边界依赖域（company-ops workers 门控镜像 / GPU 机 jiuwenswarm dotenv 链 / deploy 脚本形状闸）
- 权威去重基准：`docs/acceptance-adjudicated.md`（每条问题的裁决/登记/收口状态以该清单为准；本集只收「已修复」族）

## 纪律（写死，同 guardrail-aggregate 集）

1. **只增不删**：case 失效必须走「失效证明」（`decommissioned.jsonl` 记 case id + 失效证据 + 日期），文件中保留原行并加 `"decommissioned": true`——直接删除即违规。
2. **红证明绑定**：每条 case 的 `kills_impl` 声明它杀死哪类错误实现；修复前树必须能复现断言失败（先行红证口径，参照 D1-R7/R8 各 PR 的「先行红证 N 用例均在各自修前树复现」）。
3. **迁移须留痕**：eval 集迁位必须开工单，`from→to` 转换函数入 `eval_migrations/`。

## case 模式（red_cases.jsonl，JSON Lines，guardrail-aggregate schema 的超集）

| 字段 | 含义 |
| --- | --- |
| `id` | case 稳定 ID（`acc-red-NNN`，只增不复用） |
| `family` | 问题族（nan-fail-open / orphan / tenant / aggregate / giant-int / falsy / hash-scope / dns-prefix / dotenv-override / undeclared-rows） |
| `found` | 发现轨迹：轮次+发现者+时间+锚点（报告/清单条目/红队 thought） |
| `fix` | 修复提交：仓库+PR/commit sha（凡注「squash=」者为合并体哈希） |
| `assertion` | 回归断言（可执行形态：pytest 用例名/探针命令+期望判定） |
| `kills_impl` | 本 case 杀死的错误实现族 |
| `entered` | 入库日期与工单（RSI 首航·线3，2026-09-30） |

## 与测试suite的关系（红绿分离）

- 每条 case 的 assertion 对应 glue `tests/` 下既有回归（PR 附带）或本 README 声明的探针形态；本集是验收发现的**可执行载体**，与 `tests/` 双层引用由后续 spec-gate 静态一致性检查维护（接线归 W-02 收口轮）。
- floor 棘轮史（CI 口径）：499→500→503→509→510→519→527→535（`ci.yml` 头注为活口径）。
