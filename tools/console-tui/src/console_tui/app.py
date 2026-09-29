# coding: utf-8
"""console-tui 治理面作战室（WO-0012 / PROP-0005，PROP-0001 v1.7 §12.3；W-04 计量面板）.

塔式多列布局（借 tower 模式显示形态）+ 顶部节点池状态栏 + 八个数据面板
（工单看板/租约/ask 审批队列/决策记录/节点利用率/计量 usage/意图时间线/服务健康）
+ 三级干预快捷键 + 下单接口。

**分层边界（写死）**：执行面 = jiuwenswarm 自带 TUI（人机交互/对话/任务执行）；
本工具 = 治理面，只读 glue 数据 + 受控三级干预（s/a/p）+ 下单接口（n），
不替代执行面交互，不直连任何执行面实例——干预只经数据层落审计
（GuardrailRun Challenge 语义）。

三级干预（12.3 写死，不得增加）:
  s  steer  —— 向指定 agent 注入 ``_steer`` 指令（可逆，弹窗输入）
  a  approve—— 对 ask 审批队列选中 Challenge 裁决 approve/deny
               （走 Challenge 结构化对象状态机，不绕过）
  p  pause  —— 对工单看板选中任务切换 暂停/恢复（任务级 pause 标记）

下单接口（线E，2026-09-29；**创建新对象，不是干预**——不触碰既有对象状态机，
三级干预键位 s/a/p 不变）:
  n  new order —— 自然语言指令 → intake.decompose（RULES：周报/复审/测试/部署
               模板，匹配不上落 PENDING 等人工拆）→ team_task(PENDING, owner=NULL)
               → worker 自取执行；本面板可观察认领/完成（自动刷新）。

易用性（线E动作4，设计借鉴 docs/tui-research.md 落地）:
  ?  帮助栏 —— 全键位 + 颜色语义图例（kanban-md 看板语义 / lazyagent 状态归一）
  o  看板排序 —— 工单看板排序轮换（状态阻塞优先/创建时间/owner）
  颜色语义 —— 绿=运行（CLAIMED/服务 active）黄=待处理（PENDING/unreachable）
            红=阻塞（BLOCKED/failed/心跳超窗）灰=终态（COMPLETED/CANCELLED）
  自动刷新 —— 30s 全面板轮询 + 75s 服务健康探针（探针 TTL 缓存 60s）

其余键：r=刷新；q=退出。每次 s/a/p 都产生一条可审计事件（pg: decision_record
+ challenge 状态转移；mock: 内存审计表），经决策面板复核。
n 下单的留痕 = team_task 行 + task_transition（NULL→PENDING, source='admin'）。

面板刷新是**逐面板守护**的：单个数据源缺失/未授权（如 [待 DDL] 的裁决卡视图）
只在该面板显示"数据源暂不可用"行，不拖垮整个控制台（fail-open 展示；写路径
fail-closed 语义不变）。

裁决卡（W-06，v2.1 §4.7 升级体系）：``open_adjudication(card_id)`` 打开人类就绪包
四件套裁决卡，y=approve（递呈人类，包必须 READY）/ e=escalate（打回 L3）两键
经数据层状态机留痕——不占用全局键位，三级干预 s/a/p 写死不变。
"""
from __future__ import annotations

import time
from typing import List, Optional

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import HorizontalScroll, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Static, TabbedContent, TabPane

from .data import (USAGE_KIND_LABELS, ChallengeRow, ConsoleStore,
                   MockConsoleStore, TaskRow, connect_pg, format_quantity)
from .adjudication import AdjudicationCardScreen
from .health import (PROBE_ENV, SshServiceProbe, ServiceHealthRow, demo_rows)
from .intake import TaskDraft, decompose
from .state import OPERATOR, GovernanceError, confirmer_may_resolve

PANE_TASKS = "pane-tasks"
PANE_LEASES = "pane-leases"
PANE_CHALLENGES = "pane-challenges"
PANE_DECISIONS = "pane-decisions"
PANE_NODES = "pane-nodes"
PANE_USAGE = "pane-usage"
PANE_TIMELINE = "pane-timeline"
PANE_HEALTH = "pane-health"

