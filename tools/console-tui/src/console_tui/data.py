# coding: utf-8
"""console-tui 数据访问层 — mock / pg 双实现（WO-0012，v1.7 §12.3 / PROP-0005）.

六个数据面板（12.3 治理面清单 + W-04 计量面板）与取数来源:

1. 工单看板    → pg: glue.v_task_board（sql/003）      / mock: 种子数据
2. 租约        → pg: glue.v_active_lease               / mock: 种子数据
3. ask 审批队列 → pg: glue.v_pending_challenge          / mock: 种子数据
4. 决策记录    → pg: glue.v_recent_decision            / mock: 种子+干预追加
5. 节点利用率  → pg: glue.v_node_utilization           / mock: 种子数据
6. 计量 usage  → pg: glue.v_usage（004_usage_events）   / mock: 种子数据（W-04，
   v2.0 §4.4 四维度聚合）
7. 意图时间线  → pg: glue.v_signal_timeline（007_signal_inbox，W-11 v2.1 §10）
   / mock: 种子数据——signal_inbox 状态机投影（received→…→completed 与
   rejected/suppressed/void 侧出口）。Temporal 只持编排状态（workflow_id 互引），
   业务事实在此面板可见：裁决与编排分离的展示面。
   塔列（agent/团队）无独立表——两种模式都从工单数据推导（derive_agents）。

裁决卡队列（W-06，v2.1 §4.7 升级体系）——渲染与两键裁决在 adjudication 模块：
- 读：pg glue.v_readiness_card（[待 DDL]，SQL 接口先留） / mock: 种子卡；
- 写：approve（递呈人类，包必须 READY——fail-closed）/ escalate（打回 L3），
  走 state.resolve_adjudication_state 状态机 + append-only 决策记录留痕。

干预写路径（s/a/p 三级，全部留痕）:
- s steer   → mock: 内存决策+审计表；pg: INSERT glue.decision_record（type=steer
  记在 meta.intervention / chosen="steer"，append-only 表无 update 路径）；
- a 审批    → 走 state.resolve_challenge_state 状态机；pg: UPDATE glue.challenge
  （state='pending' AND expires_at > now() 守卫 + DDL 触发器第二道闸）+ INSERT
  glue.decision_record 留痕；
- p 暂停/恢复 → 合法性校验后写一条 pause/resume 决策记录；任务暂停态由最新一条
  推导（003 v_task_board.paused），可恢复。

安全纪律:
- pg 模式所有 SQL 走参数化（%s 占位，值经 psycopg 适配，不拼接）；
- DSN 只从环境变量 ``CONSOLE_TUI_DSN`` 读，**不打印、不进 repr/日志/异常文本**；
- 本模块不 import textual（textual 缺失时数据层照常可用，只影响 app.py）。
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .state import (CARD_PENDING, CH_PENDING, KIND_APPROVE, KIND_DENY, KIND_ESCALATE_BACK,
                    KIND_PAUSE, KIND_RESUME, KIND_STEER, OPERATOR, GovernanceError,
                    AdjudicationResolutionError, ChallengeResolutionError, UnknownTargetError,
                    apply_pause, audit_event, canonical_hash, resolve_adjudication_state,
                    resolve_challenge_state, utcnow)  # noqa: F401 (OPERATOR=写路径默认裁决人)

DSN_ENV = "CONSOLE_TUI_DSN"

TASK_STATES = ("PENDING", "CLAIMED", "COMPLETED", "BLOCKED", "CANCELLED")


# ── 行模型（pg/mock 两模式共用的展示形态；时间统一 float epoch）───────────────

@dataclass(frozen=True)
class TaskRow:
    task_id: str
    title: str
    owner: str
    state: str
    deliverable: str
    run_ref: str
    artifact_ref: str
    created_at: float
    last_transition_at: float
    paused: bool = False


@dataclass(frozen=True)
class LeaseRow:
    lease_id: str
    task_ref: str
    parent_lease_id: str
    amount: int
    remaining: int
    status: str
    granted_at: float
    expires_at: Optional[float]

    @property
    def utilization(self) -> float:
        return 1.0 - (self.remaining / self.amount) if self.amount else 0.0


@dataclass(frozen=True)
class ChallengeRow:
    challenge_id: str
    who_confirms: str
    resource: str
    action: str
    method: str
    agent_identity_ref: str
    guardrail_run_ref: str
    created_at: float
    expires_at: float
    state: str = CH_PENDING

    def seconds_left(self, now: float) -> float:
        return self.expires_at - now


@dataclass(frozen=True)
class DecisionRow:
    decision_id: str
    ts: float
    agent_ref: str
    context_hash: str
    options: Tuple[str, ...]
    chosen: str
    rationale_ref: str
    guardrail_run_ref: str
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NodeRow:
    node_id: str
    cpu_frac: float
    gpu_frac: float
    tools: Tuple[str, ...]
    trust_level: str
    max_parallel: int
    online_window: str


@dataclass(frozen=True)
class UsageRow:
    """计量面板行模型（v2.0 §4.4 四维度聚合；pg 读 glue.v_usage，同构）。"""

    kind: str                    # llm_relay / compute_seconds / storage_bytes / sandbox_seconds
    events: int
    total_quantity: float
    first_at: Optional[float]
    last_at: Optional[float]


@dataclass(frozen=True)
class ReadinessCardRow:
    """人类就绪包裁决卡行模型（W-06，v2.1 §4.7；pg 读 glue.v_readiness_card，[待 DDL]）。

    pieces = 四件套 (key, verdict, detail) 三元组序列（与 glue ReadinessPiece 同构，
    字符串形态跨层传递——本包不 import jiuwen_glue，语义由 tests 交叉校验防漂移）。
    """

    card_id: str
    task_ref: str
    task_title: str
    signature: str                     # 阻塞签名（glue escalation.signature_for，64 hex）
    category: str                      # 四类硬清单之一；""=未归类（打回 L3）
    pieces: Tuple[Tuple[str, str, str], ...]   # (piece_key, verdict, detail)
    missing: Tuple[str, ...]           # 非 PASS 的件
    ready: bool                        # 四件套聚合 = PASS（fail-closed 批准闸）
    knock_back_to_l3: bool             # 范畴不在四类硬清单
    level: str                         # 递包时的阶梯层（正常为 L4）
    decision_refs: Tuple[str, ...]     # 挂联决策记录引用（人类复核入口）
    generated_at: float
    state: str = CARD_PENDING


HARD_LIST_CATEGORIES = ("money", "legal_tos", "irreversible", "theory_approval")
"""人类专属四类硬清单（与 jiuwen_glue.escalation.HARD_LIST 同源；tests 交叉校验防漂移）。"""


USAGE_KIND_LABELS = {
    "llm_relay": "LLM 中继(consumer)",
    "compute_seconds": "计算秒",
    "storage_bytes": "存储字节",
    "sandbox_seconds": "沙箱秒",
}


# ── 意图时间线（W-11，v2.1 §10；signal_inbox 状态机投影）─────────────────────

SIGNAL_STATUS_ORDER = {
    # 主链（深度=时间线推进度）；侧出口单列（rejected/suppressed/void）
    "received": 0, "triaged": 1, "routed": 2, "drafted": 3, "gated": 4,
    "ticketed": 5, "tracking": 6, "completed": 7,
    "rejected": -1, "suppressed": -1, "void": -1,
}

SIGNAL_STATUS_LABELS = {
    "received": "已收件", "triaged": "已分诊", "routed": "已路由",
    "drafted": "PROP 草稿", "gated": "已门控", "ticketed": "已开工单",
    "tracking": "跟踪中", "completed": "已完成",
    "rejected": "已拒绝", "suppressed": "已去重", "void": "已失效",
}


@dataclass(frozen=True)
class SignalTimelineRow:
    """意图时间线行（glue.v_signal_timeline 同构；007_signal_inbox 投影）。"""

    signal_id: str
    source: str
    type: str
    status: str
    prop_ref: str
    guardrail_run_ref: str
    workflow_id: str
    ticket_refs: Tuple[str, ...]
    created_at: float
    last_transition_at: float
    transition_count: int
    last_reason_code: str

    @property
    def status_label(self) -> str:
        return SIGNAL_STATUS_LABELS.get(self.status, self.status)

    @property
    def depth(self) -> int:
        """主链推进深度（侧出口=-1，显示时排后并标终态）。"""
        return SIGNAL_STATUS_ORDER.get(self.status, -1)


def format_quantity(kind: str, total: float) -> str:
    """计量总量的展示格式化（两模式共用同一条显示逻辑，无第二决策点）。"""
    if kind == "storage_bytes":
        size = float(total)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024.0
    if kind in ("compute_seconds", "sandbox_seconds"):
        secs = float(total)
        if secs >= 3600:
            return f"{secs / 3600:.1f} h"
        if secs >= 60:
            return f"{secs / 60:.1f} min"
        return f"{secs:.0f} s"
    if kind == "llm_relay":
        return f"{float(total):,.0f}"
    return f"{float(total):,.2f}"


@dataclass(frozen=True)
class AgentColumn:
    """塔式布局的一列：agent/团队（状态+当前任务+心跳，12.3 显示形态）。"""

    agent_ref: str
    status: str                 # working | blocked | idle
    current_task: str
    heartbeat_seconds: float    # 距最近一次任务状态转移的秒数
    open_tasks: int


@dataclass(frozen=True)
class PoolSummary:
    """顶部状态栏的节点池利用率汇总（nodes 的 gpu_frac 占用）。"""

    node_count: int
    gpu_frac_total: float
    gpu_frac_trusted: float
    untrusted_count: int
    max_parallel_total: int


@dataclass(frozen=True)
class AuditEntry:
    ts: float
    kind: str
    target: str
    by: str
    detail: Mapping[str, Any]


# ── 共享推导（两模式同一条代码路径）─────────────────────────────────────────

def derive_agents(tasks: Sequence[TaskRow], now: float) -> List[AgentColumn]:
    """从工单数据推导塔列：owner = agent/团队；状态/当前任务/心跳由任务态推导。

    推导规则（确定性，无第二个决策点）: 有 CLAIMED → working（当前任务=最新的
    CLAIMED 工单标题）；否则有 BLOCKED → blocked；否则 idle。心跳 = now − 该
    agent 最新一次 last_transition_at。
    """
    by_owner: Dict[str, List[TaskRow]] = {}
    for t in tasks:
        if t.owner:
            by_owner.setdefault(t.owner, []).append(t)
    cols: List[AgentColumn] = []
    for owner in sorted(by_owner):
        rows = by_owner[owner]
        claimed = [t for t in rows if t.state == "CLAIMED"]
        blocked = [t for t in rows if t.state == "BLOCKED"]
        current = max(claimed, key=lambda t: t.last_transition_at, default=None)
        if current is not None:
            status, title = "working", current.title
        elif blocked:
            status, title = "blocked", blocked[0].title
        else:
            status, title = "idle", "-"
        last = max(t.last_transition_at for t in rows)
        cols.append(AgentColumn(agent_ref=owner, status=status, current_task=title,
                                heartbeat_seconds=max(0.0, now - last),
                                open_tasks=sum(1 for t in rows
                                               if t.state in ("PENDING", "CLAIMED", "BLOCKED"))))
    return cols


def summarize_nodes(nodes: Sequence[NodeRow]) -> PoolSummary:
    """节点池利用率汇总：gpu_frac 占用合计、可信占比、不可信节点数。"""
    return PoolSummary(
        node_count=len(nodes),
        gpu_frac_total=round(sum(n.gpu_frac for n in nodes), 4),
        gpu_frac_trusted=round(sum(n.gpu_frac for n in nodes
                                   if n.trust_level == "trusted"), 4),
        untrusted_count=sum(1 for n in nodes if n.trust_level == "untrusted"),
        max_parallel_total=sum(n.max_parallel for n in nodes),
    )


def _decision_row_for(kind: str, *, target: str, by: str, text: str = "",
                      detail: Optional[Mapping[str, Any]] = None,
                      ts: float) -> Tuple[DecisionRow, Dict[str, Any]]:
    """s/a/p 干预 → 一条 append-only 决策记录 + 配套审计事件（两模式共用构造）。

    agent_ref 一律 = 决策者（治理面操作员）；被干预对象放 meta.target——
    agent_ref 语义是"谁做的决策"，与 decisions.py 的 agent_ref 一致。
    """
    meta = {"intervention": kind, "target": target, "by": by}
    if text:
        meta["text"] = text
    if detail:
        meta.update(detail)
    ctx = {"intervention": kind, "target": target, "by": by,
           **({"text": text} if text else {})}
    did = str(uuid.uuid4())
    row = DecisionRow(
        decision_id=did, ts=ts, agent_ref=by, context_hash=canonical_hash(ctx),
        options=(KIND_STEER, "noop") if kind == KIND_STEER else
                (KIND_APPROVE, KIND_DENY) if kind in (KIND_APPROVE, KIND_DENY) else
                (KIND_APPROVE, KIND_ESCALATE_BACK) if kind == KIND_ESCALATE_BACK else
                (KIND_PAUSE, KIND_RESUME),
        chosen=kind, rationale_ref=f"console://audit/{kind}/{int(ts * 1000)}",
        guardrail_run_ref="", meta=meta)
    event = audit_event(kind, target=target, by=by, at=ts, **dict(detail or {}))
    if text:
        event["detail"]["text"] = text
    return row, event


# ── 后端接口 ─────────────────────────────────────────────────────────────────

class ConsoleStore:
    """治理面数据访问接口（mock 与 pg 双实现；app.py 只面向本接口）。"""

    mode: str = "base"

    def tasks(self) -> List[TaskRow]: raise NotImplementedError
    def leases(self) -> List[LeaseRow]: raise NotImplementedError
    def challenges(self) -> List[ChallengeRow]: raise NotImplementedError
    def decisions(self, limit: int = 20) -> List[DecisionRow]: raise NotImplementedError
    def nodes(self) -> List[NodeRow]: raise NotImplementedError
    def usage(self) -> List[UsageRow]: raise NotImplementedError
    def adjudication_cards(self) -> List[ReadinessCardRow]: raise NotImplementedError
    def timeline(self, limit: int = 50) -> List[SignalTimelineRow]:
        """意图时间线（W-11）：signal_inbox 状态机投影，按收件时间倒序。"""
        raise NotImplementedError
    def agents(self) -> List[AgentColumn]:
        return derive_agents(self.tasks(), utcnow())
    def pool_summary(self) -> PoolSummary:
        return summarize_nodes(self.nodes())
    def now(self) -> float:
        """取数时钟（mock 可注入；pg 用 wall clock）。"""
        return utcnow()
    def audit_trail(self, limit: int = 50) -> List[AuditEntry]: raise NotImplementedError

    # ── 三级干预（s/a/p；全部留痕）─────────────────────────────────────────
    def steer(self, agent_ref: str, text: str, by: str = OPERATOR) -> AuditEntry:
        raise NotImplementedError
    def resolve_challenge(self, challenge_id: str, approved: bool,
                          by: str = OPERATOR) -> AuditEntry:
        raise NotImplementedError
    def set_task_pause(self, task_id: str, pause: bool, by: str = OPERATOR) -> AuditEntry:
        raise NotImplementedError

    # ── 裁决卡两键（W-06，v2.1 §4.7；approve=递呈人类 / escalate=打回 L3）────
    def resolve_adjudication(self, card_id: str, approved: bool,
                             by: str = OPERATOR) -> AuditEntry:
        raise NotImplementedError


# ── MOCK 后端 ────────────────────────────────────────────────────────────────

def _readiness_card(card_id: str, task_ref: str, title: str, category: str, *,
                    ready: bool, now: float) -> ReadinessCardRow:
    """确定性裁决卡种子（演示/测试共用）：ready 卡四件套全 PASS，否则按缺料给 BLOCKED。"""
    pieces = [
        ("facts_fixed", "PASS" if ready else "BLOCKED",
         "3 facts pinned with evidence refs" if ready else "no facts assembled"),
        ("category_clear", "PASS" if category in HARD_LIST_CATEGORIES else "BLOCKED",
         f"hard-list category: {category}" if category in HARD_LIST_CATEGORIES
         else f"no hard-list category declared (not in {HARD_LIST_CATEGORIES})"),
        ("in_permission_no_solution", "PASS",
         "levels L0..L3 each hit the per-level attempt cap for this signature"),
        ("reversibility", "PASS" if ready else "BLOCKED",
         "reversible=False; rollback=n/a" if ready else "no reversibility assessment"),
    ]
    missing = tuple(k for k, v, _ in pieces if v != "PASS")
    return ReadinessCardRow(
        card_id=card_id, task_ref=task_ref, task_title=title,
        signature=canonical_hash({"task": task_ref}), category=category,
        pieces=tuple(pieces), missing=missing, ready=(not missing),
        knock_back_to_l3=(category not in HARD_LIST_CATEGORIES),
        level="L4", decision_refs=("dec-3001",), generated_at=now - 8 * 60.0)


def default_mock_seed(now: float) -> Tuple[List[TaskRow], List[LeaseRow],
                                           List[ChallengeRow], List[DecisionRow],
                                           List[NodeRow], List[UsageRow],
                                           List[ReadinessCardRow]]:
    """确定性种子数据（演示/测试共用；now 可注入以便过期场景可控）。"""
    m = 60.0
    tasks = [
        TaskRow("tsk-001", "盘点 skill-pack 测试缺口", "alpha-planner-01", "CLAIMED",
                "docs/test-gap.md", "run://r1", "", now - 30 * m, now - 12 * m),
        TaskRow("tsk-002", "评审 leases 派生限额", "beta-reviewer-01", "CLAIMED",
                "review/leases.md", "run://r2", "", now - 45 * m, now - 3 * m),
        TaskRow("tsk-003", "修 guardrail fail-closed 缺陷", "alpha-planner-01", "BLOCKED",
                "patch/guardrail.diff", "", "", now - 90 * m, now - 20 * m),
        TaskRow("tsk-004", "出卷器 v3 回归", "gamma-ops-01", "PENDING",
                "reports/regression.md", "", "", now - 10 * m, now - 10 * m),
        TaskRow("tsk-005", "节点注册协议对齐", "gamma-ops-01", "COMPLETED",
                "docs/node-registry.md", "run://r5", "art://a5", now - 200 * m, now - 60 * m),
        TaskRow("tsk-006", "租约到期巡检", "delta-runner-02", "CLAIMED",
                "ops/lease-sweep.md", "run://r6", "", now - 15 * m, now - 2 * m),
    ]
    leases = [
        LeaseRow("lease-100", "tsk-001", "", 1000, 620, "ACTIVE", now - 60 * m, now + 240 * m),
        LeaseRow("lease-101", "tsk-002", "lease-100", 300, 120, "ACTIVE", now - 40 * m, now + 120 * m),
        LeaseRow("lease-102", "tsk-005", "", 500, 0, "EXHAUSTED", now - 300 * m, now - 60 * m),
    ]
    challenges = [
        ChallengeRow("ch-2001", "user", "prod/release/batch-17", "release.resume",
                     "console.ask", "gamma-ops-01", "run://g1",
                     now - 5 * m, now + 55 * m),
        ChallengeRow("ch-2002", "resource_owner", "vault/credentials/stepfun", "credential.grant",
                     "console.ask", "delta-runner-02", "run://g2",
                     now - 30 * m, now + 45 * m),
    ]
    decisions = [
        DecisionRow("dec-3001", now - 70 * m, "console:operator",
                    canonical_hash({"intervention": "steer", "target": "alpha-planner-01"}),
                    (KIND_STEER, "noop"), KIND_STEER, "console://audit/steer/0", "",
                    {"intervention": KIND_STEER, "target": "alpha-planner-01", "text": "优先补测试"}),
        DecisionRow("dec-3002", now - 25 * m, "jev:decision-layer",
                    canonical_hash({"route": "vllm"}), ("higress", "direct"),
                    "higress", "evidence://ev-9", "", {"route": "model"}),
    ]
    nodes = [
        NodeRow("srv-1", 0.8, 0.5, ("shell", "pg", "higress"), "trusted", 4, "always"),
        NodeRow("work-01", 1.0, 1.0, ("shell", "vllm"), "trusted", 2, "09:00-18:00+08"),
        NodeRow("edge-relay", 0.3, 0.0, ("curl",), "untrusted", 1, "always"),
    ]
    usage = [
        UsageRow("llm_relay", 42, 128_500.0, now - 240 * m, now - 2 * m),
        UsageRow("compute_seconds", 7, 5_220.0, now - 180 * m, now - 5 * m),
        UsageRow("storage_bytes", 15, 3_355_443_200.0, now - 300 * m, now - 30 * m),
        UsageRow("sandbox_seconds", 11, 1_860.0, now - 120 * m, now - 1 * m),
    ]
    # 裁决卡（W-06）：一张 READY（irreversible，可批准递呈人类）、
    # 一张 BLOCKED（未归类，只能打回 L3——approve 键会被 fail-closed 拒绝）
    cards = [
        _readiness_card("card-4001", "tsk-003", "修 guardrail fail-closed 缺陷",
                        "irreversible", ready=True, now=now),
        _readiness_card("card-4002", "tsk-004", "出卷器 v3 回归",
                        "", ready=False, now=now),
    ]
    return tasks, leases, challenges, decisions, nodes, usage, cards


def default_signal_seed(now: float) -> List[SignalTimelineRow]:
    """意图时间线确定性种子（W-11，v2.1 §10 演示；与 srv-1 jiuwen_team 的
    smoke-w11 冒烟种子同构）：主链一条走全程，另覆盖在途与三个侧出口。"""
    m = 60.0
    return [
        SignalTimelineRow(
            "sig-7001", "cnb-issue", "issue.feedback", "completed",
            "PROP-00xx-draft", "run://g11", "wf/issue.feedback/sig-7001",
            ("task://t-11a", "task://t-11b"), now - 240 * m, now - 30 * m, 7, ""),
        SignalTimelineRow(
            "sig-7002", "tech-reality", "tech.reality-change", "tracking",
            "PROP-00yy-draft", "run://g12", "wf/tech.reality-change/sig-7002",
            ("task://t-12a",), now - 120 * m, now - 8 * m, 6, ""),
        SignalTimelineRow(
            "sig-7003", "pipeline", "pipeline.feedback", "received",
            "", "", "", (), now - 12 * m, now - 12 * m, 0, ""),
        SignalTimelineRow(
            "sig-7004", "monitoring", "monitoring.insight", "rejected",
            "", "", "", (), now - 90 * m, now - 60 * m, 2, "NO_CONSUMER"),
        SignalTimelineRow(
            "sig-7005", "cnb-issue", "issue.feedback", "suppressed",
            "", "", "", (), now - 45 * m, now - 44 * m, 1, "DUPLICATE"),
    ]


class MockConsoleStore(ConsoleStore):
    """内存后端：无 psycopg/textual 依赖即可全功能演示；干预落内存审计表。"""

    mode = "mock"

    def __init__(self, *, now: Optional[Callable[[], float]] = None,
                 seed: bool = True) -> None:
        self._now = now or utcnow
        now = self._now()
        if seed:
            (self._tasks, self._leases, self._challenges,
             self._decisions, self._nodes, self._usage, self._cards) = default_mock_seed(now)
            self._signals: List[SignalTimelineRow] = default_signal_seed(now)
        else:
            self._tasks, self._leases, self._challenges, self._decisions, \
                self._nodes, self._usage, self._cards = [], [], [], [], [], [], []
            self._signals = []
        self._challenge_by_id: Dict[str, ChallengeRow] = {c.challenge_id: c for c in self._challenges}
        self._card_by_id: Dict[str, ReadinessCardRow] = {c.card_id: c for c in self._cards}
        self._audit: List[AuditEntry] = []

    # ── 面板取数 ──────────────────────────────────────────────────────────
    def tasks(self) -> List[TaskRow]:
        return list(self._tasks)

    def leases(self) -> List[LeaseRow]:
        return list(self._leases)

    def challenges(self) -> List[ChallengeRow]:
        """ask 审批队列：pending（含惰性过期标记——过期项移出队列，fail-closed）。"""
        now = self._now()
        out = []
        for cid in list(self._challenge_by_id):
            ch = self._challenge_by_id[cid]
            if ch.state == CH_PENDING and ch.expires_at <= now:
                ch = ChallengeRow(**{**ch.__dict__, "state": "expired"})
                self._challenge_by_id[cid] = ch
                self._audit.append(AuditEntry(now, "expire", cid, "system",
                                              {"reason": "ttl elapsed"}))
            if ch.state == CH_PENDING:
                out.append(ch)
        return sorted(out, key=lambda c: c.expires_at)

    def decisions(self, limit: int = 20) -> List[DecisionRow]:
        return list(self._decisions[-limit:])

    def nodes(self) -> List[NodeRow]:
        return list(self._nodes)

    def usage(self) -> List[UsageRow]:
        """计量面板（W-04）：四维度聚合行，按 kind 确定性排序。"""
        return sorted(self._usage, key=lambda u: u.kind)

    def adjudication_cards(self) -> List[ReadinessCardRow]:
        """裁决卡队列（W-06）：pending 卡按 generated_at 先到先裁。"""
        return sorted((c for c in self._card_by_id.values() if c.state == CARD_PENDING),
                      key=lambda c: c.generated_at)

    def timeline(self, limit: int = 50) -> List[SignalTimelineRow]:
        """意图时间线（W-11）：状态机投影按收件时间倒序（最新意图在上）。"""
        rows = sorted(self._signals, key=lambda s: s.created_at, reverse=True)
        return rows[:max(0, int(limit))]

    def agents(self) -> List[AgentColumn]:
        return derive_agents(self._tasks, self._now())

    def now(self) -> float:
        return self._now()

    def audit_trail(self, limit: int = 50) -> List[AuditEntry]:
        return list(self._audit[-limit:])

    # ── 干预：s steer ─────────────────────────────────────────────────────
    def steer(self, agent_ref: str, text: str, by: str = OPERATOR) -> AuditEntry:
        if not text or not text.strip():
            raise GovernanceError("steer requires non-empty instruction text")
        known = {a.agent_ref for a in self.agents()}
        if agent_ref not in known:
            raise UnknownTargetError(f"unknown agent on tower: {agent_ref!r}")
        row, event = _decision_row_for(KIND_STEER, target=agent_ref, by=by,
                                       text=text.strip(), ts=self._now())
        self._decisions.append(row)
        self._audit.append(AuditEntry(event["ts"], event["kind"], event["target"],
                                      event["by"], event["detail"]))
        return AuditEntry(event["ts"], event["kind"], event["target"], event["by"], event["detail"])

    # ── 干预：a 审批（走 Challenge 状态机，不绕过）────────────────────────
    def resolve_challenge(self, challenge_id: str, approved: bool,
                          by: str = OPERATOR) -> AuditEntry:
        ch = self._challenge_by_id.get(challenge_id)
        if ch is None:
            raise UnknownTargetError(f"unknown challenge: {challenge_id!r}")
        kind = KIND_APPROVE if approved else KIND_DENY
        new_state, resolved_at, resolved_by = resolve_challenge_state(
            ch.state, expires_at=ch.expires_at, now=self._now(),
            approved=approved, by=by, who_confirms=ch.who_confirms)
        resolved = ChallengeRow(**{**ch.__dict__, "state": new_state})
        self._challenge_by_id[challenge_id] = resolved
        row, event = _decision_row_for(kind, target=challenge_id, by=resolved_by,
                                       detail={"resource": ch.resource, "action": ch.action,
                                               "challenge_state": new_state},
                                       ts=self._now())
        self._decisions.append(row)
        self._audit.append(AuditEntry(event["ts"], event["kind"], event["target"],
                                      event["by"], event["detail"]))
        return AuditEntry(event["ts"], event["kind"], event["target"], event["by"], event["detail"])

    # ── 干预：p 暂停/恢复 ─────────────────────────────────────────────────
    def set_task_pause(self, task_id: str, pause: bool, by: str = OPERATOR) -> AuditEntry:
        idx = next((i for i, t in enumerate(self._tasks) if t.task_id == task_id), None)
        if idx is None:
            raise UnknownTargetError(f"unknown task: {task_id!r}")
        cur = self._tasks[idx]
        kind = apply_pause(cur.paused, KIND_PAUSE if pause else KIND_RESUME)
        paused = (kind == KIND_PAUSE)
        self._tasks[idx] = TaskRow(**{**cur.__dict__, "paused": paused})
        row, event = _decision_row_for(kind, target=task_id, by=by,
                                       detail={"title": cur.title}, ts=self._now())
        self._decisions.append(row)
        self._audit.append(AuditEntry(event["ts"], event["kind"], event["target"],
                                      event["by"], event["detail"]))
        return AuditEntry(event["ts"], event["kind"], event["target"], event["by"], event["detail"])

    # ── 裁决卡两键（W-06，v2.1 §4.7；状态机先行校验，不绕过）──────────────
    def resolve_adjudication(self, card_id: str, approved: bool,
                             by: str = OPERATOR) -> AuditEntry:
        """裁决就绪包卡：approve=批准递呈人类（包必须 READY，fail-closed）；
        escalate=打回 L3。走 state.resolve_adjudication_state 纯状态机。"""
        card = self._card_by_id.get(card_id)
        if card is None:
            raise UnknownTargetError(f"unknown readiness card: {card_id!r}")
        kind = KIND_APPROVE if approved else KIND_ESCALATE_BACK
        new_state, resolved_by = resolve_adjudication_state(
            card.state, ready=card.ready, approved=approved, by=by)
        resolved = ReadinessCardRow(**{**card.__dict__, "state": new_state})
        self._card_by_id[card_id] = resolved
        row, event = _decision_row_for(kind, target=card_id, by=resolved_by,
                                       detail={"task_ref": card.task_ref,
                                               "card_state": new_state,
                                               "ready": card.ready},
                                       ts=self._now())
        self._decisions.append(row)
        self._audit.append(AuditEntry(event["ts"], event["kind"], event["target"],
                                      event["by"], event["detail"]))
        return AuditEntry(event["ts"], event["kind"], event["target"], event["by"], event["detail"])


# ── PG 后端（只读视图 + 受控干预写路径；SQL 全参数化）────────────────────────

class PgConsoleStore(ConsoleStore):
    """Postgres 后端：读走 sql/003 只读视图，干预写 decision_record/challenge。

    连接对象由调用方注入（``connect_pg`` 工厂负责从环境变量读 DSN）——本类
    **不持有、不打印 DSN**；repr 恒为类名，防止连接串经日志外泄。
    """

    mode = "pg"

    # 只读面板（消费 sql/003 视图；全部 %s 参数化）
    _Q_TASKS = ("SELECT task_id, title, owner, state, deliverable, run_ref, artifact_ref,"
                " created_at, last_transition_at, paused FROM glue.v_task_board"
                " WHERE tenant_id = %s ORDER BY created_at")
    _Q_LEASES = ("SELECT lease_id, task_ref, parent_lease_id, amount, remaining, status,"
                 " granted_at, expires_at FROM glue.v_active_lease"
                 " WHERE tenant_id = %s ORDER BY granted_at DESC")
    _Q_CHALLENGES = ("SELECT challenge_id, who_confirms, resource, action, method,"
                     " agent_identity_ref, guardrail_run_ref, created_at, expires_at"
                     " FROM glue.v_pending_challenge WHERE tenant_id = %s"
                     " ORDER BY expires_at")
    _Q_DECISIONS = ("SELECT decision_id, ts, agent_ref, context_hash, options, chosen,"
                    " rationale_ref, guardrail_run_ref, meta FROM glue.v_recent_decision"
                    " WHERE tenant_id = %s ORDER BY ts DESC LIMIT %s")
    _Q_NODES = ("SELECT node_id, cpu_frac, gpu_frac, tools, trust_level, max_parallel,"
                " online_window FROM glue.v_node_utilization WHERE tenant_id = %s"
                " ORDER BY node_id")
    _Q_USAGE = ("SELECT kind, events, total_quantity, first_at, last_at"
                " FROM glue.v_usage WHERE tenant_id = %s ORDER BY kind")
    # 意图时间线（W-11，v2.1 §10）：007_signal_inbox 的状态机投影（视图已落库
    # srv-1 jiuwen_team；DDL 在 company-ops ops/sql/007_signal_inbox.sql）。
    _Q_TIMELINE = ("SELECT signal_id, source, type, status, prop_ref,"
                   " guardrail_run_ref, workflow_id, ticket_refs, created_at,"
                   " last_transition_at, transition_count, last_reason_code"
                   " FROM glue.v_signal_timeline WHERE tenant_id = %s"
                   " ORDER BY created_at DESC LIMIT %s")
    _Q_TASK_PAUSED = ("SELECT paused FROM glue.v_task_board WHERE tenant_id = %s"
                      " AND task_id = %s")
    # 裁决卡（W-06，v2.1 §4.7）——SQL 接口先留：视图 glue.v_readiness_card 与表
    # glue.readiness_card 的 DDL 随 escalation 模块落库（[待 DDL]，见模块头注释）。
    # 口径与 mock 同构：pieces/missing 为 jsonb，ready 为布尔聚合，state 守卫在
    # UPDATE 的 WHERE 里（第二道闸），四件套 fail-closed 校验在状态机（第一道）。
    _Q_CARDS = ("SELECT card_id, task_ref, task_title, signature, category, pieces,"
                " missing, ready, knock_back_to_l3, level, decision_refs, generated_at,"
                " state FROM glue.v_readiness_card WHERE tenant_id = %s AND state = 'pending'"
                " ORDER BY generated_at")
    _Q_CARD_SELECT = ("SELECT state, ready FROM glue.readiness_card"
                      " WHERE card_id = %s")
    _Q_CARD_RESOLVE = ("UPDATE glue.readiness_card SET state = %s, resolved_at = now(),"
                       " resolved_by = %s WHERE card_id = %s AND state = 'pending'")

    # 干预写路径（s/a/p；append-only + 状态机守卫；DDL 触发器是第二道闸）
    _Q_STEER = ("INSERT INTO glue.decision_record (decision_id, agent_ref, context_hash,"
                " options, chosen, rationale_ref, meta, tenant_id)"
                " VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s)")
    _Q_CH_SELECT = ("SELECT state, expires_at, who_confirms FROM glue.challenge"
                    " WHERE challenge_id = %s")
    _Q_CH_RESOLVE = ("UPDATE glue.challenge SET state = %s, resolved_at = now(),"
                     " resolved_by = %s WHERE challenge_id = %s AND state = 'pending'"
                     " AND expires_at > now()")
    _Q_PAUSE = ("INSERT INTO glue.decision_record (decision_id, agent_ref, context_hash,"
                " options, chosen, rationale_ref, meta, tenant_id)"
                " VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s)")
    _Q_AUDIT = ("SELECT ts, chosen, meta FROM glue.v_recent_decision"
                " WHERE tenant_id = %s AND meta->>'intervention' IS NOT NULL"
                " ORDER BY ts DESC LIMIT %s")

    def __init__(self, conn: Any, *, tenant_id: str = "t0") -> None:
        # conn 为 psycopg connection（或满足 cursor()/commit() 协议的对象——测试用桩）。
        # 不存 DSN；__repr__ 不含任何连接信息。
        self._conn = conn
        self._tenant = tenant_id

    def __repr__(self) -> str:  # 防止连接串经 repr 外泄
        return f"<PgConsoleStore mode=pg tenant={self._tenant!r}>"

    def _exec(self, sql: str, params: Sequence[Any], *, fetch: str = "") -> Any:
        """执行参数化 SQL。fetch="all" 返回行列表；fetch="rowcount" 返回影响行数；
        其余仅提交（写路径）。"""
        with self._conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            if fetch == "all":
                rows: Any = cur.fetchall()
            elif fetch == "rowcount":
                rows = cur.rowcount
            else:
                rows = []
        self._conn.commit()
        return rows

    @staticmethod
    def _ts(value: Any) -> float:
        return value.timestamp() if hasattr(value, "timestamp") else float(value)

    # ── 面板取数 ──────────────────────────────────────────────────────────
    def tasks(self) -> List[TaskRow]:
        rows = self._exec(self._Q_TASKS, [self._tenant], fetch="all")
        return [TaskRow(str(r[0]), r[1], r[2], r[3], r[4] or "", r[5] or "", r[6] or "",
                        self._ts(r[7]), self._ts(r[8]), bool(r[9])) for r in rows]

    def leases(self) -> List[LeaseRow]:
        rows = self._exec(self._Q_LEASES, [self._tenant], fetch="all")
        return [LeaseRow(str(r[0]), r[1], r[2] or "", int(r[3]), int(r[4]), r[5],
                         self._ts(r[6]), self._ts(r[7]) if r[7] is not None else None)
                for r in rows]

    def challenges(self) -> List[ChallengeRow]:
        rows = self._exec(self._Q_CHALLENGES, [self._tenant], fetch="all")
        return [ChallengeRow(str(r[0]), r[1], r[2], r[3], r[4], r[5] or "", r[6] or "",
                             self._ts(r[7]), self._ts(r[8])) for r in rows]

    def decisions(self, limit: int = 20) -> List[DecisionRow]:
        rows = self._exec(self._Q_DECISIONS, [self._tenant, int(limit)], fetch="all")
        out = []
        for r in rows:
            opts = r[4] if isinstance(r[4], (list, tuple)) else json.loads(r[4] or "[]")
            out.append(DecisionRow(str(r[0]), self._ts(r[1]), r[2], r[3],
                                   tuple(opts), r[5], r[6], r[7] or "",
                                   r[8] if isinstance(r[8], dict) else json.loads(r[8] or "{}")))
        return out

    def nodes(self) -> List[NodeRow]:
        rows = self._exec(self._Q_NODES, [self._tenant], fetch="all")
        out = []
        for r in rows:
            tools = r[3] if isinstance(r[3], (list, tuple)) else json.loads(r[3] or "[]")
            out.append(NodeRow(r[0], float(r[1]), float(r[2]), tuple(tools), r[4],
                               int(r[5]), r[6]))
        return out

    def usage(self) -> List[UsageRow]:
        """计量面板（W-04）：glue.v_usage 按维度聚合（%s 参数化，值不拼接）。"""
        rows = self._exec(self._Q_USAGE, [self._tenant], fetch="all")
        return [UsageRow(str(r[0]), int(r[1]), float(r[2]),
                         self._ts(r[3]) if r[3] is not None else None,
                         self._ts(r[4]) if r[4] is not None else None)
                for r in rows]

    def adjudication_cards(self) -> List[ReadinessCardRow]:
        """裁决卡队列（W-06）：glue.v_readiness_card（[待 DDL]，SQL 接口先留）。"""
        rows = self._exec(self._Q_CARDS, [self._tenant], fetch="all")
        out = []
        for r in rows:
            pieces = r[5] if isinstance(r[5], (list, tuple)) else json.loads(r[5] or "[]")
            missing = r[6] if isinstance(r[6], (list, tuple)) else json.loads(r[6] or "[]")
            refs = r[10] if isinstance(r[10], (list, tuple)) else json.loads(r[10] or "[]")
            out.append(ReadinessCardRow(
                str(r[0]), r[1], r[2], r[3] or "", r[4] or "",
                tuple(tuple(p) for p in pieces), tuple(missing), bool(r[7]),
                bool(r[8]), r[9] or "L4", tuple(refs), self._ts(r[11]),
                r[12] if r[12] in (CARD_PENDING, "approved", "returned_l3") else CARD_PENDING))
        return out

    def timeline(self, limit: int = 50) -> List[SignalTimelineRow]:
        """意图时间线（W-11）：glue.v_signal_timeline 只读投影（%s 参数化）。"""
        rows = self._exec(self._Q_TIMELINE, [self._tenant, int(limit)], fetch="all")
        out = []
        for r in rows:
            tickets = r[7] if isinstance(r[7], (list, tuple)) else json.loads(r[7] or "[]")
            out.append(SignalTimelineRow(
                str(r[0]), r[1], r[2], r[3], r[4] or "", r[5] or "", r[6] or "",
                tuple(str(t) for t in tickets), self._ts(r[8]), self._ts(r[9]),
                int(r[10]), r[11] or ""))
        return out

    def audit_trail(self, limit: int = 50) -> List[AuditEntry]:
        rows = self._exec(self._Q_AUDIT, [self._tenant, int(limit)], fetch="all")
        return [AuditEntry(self._ts(r[0]), r[1], r[2].get("target", ""), r[2].get("by", ""),
                           r[2] if isinstance(r[2], dict) else {}) for r in rows]

    # ── 干预：s steer（pg 写一条 type=steer 的 decision_record）───────────
    def steer(self, agent_ref: str, text: str, by: str = OPERATOR) -> AuditEntry:
        if not text or not text.strip():
            raise GovernanceError("steer requires non-empty instruction text")
        known = {a.agent_ref for a in self.agents()}
        if agent_ref not in known:
            raise UnknownTargetError(f"unknown agent on tower: {agent_ref!r}")
        row, event = _decision_row_for(KIND_STEER, target=agent_ref, by=by,
                                       text=text.strip(), ts=utcnow())
        self._exec(self._Q_STEER, [uuid.UUID(row.decision_id), row.agent_ref,
                                   row.context_hash, json.dumps(list(row.options)),
                                   row.chosen, row.rationale_ref,
                                   json.dumps(row.meta, ensure_ascii=False), self._tenant])
        return AuditEntry(event["ts"], event["kind"], event["target"], event["by"], event["detail"])

    # ── 干预：a 审批（状态机先行校验，SQL 带守卫，DDL 触发器兜底）─────────
    def resolve_challenge(self, challenge_id: str, approved: bool,
                          by: str = OPERATOR) -> AuditEntry:
        rows = self._exec(self._Q_CH_SELECT, [uuid.UUID(challenge_id)], fetch="all")
        if not rows:
            raise UnknownTargetError(f"unknown challenge: {challenge_id!r}")
        state, expires_at, who_confirms = rows[0][0], self._ts(rows[0][1]), rows[0][2]
        kind = KIND_APPROVE if approved else KIND_DENY
        new_state, _resolved_at, resolved_by = resolve_challenge_state(
            state, expires_at=expires_at, now=utcnow(), approved=approved, by=by,
            who_confirms=str(who_confirms or ""))
        cur = self._exec(self._Q_CH_RESOLVE, [new_state, resolved_by,
                                              uuid.UUID(challenge_id)], fetch="rowcount")
        if cur != 1:
            raise ChallengeResolutionError(
                f"challenge {challenge_id} raced to a non-resolvable state (fail-closed)")
        row, event = _decision_row_for(kind, target=challenge_id, by=resolved_by,
                                       detail={"challenge_state": new_state}, ts=utcnow())
        self._exec(self._Q_STEER, [uuid.UUID(row.decision_id), row.agent_ref,
                                   row.context_hash, json.dumps(list(row.options)),
                                   row.chosen, row.rationale_ref,
                                   json.dumps(row.meta, ensure_ascii=False), self._tenant])
        return AuditEntry(event["ts"], event["kind"], event["target"], event["by"], event["detail"])

    # ── 干预：p 暂停/恢复（合法性校验后写 pause/resume 决策记录）──────────
    def set_task_pause(self, task_id: str, pause: bool, by: str = OPERATOR) -> AuditEntry:
        rows = self._exec(self._Q_TASK_PAUSED, [self._tenant, task_id], fetch="all")
        if not rows:
            raise UnknownTargetError(f"unknown task: {task_id!r}")
        kind = apply_pause(bool(rows[0][0]), KIND_PAUSE if pause else KIND_RESUME)
        row, event = _decision_row_for(kind, target=task_id, by=by, ts=utcnow())
        self._exec(self._Q_PAUSE, [uuid.UUID(row.decision_id), row.agent_ref,
                                   row.context_hash, json.dumps(list(row.options)),
                                   row.chosen, row.rationale_ref,
                                   json.dumps(row.meta, ensure_ascii=False), self._tenant])
        return AuditEntry(event["ts"], event["kind"], event["target"], event["by"], event["detail"])

    # ── 裁决卡两键（W-06；状态机先行校验，SQL 带守卫，DDL 触发器兜底 [待 DDL]）──
    def resolve_adjudication(self, card_id: str, approved: bool,
                             by: str = OPERATOR) -> AuditEntry:
        rows = self._exec(self._Q_CARD_SELECT, [uuid.UUID(card_id)], fetch="all")
        if not rows:
            raise UnknownTargetError(f"unknown readiness card: {card_id!r}")
        state, ready = rows[0][0], bool(rows[0][1])
        kind = KIND_APPROVE if approved else KIND_ESCALATE_BACK
        new_state, resolved_by = resolve_adjudication_state(
            state, ready=ready, approved=approved, by=by)
        cur = self._exec(self._Q_CARD_RESOLVE, [new_state, resolved_by,
                                                uuid.UUID(card_id)], fetch="rowcount")
        if cur != 1:
            raise AdjudicationResolutionError(
                f"readiness card {card_id} raced to a non-resolvable state (fail-closed)")
        row, event = _decision_row_for(kind, target=card_id, by=resolved_by,
                                       detail={"card_state": new_state, "ready": ready},
                                       ts=utcnow())
        self._exec(self._Q_STEER, [uuid.UUID(row.decision_id), row.agent_ref,
                                   row.context_hash, json.dumps(list(row.options)),
                                   row.chosen, row.rationale_ref,
                                   json.dumps(row.meta, ensure_ascii=False), self._tenant])
        return AuditEntry(event["ts"], event["kind"], event["target"], event["by"], event["detail"])


def connect_pg(dsn: Optional[str] = None, *, tenant_id: str = "t0") -> PgConsoleStore:
    """从环境变量 ``CONSOLE_TUI_DSN`` 建立连接（DSN 值不打印、不落日志）。

    psycopg 未安装时给出可操作提示（TUI 仍可退回 mock 模式）；DSN 缺失同理。
    """
    real_dsn = dsn if dsn is not None else os.environ.get(DSN_ENV, "")
    if not real_dsn:
        raise GovernanceError(
            f"{DSN_ENV} is not set; pg mode unavailable — run mock mode or export {DSN_ENV}")
    try:
        import psycopg
    except ImportError:
        raise GovernanceError(
            "psycopg is not installed (pip install 'jiuwen-console-tui[pg]'); "
            "or run mock mode") from None
    try:
        conn = psycopg.connect(real_dsn)
    except GovernanceError:
        raise
    except Exception as exc:                     # 连接失败不回显 DSN（密钥纪律）
        raise GovernanceError(
            f"pg connect failed ({type(exc).__name__}); check {DSN_ENV} value, "
            "network, and server state") from None
    return PgConsoleStore(conn, tenant_id=tenant_id)
