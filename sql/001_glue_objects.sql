-- =====================================================================
-- jiuwen-glue 胶水层 Postgres DDL（WO-0003 动作 1）
-- 目标库：srv-1 控制面 Postgres（compose 服务 pg）
-- 对应实现：github.com/CloudCrane-Software/jiuwen-glue（src/jiuwen_glue/）
--
-- 覆盖四个最小对象（PROP-0001 v1.6 第 0 节第 4 条 / 第 4.1 节）：
--   1. Budget Lease   预算租约（发放/占用/过期 + 级联撤销）   → leases.py
--   2. Evidence 三态  draft / verified / finalized          → evidence.py
--   3. 能力元数据注册 五类机读声明 + 版本准入 + 指标单向回流 → capabilities.py
--   4. 协同三铁律     Task 台账 + 消息回执 + 拆分校验留痕    → rules.py
--
-- 约束原则：Python 层是第一道闸，DDL 约束/触发器是第二道闸（防绕过）。
-- 本脚本可重复执行（IF NOT EXISTS / DROP TRIGGER IF EXISTS）。
-- 依据方案纪律：实例配置变更走 PR（company-ops）；本文件放 ops/sql/。
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS glue;

-- =====================================================================
-- 1) Budget Lease（预算租约）
--    规格：Handbook ch30「随子任务派生、级联撤销」；额度为抽象整数单位。
-- =====================================================================

CREATE TABLE IF NOT EXISTS glue.budget_lease (
    lease_id        UUID PRIMARY KEY,
    task_ref        TEXT        NOT NULL,                 -- 服务的 Task/Subtask 引用（跨层只传引用）
    parent_lease_id UUID        REFERENCES glue.budget_lease(lease_id),
    amount          BIGINT      NOT NULL CHECK (amount >= 0),
    remaining       BIGINT      NOT NULL CHECK (remaining >= 0 AND remaining <= amount),
    status          TEXT        NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE','EXHAUSTED','EXPIRED','REVOKED')),
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    revoke_reason   TEXT,
    CHECK (expires_at IS NULL OR expires_at > granted_at),
    CHECK (status <> 'REVOKED' OR revoked_at IS NOT NULL)
);

-- 租约事件：追加式台账（占用被拒绝也要留痕 —— 检测 = 拒绝 + 留痕）
CREATE TABLE IF NOT EXISTS glue.lease_event (
    event_id   BIGSERIAL PRIMARY KEY,
    lease_id   UUID        NOT NULL REFERENCES glue.budget_lease(lease_id),
    event_type TEXT        NOT NULL
               CHECK (event_type IN ('GRANT','ACQUIRE','EXPIRE','REVOKE',
                                     'ACQUIRE_REJECTED','GRANT_REJECTED')),
    cost       BIGINT      CHECK (cost IS NULL OR cost >= 0),
    detail     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_lease_event_lease ON glue.lease_event(lease_id, occurred_at);

-- 第二道闸：占用不允许超额（remaining 不得被更新为小于 0；状态只进不退）
CREATE OR REPLACE FUNCTION glue.budget_lease_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.remaining < 0 THEN
        RAISE EXCEPTION 'budget overdraft: lease % remaining would go negative', NEW.lease_id;
    END IF;
    IF OLD.status IN ('EXHAUSTED','EXPIRED','REVOKED') AND NEW.status <> OLD.status THEN
        RAISE EXCEPTION 'terminal lease status % cannot change to %', OLD.status, NEW.status;
    END IF;
    IF OLD.status = 'ACTIVE' AND NEW.status NOT IN ('ACTIVE','EXHAUSTED','EXPIRED','REVOKED') THEN
        RAISE EXCEPTION 'illegal lease status transition % -> %', OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_budget_lease_guard ON glue.budget_lease;
CREATE TRIGGER trg_budget_lease_guard
    BEFORE UPDATE ON glue.budget_lease
    FOR EACH ROW EXECUTE FUNCTION glue.budget_lease_guard();

-- =====================================================================
-- 2) Evidence 三态（draft / verified / finalized）
--    规格：只进不退；内容仅 DRAFT 可编辑；FINALIZED 终态封存。
-- =====================================================================