_HEARTBEAT_WARN = 900.0     # 心跳超过 15 分钟视为可疑/超窗（展示层提示，不做决策）

# 颜色语义（线E动作4；绿=运行 黄=待处理/过渡 红=阻塞/坏 灰=终态）。
# 工单状态色（kanban-md 看板语义映射）；服务健康色复用 health 归一词表。
_STATE_STYLES = {
    "PENDING": "yellow",       # 待派（owner 下单后等 worker 认领）
    "CLAIMED": "green",        # 运行中
    "BLOCKED": "red",          # 阻塞（等人工）
    "COMPLETED": "dim",        # 终态
    "CANCELLED": "dim",        # 终态
}
_HEALTH_STYLES = {
    "active": "green", "starting": "yellow", "stopped": "yellow",
    "failed": "red", "unreachable": "red", "unknown": "yellow",
}

# 工单看板排序轮换（o 键；确定性 key 函数，首键=阻塞优先的作战视角）
_TASK_SORTS = (
    ("状态(阻塞优先)",
     lambda t: ({"BLOCKED": 0, "PENDING": 1, "CLAIMED": 2,
                 "COMPLETED": 3, "CANCELLED": 4}.get(t.state, 9), -t.created_at)),
    ("创建时间(新→旧)", lambda t: -t.created_at),
    ("Owner", lambda t: (t.owner or "", t.state, -t.created_at)),
)

_HELP_LINES = (
    "[b]键位[/b]",
    "  [b]n[/b] 新建工单（自然语言下单 → RULES 分解 → PENDING 落库 → worker 自取）",
    "  [b]s[/b] steer 注入   [b]a[/b] 审批 ask   [b]p[/b] 暂停/恢复（三级干预，全留痕）",
    "  [b]r[/b] 刷新   [b]o[/b] 工单看板排序   [b]?[/b] 本帮助   [b]q[/b] 退出",
    "",
    "[b]颜色语义[/b]",
    "  [green]绿=运行[/green]（CLAIMED / 服务 active / 心跳在窗）  "
    "[yellow]黄=待处理[/yellow]（PENDING / 探测中/未知 / 服务 stopped）",
    "  [red]红=阻塞[/red]（BLOCKED / 服务 failed / 探针 unreachable / 心跳超窗）  "
    "[dim]灰=终态[/dim]（COMPLETED / CANCELLED）",
    "",
    "[b]下单[/b]：内置模板匹配 周报/复审/测试/部署；未匹配 → PENDING 等人工拆"
    "（不硬造 LLM 依赖）。指令开头 [worker-smoke] 等标签原样保留（冒烟红线兼容）。",
    "[b]边界[/b]：治理面只读 glue + 受控写路径；执行面交互归 jiuwenswarm TUI。",
)


def _fmt_ts(ts: Optional[float]) -> str:
    """epoch → 本地 HH:MM 展示（仅展示层；None = 该维度暂无事件）。"""
    if ts is None:
        return "-"
    import time as _time
    return _time.strftime("%H:%M", _time.localtime(ts))


def _fmt_heartbeat(at: Optional[float], now: float) -> Text:
    """节点心跳龄展示（线E）：在窗=绿、超窗=红（标注）、无心跳=灰"-"。"""
    if at is None:
        return Text("-", style="dim")
    age = max(0.0, now - at)
    label = f"{int(age // 60)}min 前" if age >= 90 else f"{int(age)}s 前"
    if age > _HEARTBEAT_WARN:
        return Text(f"{label}（超窗）", style="red")
    return Text(label, style="green")


_PROBE_UNSET = object()
"""探针参数哨兵：未显式传 probe 时从 CONSOLE_TUI_PROBE_TARGETS 构造（None=显式关闭）。"""


