-- =====================================================================
-- jiuwen-glue 胶水层 Postgres DDL v2（WO-0003 返工 / M0.5 前置）
-- 目标库：srv-1 控制面 Postgres（compose 服务 pg）——本工单只交 DDL，
--         不落生产库（由后续 srv-1 工单执行）。
-- 对应实现：github.com/CloudCrane-Software/jiuwen-glue（src/jiuwen_glue/）
--
-- 覆盖（PROP-0001 v1.7 §12.5 JIT 身份三件 / §13 便宜三件 / M0 审计发现 1）：
--   1. glue.guardrail_run / guardrail_check_result   GuardrailRun 协议聚合  → guardrail.py
--   2. glue.challenge                                Challenge 授权三态    → challenge.py
--   3. glue.promotion_record / promotion_transition  记忆晋升管线          → promotion.py
--   4. glue.decision_record                          决策记录 append-only  → decisions.py
--   5. glue.artifact_route                           产物路由表            → routes.py
--   6. glue.node                                     节点容量模型          → routes.py
--   7. 对 001 既有表幂等 ALTER 加 tenant_id（v1.7 §4.1 全对象字段，单租户起步 't0'）
--
-- 消费方：jiuwen-glue 各模块 + Wave2 fleet 调度器 / 控制台 TUI（PROP-0004/0005）。
--
-- 约束原则：Python 层是第一道闸，DDL 约束/触发器是第二道闸（防绕过）。
-- 本脚本可重复执行（IF NOT EXISTS / DROP TRIGGER IF EXISTS）。
-- 依据方案纪律：实例配置变更走 PR（company-ops）；本文件放 ops/sql/。
-- 表名/字段与 src/jiuwen_glue/ 各 dataclass 严格对齐；*_ref 列一律 TEXT
--（跨层只传引用，不复制状态——4.9 #10；同 001 的 task_ref 先例）。
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS glue;

-- =====================================================================
-- 0) 001 既有表补 tenant_id（幂等；单租户起步，默认 't0'）
-- =====================================================================

ALTER TABLE glue.budget_lease         ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 't0';
ALTER TABLE glue.lease_event          ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 't0';
ALTER TABLE glue.evidence             ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 't0';
ALTER TABLE glue.evidence_transition  ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 't0';
ALTER TABLE glue.capability_version   ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 't0';
ALTER TABLE glue.capability_metric_event ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 't0';
ALTER TABLE glue.team_task            ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 't0';
ALTER TABLE glue.task_transition      ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 't0';
ALTER TABLE glue.message_receipt      ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 't0';
ALTER TABLE glue.rule_violation       ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 't0';

-- =====================================================================
-- 1) GuardrailRun 协议聚合（手册 §3.3.2 五步链路；聚合 verdict = 唯一门控输出）
--    fail-closed：OPEN/VOID 状态对查询方等同 UNKNOWN——必须拒绝执行。
-- =====================================================================

CREATE TABLE IF NOT EXISTS glue.guardrail_run (
    run_id            UUID PRIMARY KEY,
    action            TEXT        NOT NULL,                -- 什么动作（如 release.resume）
    resource          TEXT        NOT NULL,                -- 什么资源（发布批次/配置项）
    agent_identity_ref TEXT       NOT NULL,                -- 哪个 Agent（三层复合身份引用）
    env_ref           TEXT,                                -- 环境定义引用（12.2）
    spec              JSONB       NOT NULL,                -- 固化的 GuardrailSpec（含 checks 声明）
    spec_version      TEXT        NOT NULL DEFAULT '1',    -- 固化规则版本（准入记录绑定）
    state             TEXT        NOT NULL DEFAULT 'OPEN'
                      CHECK (state IN ('OPEN','FINALIZED','VOID')),
    seal_ref          TEXT,
    void_reason       TEXT,                                -- 现场变化 → 原有结论失效
    tenant_id         TEXT        NOT NULL DEFAULT 't0',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalized_at      TIMESTAMPTZ,
    CHECK (state <> 'FINALIZED' OR seal_ref IS NOT NULL),
    CHECK (state <> 'VOID' OR void_reason IS NOT NULL)
);

-- =====================================================================
-- 2) Challenge（授权三态第三态：结构化授权要求）
--    模型只见高层状态（pending/approved/denied/expired），无授权码/token 字段。
--    （先建本表：guardrail_check_result.challenge_id 外键引用它）
-- =====================================================================