CREATE TABLE IF NOT EXISTS glue.evidence (
    evidence_id    UUID PRIMARY KEY,
    subject        TEXT        NOT NULL,               -- 证据主体（task_run / capability_version / experience）
    content        JSONB       NOT NULL,
    state          TEXT        NOT NULL DEFAULT 'DRAFT'
                   CHECK (state IN ('DRAFT','VERIFIED','FINALIZED')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at    TIMESTAMPTZ,
    verified_by    TEXT,
    verified_method TEXT,
    finalized_at   TIMESTAMPTZ,
    seal_ref       TEXT,
    CHECK (state <> 'VERIFIED'  OR (verified_at IS NOT NULL AND verified_method IS NOT NULL)),
    CHECK (state <> 'FINALIZED' OR (finalized_at IS NOT NULL AND seal_ref IS NOT NULL))
);

-- 迁移留痕（违规尝试也记录：kind='VIOLATION'）
CREATE TABLE IF NOT EXISTS glue.evidence_transition (
    transition_id BIGSERIAL PRIMARY KEY,
    evidence_id   UUID        NOT NULL REFERENCES glue.evidence(evidence_id),
    kind          TEXT        NOT NULL CHECK (kind IN ('CREATE','TRANSITION','EDIT','VIOLATION')),
    from_state    TEXT        CHECK (from_state IN ('DRAFT','VERIFIED','FINALIZED') OR from_state IS NULL),
    to_state      TEXT        CHECK (to_state   IN ('DRAFT','VERIFIED','FINALIZED') OR to_state   IS NULL),
    method        TEXT,
    checker       TEXT,
    refs          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    note          TEXT,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_evidence_transition_ev ON glue.evidence_transition(evidence_id, occurred_at);

-- 第二道闸：状态只允许 DRAFT→VERIFIED→FINALIZED；非 DRAFT 内容不可改
CREATE OR REPLACE FUNCTION glue.evidence_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.state <> OLD.state THEN
        IF NOT (OLD.state = 'DRAFT'    AND NEW.state = 'VERIFIED') AND
           NOT (OLD.state = 'VERIFIED' AND NEW.state = 'FINALIZED') THEN
            RAISE EXCEPTION 'illegal evidence transition % -> % (forward-only)', OLD.state, NEW.state;
        END IF;
    END IF;
    IF OLD.state <> 'DRAFT' AND NEW.content IS DISTINCT FROM OLD.content THEN
        RAISE EXCEPTION 'evidence % content is frozen after DRAFT', OLD.evidence_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_evidence_guard ON glue.evidence;
CREATE TRIGGER trg_evidence_guard
    BEFORE UPDATE ON glue.evidence
    FOR EACH ROW EXECUTE FUNCTION glue.evidence_guard();

-- =====================================================================
-- 3) 能力元数据注册（能力治理台账）
--    规格：五类机读声明（副作用/幂等/可重试/风险等级/前置条件）；
--    版本准入必须挂 VERIFIED/FINALIZED 证据；指标单向回流只增不改。
--    边界（4.9 #1）：发现与编排用原生 Symphony；本表只做治理台账。
-- =====================================================================

CREATE TABLE IF NOT EXISTS glue.capability_version (
    capability_id TEXT        NOT NULL,
    version       TEXT        NOT NULL,
    name          TEXT        NOT NULL,
    side_effect   TEXT        NOT NULL
                  CHECK (side_effect IN ('none','read','external_write','external_irreversible')),
    idempotent    BOOLEAN     NOT NULL,
    retry_safe    BOOLEAN     NOT NULL,
    risk_level    SMALLINT    NOT NULL CHECK (risk_level BETWEEN 0 AND 4),
    preconditions JSONB       NOT NULL DEFAULT '[]'::jsonb,
    status        TEXT        NOT NULL DEFAULT 'CANDIDATE'
                  CHECK (status IN ('CANDIDATE','ADMITTED','REJECTED')),
    admitted_evidence_id UUID REFERENCES glue.evidence(evidence_id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    admitted_at   TIMESTAMPTZ,
    PRIMARY KEY (capability_id, version),
    CHECK (NOT retry_safe OR idempotent),              -- 非幂等操作不得声称可安全重试
    CHECK (status <> 'ADMITTED' OR admitted_evidence_id IS NOT NULL)
);

-- 版本准入的第二道闸：ADMITTED 必须引用 VERIFIED/FINALIZED 的证据
CREATE OR REPLACE FUNCTION glue.capability_admit_guard() RETURNS trigger AS $$
DECLARE ev_state TEXT;
BEGIN
    IF NEW.status = 'ADMITTED' THEN
        IF NEW.admitted_evidence_id IS NULL THEN
            RAISE EXCEPTION 'admission requires an evidence reference';
        END IF;
        SELECT state INTO ev_state FROM glue.evidence WHERE evidence_id = NEW.admitted_evidence_id;
        IF ev_state NOT IN ('VERIFIED','FINALIZED') THEN
            RAISE EXCEPTION 'admission requires VERIFIED/FINALIZED evidence, got %', ev_state;
        END IF;
        NEW.admitted_at := now();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_capability_admit_guard ON glue.capability_version;
CREATE TRIGGER trg_capability_admit_guard
    BEFORE INSERT OR UPDATE ON glue.capability_version
    FOR EACH ROW EXECUTE FUNCTION glue.capability_admit_guard();

-- 指标：只追加事件（单向回流自 TrajectoryRail 执行结果），比率用视图现算
CREATE TABLE IF NOT EXISTS glue.capability_metric_event (
    metric_id     BIGSERIAL PRIMARY KEY,
    capability_id TEXT        NOT NULL,
    version       TEXT        NOT NULL,
    outcome       TEXT        NOT NULL CHECK (outcome IN ('success','failure','hit','miss')),
    run_ref       TEXT,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_cap_metric_cap ON glue.capability_metric_event(capability_id, version, occurred_at);

CREATE OR REPLACE VIEW glue.capability_metrics AS
SELECT capability_id, version,
       COUNT(*) FILTER (WHERE outcome = 'success') AS success_count,
       COUNT(*) FILTER (WHERE outcome = 'failure') AS failure_count,
       COUNT(*) FILTER (WHERE outcome = 'hit')     AS hit_count,
       COUNT(*) FILTER (WHERE outcome = 'miss')    AS miss_count,
       CASE WHEN COUNT(*) FILTER (WHERE outcome IN ('success','failure')) > 0
            THEN COUNT(*) FILTER (WHERE outcome = 'success')::numeric
                 / COUNT(*) FILTER (WHERE outcome IN ('success','failure')) END AS success_rate,
       CASE WHEN COUNT(*) FILTER (WHERE outcome IN ('hit','miss')) > 0
            THEN COUNT(*) FILTER (WHERE outcome = 'hit')::numeric
                 / COUNT(*) FILTER (WHERE outcome IN ('hit','miss')) END AS hit_rate
FROM glue.capability_metric_event
GROUP BY capability_id, version;

-- =====================================================================
-- 4) 协同三铁律（Task 台账 + 消息回执 + 违规留痕）
--    规格：① 消息发送成功不代表任务已被承接；② 对话历史不是 Team State；
--    ③ 同一成员连续完成的内部步骤不建任务。
-- =====================================================================

CREATE TABLE IF NOT EXISTS glue.team_task (
    task_id        UUID PRIMARY KEY,
    title          TEXT        NOT NULL,
    owner          TEXT,
    deliverable    TEXT,
    state          TEXT        NOT NULL DEFAULT 'PENDING'
                   CHECK (state IN ('PENDING','CLAIMED','COMPLETED','BLOCKED','CANCELLED')),
    parent_task_id UUID        REFERENCES glue.team_task(task_id),
    run_ref        TEXT,                              -- 完成必须携带 TaskRun 引用（铁律 2）
    artifact_ref   TEXT,                              -- 完成必须携带 Artifact 引用（铁律 2）
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (state <> 'COMPLETED' OR (run_ref IS NOT NULL AND artifact_ref IS NOT NULL))
);

-- 状态迁移：唯一入口；source 白名单把"消息/对话历史"挡在任务事实之外（铁律 1/2）
CREATE TABLE IF NOT EXISTS glue.task_transition (
    transition_id BIGSERIAL PRIMARY KEY,
    task_id       UUID        NOT NULL REFERENCES glue.team_task(task_id),
    from_state    TEXT        CHECK (from_state IN ('PENDING','CLAIMED','COMPLETED','BLOCKED','CANCELLED') OR from_state IS NULL),
    to_state      TEXT        NOT NULL CHECK (to_state IN ('PENDING','CLAIMED','COMPLETED','BLOCKED','CANCELLED')),
    source        TEXT        NOT NULL CHECK (source IN ('claim','run','admin')),  -- 无 message/chat 来源
    executor      TEXT,
    note          TEXT,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_task_transition_task ON glue.task_transition(task_id, occurred_at);

-- 消息回执：只证明"已送达"，独立于任务状态（不持有任何状态语义字段）
CREATE TABLE IF NOT EXISTS glue.message_receipt (
    receipt_id   UUID PRIMARY KEY,
    task_ref     TEXT        NOT NULL,                 -- 只是引用；对 task 表无外键、无级联
    sender       TEXT        NOT NULL,
    text         TEXT        NOT NULL,
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 违规留痕：三条铁律 + 其他非法变更（检测 = 拒绝 + 留痕）的统一账本
CREATE TABLE IF NOT EXISTS glue.rule_violation (
    violation_id BIGSERIAL PRIMARY KEY,
    rule_no      SMALLINT    NOT NULL CHECK (rule_no IN (0,1,2,3)),   -- 0 = 非铁律类非法操作
    code         TEXT        NOT NULL,
    detail       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_rule_violation_rule ON glue.rule_violation(rule_no, occurred_at);

-- 第二道闸：任务完成必须携带 run_ref + artifact_ref（铁律 2 的兜底）
CREATE OR REPLACE FUNCTION glue.team_task_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.state = 'COMPLETED' AND (NEW.run_ref IS NULL OR NEW.artifact_ref IS NULL) THEN
        RAISE EXCEPTION 'completing task % requires run_ref and artifact_ref '
                        '(conversation history is not Team State)', NEW.task_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_team_task_guard ON glue.team_task;
CREATE TRIGGER trg_team_task_guard
    BEFORE INSERT OR UPDATE ON glue.team_task
    FOR EACH ROW EXECUTE FUNCTION glue.team_task_guard();
