# coding: utf-8
"""console-tui 治理面作战室（WO-0012 / PROP-0005，PROP-0001 v1.7 §12.3）.

塔式多列布局（借 tower 模式显示形态）+ 顶部节点池状态栏 + 五个数据面板
（工单看板/租约/ask 审批队列/决策记录/节点利用率）+ 三级干预快捷键。

**分层边界（写死）**：执行面 = jiuwenswarm 自带 TUI（人机交互/对话/任务执行）；
本工具 = 治理面，只读 glue 数据 + 受控三级干预（s/a/p），不替代执行面交互，
不直连任何执行面实例——干预只经数据层落审计（GuardrailRun Challenge 语义）。

三级干预（12.3 写死，不得增加）:
  s  steer  —— 向指定 agent 注入 ``_steer`` 指令（可逆，弹窗输入）
  a  approve—— 对 ask 审批队列选中 Challenge 裁决 approve/deny
               （走 Challenge 结构化对象状态机，不绕过）
  p  pause  —— 对工单看板选中任务切换 暂停/恢复（任务级 pause 标记）

其余键：r=刷新；q=退出。每次 s/a/p 都产生一条可审计事件（pg: decision_record
+ challenge 状态转移；mock: 内存审计表），经 ``L`` 键可在决策面板复核。
"""
from __future__ import annotations

import time
from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import HorizontalScroll, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Static, TabbedContent, TabPane

from .data import ChallengeRow, ConsoleStore, MockConsoleStore, TaskRow, connect_pg
from .state import OPERATOR

PANE_TASKS = "pane-tasks"
PANE_LEASES = "pane-leases"
PANE_CHALLENGES = "pane-challenges"
PANE_DECISIONS = "pane-decisions"
PANE_NODES = "pane-nodes"

_HEARTBEAT_WARN = 900.0     # 心跳超过 15 分钟视为可疑（展示层提示，不做决策）


