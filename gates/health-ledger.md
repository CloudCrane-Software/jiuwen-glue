# 门禁健康度证据台账（GP §6.3）

> **定位**：与 `gates/registry.yaml` 配套——注册表管「规则是否被执行」，健康台账管「被执行的规则是否有效」。
> **版本**：v0.9（初始盘点版，随注册表同版本迭代）
> **红线**：LLM 评分只作 advisory 不进优化回路（R1）；评分字段全部标注来源 `probed/declared/doc/pending`。

---

## 1. 月度合成缺陷注入机制

### 1.1 目的

验证「该拦的还拦得住」——门禁健康度不能只看拦截量，必须定期注入已知缺陷并确认门禁能检出。

### 1.2 流程

```
月始
  ↓
合成缺陷注入（synthetic defect injection）
  - 来源 A：上月生产事件（revert / 事故 / CVE）中未覆盖的 fail-open 路径
  - 来源 B：spec-gate D2b 变异体存活路径（若某变异体连续 2 月存活 → 升级为合成缺陷）
  - 来源 C：红队 thought 草稿中已否证但未 asset 化的 borderline 路径
  ↓
全部门禁跑一轮（T0+T1+T2 shadow 全量）
  ↓
统计月拦截数（unique_captures 月增量）+ 误报数（false_positives 月增量）
  ↓
判定：月拦截 < 2 且 成本超限（T2 成本 > 月预算）→ 触发降级
```

### 1.3 降级触发条件（v0.9 草案）

| 条件 | 动作 | 棘轮 |
|------|------|------|
| 月拦截 < 2 且 T2 成本超限 | 该门禁 mode ← shadow；下月复审 | 不可逆（shadow→blocking 须 data 达标） |
| 月拦截 < 2 且 T1 成本超限 | 该门禁 tier ← T2（降档不降层） | 不可逆 |
| 月拦截 ≥ 2 且成本在预算内 | 维持现状 | — |
| 连续 3 月拦截 < 2 | 该门禁进入 review 队列；owner 裁定保留或下线 | — |

**降级 ≠ 删除**：门禁条目保留在 `gates/registry.yaml`，mode/tier 变更留痕于本台账。

### 1.4 当前状态

v0.9 阶段尚无生产月拦截数据，所有门禁初始状态为 `pending`。首次月度合成缺陷注入计划随 v1.0 注册表上线时启动。

---

## 2. 门禁初始台账

> **来源标注纪律**（v2.0 / GP 通篇）：
> - `probed` = 实机/实测数据（ci.yml 注释 / spec_gate.py 运行报告 / tests 断言）
> - `declared` = 声明未验证（spec docstring / README）
> - `doc` = 官方文档引用
> - `pending` = 尚无数据

### 2.1 T0 秒级

| gate_id | title | tier | mode | unique_captures | false_positives | last_capture | exemption (TTL) | 来源 |
|---------|-------|------|------|-----------------|-----------------|--------------|-----------------|------|
| spec-gate-static | spec-gate 静态一致性 | T0 | blocking | pending | pending | — | 无 | pending |
| contract-hash-check | 契约 hash 秒检 | T0 | blocking | `probed: 1 条全部一致` | `0` | 2026-09-28 | 无 | ci.yml D1-R5 |
| registry-drift-check | 注册表漂移校验 | T0 | blocking | pending | pending | — | 无 | pending |

### 2.2 T1 分钟级