CREATE TABLE IF NOT EXISTS glue.challenge (
    challenge_id     UUID PRIMARY KEY,
    who_confirms     TEXT        NOT NULL
                     CHECK (who_confirms IN ('user','resource_owner','duty_officer')),
    resource         TEXT        NOT NULL,
    action           TEXT        NOT NULL,
    method           TEXT        NOT NULL,                 -- 确认方式（如 console.ask）
    agent_identity_ref TEXT,                               -- 哪个 Agent（ask 载荷展示）
    guardrail_run_ref TEXT,                                 -- 发起 run 的引用（无外键，引用式）
    state            TEXT        NOT NULL DEFAULT 'pending'
                     CHECK (state IN ('pending','approved','denied','expired')),
    meta             JSONB       NOT NULL DEFAULT '{}'::jsonb,
    tenant_id        TEXT        NOT NULL DEFAULT 't0',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL,
    resolved_at      TIMESTAMPTZ,
    resolved_by      TEXT,                                  -- 确认人（Agent 不得自确认）
    CHECK (expires_at > created_at),
    CHECK (state NOT IN ('approved','denied') OR resolved_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_challenge_queue ON glue.challenge(who_confirms, state, expires_at);

-- 逐 check 结论（步骤 3 提交；ASK 落为 UNKNOWN + challenge_id——未决 ask 门控上即 UNKNOWN）
CREATE TABLE IF NOT EXISTS glue.guardrail_check_result (
    result_id    BIGSERIAL PRIMARY KEY,
    run_id       UUID        NOT NULL REFERENCES glue.guardrail_run(run_id),
    check_id     TEXT        NOT NULL,
    backend      TEXT        NOT NULL
                 CHECK (backend IN ('native_guardrail','permission_rail','eval_gate','scan')),
    verdict      TEXT        NOT NULL CHECK (verdict IN ('PASS','BLOCKED','UNKNOWN')),
    required     BOOLEAN     NOT NULL DEFAULT TRUE,      -- 必填未提交 → 聚合 UNKNOWN
    evidence_ref TEXT,                                    -- Evidence 引用（原始快照）
    challenge_id UUID REFERENCES glue.challenge(challenge_id),
    note         TEXT,
    tenant_id    TEXT        NOT NULL DEFAULT 't0',
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, check_id)
);
CREATE INDEX IF NOT EXISTS ix_guardrail_result_run ON glue.guardrail_check_result(run_id, submitted_at);

-- 第二道闸：pending → approved/denied/expired 单向；终态不可再改（过期批准无效）
CREATE OR REPLACE FUNCTION glue.challenge_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.state <> 'pending' AND NEW.state <> OLD.state THEN
        RAISE EXCEPTION 'challenge % is % (terminal); only PENDING can be resolved',
                        OLD.challenge_id, OLD.state;
    END IF;
    IF NEW.state IN ('approved','denied','expired') AND OLD.state = 'pending' THEN
        IF NEW.state IN ('approved','denied') THEN
            IF NEW.resolved_at IS NULL OR NEW.resolved_by IS NULL THEN
                RAISE EXCEPTION 'resolving challenge % requires resolved_at and resolved_by',
                                NEW.challenge_id;
            END IF;
            IF NEW.resolved_at > NEW.expires_at THEN
                RAISE EXCEPTION 'challenge % expired at %; late approval is void (fail-closed)',
                                NEW.challenge_id, NEW.expires_at;
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_challenge_guard ON glue.challenge;
CREATE TRIGGER trg_challenge_guard
    BEFORE UPDATE ON glue.challenge
    FOR EACH ROW EXECUTE FUNCTION glue.challenge_guard();

-- 第二道闸：run 固化（FINALIZED）或作废（VOID）后结论不可再改
CREATE OR REPLACE FUNCTION glue.guardrail_result_guard() RETURNS trigger AS $$
DECLARE run_state TEXT;
BEGIN
    SELECT state INTO run_state FROM glue.guardrail_run WHERE run_id = NEW.run_id;
    IF run_state IS DISTINCT FROM 'OPEN' THEN
        RAISE EXCEPTION 'guardrail run % is %; check results are frozen '
                        '(re-open via a new run)', NEW.run_id, run_state;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_guardrail_result_guard ON glue.guardrail_check_result;
CREATE TRIGGER trg_guardrail_result_guard
    BEFORE INSERT OR UPDATE ON glue.guardrail_check_result
    FOR EACH ROW EXECUTE FUNCTION glue.guardrail_result_guard();

-- =====================================================================
-- 3) 记忆晋升管线（只做"个人→组织"资产晋升；不回写执行面记忆——4.9 #4）
--    质量门 + 脱敏门（无 checker 即 UNKNOWN 拒绝）+ score_hook（决策层唯一交叉点）
-- =====================================================================

