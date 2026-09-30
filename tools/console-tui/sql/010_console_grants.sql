-- =====================================================================
-- 010_console_grants.sql — console-tui 线E（2026-09-29）补授权 DCL
-- 目标库: jiuwen_team (srv-1 company-pg-1)；角色: jiuwen（部署面创建）
-- 目的: pg 模式 8 面板完整可读——v_usage（W-04 计量）与 v_signal_timeline
--       （W-11 意图时间线）视图此前未授 jiuwen SELECT，面板取数即
--       permission denied（实测 information_schema.role_table_grants）。
-- 红线: 只授既有视图的 SELECT（只读），不动表授权、不动数据。
-- 幂等: GRANT 天然幂等；角色不存在时 NOTICE 跳过（对齐 008 角色守卫惯例）。
-- 应用方式（owner/主 agent）:
--   docker exec -i company-pg-1 psql -U postgres -d jiuwen_team \
--     -f - < tools/console-tui/sql/010_console_grants.sql
-- =====================================================================

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'jiuwen') THEN
        GRANT SELECT ON glue.v_usage TO jiuwen;
        GRANT SELECT ON glue.v_signal_timeline TO jiuwen;
    ELSE
        RAISE NOTICE 'role jiuwen 不存在——跳过授权（角色归部署面创建）';
    END IF;
END $$;
