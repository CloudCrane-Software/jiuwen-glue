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

from console_tui.app import ConsoleApp, ResolveModal, SteerModal
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

async def test_app_mounts_tower_status_bar_and_seven_panels(pilot):
    app = pilot.app
    assert len(app.query(DataTable)) == 7               # 七个面板（W-04 计量 + W-11 意图时间线）
    cols = app.query(".agent-col")                      # 塔列 = 每列一 agent
    assert len(cols) == 4
    bar = static_text(app.query_one("#pool-bar"))
    assert "GPU份额占用" in bar and "不可信节点: 1" in bar
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
    await pilot.press("p")                              # tsk-001 是看板首行
    assert app.store.tasks()[0].paused is True
    await pilot.press("p")                              # 同一键位恢复（不新增干预键）
    assert app.store.tasks()[0].paused is False
    kinds = [a.kind for a in app.store.audit_trail()[-2:]]
    assert kinds == [KIND_PAUSE, KIND_RESUME]


async def test_approve_flow_via_modal_and_y_key(pilot):
    app = pilot.app
    target = app.store.challenges()[0].challenge_id
    await pilot.press("a")                              # 弹 ResolveModal（默认选中首行）
    assert isinstance(app.screen, ResolveModal)
    await pilot.press("y")                              # approve 走 Challenge 状态机
    await pilot.pause()
    assert app.store.audit_trail()[-1].kind == KIND_APPROVE
    assert target not in {c.challenge_id for c in app.store.challenges()}


async def test_deny_via_n_key(pilot):
    app = pilot.app
    await pilot.press("a")
    await pilot.press("n")
    await pilot.pause()
    assert app.store.audit_trail()[-1].kind == KIND_DENY


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
