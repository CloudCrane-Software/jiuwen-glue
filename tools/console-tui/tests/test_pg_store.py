# coding: utf-8
"""pg 模式测试：不真连数据库（连接冒烟由主 agent 落库视图后另做）。

用桩连接验证：① 面板读走 003 视图且 SQL 全参数化（值不拼接进 SQL 文本）；
② 三级干预写路径（steer=decision_record INSERT / approve=challenge 带守卫
UPDATE + 留痕 / pause=合法性校验后 INSERT）；③ DSN 只读环境变量且永不落 repr。
"""
from __future__ import annotations

import datetime
import json
import re
import sys
import time
import types
import uuid

import pytest

from console_tui.data import DSN_ENV, PgConsoleStore, connect_pg
from console_tui.state import (ChallengeResolutionError, GovernanceError,
                               IllegalTransitionError, UnknownTargetError)


class StubCursor:
    def __init__(self, conn: "StubConn") -> None:
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=None):
        self.conn.log.append((sql, tuple(params or ())))
        rows, rowcount = self.conn.queue.pop(0) if self.conn.queue else ([], 0)
        self._rows, self.rowcount = rows, rowcount

    def fetchall(self):
        return list(self._rows)


class StubConn:
    """最小 psycopg 连接协议：cursor()/commit()；queue 预置每次执行的 (rows, rowcount)。"""

    def __init__(self, queue=None):
        self.queue = list(queue or [])
        self.log = []
        self.commits = 0

    def cursor(self):
        return StubCursor(self)

    def commit(self):
        self.commits += 1


def _task_row(owner="alpha-planner-01", ts=None):
    dt = datetime.datetime.fromtimestamp(ts or time.time())
    return ("tsk-1", "标题", owner, "CLAIMED", "交付物", "run://r", "art://a",
            dt, dt, False)


def _challenge_state_row(state="pending", expires_in=600.0):
    return (state, datetime.datetime.fromtimestamp(time.time() + expires_in))


HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ── 面板读：走视图 + 参数化 ─────────────────────────────────────────────────

def test_pg_reads_go_through_views_with_params():
    conn = StubConn()
    store = PgConsoleStore(conn)
    store.tasks()
    store.leases()
    store.challenges()
    store.decisions(limit=5)
    store.nodes()
    views = ["glue.v_task_board", "glue.v_active_lease", "glue.v_pending_challenge",
             "glue.v_recent_decision", "glue.v_node_utilization"]
    assert len(conn.log) == 5
    for (sql, params), view in zip(conn.log, views):
        assert view in sql, f"{sql} 未走视图 {view}"
        assert "%s" in sql, f"{sql} 未参数化"
        assert params in (("t0",), ("t0", 5)), f"{sql} 参数异常: {params}"
    assert conn.commits == 5


def test_pg_task_row_mapping():
    conn = StubConn(queue=[([_task_row()], 0)])
    tasks = PgConsoleStore(conn).tasks()
    assert tasks[0].task_id == "tsk-1" and tasks[0].owner == "alpha-planner-01"
    assert tasks[0].paused is False


# ── s = steer：INSERT decision_record（type=steer 记在 meta）────────────────

def test_pg_steer_inserts_decision_record():
    conn = StubConn(queue=[([_task_row()], 0)])
    event = PgConsoleStore(conn).steer("alpha-planner-01", "聚焦 guardrail 测试")
    assert event.kind == "steer" and event.target == "alpha-planner-01"
    inserts = [(s, p) for s, p in conn.log if "INSERT INTO glue.decision_record" in s]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert sql.count("%s") == 8 and "%s::jsonb" in sql
    _decision_id, agent_ref, context_hash, options, chosen, rationale, meta, tenant = params
    assert agent_ref == "console:operator" and chosen == "steer"
    assert HEX64.match(context_hash)
    assert json.loads(options) == ["steer", "noop"]
    meta_d = json.loads(meta)
    assert meta_d["intervention"] == "steer" and meta_d["target"] == "alpha-planner-01"
    assert meta_d["text"] == "聚焦 guardrail 测试"
    assert conn.commits >= 2                     # agents() 读 + INSERT 写


def test_pg_steer_unknown_agent_rejected_before_write():
    conn = StubConn()                            # 无工单 → 塔上无此 agent
    with pytest.raises(UnknownTargetError):
        PgConsoleStore(conn).steer("ghost", "x")
    assert all("INSERT" not in s for s, _ in conn.log)   # 拒绝 = 零写路径


