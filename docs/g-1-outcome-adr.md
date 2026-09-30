# ADR：decision outcome 回填全链路补齐（G-1，2026-09-29）

## 背景与三选一
W-02 复核标注缺口 G-1：v2.0 §4.2 要求决策记录"含 outcome 回填字段"，全链路缺失。三个候选：
- A：独立 outcome 账本表（decision_outcome 表，外键 decision_id）——最灵活但引入第二写入路径与第二真相源，违反"append-only 账本唯一"；
- B（采纳）：**glue 内最小闭环**——DecisionRecord 增受控单字段 outcome（NULL=未回填），record_outcome 单方法一次写入即冻结，DDL 仅 ALTER ADD 列；
- C：决策时预写占位再 UPDATE——破坏 append-only 语义（UPDATE 路径存在即违例），否。

## 裁决（B）与语义
- **一次写入即冻结**：已有 outcome 再回填 → DecisionSchemaError（无改写路径；测试锁定）；
- **结构最小校验**：outcome 必须为含 `outcome`（success|failure|superseded 语义归消费方）与 `evidence_ref` 的映射；
- **接线点**：DecisionLayer.record_outcome 门面——三原语调用方拿到任务结局后显式回填；决策层不推断结局（决策点唯一不外溢）；弃权（None）不落账故无可回填；
- **负边界迁移声明**（v2.0 §3.5）：append-only 方法集唯一性锁（test_decisions.py）显式增补 record_outcome 并注明依据——禁改写由独立测试锁定。

## 交付
- src/jiuwen_glue/decisions.py：DecisionRecord.outcome 字段 + record_outcome；
- src/jiuwen_glue/decision.py：DecisionLayer.record_outcome / outcome_of 门面；
- tests/test_g1_outcome_backfill.py 6 例 + 锁测试增补；全量 587 passed+1 skipped；
- DDL：CNB company-ops ops/sql/**011**_outcome_backfill.sql（ALTER ADD outcome jsonb NULL，幂等；append-only 触发器不受影响——UPDATE 仍被拒，回填经 INSERT 语义由 Python 层承载，DDL 注明）。〔勘误 2026-09-30 终修-仓库与文档一致性-R2：原写 009 与已跟踪 009_team_task_ledger_reconcile.sql 同号两义、010 已被 glue 仓 tools/console-tui/sql/010_console_grants.sql 占用——改号 011 入仓 company-ops（2026-09-29 已应用 srv-1，登记见其 docs/db-state-registry.md 2026-09-30 条）。〕