CREATE TABLE IF NOT EXISTS glue.promotion_record (
    promotion_id     UUID PRIMARY KEY,
    asset_key        TEXT        NOT NULL,                -- 组织资产键（如 skill:video-cut）
    origin_ref       TEXT,                                -- 来源引用（TTSE/Skill 演进轨等）
    content          JSONB       NOT NULL,                -- 资产内容（仅 working 期可改）
    stage            TEXT        NOT NULL DEFAULT 'working'
                     CHECK (stage IN ('working','shortlist','promoted','rejected','withdrawn')),
    version          INT         NOT NULL DEFAULT 0,      -- promoted 时分配的单调版本号（从 1 起）
    redaction_verdict TEXT       CHECK (redaction_verdict IN ('PASS','BLOCKED','UNKNOWN')),
    score            NUMERIC,                             -- 决策层打分（score_hook；NULL=未打分）
    promoted_at      TIMESTAMPTZ,
    promoted_version_key TEXT,
    tenant_id        TEXT        NOT NULL DEFAULT 't0',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (stage <> 'promoted' OR (version > 0 AND promoted_at IS NOT NULL
                                   AND promoted_version_key IS NOT NULL))
);

-- 组织版本唯一性：promoted 资产 (tenant, asset_key, version) 单调不重复（version=0 未入库不受限）
CREATE UNIQUE INDEX IF NOT EXISTS ux_promotion_version
    ON glue.promotion_record(tenant_id, asset_key, version) WHERE version > 0;
CREATE INDEX IF NOT EXISTS ix_promotion_stage ON glue.promotion_record(tenant_id, stage, asset_key);

