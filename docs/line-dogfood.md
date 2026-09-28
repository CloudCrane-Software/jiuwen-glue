# line. 层在 jiuwen-glue 自 dogfood — 资产地图与红绿分离流程（W-05）

> M3 P0（v2.0 §10）：line. 生产线第一套设施落在 glue 仓自己身上（递归 dogfood，§7）。
> 本文是资产地图 + 流程纪律；六维元门禁口径见 `tools/spec-gate/spec_gate.py` 模块 docstring。

## 1. 资产地图（本套设施的构成）

| 资产 | 位置 | 说明 |
| --- | --- | --- |
| spec-kit 首模块 | `specs/guardrail.spec.md`、`specs/leases.spec.md` | 逐条可判定 rubric（REQ-xxx 带正反例），格式契约见各文件头 |
| eval 集 | `evals/guardrail-aggregate/red_cases.jsonl`（+ README） | guardrail 聚合红反例集，`kills_impl` 绑定红证明 |
| 契约注册表 | `contracts/registry.json` | `(ref, semver) → sha256[实现文件]`，首条 `jiuwen_glue.guardrail.aggregate@0.3.0` |
| 契约 hash 秒检 | `tools/check_contracts.py` | G7 快检半边，秒级、纯标准库、fail-closed |
| 元门禁六维 | `tools/spec-gate/spec_gate.py` | §5.2 入库门禁首跑工具（四维实现；D3 原口径/D6 空证据 = BLOCKED 非零退出，不放行——D4-R2 修复） |
| 择优骨架 | `src/jiuwen_glue/selection.py`（`line.selection`） | §5.4 纯机器信号公式 + winner_ref；N>5 护栏；R1 平局豁免 |
| CI 钩子 | CNB company-ops `.cnb.yml` 环节9 | specs 一致性静态检查 + 契约 hash 秒检（六维全跑按需） |

依赖方向自查（§1.2）：`selection` 不 import guardrail、不查 store——只消费 verdict 值快照
（跨组件只传引用，不复制状态）；spec-gate/check_contracts 是工具链，不进运行时。

## 2. 红绿分离流程（v2.0 §5.5，写死）

1. **eval 先合**：新 REQ 的红反例以 eval PR 入库（附红证明：该 case 在现实现/旧实现上
   断言失败的原始输出）；语义尚未实现时，回归测试以 `xfail(strict)` 入库——
   strict 语义 = 一旦实现落地使测试转绿（XPASS），套件立即报错，**强制摘帽转正**，
   不允许红 case 静默变绿后继续挂在 xfail 里。
2. **实现后合**：实现 PR 使 xfail 用例 XPASS → 摘 xfail、转正为常规断言；
   同时 bump spec 版本（如涉及 REQ 语义）与契约 semver + 重算 sha256 重挂注册表。
3. **eval 只增不删**：失效走「失效证明」（`decommissioned.jsonl`），迁移走
   `eval_migrations/`（v2.0 §5.2 纪律）。
4. 门控与择优分离（§0 总则 1）：过不过 = GuardrailRun 聚合（guardrail.py 唯一）；
   选哪个 = `line.selection`（择优唯一决策点）——两个函数，永不合并。

### 2.1 复盘演示（如实声明：复盘形态，非当时真实 PR）

以 W-01 缺陷 #2（`aggregate([]) == PASS`，fail-open）为例演示本流程若已生效时的走法：

- **eval PR（先合）**：`evals/guardrail-aggregate/red_cases.jsonl` 入库
  `agg-red-001`（`[] → UNKNOWN`，`kills_impl: pre-w01-fail-open`），附红证明——
  旧实现（main 6f4674c）实测输出 `PASS` 的断言失败记录；`tests/` 以
  `xfail(strict)` 断言 `aggregate([]) == UNKNOWN`（当时主干返回 PASS，xfail 成立）。
- **实现 PR（后合）**：`guardrail.py` 聚合修复（v2.0 §3.2 语义）→ xfail 用例 XPASS →
  摘帽转正（见 `tests/test_guardrail.py::test_aggregate_semantics`，现为常规断言）。
- **门禁**：spec-gate D5（红证明）至今每次运行都在 `tools/spec-gate/old_impls.py`
  快照上复现该断言失败——缺陷不许复活。

> 实际历史：W-01 修复（commit 7895c23）先于本资产体系落地，上文顺序是流程生效后的
> 重演演示，不是当时的真实 PR 序列。红证明原始事实（旧实现输出 PASS）为实测复盘。

## 3. 元门禁六维运行口径（§5.2）

- **CI 常跑（环节9，轻）**：`spec_gate.py --static-only`——两份 spec 的元数据/REQ 唯一性/
  五要素完整性静态检查 + guardrail 可执行正例与红反例对冻结契约实现逐条断言。
- **按需全跑（重）**：`spec_gate.py` 六维全量（穷举输入域 + 双向变异 + 稳定性双跑 +
  旧实现快照红证明）。**CI 不默认全跑**——变异与穷举对 CI 时长不友好，按需（spec/eval
  变更 PR 或季度复检）在本地/工作机执行并落报告。
- **[待] 项与消除条件**（不虚报；**空证据期间门禁 BLOCKED，不放行**——D4-R2 起
  [待数据] 维度参与退出码，违反 v2.1 原则 2 的旧行为已废弃）：
  - D3 区分度原口径 = 分开过一次**真实扇出**：待 line 流水线扇出生成（后续工单）跑出
    首批真实候选后，用红/绿两组候选各过一次门禁取分离结论；
  - D6 结局一致性 = 与结局标签背离率 <5%：待 usage/事故回填产生结局标签
    （`evals/guardrail-aggregate/outcome_cases.jsonl`，revert 自动转红 case）后计算；
    当前零样本，不得编造。
  - 回归钉：`python -m pytest tools/spec-gate/tests/ -q`（空证据→非零、D6 阈值语义）。

## 4. 契约纪律

- `(ref, semver) → sha256` 冻结**接口**不冻结实现；`breaking_policy` 写在每条 entry。
- hash 秒检是 G7 门禁的快检半边（CI 环节9 每跑）；breaking 分类（评审侧）发生时
  须 bump semver、bump spec 版本、eval 集红绿复核，三者齐才重挂注册表。
