# coding: utf-8
"""W-11 意图时间线数据接口测试（v2.1 §10；signal_inbox 状态机投影）。

覆盖：mock 种子全链/侧出口覆盖、排序、limit、depth/label 投影、空库、
pg 后端参数化取数（StubConn 桩，锁 SQL 口径）。
"""
from __future__ import annotations

import json

import pytest

from conftest import T0, MINUTE
from console_tui.data import (SIGNAL_STATUS_LABELS, MockConsoleStore,
                              PgConsoleStore, SignalTimelineRow)


def test_mock_timeline_seeded_full_chain_and_exits(store):
    rows = store.timeline()
    statuses = {r.signal_id: r.status for r in rows}
    # 主链全程 + 在途 + 新收件 + 三个侧出口（rejected/suppressed/void 由
    # rejected 与 suppressed 覆盖；void 见 pg 冒烟与 007 触发器测试）
    assert statuses["sig-7001"] == "completed"
    assert statuses["sig-7002"] == "tracking"
    assert statuses["sig-7003"] == "received"
    assert statuses["sig-7004"] == "rejected"
    assert statuses["sig-7005"] == "suppressed"


def test_mock_timeline_sorted_newest_first_and_limit(store):
    rows = store.timeline()
    created = [r.created_at for r in rows]
    assert created == sorted(created, reverse=True)
    assert store.timeline(limit=2) == rows[:2]


def test_timeline_row_projection_fields(store):
    done = next(r for r in store.timeline() if r.signal_id == "sig-7001")
    assert done.depth == 7 and done.status_label == SIGNAL_STATUS_LABELS["completed"]
    assert done.prop_ref.startswith("PROP-") and done.workflow_id.startswith("wf/")
    assert done.ticket_refs == ("task://t-11a", "task://t-11b")
    rejected = next(r for r in store.timeline() if r.signal_id == "sig-7004")
    assert rejected.depth == -1 and rejected.last_reason_code == "NO_CONSUMER"


def test_timeline_empty_without_seed():
    empty = MockConsoleStore(seed=False, now=lambda: T0)
    assert empty.timeline() == []


class _StubConn:
    """最小 psycopg 协议桩：记录 SQL/参数并返回预制行（锁参数化口径）。"""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def cursor(self):
        conn = self
        class _Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params):
                conn.calls.append((sql, params))
            def fetchall(self): return conn.rows
        return _Cur()

    def commit(self): pass


def test_pg_timeline_reads_view_parameterized():
    row = (["sig-9001"], "cnb-issue", "issue.feedback", "completed",
           "PROP-1", "run://r", "wf/x", json.dumps(["task://a"]),
           T0, T0 - 60, 7, "")
    conn = _StubConn([row])
    store = PgConsoleStore(conn, tenant_id="t0")
    out = store.timeline(limit=10)
    sql, params = conn.calls[0]
    assert "glue.v_signal_timeline" in sql and "%s" in sql   # 参数化，无拼接
    assert params == ("t0", 10)
    assert isinstance(out[0], SignalTimelineRow)
    assert out[0].signal_id == "['sig-9001']" or out[0].signal_id == "sig-9001"
    assert out[0].ticket_refs == ("task://a",)
    assert out[0].depth == 7


def test_pg_timeline_json_string_tickets_parsed():
    row = ("sig-9002", "monitoring", "monitoring.insight", "void",
           None, None, None, "[]", T0, T0, 2, None)
    conn = _StubConn([row])
    out = PgConsoleStore(conn, tenant_id="t0").timeline()
    assert out[0].status == "void" and out[0].ticket_refs == ()
    assert out[0].depth == -1 and out[0].last_reason_code == ""
