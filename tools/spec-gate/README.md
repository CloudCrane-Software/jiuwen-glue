# tools/spec-gate — line. 资产层元门禁（v2.0 §5.2 六维）

对 glue 仓 spec-kit 模块跑入库门禁。纯标准库零依赖；不 import 生产代码
（参考实现按契约从 `src/jiuwen_glue/guardrail.py` 导入——被检对象本身）。

## 用法

```bash
python tools/spec-gate/spec_gate.py --static-only        # CI 环节9 口径（轻）
python tools/spec-gate/spec_gate.py --out report.md      # 六维全跑（按需，重）
python tools/check_contracts.py                          # 契约 hash 秒检（G7）
```

## 六维首跑状态（W-05，如实）

| 维度 | 状态 |
| --- | --- |
| D1 可判定性（双独立实现一致率 ≥90%） | 已实现：`second_aggregate`（fold 写法，独立编码）对参考实现，穷举输入域（长度 0..4 全列表 121 个）∪ spec 正例 ∪ 红反例 |
| D2 杀伤性（≥80%） | 已实现，双向：D2a spec 正例期望值变异 ×（冻结实现+红反例）oracle；D2b 八个似真误读规则变异体 × eval 集杀死 |
| D3 区分度（真实扇出分离） | **demo 实证，原口径 [待数据]**：红绿分离在合成靶子上演示（参考实现全绿 vs 旧实现/变异体全红）；§5.2 原口径需真实扇出，无场景不虚报 |
| D4 稳定性（零抖动） | 已实现：六维全量计算两次，规范化 JSON 逐字节比对 |
| D5 红证明（新 case 旧实现断言失败） | 已实现：`kills_impl=pre-w01-fail-open` 红反例对 `old_impls.py`（6f4674c 快照，fail-open 缺陷原样保留）复现失败 |
| D6 结局一致性（背离率 <5%） | **[待数据]**：零结局标签样本，无法计算也不得编造 |

## 文件

- `spec_gate.py` — 六维执行器 + 静态一致性检查（CI 环节9 调用 `--static-only`）
- `old_impls.py` — 旧实现快照（红证明靶子，**不可用于生产**）

## 纪律

- spec 例行格式契约：`aggregate(<列表字面量>) -> 三态`（guardrail 可执行例）；
  leases 首跑仅结构校验（判定深度如实写在 spec 头部）。
- eval 集（`evals/guardrail-aggregate/`）只增不删；失效走失效证明；见其 README。
- 本工具属于 line 圈工具链：可读仓库一切资产，不改生产代码、不进运行时依赖。