class SteerModal(ModalScreen[None]):
    """s 干预弹窗：输入目标 agent 与 _steer 指令文本（可逆干预，留痕后生效）。"""

    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, agent_ref: str = "") -> None:
        super().__init__()
        self._agent_ref = agent_ref

    def compose(self) -> ComposeResult:
        with Vertical(id="steer-dialog"):
            yield Static("s = steer：向 agent 注入 _steer 指令（可逆；全程留痕）", id="steer-title")
            yield Input(value=self._agent_ref, placeholder="目标 agent（塔列名）", id="steer-agent")
            yield Input(placeholder="注入的指令文本（_steer）", id="steer-text")
            yield Static("Enter 提交 / Esc 取消", id="steer-hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "steer-agent":
            self.query_one("#steer-text", Input).focus()
            return
        agent = self.query_one("#steer-agent", Input).value.strip()
        text = event.input.value.strip()
        self.app.action_steer_submit(agent, text)
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ResolveModal(ModalScreen[None]):
    """a 干预弹窗：对选中的 Challenge 裁决 approve（y）/ deny（n）/ 取消（Esc）。

    裁决走 data 层的 Challenge 状态机；过期 Challenge 会被拒绝（fail-closed）。
    """

    BINDINGS = [("y", "approve", "approve"), ("n", "deny", "deny"), ("escape", "cancel", "取消")]

    def __init__(self, challenge_id: str, summary: str) -> None:
        super().__init__()
        self._challenge_id = challenge_id
        self._summary = summary

    def compose(self) -> ComposeResult:
        with Vertical(id="resolve-dialog"):
            yield Static(f"a = ask 审批：{self._challenge_id}", id="resolve-title")
            yield Static(self._summary, id="resolve-summary")
            yield Static("y=approve / n=deny / Esc=取消", id="resolve-hint")

    def action_approve(self) -> None:
        self.app.action_resolve_submit(self._challenge_id, True)
        self.dismiss(None)

    def action_deny(self) -> None:
        self.app.action_resolve_submit(self._challenge_id, False)
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConsoleApp(App):
    """治理面作战室 TUI。store 可注入（测试用 mock；生产 CONSOLE_TUI_DSN → pg）。"""

    TITLE = "jiuwen console — governance plane (WO-0012 / v1.7 §12.3)"
    CSS = """
    #pool-bar { height: 1; background: $panel; color: $text; padding: 0 1; }
    #tower { height: 40%; border: round $primary; }
    .agent-col { width: 1fr; border-right: solid $secondary; padding: 0 1; }
    .agent-name { text-style: bold; }
    .agent-status-working { color: $success; }
    .agent-status-blocked { color: $error; }
    .agent-status-idle { color: $text-muted; }
    .agent-task { color: $text; }
    .agent-heart { color: $text-muted; }
    DataTable { height: 100%; }
    #steer-dialog, #resolve-dialog {
        width: 60%; height: auto; border: thick $accent; background: $surface;
        padding: 1 2; }
    #resolve-summary { margin: 1 0; }
    """
    BINDINGS = [
        ("r", "refresh", "刷新"),
        ("s", "steer", "steer 注入"),
        ("a", "approve", "审批 ask"),
        ("p", "pause", "暂停/恢复"),
        ("q", "quit", "退出"),
    ]

    def __init__(self, store: Optional[ConsoleStore] = None) -> None:
        super().__init__()
        if store is None:
            import os
            store = connect_pg() if os.environ.get("CONSOLE_TUI_DSN") else MockConsoleStore()
        self.store = store
        self._task_rows: list[TaskRow] = []
        self._challenge_rows: list[ChallengeRow] = []
        self.sub_title = f"mode={store.mode}"

    # ── 布局 ──────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("", id="pool-bar")
        with HorizontalScroll(id="tower"):
            yield Vertical(id="tower-slots")
        with TabbedContent(id="panels"):
            yield TabPane("工单看板", DataTable(id="tasks"), id=PANE_TASKS)
            yield TabPane("租约", DataTable(id="leases"), id=PANE_LEASES)
            yield TabPane("ask 审批队列", DataTable(id="challenges"), id=PANE_CHALLENGES)
            yield TabPane("决策记录", DataTable(id="decisions"), id=PANE_DECISIONS)
            yield TabPane("节点利用率", DataTable(id="nodes"), id=PANE_NODES)
        yield Footer()

    def on_mount(self) -> None:
        tasks = self.query_one("#tasks", DataTable)
        tasks.add_columns("任务", "owner", "状态", "暂停", "交付物")
        leases = self.query_one("#leases", DataTable)
        leases.add_columns("租约", "task_ref", "额度", "余量", "状态", "已用%")
        chal = self.query_one("#challenges", DataTable)
        chal.add_columns("challenge", "谁确认", "资源", "动作", "agent", "剩余秒")
        dec = self.query_one("#decisions", DataTable)
        dec.add_columns("决策", "决策者", "选择", "上下文哈希", "留痕")
        nodes = self.query_one("#nodes", DataTable)
        nodes.add_columns("节点", "CPU份额", "GPU份额", "信任", "并行", "在线窗口")
        for dt in self.query(DataTable):
            dt.cursor_type = "row"
        self.action_refresh()

    # ── 刷新（面板与塔列全量重绘；数据层是唯一事实来源）───────────────────
    def action_refresh(self) -> None:
        summary = self.store.pool_summary()
        self.query_one("#pool-bar", Static).update(
            f"节点池: {summary.node_count} 节点 | GPU份额占用(gpu_frac合计): "
            f"{summary.gpu_frac_total:.2f}（其中可信 {summary.gpu_frac_trusted:.2f}）"
            f" | 不可信节点: {summary.untrusted_count} | 并行槽位: {summary.max_parallel_total}"
            f" | mode={self.store.mode}")
        self._refresh_tower()
        self._refresh_tasks()
        self._refresh_leases()
        self._refresh_challenges()
        self._refresh_decisions()
        self._refresh_nodes()

    def _refresh_tower(self) -> None:
        slots = self.query_one("#tower-slots", Vertical)
        slots.remove_children()
        for agent in self.store.agents():
            heart = (f"心跳 {int(agent.heartbeat_seconds)}s 前"
                     + ("（>15min 可疑）" if agent.heartbeat_seconds > _HEARTBEAT_WARN else ""))
            col = Vertical(classes=f"agent-col agent-status-{agent.status}")
            slots.mount(col)
            col.mount(Static(agent.agent_ref, classes="agent-name"))
            col.mount(Static(agent.status, classes=f"agent-status-{agent.status}"))
            col.mount(Static(agent.current_task, classes="agent-task"))
            col.mount(Static(heart, classes="agent-heart"))

    def _refresh_tasks(self) -> None:
        table = self.query_one("#tasks", DataTable)
        table.clear()
        self._task_rows = self.store.tasks()
        for t in self._task_rows:
            table.add_row(t.title, t.owner, t.state, "⏸ 已暂停" if t.paused else "",
                          t.deliverable, key=t.task_id)

    def _refresh_leases(self) -> None:
        table = self.query_one("#leases", DataTable)
        table.clear()
        for x in self.store.leases():
            table.add_row(x.lease_id, x.task_ref, str(x.amount), str(x.remaining),
                          x.status, f"{x.utilization * 100:.0f}", key=x.lease_id)

    def _refresh_challenges(self) -> None:
        table = self.query_one("#challenges", DataTable)
        table.clear()
        self._challenge_rows = self.store.challenges()
        now = self.store_now()
        for c in self._challenge_rows:
            table.add_row(c.challenge_id, c.who_confirms, c.resource, c.action,
                          c.agent_identity_ref, f"{c.seconds_left(now):.0f}",
                          key=c.challenge_id)

    def _refresh_decisions(self) -> None:
        table = self.query_one("#decisions", DataTable)
        table.clear()
        for d in self.store.decisions(limit=30):
            tag = str(d.meta.get("intervention", ""))
            table.add_row(d.decision_id[:8], d.agent_ref, d.chosen,
                          d.context_hash[:12], tag or "-", key=d.decision_id)

    def _refresh_nodes(self) -> None:
        table = self.query_one("#nodes", DataTable)
        table.clear()
        for n in self.store.nodes():
            table.add_row(n.node_id, f"{n.cpu_frac:.2f}", f"{n.gpu_frac:.2f}",
                          n.trust_level, str(n.max_parallel), n.online_window,
                          key=n.node_id)

    def store_now(self) -> float:
        """取数时钟：mock 有可控时钟；pg 用 wall clock（仅展示剩余秒）。"""
        return self.store.now()

    # ── 三级干预：s / a / p ───────────────────────────────────────────────
    def action_steer(self) -> None:
        agents = self.store.agents()
        prefill = agents[0].agent_ref if agents else ""
        self.push_screen(SteerModal(prefill))

    def action_steer_submit(self, agent_ref: str, text: str) -> None:
        """s 干预落库（SteerModal 提交；测试可直接调用）。"""
        try:
            event = self.store.steer(agent_ref, text, by=OPERATOR)
            self.notify(f"steer 已留痕: {event.target}")
        except Exception as exc:   # 治理拒绝也要可见，但不当决策点
            self.notify(f"steer 被拒绝: {exc}", severity="error")
        self.action_refresh()

    def _selected_challenge(self) -> Optional[ChallengeRow]:
        table = self.query_one("#challenges", DataTable)
        if table.row_count == 0:
            return None
        idx = max(0, min(table.cursor_row or 0, table.row_count - 1))
        return self._challenge_rows[idx]

    def action_approve(self) -> None:
        ch = self._selected_challenge()
        if ch is None:
            self.notify("ask 审批队列无选中项", severity="warning")
            return
        summary = (f"{ch.agent_identity_ref} 想对 {ch.resource} 执行 {ch.action}"
                   f"（确认人: {ch.who_confirms}，剩余 {ch.seconds_left(self.store_now()):.0f}s）")
        self.push_screen(ResolveModal(ch.challenge_id, summary))

    def action_resolve_submit(self, challenge_id: str, approved: bool) -> None:
        """a 干预落库（ResolveModal 的 y/n；走 Challenge 状态机）。"""
        try:
            event = self.store.resolve_challenge(challenge_id, approved, by=OPERATOR)
            self.notify(f"{event.kind} 已留痕: {event.target}")
        except Exception as exc:
            self.notify(f"裁决被拒绝: {exc}", severity="error")
        self.action_refresh()

    def _selected_task(self) -> Optional[TaskRow]:
        table = self.query_one("#tasks", DataTable)
        if table.row_count == 0:
            return None
        idx = max(0, min(table.cursor_row or 0, table.row_count - 1))
        return self._task_rows[idx]

    def action_pause(self) -> None:
        task = self._selected_task()
        if task is None:
            self.notify("工单看板无选中任务", severity="warning")
            return
        self.action_pause_submit(task.task_id, not task.paused)

    def action_pause_submit(self, task_id: str, pause: bool) -> None:
        """p 干预落库（暂停/恢复同一键位复用，不新增干预键）。"""
        try:
            event = self.store.set_task_pause(task_id, pause, by=OPERATOR)
            self.notify(f"{event.kind} 已留痕: {event.target}")
        except Exception as exc:
            self.notify(f"暂停干预被拒绝: {exc}", severity="error")
        self.action_refresh()


def main(store: Optional[ConsoleStore] = None) -> None:
    """入口：``jiuwen-console`` / ``python -m console_tui``。

    模式选择：设置 CONSOLE_TUI_DSN → pg 模式（需 pip install 'jiuwen-console-tui[pg]'）；
    未设置 → mock 演示模式（内存假数据，干预落内存审计表）。
    """
    ConsoleApp(store).run()


if __name__ == "__main__":
    main()
