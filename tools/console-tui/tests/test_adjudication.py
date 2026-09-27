# coding: utf-8
"""就绪包裁决卡测试（W-06，v2.1 §4.7）：四件套渲染 + approve/escalate 两键 +
mock 留痕 + pg SQL 接口（参数化）+ 状态机 fail-closed。

分层与 test_app.py 同款：textual 组件用例在缺 textual 环境整体 skip；
渲染纯函数 / mock 数据层 / pg SQL 桩用例不受影响。
"""
from __future__ import annotations

import uuid

import pytest

from console_tui.adjudication import AdjudicationCardScreen, render_readiness_card
from console_tui.data import (HARD_LIST_CATEGORIES, MockConsoleStore, PgConsoleStore)
from console_tui.state import (CARD_APPROVED, CARD_PENDING, CARD_RETURNED,
                               KIND_APPROVE, KIND_ESCALATE_BACK,
                               AdjudicationResolutionError, UnknownTargetError,
                               resolve_adjudication_state)

# 本仓 jiuwen_glue 可导入时交叉校验四类硬清单不漂移（data.py 注明的防漂移纪律）
jiuwen_glue = pytest.importorskip("jiuwen_glue", reason="jiuwen_glue 不在路径（独立安装时跳过交叉校验）")


def test_hard_list_categories_match_glue():
    """四类硬清单与 glue.escalation.HARD_LIST 同源（防漂移交叉校验）。"""
    assert set(HARD_LIST_CATEGORIES) == set(jiuwen_glue.escalation.HARD_LIST)


# ── 渲染纯函数 ────────────────────────────────────────────────────────────────

def test_render_readiness_card_shows_four_pieces_and_keys():
    """裁决卡渲染：四件套逐件 + 两键提示 + 签名/范畴/工单头（READY 卡）。"""
    store = MockConsoleStore()
    card = next(c for c in store.adjudication_cards() if c.card_id == "card-4001")
    text = "\n".join(render_readiness_card(card))
    assert "人类就绪包裁决卡" in text and "card-4001" in text and "tsk-003" in text
    for label in ("① 事实固定", "② 范畴清晰", "③ 权限内无解", "④ 可逆性评估"):
        assert label in text
    assert text.count("PASS") == 4                       # READY 卡四件全 PASS
    assert "BLOCKED" not in text
    assert card.category in HARD_LIST_CATEGORIES
    assert "y=approve" in text and "e=escalate" in text


def test_render_readiness_card_blocked_shows_missing_and_knock_back():
    """BLOCKED 卡渲染：缺料逐件列出 + 未归类 → 打回 L3 提示。"""
    store = MockConsoleStore()
    card = next(c for c in store.adjudication_cards() if c.card_id == "card-4002")
    text = "\n".join(render_readiness_card(card))
    assert "BLOCKED" in text and "打回 L3" in text
    assert "facts_fixed" in text and "reversibility" in text      # 缺料键名可见
    assert "未归类" in text


# ── 裁决状态机（纯函数）───────────────────────────────────────────────────────

def test_adjudication_state_machine_fail_closed():
    """approve 键 fail-closed：包非 READY 一律拒绝；escalate 键对 pending 放行；
    终态不可再裁；无裁决人拒绝。"""
    assert resolve_adjudication_state(CARD_PENDING, ready=True, approved=True,
                                      by="op") == (CARD_APPROVED, "op")
    assert resolve_adjudication_state(CARD_PENDING, ready=False, approved=False,
                                      by="op") == (CARD_RETURNED, "op")
    with pytest.raises(AdjudicationResolutionError):     # 非 READY 批准 → 拒
        resolve_adjudication_state(CARD_PENDING, ready=False, approved=True, by="op")
    with pytest.raises(AdjudicationResolutionError):     # 终态再裁 → 拒
        resolve_adjudication_state(CARD_APPROVED, ready=True, approved=True, by="op")
    with pytest.raises(AdjudicationResolutionError):     # 无裁决人 → 拒
        resolve_adjudication_state(CARD_PENDING, ready=True, approved=True, by="")


# ── mock 数据层：队列 + 两键 + 留痕 ──────────────────────────────────────────

def test_mock_cards_queue_only_pending_sorted(store):
    """裁决队列只含 pending 卡，按生成时间先到先裁。"""
    cards = store.adjudication_cards()
    assert [c.card_id for c in cards] == ["card-4001", "card-4002"]
    assert all(c.state == CARD_PENDING for c in cards)
    store.resolve_adjudication("card-4001", approved=True)       # 裁决后离队
    assert [c.card_id for c in store.adjudication_cards()] == ["card-4002"]


def test_mock_approve_ready_card_routes_to_human_and_audits(store):
    """approve 键：READY 卡批准 → state=approved（递呈人类 L5）+ 决策留痕。"""
    event = store.resolve_adjudication("card-4001", approved=True, by="console:operator")
    assert event.kind == KIND_APPROVE and event.target == "card-4001"
    card = next(c for c in store._card_by_id.values() if c.card_id == "card-4001")
    assert card.state == CARD_APPROVED
    assert store._decisions[-1].chosen == KIND_APPROVE           # append-only 留痕


