# coding: utf-8
"""Textual 组件测试（run_test 异步驱动）。textual 是本包硬依赖——已安装则真跑；
若环境未装 textual，本文件整体 skip（唯一允许的降级；data/状态机用例不受影响）。
"""
from __future__ import annotations

import pytest

textual = pytest.importorskip("textual", reason="textual 未安装（组件测试整体 skip）")
pytest.importorskip("pytest_asyncio", reason="pytest-asyncio 未安装（组件测试整体 skip）")

from textual.binding import Binding
from textual.widgets import DataTable, Input

from console_tui.app import (ConsoleApp, HelpModal, NewOrderModal, ResolveModal,
                             SteerModal)
from console_tui.data import USAGE_KIND_LABELS, MockConsoleStore
from console_tui.state import KIND_APPROVE, KIND_DENY, KIND_PAUSE, KIND_RESUME, KIND_STEER


@pytest.fixture()
def store():
    return MockConsoleStore()


def static_text(widget) -> str:
    """textual 版本兼容的 Static 文本读取（8.x=content，旧版=renderable）。"""
    return str(widget.content) if hasattr(widget, "content") else str(widget.renderable)


@pytest.fixture()
async def pilot(store):
    async with ConsoleApp(store=store).run_test(size=(120, 40)) as p:
        yield p


# ── 布局：塔式多列 + 状态栏 + 七面板 ─────────────────────────────────────────

async def test_app_mounts_tower_status_bar_and_eight_panels(pilot):
    app = pilot.app
    assert len(app.query(DataTable)) == 8               # 八面板（线E +服务健康；W-04 计量 + W-11 时间线）
    cols = app.query(".agent-col")                      # 塔列 = 每列一 agent
    assert len(cols) == 4
    bar = static_text(app.query_one("#pool-bar"))
    assert "GPU份额占用" in bar and "不可信节点: 1" in bar
    assert "心跳在线" in bar                            # 线E：状态栏含心跳在线数
    names = {static_text(c.query_one(".agent-name")) for c in cols}
    assert "alpha-planner-01" in names


async def test_bindings_declare_exactly_three_intervention_keys(pilot):
    keys = set()
    for b in ConsoleApp.BINDINGS:
        keys.add(b.key if isinstance(b, Binding) else b[0])
    assert {"s", "a", "p"} <= keys                      # 三级干预写死
    assert "r" in keys and "q" in keys


# ── 干预：s / a / p 走数据层并刷新面板 ───────────────────────────────────────

async def test_steer_submit_writes_audit_and_decision_panel(pilot):
    app = pilot.app
    before = app.query_one("#decisions", DataTable).row_count
    app.action_steer_submit("alpha-planner-01", "先补 guardrail 测试")
    await pilot.pause()
    assert app.store.audit_trail()[-1].kind == KIND_STEER
    assert app.query_one("#decisions", DataTable).row_count == before + 1


async def test_steer_rejection_is_visible_not_crashing(pilot):
    app = pilot.app
    app.action_steer_submit("ghost-agent", "x")         # 未知 agent → notify，不炸
    await pilot.pause()
    assert all(a.kind != KIND_STEER for a in app.store.audit_trail())


async def test_pause_key_toggles_selected_task(pilot):
    app = pilot.app
    _select_task_row(app, "tsk-001")                    # 显式选中（线E 起看板默认排序=状态阻塞优先）
    await pilot.press("p")
    assert app.store.tasks()[0].paused is True
    _select_task_row(app, "tsk-001")                    # 刷新重排后重新选中同一行
    await pilot.press("p")                              # 同一键位恢复（不新增干预键）
    assert app.store.tasks()[0].paused is False
    kinds = [a.kind for a in app.store.audit_trail()[-2:]]
    assert kinds == [KIND_PAUSE, KIND_RESUME]


def _select_task_row(app, task_id: str) -> None:
    """把工单看板光标移到指定任务行（展示序 = app._task_rows）。"""
    rows = app._task_rows
    idx = next(i for i, t in enumerate(rows) if t.task_id == task_id)
    app.query_one("#tasks", DataTable).move_cursor(row=idx)


def _select_challenge_row(app, who_confirms: str) -> str:
    """把 ask 队列光标移到指定 who_confirms 的行，返回其 challenge_id。"""
    rows = app._challenge_rows
    idx = next(i for i, c in enumerate(rows) if c.who_confirms == who_confirms)
    app.query_one("#challenges", DataTable).move_cursor(row=idx)
    return rows[idx].challenge_id


