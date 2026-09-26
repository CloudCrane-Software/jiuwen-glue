-- =====================================================================
-- console-tui 只读视图 v1（WO-0012 / PROP-0005，PROP-0001 v1.7 §12.3）
-- 消费方：tools/console-tui/src/console_tui/data.py 的 pg 模式；
--         落库由主 agent 执行（本工单不真连 srv-1 数据库）。
--
-- 依赖：sql/001_glue_objects.sql + ops/sql/002_glue_v2.sql（tenant_id 列、
--       glue.decision_record、glue.challenge、glue.node 必须先建）。
-- 方言：PostgreSQL（jsonb / TIMESTAMPTZ / FILTER），sqlite 不可执行——
--       测试对字段做静态断言（tests/test_sql_views_static.py）。
--
-- 五个视图 ↔ 12.3 五个数据面板：
--   glue.v_task_board        工单看板（含任务级 pause 标记推导）
--   glue.v_active_lease      活跃租约
--   glue.v_pending_challenge ask 审批队列（pending 且未过期；fail-closed）
--   glue.v_recent_decision   最近决策记录（干预留痕回读源）
--   glue.v_node_utilization  节点利用率（gpu_frac 份额占用；汇总由 data.py 现算）
--
-- 纪律：视图只读（SELECT 语义）；干预写路径只有 data.py 的三条参数化 SQL
--（decision_record INSERT / challenge 状态转移 UPDATE），DDL 触发器（002）是
-- 第二道闸。全部视图带 tenant_id（v1.7 §4.1 全对象字段）。
-- =====================================================================

-- 1) 工单看板：team_task + 最近一次状态转移时间 + 任务级暂停标记。
--    paused 推导 = 该任务最新一条 pause/resume 决策记录（meta.intervention），
--    chosen='pause' 即暂停——干预可恢复，事实源是 append-only 留痕。
CREATE OR REPLACE VIEW glue.v_task_board AS
SELECT t.tenant_id,
       t.task_id::text                       AS task_id,
       t.title,
       t.owner,
       t.state,
       t.deliverable,
       t.run_ref,
       t.artifact_ref,
       t.created_at,
       COALESCE(lt.last_at, t.created_at)    AS last_transition_at,
       COALESCE(pp.paused, FALSE)            AS paused
FROM glue.team_task t
LEFT JOIN LATERAL (
    SELECT MAX(tr.occurred_at) AS last_at
    FROM glue.task_transition tr
    WHERE tr.task_id = t.task_id
) lt ON TRUE
LEFT JOIN LATERAL (
    SELECT d.chosen = 'pause' AS paused
    FROM glue.decision_record d
    WHERE d.tenant_id = t.tenant_id
      AND d.meta->>'intervention' IN ('pause', 'resume')
      AND d.meta->>'target' = t.task_id::text
    ORDER BY d.ts DESC
    LIMIT 1
) pp ON TRUE;

-- 2) 活跃租约：仅 ACTIVE（终态租约属台账回溯，不是作战室常驻面板）。
CREATE OR REPLACE VIEW glue.v_active_lease AS
SELECT l.tenant_id,
       l.lease_id::text                      AS lease_id,
       l.task_ref,
       l.parent_lease_id::text               AS parent_lease_id,
       l.amount,
       l.remaining,
       l.status,
       l.granted_at,
       l.expires_at
FROM glue.budget_lease l
WHERE l.status = 'ACTIVE';

-- 3) ask 审批队列：pending 且未过期（过期即 fail-closed 出队，由 002 触发器
--    兜底禁止过期后补批）。seconds_left 供 TUI 排序与倒计时展示。
CREATE OR REPLACE VIEW glue.v_pending_challenge AS
SELECT c.tenant_id,
       c.challenge_id::text                  AS challenge_id,
       c.who_confirms,
       c.resource,
       c.action,
       c.method,
       c.agent_identity_ref,
       c.guardrail_run_ref,
       c.created_at,
       c.expires_at,
       EXTRACT(EPOCH FROM (c.expires_at - now())) AS seconds_left
FROM glue.challenge c
WHERE c.state = 'pending'
  AND c.expires_at > now();

-- 4) 最近决策记录：决策 + 干预留痕共用一张 append-only 账本；排序与截断由
--    data.py 参数化完成（ORDER BY ts DESC LIMIT %s）。
CREATE OR REPLACE VIEW glue.v_recent_decision AS
SELECT d.tenant_id,
       d.decision_id::text                   AS decision_id,
       d.ts,
       d.agent_ref,
       d.context_hash,
       d.options,
       d.chosen,
       d.rationale_ref,
       d.guardrail_run_ref,
       d.meta
FROM glue.decision_record d;

-- 5) 节点利用率：每节点一行（字段与 002 glue.node 对齐）；顶部状态栏的
--    gpu_frac 占用汇总（合计/可信/不可信计数）由 data.py summarize_nodes 现算，
--    不在本视图物化——避免与调度器（WO-0011）将来引入的真实占用表打架。
CREATE OR REPLACE VIEW glue.v_node_utilization AS
SELECT n.tenant_id,
       n.node_id,
       n.cpu_frac,
       n.gpu_frac,
       n.tools,
       n.trust_level,
       n.max_parallel,
       n.online_window,
       n.registered_at
FROM glue.node n;