-- 每次阶段转移带理由与证据引用（含 GATE_REJECT 与 VIOLATION 留痕）
CREATE TABLE IF NOT EXISTS glue.promotion_transition (
    transition_id BIGSERIAL PRIMARY KEY,
    promotion_id  UUID        NOT NULL REFERENCES glue.promotion_record(promotion_id),
    asset_key     TEXT        NOT NULL,
    from_stage    TEXT        CHECK (from_stage IN ('working','shortlist','promoted','rejected','withdrawn') OR from_stage IS NULL),
    to_stage      TEXT        CHECK (to_stage   IN ('working','shortlist','promoted','rejected','withdrawn') OR to_stage   IS NULL),
    kind          TEXT        NOT NULL CHECK (kind IN ('CREATE','TRANSITION','GATE_REJECT','VIOLATION')),
    reason        TEXT        NOT NULL,
    evidence_ref  TEXT,
    detail        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_promotion_transition ON glue.promotion_transition(promotion_id, occurred_at);

-- 第二道闸：状态机只允许 working→shortlist→promoted / →rejected / promoted→withdrawn(墓碑)
CREATE OR REPLACE FUNCTION glue.promotion_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.stage <> OLD.stage THEN
        IF NOT (OLD.stage = 'working'    AND NEW.stage IN ('shortlist','rejected')) AND
           NOT (OLD.stage = 'shortlist'  AND NEW.stage IN ('promoted','rejected','withdrawn')) AND
           NOT (OLD.stage = 'promoted'   AND NEW.stage = 'withdrawn') THEN
            RAISE EXCEPTION 'illegal promotion transition % -> % (stage machine)',
                            OLD.stage, NEW.stage;
        END IF;
    END IF;
    IF OLD.stage <> 'working' AND NEW.content IS DISTINCT FROM OLD.content THEN
        RAISE EXCEPTION 'promotion % content is frozen after working', OLD.promotion_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_promotion_guard ON glue.promotion_record;
CREATE TRIGGER trg_promotion_guard
    BEFORE UPDATE ON glue.promotion_record
    FOR EACH ROW EXECUTE FUNCTION glue.promotion_guard();

-- eval 指标：只追加事件（单向回流，复用 001 capability_metric_event 模式），比率用视图现算
CREATE TABLE IF NOT EXISTS glue.promotion_eval_event (
    eval_id     BIGSERIAL PRIMARY KEY,
    asset_key   TEXT        NOT NULL,
    outcome     TEXT        NOT NULL CHECK (outcome IN ('success','failure')),
    run_ref     TEXT,
    tenant_id   TEXT        NOT NULL DEFAULT 't0',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_promotion_eval ON glue.promotion_eval_event(tenant_id, asset_key, occurred_at);

CREATE OR REPLACE VIEW glue.promotion_eval_metrics AS
SELECT tenant_id, asset_key,
       COUNT(*) AS samples,
       CASE WHEN COUNT(*) > 0 THEN
            COUNT(*) FILTER (WHERE outcome = 'success')::numeric / COUNT(*) END AS success_rate
FROM glue.promotion_eval_event
GROUP BY tenant_id, asset_key;

-- =====================================================================
-- 4) 决策记录（append-only：只增不改——无 UPDATE/DELETE 路径）
-- =====================================================================

CREATE TABLE IF NOT EXISTS glue.decision_record (
    decision_id      UUID PRIMARY KEY,
    ts               TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_ref        TEXT        NOT NULL,               -- 三层复合身份引用
    context_hash     TEXT        NOT NULL
                     CHECK (context_hash ~ '^[0-9a-f]{64}$'),   -- canonical JSON 的 SHA-256
    options          JSONB       NOT NULL,                -- 候选选项空间（数组）
    chosen           TEXT        NOT NULL,
    rationale_ref    TEXT        NOT NULL,                -- 理由的证据引用
    guardrail_run_ref TEXT,                               -- 绑定的门控运行（引用式）
    meta             JSONB       NOT NULL DEFAULT '{}'::jsonb,
    tenant_id        TEXT        NOT NULL DEFAULT 't0'
);
CREATE INDEX IF NOT EXISTS ix_decision_agent ON glue.decision_record(tenant_id, agent_ref, ts);
CREATE INDEX IF NOT EXISTS ix_decision_ctx ON glue.decision_record(tenant_id, context_hash);

-- 第二道闸：append-only 写死——任何 UPDATE/DELETE 一律拒绝
CREATE OR REPLACE FUNCTION glue.decision_record_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'decision records are append-only: % cannot be %',
                    OLD.decision_id, TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_decision_record_append_only ON glue.decision_record;
CREATE TRIGGER trg_decision_record_append_only
    BEFORE UPDATE OR DELETE ON glue.decision_record
    FOR EACH ROW EXECUTE FUNCTION glue.decision_record_guard();

-- =====================================================================
-- 5) 产物路由表（v1.7 §13：流水线差异三声明之一；未声明路由 = 显式错误，防产物误入 git）
--    基线：code→git / video→minio / eval→eval-assets（pipeline='*' 通配，精确声明优先）
-- =====================================================================

CREATE TABLE IF NOT EXISTS glue.artifact_route (
    tenant_id     TEXT        NOT NULL DEFAULT 't0',
    pipeline      TEXT        NOT NULL,
    artifact_kind TEXT        NOT NULL,
    sink          TEXT        NOT NULL,                 -- git / minio / eval-assets / customer-dam ...
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, pipeline, artifact_kind),
    CHECK (length(btrim(pipeline)) > 0 AND length(btrim(artifact_kind)) > 0
           AND length(btrim(sink)) > 0)
);

-- =====================================================================
-- 6) 节点容量模型（v1.7 §12.6 节点池纳管；消费方：Wave2 fleet 调度器 / 控制台 TUI）
--    GPU_FRAC 吸收为 gpu_frac 份额字段；不可信节点只派沙箱任务类（sandbox_only）。
-- =====================================================================

CREATE TABLE IF NOT EXISTS glue.node (
    tenant_id     TEXT        NOT NULL DEFAULT 't0',
    node_id       TEXT        NOT NULL,
    cpu_frac      NUMERIC     NOT NULL DEFAULT 1.0 CHECK (cpu_frac BETWEEN 0 AND 1),
    gpu_frac      NUMERIC     NOT NULL DEFAULT 0.0 CHECK (gpu_frac BETWEEN 0 AND 1),
    tools         JSONB       NOT NULL DEFAULT '[]'::jsonb,   -- 工具/能力标签
    trust_level   TEXT        NOT NULL DEFAULT 'trusted'
                  CHECK (trust_level IN ('trusted','untrusted')),
    max_parallel  INT         NOT NULL DEFAULT 1 CHECK (max_parallel >= 1),
    online_window TEXT        NOT NULL DEFAULT 'always',      -- 如 'always' / '09:00-18:00+08'
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, node_id)
);