async def test_approve_flow_via_modal_and_y_key(pilot):
    app = pilot.app
    target = _select_challenge_row(app, "user")         # user 级：控制台操作员可裁
    await pilot.press("a")                              # 弹 ResolveModal
    assert isinstance(app.screen, ResolveModal)
    await pilot.press("y")                              # approve 走 Challenge 状态机
    await pilot.pause()
    assert app.store.audit_trail()[-1].kind == KIND_APPROVE
    assert target not in {c.challenge_id for c in app.store.challenges()}


async def test_deny_via_n_key(pilot):
    app = pilot.app
    _select_challenge_row(app, "user")
    await pilot.press("a")
    await pilot.press("n")
    await pilot.pause()
    assert app.store.audit_trail()[-1].kind == KIND_DENY


async def test_approve_refuses_non_user_challenge(pilot):
    """who 维度强制：resource_owner/duty_officer 级 ask 控制台不可代裁——
    按 a 不弹裁决窗、不留 approve 痕（须由对应人在独立界面确认）。"""
    app = pilot.app
    for who in ("resource_owner", "duty_officer"):
        row = next((c for c in app._challenge_rows if c.who_confirms == who), None)
        if row is None:                                 # mock 种子仅含 user/resource_owner
            continue
        _select_challenge_row(app, who)
        await pilot.press("a")
        await pilot.pause()
        assert not isinstance(app.screen, ResolveModal)
    assert all(a.kind != KIND_APPROVE for a in app.store.audit_trail())


async def test_approve_unknown_challenge_is_safe(pilot):
    app = pilot.app
    app.action_resolve_submit("ch-9999", approved=True)  # 未知 challenge → notify
    await pilot.pause()
    assert all(a.kind != KIND_APPROVE for a in app.store.audit_trail())


# ── s 弹窗交互 ───────────────────────────────────────────────────────────────

async def test_steer_modal_opens_and_cancels(pilot):
    app = pilot.app
    await pilot.press("s")
    assert isinstance(app.screen, SteerModal)
    await pilot.press("escape")
    await pilot.pause()
    assert not isinstance(app.screen, SteerModal)
    assert all(a.kind != KIND_STEER for a in app.store.audit_trail())


async def test_steer_modal_submits_from_text_input(pilot):
    app = pilot.app
    await pilot.press("s")
    modal = app.screen
    agent_input = modal.query_one("#steer-agent", Input)
    text_input = modal.query_one("#steer-text", Input)
    agent_input.value = "alpha-planner-01"
    text_input.value = "聚焦租约巡检"
    text_input.focus()
    await pilot.press("enter")                          # Input.Submitted → 提交
    await pilot.pause()
    assert app.store.audit_trail()[-1].kind == KIND_STEER
    assert not isinstance(app.screen, SteerModal)


async def test_refresh_key_repaints_pool_bar(pilot):
    app = pilot.app
    app.store.set_task_pause("tsk-002", pause=True)
    await pilot.press("r")
    await pilot.pause()
    assert app.query_one("#tasks", DataTable).row_count == 6
    assert "mode=mock" in static_text(app.query_one("#pool-bar"))


# ── 计量面板（W-04，v2.0 §4.4）：mock 渲染（截图级证据的 DOM 断言形态）────────

async def test_usage_panel_renders_four_dimensions(pilot):
    """mock 模式计量面板全功能渲染：四行维度 × 5 列，单元格是可读的格式化值。"""
    app = pilot.app
    table = app.query_one("#usage", DataTable)
    assert table.row_count == 4                          # 四个计量维度各一行
    assert len(table.columns) == 5                       # 维度/事件数/总量/最早/最近
    # 面板行键 = 维度 kind；单元格含中英文标签与格式化总量（截图级证据的 DOM 断言）
    keyed = {str(key.value): [str(c) for c in table.get_row(key)]
             for key in table.rows.keys()}
    assert set(keyed) == {"llm_relay", "compute_seconds",
                          "storage_bytes", "sandbox_seconds"}
    assert keyed["llm_relay"][0] == USAGE_KIND_LABELS["llm_relay"]
    assert keyed["llm_relay"][1] == "42"
    assert keyed["llm_relay"][2] == "128,500"            # token 总量带千分位
    assert keyed["storage_bytes"][2].endswith("GB")      # 3_355_443_200 B → GB 展示
    assert keyed["compute_seconds"][2] == "1.4 h"        # 5220 s → 小时展示
    assert keyed["sandbox_seconds"][2] == "31.0 min"     # 1860 s → 分钟展示
    assert all(":" in cell for row in keyed.values()
               for cell in (row[3], row[4]))             # 时间列是 HH:MM