def _fmt_ts(ts: Optional[float]) -> str:
    """epoch → 本地 HH:MM 展示（仅展示层；None = 该维度暂无事件）。"""
    if ts is None:
        return "-"
    import time as _time
    return _time.strftime("%H:%M", _time.localtime(ts))


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


class NewOrderModal(ModalScreen[None]):
    """n 下单弹窗：输入自然语言指令 → 实时分解预览 → Enter 落 team_task(PENDING)。

    分解是纯 RULES（intake.decompose，周报/复审/测试/部署模板，未匹配=等人工拆），
    预览与提交走同一条分解路径（无第二个决策点）；弹窗内 ``n`` 键不被占用
    （本弹窗只响应 Enter/Esc——ResolveModal 里的 n=deny 互不影响）。
    """

    BINDINGS = [("escape", "cancel", "取消")]

    def compose(self) -> ComposeResult:
        with Vertical(id="order-dialog"):
            yield Static("n = 新建工单：指令 → RULES 分解（周报/复审/测试/部署）→ PENDING → worker 自取",
                         id="order-title")
            yield Input(placeholder="例：给 eval-gate 加个 README 徽章 / [worker-smoke] 出一版周报",
                        id="order-text")
            yield Static("（输入指令后此处显示分解预览）", id="order-preview")
            yield Static("Enter 提交 / Esc 取消；开头 [标签] 原样保留（如 [worker-smoke] 冒烟隔离）",
                         id="order-hint")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "order-text":
            return
        preview = self.query_one("#order-preview", Static)
        if not event.input.value.strip():
            preview.update("（输入指令后此处显示分解预览）")
            return
        try:
            drafts = decompose(event.input.value)
        except GovernanceError as exc:
            preview.update(f"（{exc}）")
            return
        lines = [f"→ {d.kind_label}{'（worker 可执行）' if d.executable else '（等人工拆）'}:"
                 f" {d.title}" for d in drafts]
        preview.update("\n".join(lines))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.input.value.strip()
        self.app.action_new_order_submit(text)
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpModal(ModalScreen[None]):
    """? 帮助弹窗：全键位 + 颜色语义图例（线E动作4；Esc 或 ? 关闭）。"""

    BINDINGS = [("escape", "cancel", "关闭"), ("question_mark", "cancel", "关闭")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-dialog"):
            for line in _HELP_LINES:
                yield Static(line, classes="help-line")

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConsoleApp(App):
    """治理面作战室 TUI。store 可注入（测试用 mock；生产 CONSOLE_TUI_DSN → pg）。

    probe：跨机服务健康探针（线E）。sentinel ``_PROBE_UNSET`` = 从
    ``CONSOLE_TUI_PROBE_TARGETS`` 构造（未设置则 None=探针关闭）；显式传 None
    可强制关闭（测试用）。探针经线程 worker 执行，UI 不因 ssh 阻塞。
    """

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
    #steer-dialog, #resolve-dialog, #adjudication-card, #order-dialog, #help-dialog {
        width: 60%; height: auto; border: thick $accent; background: $surface;
        padding: 1 2; }
    #adjudication-card, #help-dialog { max-height: 80%; }
    #order-dialog { width: 70%; }
    .adjudication-line { padding: 0 1; }
    .help-line { padding: 0 1; }
    #resolve-summary { margin: 1 0; }
    #order-preview { margin: 1 0; color: $text-muted; }
    """
    BINDINGS = [
        ("r", "refresh", "刷新"),
        ("n", "new_order", "新建工单"),
        ("s", "steer", "steer 注入"),
        ("a", "approve", "审批 ask"),
        ("p", "pause", "暂停/恢复"),
        ("o", "cycle_sort", "看板排序"),
        ("question_mark", "help_keys", "帮助"),
        ("q", "quit", "退出"),
    ]

    def __init__(self, store: Optional[ConsoleStore] = None,
                 probe: object = _PROBE_UNSET) -> None:
        super().__init__()
        if store is None:
            import os
            store = connect_pg() if os.environ.get("CONSOLE_TUI_DSN") else MockConsoleStore()
        self.store = store
        self.probe = SshServiceProbe.from_env() if probe is _PROBE_UNSET else probe
        self._task_rows: list[TaskRow] = []
        self._challenge_rows: list[ChallengeRow] = []
        self._health_rows: List[ServiceHealthRow] = []
        self._sort_idx = 0                       # 工单看板排序轮换游标（o 键）
        self._panel_errors: dict[str, str] = {}  # 逐面板守护：面板名 → 异步类名
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
            yield TabPane("服务健康", DataTable(id="health"), id=PANE_HEALTH)
            yield TabPane("计量", DataTable(id="usage"), id=PANE_USAGE)
            yield TabPane("意图时间线", DataTable(id="timeline"), id=PANE_TIMELINE)
        yield Footer()

    def on_mount(self) -> None:
        tasks = self.query_one("#tasks", DataTable)
        tasks.add_columns("任务", "owner", "状态", "暂停", "交付物", "创建")
        leases = self.query_one("#leases", DataTable)
        leases.add_columns("租约", "task_ref", "额度", "余量", "状态", "已用%")
        chal = self.query_one("#challenges", DataTable)
        chal.add_columns("challenge", "谁确认", "资源", "动作", "agent", "剩余秒")
        dec = self.query_one("#decisions", DataTable)
        dec.add_columns("决策", "决策者", "选择", "上下文哈希", "留痕")
        nodes = self.query_one("#nodes", DataTable)
        nodes.add_columns("节点", "CPU份额", "GPU份额", "信任", "并行", "在线窗口", "心跳")
        health = self.query_one("#health", DataTable)
        health.add_columns("服务", "主机", "状态", "说明", "检查")
        usage = self.query_one("#usage", DataTable)
        usage.add_columns("维度", "事件数", "总量", "最早", "最近")
        timeline = self.query_one("#timeline", DataTable)
        timeline.add_columns("信号", "来源", "类型", "状态", "推进", "工单", "workflow")
        for dt in self.query(DataTable):
            dt.cursor_type = "row"
        self.action_refresh()
        # 自动刷新（线E）：工单被认领/完成 30s 内可见；探针 75s 轮一次（TTL 60s）。
        self.set_interval(30.0, self.action_refresh)
        self.set_interval(75.0, self.action_probe)
        self.action_probe()

    # ── 刷新（面板与塔列全量重绘；数据层是唯一事实来源；逐面板守护）─────────
    def action_refresh(self) -> None:
        try:
            summary = self.store.pool_summary()
            self.query_one("#pool-bar", Static).update(
                f"节点池: {summary.node_count} 节点 | GPU份额占用(gpu_frac合计): "
                f"{summary.gpu_frac_total:.2f}（其中可信 {summary.gpu_frac_trusted:.2f}）"
                f" | 不可信节点: {summary.untrusted_count} | 并行槽位: {summary.max_parallel_total}"
                f" | 心跳在线: {summary.heartbeat_online}/{summary.node_count}"
                f" | 看板排序: {_TASK_SORTS[self._sort_idx][0]}"
                f" | mode={self.store.mode}")
        except Exception as exc:                     # 状态栏失源也不拖垮面板
            self._panel_errors["pool-bar"] = type(exc).__name__
        self._guarded("tower", self._refresh_tower)
        self._guarded("tasks", self._refresh_tasks)
        self._guarded("leases", self._refresh_leases)
        self._guarded("challenges", self._refresh_challenges)
        self._guarded("decisions", self._refresh_decisions)
        self._guarded("nodes", self._refresh_nodes)
        self._guarded("health", self._refresh_health)
        self._guarded("usage", self._refresh_usage)
        self._guarded("timeline", self._refresh_timeline)

    def _guarded(self, name: str, fn) -> None:
        """逐面板守护：单面板数据源缺失/未授权 → 面板内错误行，不炸控制台。"""
        try:
            fn()
        except Exception as exc:
            self._panel_errors[name] = type(exc).__name__
            table_id = {"tower": None}.get(name, f"#{name}")
            try:
                if table_id:
                    self._fill_error(self.query_one(table_id, DataTable), exc)
            except Exception:
                pass

    @staticmethod
    def _fill_error(table: DataTable, exc: Exception) -> None:
        """在面板表内放一行可读错误（类型名 + 常见原因提示；不含 DSN/密钥）。"""
        first = Text(f"⚠ 数据源暂不可用（{type(exc).__name__}）", style="red")
        ncols = len(table.columns)
        rest = ["缺表/未授权/连接异常"] + [""] * max(0, ncols - 2)
        table.clear()
        table.add_row(first, *rest)

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
        """工单看板（线E）：排序轮换（o 键）+ 状态颜色语义 + 创建时间列。"""
        table = self.query_one("#tasks", DataTable)
        table.clear()
        self._task_rows = sorted(self.store.tasks(), key=_TASK_SORTS[self._sort_idx][1])
        for t in self._task_rows:
            table.add_row(t.title, t.owner or "-",
                          Text(t.state, style=_STATE_STYLES.get(t.state, "")),
                          "⏸ 已暂停" if t.paused else "",
                          t.deliverable or "-", _fmt_ts(t.created_at), key=t.task_id)

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
        """节点利用率（线E 补全）：+ 心跳列（glue.node_heartbeat 投影）。"""
        table = self.query_one("#nodes", DataTable)
        table.clear()
        now = self.store_now()
        for n in self.store.nodes():
            table.add_row(n.node_id, f"{n.cpu_frac:.2f}", f"{n.gpu_frac:.2f}",
                          n.trust_level, str(n.max_parallel), n.online_window,
                          _fmt_heartbeat(n.heartbeat_at, now), key=n.node_id)

    def _refresh_health(self) -> None:
        """服务健康面板（线E动作2）：跨机 systemd 只读探针行的展示面。

        探针未配置时 pg 模式给"未配置"提示行；mock 模式给确定性演示行
        （面板形态可见，不产生任何子进程）。行状态词表见 health 模块。
        """
        table = self.query_one("#health", DataTable)
        table.clear()
        rows = self._health_rows
        if not rows:
            if self.store.mode == "mock":
                rows = demo_rows(self.store_now())
            else:
                table.add_row(Text("探针未配置", style="yellow"), "-",
                              Text("unknown", style="yellow"),
                              f"设 {PROBE_ENV} 启用跨机只读探针", "-", key="probe-off")
                return
        for i, h in enumerate(rows):
            table.add_row(h.service, h.host,
                          Text(h.state, style=_HEALTH_STYLES.get(h.state, "")),
                          h.detail, _fmt_ts(h.checked_at),
                          key=f"{h.host}:{h.service}:{i}")

    def action_cycle_sort(self) -> None:
        """o 键：工单看板排序轮换（易用性；不改数据层顺序语义，只改展示序）。"""
        self._sort_idx = (self._sort_idx + 1) % len(_TASK_SORTS)
        self.notify(f"看板排序 → {_TASK_SORTS[self._sort_idx][0]}")
        self.action_refresh()

    def action_help_keys(self) -> None:
        """? 键：键位 + 颜色语义帮助弹窗。"""
        self.push_screen(HelpModal())

    # ── 下单接口（线E，n 键；创建新对象，不是干预）────────────────────────
    def action_new_order(self) -> None:
        self.push_screen(NewOrderModal())

    def action_new_order_submit(self, instruction: str) -> None:
        """n 提交（NewOrderModal 的 Enter；测试可直接调用）。

        RULES 分解（intake.decompose）→ 逐张 create_task 落库（PENDING/owner=NULL）
        → notify 摘要 → 刷新面板。落库失败可见（notify error），不冒充成功。
        """
        try:
            drafts = decompose(instruction)
        except GovernanceError as exc:
            self.notify(f"下单被拒绝: {exc}", severity="error")
            return
        try:
            events = [self.store.create_task(d, by=OPERATOR) for d in drafts]
        except Exception as exc:
            self.notify(f"下单落库失败: {type(exc).__name__}；工单未建，请检查数据源",
                        severity="error")
            self.action_refresh()
            return
        labels = "；".join(f"{e.detail.get('kind')}「{e.detail.get('title')}」"
                           for e in events)
        note = drafts[0].note or ""
        self.notify(f"已建 {len(events)} 张工单（PENDING）：{labels}"
                    + (f"｜{note}" if note else ""))
        self.action_refresh()

    # ── 服务健康探针（线程 worker；ssh 阻塞不进 UI 线程）──────────────────
    def action_probe(self) -> None:
        if self.probe is not None:
            self._probe_worker()

    @work(thread=True, exclusive=True)
    def _probe_worker(self) -> None:
        rows = self.probe.probe(force=True)          # SshServiceProbe 或注入的假探针
        self.call_from_thread(self._apply_health_rows, rows)

    def _apply_health_rows(self, rows: List[ServiceHealthRow]) -> None:
        self._health_rows = list(rows)
        try:
            self._refresh_health()
        except Exception:
            pass

    def _refresh_usage(self) -> None:
        """计量面板（W-04，v2.0 §4.4 四维度聚合；展示格式化两模式同一条路径）。"""
        table = self.query_one("#usage", DataTable)
        table.clear()
        for u in self.store.usage():
            label = USAGE_KIND_LABELS.get(u.kind, u.kind)
            table.add_row(label, str(u.events), format_quantity(u.kind, u.total_quantity),
                          _fmt_ts(u.first_at), _fmt_ts(u.last_at), key=u.kind)

    def _refresh_timeline(self) -> None:
        """意图时间线（W-11，v2.1 §10：signal_inbox 状态机投影；Temporal 只持
        编排状态，业务事实在此面板可见——裁决与编排分离的展示面）。"""
        table = self.query_one("#timeline", DataTable)
        table.clear()
        for s in self.store.timeline(limit=30):
            table.add_row(s.signal_id[:8], s.source, s.type, s.status_label,
                          str(s.depth) if s.depth >= 0 else "终态",
                          str(len(s.ticket_refs)) if s.ticket_refs else "-",
                          s.workflow_id or "-", key=s.signal_id)

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
        if not confirmer_may_resolve(OPERATOR, ch.who_confirms):
            # who 维度强制：控制台操作员只覆盖 user 级 ask；resource_owner /
            # duty_officer 级必须由对应人在独立界面确认（challenge.py 同款纪律）。
            self.notify(
                f"裁决被拒绝: 该 Challenge 需 {ch.who_confirms} 在独立界面确认"
                "（console 操作员不可代裁）", severity="error")
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

    # ── 裁决卡（W-06，v2.1 §4.7）：四件套展示 + approve/escalate 两键 ────────
    # 不新增全局干预键（三级干预 s/a/p 写死不变）；卡片经本方法打开
    # （裁决队列的入口接线 [待 W-11/W-12]），两键裁决走数据层状态机留痕。
    def open_adjudication(self, card_id: str) -> None:
        """打开指定就绪包裁决卡（测试与后续裁决队列共用的入口）。"""
        cards = {c.card_id: c for c in self.store.adjudication_cards()}
        card = cards.get(card_id)
        if card is None:
            self.notify(f"裁决卡不在队列: {card_id}", severity="warning")
            return
        self.push_screen(AdjudicationCardScreen(card))

    def action_adjudication_submit(self, card_id: str, approved: bool) -> None:
        """裁决卡 y/e 两键落库（AdjudicationCardScreen 提交；测试可直接调用）。"""
        try:
            event = self.store.resolve_adjudication(card_id, approved, by=OPERATOR)
            label = "已批准递呈人类" if approved else "已打回 L3"
            self.notify(f"{event.kind} 已留痕: {event.target}（{label}）")
        except Exception as exc:   # fail-closed 拒绝也要可见（包不齐 approve 被拒等）
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