# ── a = 审批：状态机先行 + 带守卫 UPDATE + 留痕 ─────────────────────────────

def test_pg_approve_issues_guarded_update_and_audit():
    cid = str(uuid.uuid4())
    conn = StubConn(queue=[([_challenge_state_row()], 0), ([], 1)])
    event = PgConsoleStore(conn).resolve_challenge(cid, approved=True)
    assert event.kind == "approve"
    select_sql, select_params = conn.log[0]
    update_sql, update_params = conn.log[1]
    insert_sql, insert_params = conn.log[2]
    assert "FROM glue.challenge" in select_sql and select_params == (uuid.UUID(cid),)
    for guard in ("state = 'pending'", "expires_at > now()", "resolved_by = %s"):
        assert guard in update_sql, f"UPDATE 缺守卫 {guard!r}: {update_sql}"
    assert update_params[0] == "approved"
    assert "INSERT INTO glue.decision_record" in insert_sql
    assert json.loads(insert_params[6])["challenge_state"] == "approved"


def test_pg_approve_race_lost_rejected():
    cid = str(uuid.uuid4())
    conn = StubConn(queue=[([_challenge_state_row()], 0), ([], 0)])   # rowcount=0 → 竞态
    with pytest.raises(ChallengeResolutionError, match="fail-closed"):
        PgConsoleStore(conn).resolve_challenge(cid, approved=True)


def test_pg_expired_challenge_rejected_without_update():
    cid = str(uuid.uuid4())
    conn = StubConn(queue=[([_challenge_state_row(expires_in=-60.0)], 0)])
    with pytest.raises(ChallengeResolutionError, match="expired"):
        PgConsoleStore(conn).resolve_challenge(cid, approved=True)
    assert len(conn.log) == 1                     # 状态机先拒 → 不发 UPDATE
    assert "UPDATE" not in conn.log[0][0]


def test_pg_unknown_challenge_rejected():
    with pytest.raises(UnknownTargetError):
        PgConsoleStore(StubConn()).resolve_challenge(str(uuid.uuid4()), approved=True)


# ── p = 暂停/恢复 ───────────────────────────────────────────────────────────

def test_pg_pause_writes_decision_record():
    conn = StubConn(queue=[([(False,)], 0)])
    event = PgConsoleStore(conn).set_task_pause("tsk-1", pause=True)
    assert event.kind == "pause"
    check_sql, check_params = conn.log[0]
    assert "glue.v_task_board" in check_sql and check_params == ("t0", "tsk-1")
    insert_sql, insert_params = conn.log[1]
    assert "INSERT INTO glue.decision_record" in insert_sql
    meta = json.loads(insert_params[6])
    assert meta["intervention"] == "pause" and meta["target"] == "tsk-1"


def test_pg_resume_when_not_paused_rejected():
    conn = StubConn(queue=[([(False,)], 0)])
    with pytest.raises(IllegalTransitionError):
        PgConsoleStore(conn).set_task_pause("tsk-1", pause=False)
    assert len(conn.log) == 1                     # 校验先行，无写入


def test_pg_pause_unknown_task_rejected():
    with pytest.raises(UnknownTargetError):
        PgConsoleStore(StubConn()).set_task_pause("tsk-ghost", pause=True)


# ── DSN 纪律：只读环境变量；任何失败不回显连接串 ────────────────────────────

def test_connect_pg_requires_dsn_env(monkeypatch):
    monkeypatch.delenv(DSN_ENV, raising=False)
    with pytest.raises(GovernanceError, match=DSN_ENV):
        connect_pg()


def test_connect_pg_failure_never_echoes_dsn(monkeypatch):
    """假 psycopg：任何连接失败都归一为 GovernanceError，且不含 DSN 值。"""
    fake = types.ModuleType("psycopg")
    fake.connect = lambda dsn: (_ for _ in ()).throw(RuntimeError("boom"))
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    dsn = "postgresql://user:secret@host:5432/db"
    with pytest.raises(GovernanceError) as ei:
        connect_pg(dsn)
    assert "secret" not in str(ei.value) and "postgresql://" not in str(ei.value)


def test_pg_store_repr_leaks_no_connection_details():
    store = PgConsoleStore(StubConn())
    assert "StubConn" not in repr(store) and "dsn" not in repr(store).lower()