async def test_usage_panel_row_keyed_by_kind_and_refreshable(pilot):
    app = pilot.app
    table = app.query_one("#usage", DataTable)
    assert {str(k.value) for k in table.rows.keys()} >= {
        "llm_relay", "compute_seconds", "storage_bytes", "sandbox_seconds"}
    app.action_refresh()
    await pilot.pause()
    assert app.query_one("#usage", DataTable).row_count == 4


# ── 下单接口（线E，n 键）：弹窗/预览/提交全流程 ────────────────────────────────

async def test_new_order_modal_opens_and_cancels(pilot):
    app = pilot.app
    await pilot.press("n")
    assert isinstance(app.screen, NewOrderModal)
    await pilot.press("escape")
    await pilot.pause()
    assert not isinstance(app.screen, NewOrderModal)
    assert all(a.kind != "create_order" for a in app.store.audit_trail())


async def test_new_order_modal_live_preview_shows_decomposition(pilot):
    app = pilot.app
    await pilot.press("n")
    modal = app.screen
    text_input = modal.query_one("#order-text", Input)
    text_input.value = "[worker-smoke] 出一版周报"
    modal.on_input_changed(Input.Changed(text_input, text_input.value))
    await pilot.pause()
    preview = static_text(modal.query_one("#order-preview"))
    assert "周报" in preview and "worker 可执行" in preview


async def test_n_key_full_flow_order_lands_pending_and_visible(pilot):
    """n 下单全链（mock）：弹窗 → 输入 → Enter → 工单 PENDING 落库 → 看板行可见。"""
    app = pilot.app
    before = app.query_one("#tasks", DataTable).row_count
    await pilot.press("n")
    assert isinstance(app.screen, NewOrderModal)
    text_input = app.screen.query_one("#order-text", Input)
    text_input.value = "[worker-smoke] 出一版周报草稿"
    text_input.focus()
    await pilot.press("enter")
    await pilot.pause()
    assert not isinstance(app.screen, NewOrderModal)
    event = app.store.audit_trail()[-1]
    assert event.kind == "create_order" and event.detail["state"] == "PENDING"
    table = app.query_one("#tasks", DataTable)
    assert table.row_count == before + 1                # 面板已刷新（自动重绘）
    assert event.target in {str(k.value) for k in table.rows.keys()}


async def test_new_order_submit_via_app_method_creates_pending_unowned(pilot):
    app = pilot.app
    app.action_new_order_submit("给 eval-gate 加个 README 徽章")
    await pilot.pause()
    (event,) = [a for a in app.store.audit_trail() if a.kind == "create_order"]
    assert event.detail["kind"] == "自由单" and event.detail["executable"] is False
    (row,) = [t for t in app.store.tasks() if t.task_id == event.target]
    assert row.state == "PENDING" and row.owner == ""   # owner 空串 = worker 可自取


async def test_new_order_empty_instruction_rejected_no_row(pilot):
    app = pilot.app
    before = len(app.store.tasks())
    app.action_new_order_submit("   ")
    await pilot.pause()
    assert len(app.store.tasks()) == before
    assert all(a.kind != "create_order" for a in app.store.audit_trail())


async def test_new_order_claimed_task_becomes_visible_after_refresh(pilot):
    """闭环可见性：下单 → worker 认领（直接改 store）→ r 刷新 → 看板显示 CLAIMED。"""
    app = pilot.app
    app.action_new_order_submit("[worker-smoke] 出一版周报草稿")
    await pilot.pause()
    (event,) = [a for a in app.store.audit_trail() if a.kind == "create_order"]
    from console_tui.data import TaskRow as _TaskRow
    rows = app.store._tasks
    (row,) = [t for t in rows if t.task_id == event.target]
    rows[rows.index(row)] = _TaskRow(**{**row.__dict__, "state": "CLAIMED",
                                        "owner": "windev-worker"})
    await pilot.press("r")
    await pilot.pause()
    table = app.query_one("#tasks", DataTable)
    keyed = {str(k.value): [str(c) for c in table.get_row(k)] for k in table.rows.keys()}
    assert keyed[event.target][2] == "CLAIMED"          # 状态列原文本（Rich Text 的 str）
    assert keyed[event.target][1] == "windev-worker"


# ── 易用性（线E动作4）：帮助 / 排序 / 颜色语义 / 健康面板 / 守护刷新 ──────────

async def test_help_modal_lists_keys_and_color_semantics(pilot):
    app = pilot.app
    await pilot.press("question_mark")
    assert isinstance(app.screen, HelpModal)
    joined = "\n".join(static_text(s) for s in app.screen.query(".help-line"))
    for token in ("n", "s", "a", "p", "o", "绿=运行", "黄=待处理", "红=阻塞"):
        assert token in joined
    await pilot.press("escape")
    await pilot.pause()
    assert not isinstance(app.screen, HelpModal)