def test_mock_approve_blocked_card_is_rejected_fail_closed(store):
    """fail-closed 主验证：BLOCKED 卡按 y → 拒绝（AdjudicationResolutionError），
    卡保持 pending，无任何留痕写入；e 键（打回 L3）安全方向放行。"""
    n_audit, n_decisions = len(store.audit_trail()), len(store._decisions)
    with pytest.raises(AdjudicationResolutionError):
        store.resolve_adjudication("card-4002", approved=True)
    card = next(c for c in store._card_by_id.values() if c.card_id == "card-4002")
    assert card.state == CARD_PENDING
    assert len(store.audit_trail()) == n_audit and len(store._decisions) == n_decisions
    event = store.resolve_adjudication("card-4002", approved=False)
    assert event.kind == KIND_ESCALATE_BACK
    assert next(c for c in store._card_by_id.values()
                if c.card_id == "card-4002").state == CARD_RETURNED


def test_mock_unknown_card_and_double_resolve(store):
    """未知卡拒绝；已决卡再裁拒绝（终态不可改）。"""
    with pytest.raises(UnknownTargetError):
        store.resolve_adjudication("card-ghost", approved=True)
    store.resolve_adjudication("card-4001", approved=False)      # 打回 L3（终态）
    with pytest.raises(AdjudicationResolutionError):
        store.resolve_adjudication("card-4001", approved=True)


# ── pg SQL 接口（[待 DDL]；桩连接验证走视图/表 + 参数化）────────────────────

class StubCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.log.append((sql, tuple(params or ())))
        rows, rowcount = self.conn.queue.pop(0) if self.conn.queue else ([], 0)
        self._rows, self.rowcount = rows, rowcount

    def fetchall(self):
        return list(self._rows)


class StubConn:
    def __init__(self, queue=None):
        self.queue = list(queue or [])
        self.log = []
        self.commits = 0

    def cursor(self):
        return StubCursor(self)

    def commit(self):
        self.commits += 1


def test_pg_cards_read_goes_through_view_with_params():
    """pg 读裁决队列走 glue.v_readiness_card 且参数化（SQL 接口先留，[待 DDL]）。"""
    conn = StubConn()
    PgConsoleStore(conn).adjudication_cards()
    (sql, params), = conn.log
    assert "glue.v_readiness_card" in sql
    assert "%s" in sql and params == ("t0",)


def test_pg_resolve_adjudication_guarded_update_and_audit_insert():
    """pg 两键写路径：先 SELECT state/ready（状态机校验）→ 守卫 UPDATE → 决策留痕 INSERT。"""
    conn = StubConn(queue=[([("pending", True)], 0), ([], 1), ([], 0)])
    event = PgConsoleStore(conn).resolve_adjudication(str(uuid.uuid4()), approved=True)
    assert event.kind == KIND_APPROVE
    select_sql, update_sql, insert_sql = (s for s, _ in conn.log)
    assert "glue.readiness_card" in select_sql and "%s" in select_sql
    assert "state = 'pending'" in update_sql and "%s" in update_sql   # 守卫第二道闸
    assert "glue.decision_record" in insert_sql and "%s" in insert_sql
    assert conn.commits == 3


def test_pg_resolve_blocked_card_fails_in_state_machine_before_update():
    """pg 侧 fail-closed：ready=False 的 approve 在状态机即拒（不发 UPDATE）。"""
    conn = StubConn(queue=[([("pending", False)], 0)])
    with pytest.raises(AdjudicationResolutionError):
        PgConsoleStore(conn).resolve_adjudication(str(uuid.uuid4()), approved=True)
    assert len(conn.log) == 1                                    # 只发了 SELECT


# ── textual 组件（run_test 异步驱动；缺 textual 整体 skip）───────────────────

textual = pytest.importorskip("textual", reason="textual 未安装（组件测试整体 skip）")
pytest.importorskip("pytest_asyncio", reason="pytest-asyncio 未安装（组件测试整体 skip）")

from console_tui.app import ConsoleApp  # noqa: E402  （app 依赖 textual，须在 importorskip 之后）


async def test_adjudication_screen_renders_four_pieces():
    """裁决卡弹层渲染（mock 模式）：四件套逐件 + 两键提示。"""
    store = MockConsoleStore()
    async with ConsoleApp(store=store).run_test(size=(120, 40)) as pilot:
        card_id = store.adjudication_cards()[0].card_id
        pilot.app.open_adjudication(card_id)
        await pilot.pause()
        assert isinstance(pilot.app.screen, AdjudicationCardScreen)
        joined = "\n".join(
            str(getattr(w, "content", getattr(w, "renderable", "")))
            for w in pilot.app.screen.query(".adjudication-line"))
        for label in ("① 事实固定", "② 范畴清晰", "③ 权限内无解", "④ 可逆性评估",
                      "y=approve", "e=escalate"):
            assert label in joined


async def test_adjudication_two_keys_resolve_via_data_layer():
    """y=approve 走数据层批准（READY 卡）；e=escalate 打回 L3；全部留痕。"""
    store = MockConsoleStore()
    async with ConsoleApp(store=store).run_test(size=(120, 40)) as pilot:
        ready_id = store.adjudication_cards()[0].card_id         # card-4001 READY
        pilot.app.open_adjudication(ready_id)
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert store.audit_trail()[-1].kind == KIND_APPROVE
        assert not isinstance(pilot.app.screen, AdjudicationCardScreen)

        blocked_id = store.adjudication_cards()[0].card_id       # card-4002 BLOCKED
        pilot.app.open_adjudication(blocked_id)
        await pilot.pause()
        await pilot.press("e")                                   # 安全方向：打回 L3
        await pilot.pause()
        assert store.audit_trail()[-1].kind == KIND_ESCALATE_BACK
        assert store.adjudication_cards() == []                  # 队列清空