| gate_id | title | tier | mode | unique_captures | false_positives | last_capture | exemption (TTL) | 来源 |
|---------|-------|------|------|-----------------|-----------------|--------------|-----------------|------|
| ci-pytest-suite | glue pytest 全量 | T1 | blocking | `probed: 16（D1-R8 两批）` | `0` | 2026-09-29 | 无 | ci.yml D1-R8 |
| guardrail-aggregate | GuardrailRun 聚合语义 | T1 | blocking | `probed: 4（W-01/R5/R6/D1-R7 终局）` | `0` | 2026-09-28 | 无 | test_guardrail.py 14 tests |
| lease-semantics | 租约语义 | T1 | blocking | `probed: 1（W-01 缺陷#1）` | `0` | 2026-09-27 | 无 | test_lease.py |
| decision-layer | 决策层三原语 | T1 | blocking | `probed: 5+2（D1-R7 浮点参数）` | `0` | 2026-09-28 | 无 | test_decision.py |
| admission-mirror | admission 镜像 | T1 | blocking | `probed: 1（D1-R7 终局 workers）` | `0` | 2026-09-28 | 无 | test_admission.py |
| challenge-ask | Challenge ASK 授权三态 | T1 | blocking | pending | pending | — | 无 | pending |
| promotion-gate | 晋升三关 | T1 | blocking | `probed: 1（D1-R7 score_hook NaN）` | `0` | 2026-09-28 | 无 | test_promotion.py |
| selection-formula | 择优公式 | T1 | blocking | `probed: 1（D1-R8 巨型 int）` | `0` | 2026-09-29 | 无 | test_selection.py |
| fleet-scheduler | fleet 调度 | T1 | blocking | `probed: 2（D1-R7 gpu_frac + D1-R8 巨型 int）` | `0` | 2026-09-29 | 无 | test_fleet_*.py |
| billing-usage | 计费与用量 | T1 | blocking | `probed: 5（D1-R7 四类 + duck 包转）` | `0` | 2026-09-28 | 无 | test_billing.py+test_usage.py |
| iron-rules | 铁律三件套 | T1 | blocking | `probed: 2（D1-R8 依赖核验+租户贯通）` | `0` | 2026-09-29 | 无 | test_iron_rules.py |
| statebuilder | 状态装配器 | T1 | blocking | `probed: 1（D1-R8 巨型 int）` | `0` | 2026-09-29 | 无 | test_statebuilder.py |
| finite-value-guard | 有限值跨模块闸 | T1 | blocking | `probed: 11（D1-R7/D1-R8 轮）` | `0` | 2026-09-29 | 无 | 多模块测试 |
| escalation-finite | 升级 time_budget 有限闸 | T1 | blocking | `probed: 1（D1-R7）` | `0` | 2026-09-28 | 无 | test_escalation.py |
| tenant-inheritance | 租户贯通 | T1 | blocking | `probed: 1（D1-R8）` | `0` | 2026-09-29 | 无 | test_tenant_id.py |
| platform-tokens | 平台令牌 | T1 | blocking | pending | pending | — | 无 | pending |
| meta-governance | 元治理 schema | T1 | blocking | pending | pending | — | 无 | pending |

### 2.3 T2 抽样 / 影子

| gate_id | title | tier | mode | unique_captures | false_positives | last_capture | exemption (TTL) | 来源 |
|---------|-------|------|------|-----------------|-----------------|--------------|-----------------|------|
| spec-gate-d1-determinism | D1 双实现一致率 | T2 | shadow | `probed: 0（100% 一致，无 divergence）` | `0` | 2026-09-29 | 无 | spec_gate.py D1 |
| spec-gate-d2-lethality | D2 双向变异杀死率 | T2 | shadow | `probed: 10（D2a 全杀 + D2b 8/8）` | `0` | 2026-09-29 | 无 | spec_gate.py D2 |
| spec-gate-d4-stability | D4 零抖动 | T2 | shadow | `probed: 0（identical=True）` | `0` | 2026-09-29 | 无 | spec_gate.py D4 |
| spec-gate-d5-redproof | D5 红证明 | T2 | shadow | `probed: 6（6 条 agg-red-* 全复现）` | `0` | 2026-09-28 | 无 | spec_gate.py D5 |
| spec-gate-d6-outcome | D6 结局一致性 | T2 | shadow | pending | pending | — | 无 | pending（outcome_cases.jsonl 待回流） |

### 2.4 L0 写码时镜像（shadow，不裁决）

| gate_id | title | tier | mode | unique_captures | false_positives | last_capture | exemption (TTL) | 来源 |
|---------|-------|------|------|-----------------|-----------------|--------------|-----------------|------|
| lint-type-format | L0 lint/类型/格式 | T0 | shadow | pending | pending | — | 无 | pending（待接线） |
| stop-hook-affected-tests | L0 Stop hook 预跑 | T1 | shadow | pending | pending | — | 无 | pending（待接线） |

### 2.5 L5 合入后扫描

| gate_id | title | tier | mode | unique_captures | false_positives | last_capture | exemption (TTL) | 来源 |
|---------|-------|------|------|-----------------|-----------------|--------------|-----------------|------|
| post-merge-night-scan | 夜扫快档 | T1 | shadow | pending | pending | — | 无 | pending（待接线） |
| post-merge-weekly-scan | 周扫重档 | T2 | shadow | pending | pending | — | 无 | pending（待接线） |
| post-merge-event-scan | 事件触发重扫 | T0 | shadow | pending | pending | — | 无 | pending（待接线） |

---

## 3. 豁免物账目

v0.9 阶段无豁免物。豁免物登记格式：

```
- gate_id: <gate>
  reason: <为什么豁免>
  TTL: <到期日期>
  approved_by: <approver>
  status: active / expired
```

---

## 4. 月度复审记录

| 月份 | 月拦截合计 | 降级动作 | 备注 |
|------|-----------|---------|------|
| 2026-09 | N/A（v0.9 首月，无生产数据） | 无 | 初始盘点版；首次月度注入计划随 v1.0 启动 |
| — | — | — | — |

---

## 5. 与 registry.yaml 的引用关系

本台账每行以 `gate_id` 锚定 `gates/registry.yaml` 对应条目；注册表变更（mode/tier 降级）须同步更新本表对应行的 `tier` / `mode` / `last_capture`，保持两侧一致。