async def test_sort_key_cycles_and_reorders_tasks(pilot):
    app = pilot.app
    table = app.query_one("#tasks", DataTable)
    first_default = str(table.get_row_at(0)[0])
    await pilot.press("o")                              # → 创建时间(新→旧)
    await pilot.pause()
    newest = max(app.store.tasks(), key=lambda t: t.created_at)
    assert app._task_rows[0].task_id == newest.task_id
    await pilot.press("o")                              # → Owner 排序
    await pilot.pause()
    assert [t.owner for t in app._task_rows] == sorted(
        [t.owner or "" for t in app.store.tasks()])
    await pilot.press("o")                              # → 状态(阻塞优先) 首键
    await pilot.pause()
    assert app._task_rows[0].state == "BLOCKED"
    assert str(table.get_row_at(0)[0]) == first_default


async def test_task_state_cells_carry_semantic_colors(pilot):
    """颜色语义（线E动作4）：CLAIMED=绿 PENDING=黄 BLOCKED=红 COMPLETED=灰。"""
    app = pilot.app
    table = app.query_one("#tasks", DataTable)
    styled = {str(k.value): table.get_row(k)[2] for k in table.rows.keys()}
    checks = {"tsk-001": "green", "tsk-004": "yellow",
              "tsk-003": "red", "tsk-005": "dim"}
    for task_id, style in checks.items():
        cell = styled[task_id]
        assert getattr(cell, "style", "") == style, f"{task_id} → {style}"


async def test_health_panel_renders_mock_demo_rows_without_probe(pilot):
    app = pilot.app
    assert app.probe is None                            # 测试环境未设探针 env
    table = app.query_one("#health", DataTable)
    keyed = {str(k.value).split(":")[1]: [str(c) for c in table.get_row(k)]
             for k in table.rows.keys()}
    assert keyed["jiuwenswarm-app"][2] == "active"      # mock 演示行（零子进程）
    assert keyed["example-down"][2] == "failed"


async def test_probe_results_render_when_probe_injected():
    from console_tui.health import ServiceHealthRow

    class FakeProbe:
        def probe(self, force=False, now=None):
            return [ServiceHealthRow("anolis-gpu-01", "jiuwenswarm-app",
                                     "active", "active", 1234.0)]

    async with ConsoleApp(store=MockConsoleStore(seed=False),
                          probe=FakeProbe()).run_test(size=(120, 40)) as p:
        await p.pause()
        table = p.app.query_one("#health", DataTable)
        assert table.row_count == 1
        row = table.get_row_at(0)
        assert str(row[0]) == "jiuwenswarm-app" and str(row[2]) == "active"


async def test_pg_mode_without_probe_shows_probe_not_configured_row():
    store = MockConsoleStore(seed=False)
    store.mode = "pg"                                   # 仅走展示分支（不真连）
    async with ConsoleApp(store=store, probe=None).run_test(size=(120, 40)) as p:
        table = p.app.query_one("#health", DataTable)
        row = [str(c) for c in table.get_row_at(0)]
        assert "探针未配置" in row[0] and "CONSOLE_TUI_PROBE_TARGETS" in row[3]


class ExplodingStore(MockConsoleStore):
    """usage 面板数据源爆炸的 mock：守护刷新应显示错误行而不炸控制台。"""

    def usage(self):
        raise RuntimeError("v_usage permission denied (simulated)")


async def test_guarded_refresh_isolates_broken_panel():
    async with ConsoleApp(store=ExplodingStore()).run_test(size=(120, 40)) as p:
        app = p.app
        await p.pause()
        first = str(app.query_one("#usage", DataTable).get_row_at(0)[0])
        assert "数据源暂不可用" in first and "RuntimeError" in first
        assert app.query_one("#tasks", DataTable).row_count == 6   # 其余面板不受影响


async def test_nodes_panel_shows_heartbeat_column_with_stale_semantics(pilot):
    """节点面板（线E动作2）：心跳在窗=绿、超窗=红（标注）、无心跳='-'。"""
    app = pilot.app
    table = app.query_one("#nodes", DataTable)
    keyed = {str(k.value): table.get_row(k) for k in table.rows.keys()}
    fresh, stale, missing = keyed["srv-1"][6], keyed["work-01"][6], keyed["edge-relay"][6]
    assert getattr(fresh, "style", "") == "green"       # T0-45s → 在窗
    assert getattr(stale, "style", "") == "red" and "超窗" in str(stale)  # T0-1800s
    assert str(missing) == "-"
