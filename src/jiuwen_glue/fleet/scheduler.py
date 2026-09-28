# coding: utf-8
"""fleet 调度器 P1 贪心 + P2 bin-packing（PROP-0004 / v1.7 §12.6/§13；v2.0 §10 M2）— WO-0011 / W-04.

规格来源（PROP-0004 三阶段：P1 贪心 → P2 利用率优化 → P3 云突发伸缩）:

- **P1 贪心**（默认策略 ``greedy``）：``assign(offering, registry) -> Assignment | Rejected``
  过滤（can_take / 信任等级 / 在线窗口 / GPU 份额余量 / 并行余量）→
  按（信任等级降序、空闲 gpu_frac 降序、max_parallel 余量降序）排序取首，
  node_id 升序为最终确定性 tie-break。
- **P2 bin-packing**（策略 ``best-fit``，W-04 / v2.0 §10 M2 利用率优化）：
  :func:`rank_key_best_fit` 按"放置后 gpu_frac 剩余最小"排序（装得更满、碎片
  最少），平局比节点 GPU 优先级（``NodeCapacity.gpu_priority`` 大者优先），
  再按 node_id 确定性 tie-break；P1 排序键锁定不变（既有测试语义）。
  **优先级抢占**：``TaskOffering.preempt_ok`` 只加字段与声明校验（无优先级的
  抢占声明 = 构造错误），**抢占语义 [待]**——本工单不实现任何真实驱逐。
- **硬规则（写死）**：不可信节点只接沙箱任务类（sandbox_class）——产物隔离 +
  人工复核；Assignment 携带 ``sandbox_only`` 与 ``review_required``，
  复核流程归控制台（WO-0012）。谓词基元复用 ``routes.NodeCapacity.can_take``。
- **份额记账**：Assignment 落账后 gpu_frac 余量扣减（:class:`ShareLedger`），
  完成/超时/回收释放。
- **两种取活模式**（§12.6）：:class:`DispatchMode`（调度制：控制台直接派）与
  :class:`SelfPickMode`（自取制：节点 ``claim`` 从 PENDING 工单队列认领，
  租约 TTL 过期回收工单回 PENDING——间歇在线线下机器 + 兜底死亡语义）。
- **Budget Lease 复用**：自取制认领即从工单的父租约派生一笔短时租约
  （TTL ≤ 1h，对齐 M0 审计 B4 execution-worker policy 形状），释放/回收即撤销
  （级联语义）；工单权限交集快照（identity.EffectivePerms）在派生时经
  既有"子 ⊆ 父"不变式收敛，本模块不重造。
- **GuardrailRun 唯一门控输出被消费**：offering 可携带 guardrail_run_ref +
  verdict；verdict ≠ PASS（含 UNKNOWN/缺失）→ 拒绝派发（fail-closed）。
  本模块不新增第二个决策点。
- **FEFO 资源压力感知（v2.1 §7 消耗策略，W-04）**：:class:`ResourcePressure`
  是本模块对"resources 档案投影"的**输入接口**——输入是计量数据对象（不是
  档案原文）：周限剩余比为主信号（剩余越少压力越大，将过期额度先用），
  5h 窗剩余 <15% 升权（辅信号）；:func:`pressures_from_meter_rows` 把
  W-03 计量器五键 NDJSON 行投影为本接口。**影子期（v2.1 施工红线）**：
  ``assign(..., shadow_fefo=...)`` 只把 FEFO 本应作出的选择记进
  ``Assignment.choice_trace``（``policy_active=False``），**不改变实际指派**——
  影子价格与利用率周报上线前，任何调度策略调整只记录不生效。

边界（4.9 #10/#13）：任务对象跨层只传引用（task_ref/payload_ref/artifact_ref
一律是引用）；调度器只派工单，**不碰 jiuwenswarm control 实例管理 API**——
实例内部归 control 面，调度面只见工单。
"""
from __future__ import annotations

import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Tuple, Union

from ..errors import LeaseError
from ..guardrail import VERDICT_PASS
from ..identity import EffectivePerms
from ..leases import BudgetLedger
from ..routes import TRUST_TRUSTED, TRUST_UNTRUSTED
from ..rules import CANCELLED, CLAIMED, COMPLETED, PENDING
from .errors import (
    SchedulingError,
    SecretScopeError,
    UnknownAssignmentError,
    UnknownNodeError,
    WorkOrderStateError,
)
from .registration import NODE_ACTIVE, FleetRegistry, NodeRecord


def _finite(v: object) -> bool:
    """有限性闸（与 challenge/escalation/usage/billing/leases 同款）：NaN/±inf
    一律非有限；巨型 int（如 10**400）经 math.isfinite 抛 OverflowError——按
    非有限同拒（schema 错误不得变形为未归类崩溃；#16 同口径推广到本模块）。"""
    try:
        return math.isfinite(v)  # type: ignore[arg-type]
    except (OverflowError, TypeError):   # 巨型 int / 非数值类型 → 非有限
        return False

__all__ = [
    "GPU_FRAC_EPS",
    "MAX_SELF_PICK_LEASE_SECONDS", "DEFAULT_SELF_PICK_LEASE_SECONDS",
    "DISPATCH_DIRECT", "DISPATCH_SELF_PICK",
    "ASSIGN_ACTIVE", "ASSIGN_COMPLETED", "ASSIGN_RELEASED", "ASSIGN_RECLAIMED",
    "REJECT_NO_NODE", "REJECT_SANDBOX", "REJECT_TRUST", "REJECT_TOOLS",
    "REJECT_TENANT",
    "REJECT_GPU", "REJECT_SLOT", "REJECT_OFFLINE", "REJECT_GUARDRAIL",
    "REJECT_STALE", "REJECT_TTL_CAP", "REJECT_QUEUE_EMPTY", "REJECT_LEASE",
    "SCHED_POLICY_GREEDY", "SCHED_POLICY_BEST_FIT", "SCHED_POLICIES",
    "FEFO_5H_LOW", "FEFO_5H_UPLIFT",
    "WEEKLY_WINDOW_PREFIX", "FIVE_HOURS_WINDOW_ID",
    "ResourcePressure", "pressure_score", "rank_key_fefo",
    "pressures_from_meter_rows",
    "TaskOffering", "Assignment", "Rejected", "ShareLedger",
    "assign", "rank_key_best_fit", "GreedyScheduler", "DispatchMode", "SelfPickMode",
    "WorkOrder", "WorkQueue", "SECRET_REF_PATTERN", "secret_ref_ok",
]

# GPU 份额比较容差（份额是 0.0–1.0 浮点，扣减累计有噪声）
GPU_FRAC_EPS = 1e-9

# 自取制租约 TTL 上限：对齐 execution-worker ≤1h 短时令牌形状（M0 审计 B4）
MAX_SELF_PICK_LEASE_SECONDS = 3600.0
DEFAULT_SELF_PICK_LEASE_SECONDS = 900.0

DISPATCH_DIRECT = "dispatch"        # 调度制：控制台直接派
DISPATCH_SELF_PICK = "self-pick"    # 自取制：节点认领

# 调度策略（PROP-0004 三阶段；P2 = 利用率优化，v2.0 §10 M2）：
# - greedy：P1 贪心（信任降序 → 空闲 gpu_frac 降序 → 并行余量降序 → node_id），键锁定不变；
# - best-fit：P2 bin-packing 排序（按 gpu_frac 放置后剩余最小者优先 = 装得更满），
#   平局先比节点 GPU 优先级（gpu_priority 大者优先），再按 node_id 确定性 tie-break。
SCHED_POLICY_GREEDY = "greedy"
SCHED_POLICY_BEST_FIT = "best-fit"
SCHED_POLICIES = (SCHED_POLICY_GREEDY, SCHED_POLICY_BEST_FIT)

ASSIGN_ACTIVE = "ACTIVE"
ASSIGN_COMPLETED = "COMPLETED"
ASSIGN_RELEASED = "RELEASED"
ASSIGN_RECLAIMED = "RECLAIMED"

REJECT_NO_NODE = "NO_ELIGIBLE_NODE"
REJECT_SANDBOX = "SANDBOX_ONLY_NODE"      # 硬规则：不可信节点只接沙箱任务类
REJECT_TRUST = "TRUST_REQUIRED"
REJECT_TOOLS = "TOOL_MISSING"
REJECT_GPU = "GPU_SHORTFALL"
REJECT_SLOT = "NO_PARALLEL_SLOT"
REJECT_OFFLINE = "NODE_OFFLINE_WINDOW"
REJECT_GUARDRAIL = "GUARDRAIL_NOT_PASS"
REJECT_STALE = "NODE_STALE"
REJECT_TTL_CAP = "LEASE_TTL_ABOVE_CAP"
REJECT_QUEUE_EMPTY = "QUEUE_EMPTY"
REJECT_LEASE = "LEASE_DERIVATION_FAILED"
REJECT_TENANT = "TENANT_MISMATCH"


# ── 工单与结果对象 ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TaskOffering:
    """一次派工要约：任务引用 + 匹配需求（对齐调度谓词与节点容量字段）。"""

    task_ref: str                                  # glue Task 引用（跨层只传引用）
    pipeline_label: str = ""                       # 治理面只见 pipeline 标签不同的工单
    required_tools: Tuple[str, ...] = ()           # capability 标签（⊆ 节点 tools 才可派）
    gpu_demand: float = 0.0                        # gpu_frac 需求 0.0–1.0
    trust_required: Optional[str] = None           # None | trusted | untrusted
    sandbox_class: bool = False                    # 是否沙箱任务类（不可信节点唯一可接类）
    guardrail_run_ref: Optional[str] = None        # GuardrailRun 引用（唯一门控输出被消费）
    guardrail_verdict: Optional[str] = None        # PASS 之外的 verdict → fail-closed 拒绝
    parent_lease_id: Optional[str] = None          # 自取制认领时从父租约派生（额度/权限收敛）
    effective_perms: Optional[EffectivePerms] = None  # 权限交集快照（固化进派生租约）
    gpu_priority: int = 0                          # 任务侧 GPU 优先级（P2）：越大越优先；
                                                   # 仅 best-fit 策略参与排序，greedy 不消费
    preempt_ok: bool = False                       # 优先级抢占标志（P2）：声明"本任务可抢占
                                                   # 低优先级派工"。**抢占语义 [待]**——本工单
                                                   # 只加字段：flag 不触发任何真实驱逐，仅随
                                                   # Assignment 落账留待将来执行路径消费。
    tenant_id: str = "t0"

    def __post_init__(self) -> None:
        if not self.task_ref or not isinstance(self.task_ref, str):
            raise SchedulingError("task_ref must be a non-empty reference")
        if isinstance(self.gpu_demand, bool) or \
                not isinstance(self.gpu_demand, (int, float)):
            raise SchedulingError(
                f"gpu_demand must be a float in [0.0, 1.0], got {self.gpu_demand!r}")
        try:
            gpu_frac = float(self.gpu_demand)
        except OverflowError:            # 巨型 int（如 10**400）越出 float 域：
            raise SchedulingError(       # 归类拒绝，不变形为未归类 OverflowError
                f"gpu_demand must be a float in [0.0, 1.0], got {self.gpu_demand!r}") from None
        if not (0.0 <= gpu_frac <= 1.0):
            raise SchedulingError(
                f"gpu_demand must be a float in [0.0, 1.0], got {self.gpu_demand!r}")
        if isinstance(self.gpu_priority, bool) or \
                not isinstance(self.gpu_priority, int) or self.gpu_priority < 0:
            raise SchedulingError(
                f"gpu_priority must be an int >= 0, got {self.gpu_priority!r}")
        if self.preempt_ok and self.gpu_priority <= 0:
            # fail-closed：无优先级的抢占声明没有意义（抢占比的就是优先级）
            raise SchedulingError(
                "preempt_ok=True requires gpu_priority > 0 "
                "(a preemption claim without a priority is meaningless)")
        if self.trust_required is not None and \
                self.trust_required not in (TRUST_TRUSTED, TRUST_UNTRUSTED):
            raise SchedulingError(
                f"trust_required must be None or one of "
                f"({TRUST_TRUSTED!r}, {TRUST_UNTRUSTED!r}), got {self.trust_required!r}")
        for tool in self.required_tools:
            if not tool or not isinstance(tool, str):
                raise SchedulingError(f"required_tools entries must be non-empty strings, got {tool!r}")
        has_ref = self.guardrail_run_ref is not None
        has_verdict = self.guardrail_verdict is not None
        if has_ref != has_verdict:
            raise SchedulingError(
                "guardrail_run_ref and guardrail_verdict must be provided together "
                "(verdict without a run ref is not auditable; ref without verdict "
                "is UNKNOWN and fails closed at assign)")


@dataclass
class Assignment:
    """派工落账对象：账本键 + 复核标记 + （自取制）短时租约引用。"""

    assignment_id: str
    offering: TaskOffering
    node_id: str
    dispatch: str = DISPATCH_DIRECT
    gpu_frac_committed: float = 0.0
    sandbox_only: bool = False        # 节点不可信 → 产物隔离（12.6 边界）
    review_required: bool = False     # 同上 → 人工复核（流程归控制台 WO-0012）
    order_id: Optional[str] = None    # 自取制来源工单
    lease_ref: Optional[str] = None       # Budget Lease 引用（自取制必发）
    lease_expires_at: Optional[float] = None
    effective_perms: tuple = ()           # 派生租约固化的权限交集快照
    assigned_at: float = 0.0
    status: str = ASSIGN_ACTIVE
    release_reason: Optional[str] = None
    artifact_ref: Optional[str] = None    # 完成时的产物**引用**（不复制产物）
    tenant_id: str = "t0"
    preempt_ok: bool = False              # offering 抢占声明的落账回声（语义 [待]，
                                          # 本工单只随账记录，不触发驱逐）
    choice_trace: Optional[Dict[str, object]] = None   # 影子期策略轨迹（FEFO 等）：
                                          # 只记录"策略本会怎么选"，不改实际指派
                                          # （v2.1 红线：影子期只记录不生效）


@dataclass(frozen=True)
class Rejected:
    """拒绝结果：机器码 + 人读理由 + 各过滤器的命中计数（运维可见）。"""

    task_ref: str
    code: str
    reason: str
    detail: Dict[str, object] = field(default_factory=dict)


# ── 份额记账 ─────────────────────────────────────────────────────────────────

class ShareLedger:
    """GPU 份额与并行槽位记账：Assignment 落账扣减，释放回收。

    只记账不决策（决策在贪心排序）；对账键是 assignment_id。
    """

    def __init__(self) -> None:
        self._gpu_by_node: Dict[str, float] = {}
        self._slots_by_node: Dict[str, int] = {}
        self._by_assignment: Dict[str, Tuple[str, float]] = {}

    def commit(self, assignment_id: str, node_id: str, gpu_frac: float) -> None:
        if assignment_id in self._by_assignment:
            raise SchedulingError(f"assignment {assignment_id} already committed")
        # R7 修复轮（终局，D4-R7 抽查补漏）：非有限/越域 gpu_frac 构造期拒绝——
        # NaN 入账后 committed_gpu=nan，``gpu_demand > free_gpu + GPU_FRAC_EPS``
        # 对 nan 恒 False → GPU 份额闸被静默绕过（实测：节点 committed=1.0 时
        # demand 0.95 正确拒 GPU_SHORTFALL，一笔 NaN 投毒后同 demand 变
        # admitted=overcommit fail-open）；gpu_frac 声明域是 [0,1] 份额，
        # 越域值（如 1.5）本就是直接 overcommit，一并拒绝。
        if not isinstance(gpu_frac, (int, float)) or not _finite(gpu_frac) \
                or not (0.0 <= gpu_frac <= 1.0):
            raise SchedulingError(
                f"gpu_frac must be a finite fraction in [0, 1], got {gpu_frac!r}")
        self._by_assignment[assignment_id] = (node_id, round(float(gpu_frac), 9))
        self._gpu_by_node[node_id] = round(
            self._gpu_by_node.get(node_id, 0.0) + float(gpu_frac), 9)
        self._slots_by_node[node_id] = self._slots_by_node.get(node_id, 0) + 1

    def release(self, assignment_id: str) -> bool:
        entry = self._by_assignment.pop(assignment_id, None)
        if entry is None:
            return False
        node_id, frac = entry
        self._gpu_by_node[node_id] = round(self._gpu_by_node.get(node_id, 0.0) - frac, 9)
        if self._gpu_by_node[node_id] <= GPU_FRAC_EPS:
            self._gpu_by_node[node_id] = 0.0
        self._slots_by_node[node_id] = max(0, self._slots_by_node.get(node_id, 0) - 1)
        return True

    def committed_gpu(self, node_id: str) -> float:
        return self._gpu_by_node.get(node_id, 0.0)

    def active_slots(self, node_id: str) -> int:
        return self._slots_by_node.get(node_id, 0)


# ── 过滤与贪心排序（纯决策；记账由 GreedyScheduler 落账）──────────────────────

def _evaluate(offering: TaskOffering, record: NodeRecord, *,
              ledger: Optional[ShareLedger], now: float) -> Optional[str]:
    """候选资格检查：返回拒绝码；None = 可派。硬规则（沙箱）最先判。"""
    cap = record.capacity
    committed = ledger.committed_gpu(cap.node_id) if ledger else 0.0
    slots = ledger.active_slots(cap.node_id) if ledger else 0
    free_gpu = round(cap.gpu_frac - committed, 9)
    # 硬规则（12.6，写死）：不可信节点只接沙箱任务类
    if cap.sandbox_only and not offering.sandbox_class:
        return REJECT_SANDBOX
    if offering.trust_required is not None and cap.trust_level != offering.trust_required:
        return REJECT_TRUST
    if not set(offering.required_tools).issubset(set(cap.tools)):
        return REJECT_TOOLS
    if offering.gpu_demand > free_gpu + GPU_FRAC_EPS:
        return REJECT_GPU
    if slots + 1 > cap.max_parallel:
        return REJECT_SLOT
    if not record.online.is_online(now):
        return REJECT_OFFLINE
    # 既有调度谓词兜底（复用 routes.NodeCapacity.can_take，正常路径不可达分支）
    if not cap.can_take(needs_gpu=offering.gpu_demand > 0,
                        sandbox_class=offering.sandbox_class,
                        parallel_slots=slots + 1):
        return REJECT_NO_NODE
    return None


def _rank_key(record: NodeRecord, ledger: Optional[ShareLedger]) -> Tuple[float, ...]:
    """P1 贪心排序键：信任等级降序 → 空闲 gpu_frac 降序 → max_parallel 余量降序
    → node_id 升序（确定性 tie-break）。键锁定不变（既有 P1 测试语义）。"""
    cap = record.capacity
    committed = ledger.committed_gpu(cap.node_id) if ledger else 0.0
    slots = ledger.active_slots(cap.node_id) if ledger else 0
    free_gpu = round(cap.gpu_frac - committed, 9)
    trust_rank = 1 if cap.trust_level == TRUST_TRUSTED else 0
    return (-trust_rank, -free_gpu, -(cap.max_parallel - slots), cap.node_id)


def rank_key_best_fit(record: NodeRecord, ledger: Optional[ShareLedger],
                      gpu_demand: float) -> Tuple[float, ...]:
    """P2 bin-packing 排序键（best-fit by gpu_frac，v2.0 §10 M2 利用率优化）：

    放置后剩余 (free_gpu − demand) **最小**者优先 → 碎片最小、装得最满；
    平局 → 节点 gpu_priority 降序（GPU 优先级高者先拿到任务）；
    再平 → node_id 升序（确定性 tie-break）。
    只对**已过过滤链**的候选求值（调用方保证 demand ≤ free_gpu）。
    """
    cap = record.capacity
    committed = ledger.committed_gpu(cap.node_id) if ledger else 0.0
    free_gpu = round(cap.gpu_frac - committed, 9)
    leftover = round(free_gpu - float(gpu_demand), 9)
    return (leftover, -cap.gpu_priority, cap.node_id)


# ── FEFO 资源压力（v2.1 §7 消耗策略，W-04）────────────────────────────────────
#
# FEFO（First-Expired-First-Out）：周限额先到期的资源先用——把"即将随窗口重置而
# 作废的额度"先烧掉。主信号 = weekly_remaining_ratio（周限剩余比，越低压力越大）；
# 辅信号 = 5h 窗剩余 <15% 时升权（该窗口余额即将随滚动重置而蒸发，先用掉）。
# 阈值属**软项**：owner 经验 bootstrap，计量数据周重拟合（决策域 thresholds 投影）。

FEFO_5H_LOW = 0.15          # 辅信号阈值：5h 窗剩余 <15% 触发升权（v2.1 §7）
FEFO_5H_UPLIFT = 0.25       # 升权增量（加到压力分上，封顶 1.0）
WEEKLY_WINDOW_PREFIX = "weekly"        # 计量行 window_id 前缀 → 主信号
FIVE_HOURS_WINDOW_ID = "five_hours"    # 计量行 window_id 前缀 → 辅信号


@dataclass(frozen=True)
class ResourcePressure:
    """FEFO 输入接口：**计量数据对象**（resources 档案的投影，非档案原文）。

    来源链：company-ops/resources/<id>.yaml（window.limit 投影出 limit）
    → W-03 计量器五键 NDJSON（usage_events）→ :func:`pressures_from_meter_rows`
    → 本对象 → 调度排序。缺读数（None）= 中性处理，不因缺计量而排序靠前。
    """

    node_id: str                                   # 调度器节点标识
    resource_id: str = ""                          # 来源档案 resource_id（可溯）
    weekly_remaining_ratio: Optional[float] = None # 主信号 0.0–1.0；None=无读数
    window_5h_remaining: Optional[float] = None    # 辅信号 0.0–1.0；None=无读数
    as_of: float = 0.0                             # 投影时刻（epoch 秒；0=未记）
    source: str = "usage_events"                   # 计量来源（对齐档案 source 纪律）

    def __post_init__(self) -> None:
        if not self.node_id or not isinstance(self.node_id, str):
            raise SchedulingError("ResourcePressure.node_id must be a non-empty str")
        for name in ("weekly_remaining_ratio", "window_5h_remaining"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SchedulingError(
                    f"ResourcePressure.{name} must be None or float in [0.0, 1.0], "
                    f"got {value!r}")
            try:
                value_f = float(value)
            except OverflowError:            # 巨型 int：归类拒绝，不变形为未归类
                raise SchedulingError(       # OverflowError（D1-R6① 纪律）
                    f"ResourcePressure.{name} must be None or float in [0.0, 1.0], "
                    f"got {value!r}") from None
            if not (0.0 <= value_f <= 1.0):
                raise SchedulingError(
                    f"ResourcePressure.{name} must be None or float in [0.0, 1.0], "
                    f"got {value!r}")


def pressure_score(pressure: Optional[ResourcePressure]) -> float:
    """FEFO 压力分 0.0–1.0（越高 = 越该先把活派给它）。

    主信号：``1 − weekly_remaining_ratio``（周限剩余越少压力越大）；
    辅信号：``window_5h_remaining < FEFO_5H_LOW`` 时加 ``FEFO_5H_UPLIFT``（封顶 1.0）。
    ``None``（无计量读数）按中性 0 计——缺数据不制造优先级。
    """
    if pressure is None:
        return 0.0
    weekly = pressure.weekly_remaining_ratio
    score = (1.0 - float(weekly)) if weekly is not None else 0.0
    w5 = pressure.window_5h_remaining
    if w5 is not None and float(w5) < FEFO_5H_LOW:
        score = min(1.0, score + FEFO_5H_UPLIFT)
    return round(score, 9)


def rank_key_fefo(record: NodeRecord,
                  pressure: Optional[ResourcePressure]) -> Tuple[float, ...]:
    """FEFO 排序键：压力**降序**（压力大者先派）→ node_id 升序（确定性 tie-break）。

    只对已过过滤链的候选求值；与 :func:`rank_key_best_fit` 一样是纯排序键，
    在影子期只用于 choice_trace 计算，不进入实际指派（v2.1 红线）。
    """
    return (-pressure_score(pressure), record.capacity.node_id)


def pressures_from_meter_rows(
        rows: Any,
        *,
        limits: Optional[Any] = None,
        node_map: Optional[Any] = None,
        now: Optional[float] = None) -> Dict[str, ResourcePressure]:
    """W-03 计量行（五键 NDJSON 逐行 dict）→ FEFO 输入投影。

    输入（计量数据对象，不是资源档案原文）：
    - ``rows``：每行至少 ``resource``/``window_id``/``value``；``value`` 语义 =
      该窗口**已用量**（W-03 计量器口径）。``window_id`` 以 ``weekly`` 或
      ``monthly`` 前缀（含 ``-<周期后缀>``）计入主信号（只有月窗的资源——如
      cnb-sandbox 1600 核时/月——以月窗燃尽度作周限主信号的口径）；以
      ``five_hours`` 前缀计入辅信号；行内显式带 ``remaining_ratio``（0.0–1.0）
      时**优先直读**（计量器可直接输出剩余比，跳过 limit 换算）。
    - ``limits``：``{resource_id: {窗口id: 限额}}``——resources 档案
      ``window.limit`` 的投影（如 ``{"cnb-sandbox": {"monthly": 1600}}``）。
      周限主信号的限额取该资源映射里**第一个**键以 ``weekly``/``monthly``
      开头的窗口；5h 辅信号取 ``five_hours`` 窗。缺限额 → 该信号 None（中性）。
    - ``node_map``：``{resource_id: node_id}`` 档案→调度节点投影；缺省 resource_id
      即 node_id。
    - ``now``：写入 ``ResourcePressure.as_of``（epoch 秒）；None → 0.0。

    返回 ``{node_id: ResourcePressure}``；**两个信号都无读数**的资源不投影
    （全缺口行不投影——调用方 ``.get(node_id)`` 得 None = 中性 0 压力）。
    """
    limits = limits or {}
    node_map = node_map or {}
    used: Dict[str, Dict[str, float]] = {}
    explicit: Dict[str, Dict[str, float]] = {}    # 行内直读的 remaining_ratio
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        resource = row.get("resource")
        window_id = row.get("window_id")
        if not resource or not window_id:
            continue
        window_id = str(window_id)
        kind = None
        if window_id == WEEKLY_WINDOW_PREFIX or \
                window_id.startswith(WEEKLY_WINDOW_PREFIX + "-") or \
                window_id == "monthly" or window_id.startswith("monthly-"):
            kind = "weekly"
        elif window_id == FIVE_HOURS_WINDOW_ID or \
                window_id.startswith(FIVE_HOURS_WINDOW_ID + "-"):
            kind = "five_hours"
        if kind is None:
            continue
        resource = str(resource)
        ratio = row.get("remaining_ratio")
        ratio_f = None
        if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
            try:
                ratio_f = float(ratio)
            except OverflowError:         # 巨型 int：与越界有限值同款回落 value
                ratio_f = None            # 换算路径，投影不崩溃（外部计量行输入面）
        if ratio_f is not None and 0.0 <= ratio_f <= 1.0:
            explicit.setdefault(resource, {}).setdefault(kind, ratio_f)
            continue                      # 直读行优先，不再当已用量换算
        entry = used.setdefault(resource, {})
        value = row.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue                      # 缺口行（value=null）如实忽略，不投影
        if entry.get(kind) is None:
            entry[kind] = float(value)    # 同窗口多行取首行；聚合归装载器/周重拟合
    pressures: Dict[str, ResourcePressure] = {}
    for resource in sorted(set(used) | set(explicit)):
        signals = used.get(resource) or {}
        weekly_ratio: Optional[float] = None
        five_hours_ratio: Optional[float] = None
        resource_limits = limits.get(resource) or {}
        weekly_limit = next((float(v) for k, v in sorted(resource_limits.items())
                             if k == "weekly" or k.startswith("weekly-")
                             or k == "monthly" or k.startswith("monthly-")), None)
        five_hours_limit = next((float(v) for k, v in sorted(resource_limits.items())
                                 if k == FIVE_HOURS_WINDOW_ID
                                 or k.startswith(FIVE_HOURS_WINDOW_ID + "-")), None)
        used_weekly = signals.get("weekly")
        if used_weekly is not None:
            weekly_ratio = (max(0.0, 1.0 - used_weekly / weekly_limit)
                            if weekly_limit and weekly_limit > 0 else None)
        used_5h = signals.get("five_hours")
        if used_5h is not None:
            five_hours_ratio = (max(0.0, 1.0 - used_5h / five_hours_limit)
                                if five_hours_limit and five_hours_limit > 0 else None)
        # 行内直读的剩余比优先于"已用量/限额"换算（计量器直报口径）
        resource_explicit = explicit.get(resource) or {}
        if "weekly" in resource_explicit:
            weekly_ratio = resource_explicit["weekly"]
        if "five_hours" in resource_explicit:
            five_hours_ratio = resource_explicit["five_hours"]
        if weekly_ratio is None and five_hours_ratio is None:
            continue                      # 全缺口/全无读数 → 不投影（中性缺席）
        pressures[node_map.get(resource, resource)] = ResourcePressure(
            node_id=node_map.get(resource, resource),
            resource_id=resource,
            weekly_remaining_ratio=weekly_ratio,
            window_5h_remaining=five_hours_ratio,
            as_of=float(now) if now is not None else 0.0,
        )
    return pressures


def assign(offering: TaskOffering, registry: FleetRegistry, *,
           ledger: Optional[ShareLedger] = None,
           now: Optional[float] = None,
           policy: str = SCHED_POLICY_GREEDY,
           shadow_fefo: Optional[Mapping[str, ResourcePressure]] = None
           ) -> Union[Assignment, Rejected]:
    """派工（纯决策 + 可选落账）。``policy``：``greedy``（P1，默认）|
    ``best-fit``（P2 bin-packing，见 :func:`rank_key_best_fit`）；
    未知策略名 → SchedulingError（fail-closed，不静默回退）。

    过滤 → 排序取首；无可派节点 → Rejected（含各过滤器命中计数）。
    ``ledger`` 缺省为 None = 只决策不落账（dry-run 语义）；
    :class:`GreedyScheduler` 总是传入共享账本完成真实记账。

    ``shadow_fefo``（FEFO 影子期，v2.1 施工红线）：传入
    ``{node_id: ResourcePressure}`` 计量投影时，实际指派**仍完全由 ``policy``
    决定**；FEFO 在同一批过过滤链的候选上计算的排序只写进
    ``Assignment.choice_trace``（``policy_active=False``）——影子价格与利用率
    周报上线前，任何调度策略调整只记录不生效。
    """
    now = time.time() if now is None else now
    if policy not in SCHED_POLICIES:
        raise SchedulingError(f"unknown scheduling policy: {policy!r}")
    if offering.guardrail_run_ref is not None and \
            offering.guardrail_verdict != VERDICT_PASS:
        return Rejected(
            task_ref=offering.task_ref, code=REJECT_GUARDRAIL,
            reason=f"guardrail verdict {offering.guardrail_verdict!r} is not "
                   f"{VERDICT_PASS!r} — fail-closed (UNKNOWN included)",
            detail={"guardrail_run_ref": offering.guardrail_run_ref})
    counts: Dict[str, int] = {}
    eligible: List[NodeRecord] = []
    best: Optional[NodeRecord] = None
    best_key: Optional[Tuple[float, ...]] = None
    for record in registry.active_nodes():
        if record.tenant_id != offering.tenant_id:
            counts[REJECT_TENANT] = counts.get(REJECT_TENANT, 0) + 1
            continue
        reason = _evaluate(offering, record, ledger=ledger, now=now)
        if reason is not None:
            counts[reason] = counts.get(reason, 0) + 1
            continue
        eligible.append(record)
        key = (_rank_key(record, ledger) if policy == SCHED_POLICY_GREEDY
               else rank_key_best_fit(record, ledger, offering.gpu_demand))
        if best is None or key < best_key:
            best, best_key = record, key
    if best is None:
        # 拒绝码推断：全部候选因**同一**能力原因落选 → 直接给该原因码
        # （如沙箱硬规则），便于机器判定；混合原因/仅租户不匹配 → 汇总码 + 计数。
        capable = {k: v for k, v in counts.items() if k != REJECT_TENANT}
        if len(capable) == 1 and not counts.get(REJECT_TENANT):
            code = next(iter(capable))
        else:
            code = REJECT_NO_NODE
        return Rejected(
            task_ref=offering.task_ref, code=code,
            reason="no eligible node in registry for this offering",
            detail=dict(counts))
    cap = best.capacity
    assignment = Assignment(
        assignment_id=uuid.uuid4().hex,
        offering=offering,
        node_id=best.node_id,
        gpu_frac_committed=float(offering.gpu_demand),
        sandbox_only=cap.sandbox_only,
        review_required=cap.sandbox_only,
        assigned_at=now,
        tenant_id=offering.tenant_id,
        preempt_ok=offering.preempt_ok,
    )
    if shadow_fefo is not None and eligible:
        # 影子期：FEFO 只留轨迹，不碰 best（v2.1 红线——只记录不生效）。
        ranking = sorted(eligible,
                         key=lambda r: rank_key_fefo(r, shadow_fefo.get(r.capacity.node_id)))
        assignment.choice_trace = {
            "shadow": True,
            "policy_active": False,
            "policy": "fefo",
            "effective_policy": policy,
            "effective_choice": best.capacity.node_id,
            "fefo_choice": ranking[0].capacity.node_id,
            "fefo_ranking": [
                {"node_id": r.capacity.node_id,
                 "pressure": pressure_score(shadow_fefo.get(r.capacity.node_id)),
                 "weekly_remaining_ratio": (
                     shadow_fefo[r.capacity.node_id].weekly_remaining_ratio
                     if r.capacity.node_id in shadow_fefo else None),
                 "window_5h_remaining": (
                     shadow_fefo[r.capacity.node_id].window_5h_remaining
                     if r.capacity.node_id in shadow_fefo else None),
                 } for r in ranking],
            "note": "shadow period (v2.1 red line): FEFO records choice_trace "
                    "only; actual assignment unchanged until shadow price and "
                    "utilization weekly report are live",
        }
    if ledger is not None:
        ledger.commit(assignment.assignment_id, best.node_id,
                      offering.gpu_demand)
    return assignment


# ── 工单队列（自取制的 PENDING 池）────────────────────────────────────────────

SECRET_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9+._-]*://[^\s]+$"
_SECRET_REF_RE = re.compile(SECRET_REF_PATTERN)


def secret_ref_ok(ref: object) -> bool:
    """密钥范围声明只允许 scheme 限定的引用（如 ``bao://kv/data/company/gpu/t``）。"""
    return isinstance(ref, str) and bool(_SECRET_REF_RE.match(ref))


@dataclass
class WorkOrder:
    """自取制工单：PENDING → CLAIMED → COMPLETED（旁支：TTL 回收回 PENDING）。

    状态常量复用 ``jiuwen_glue.rules``（PENDING/CLAIMED/COMPLETED/CANCELLED）。
    ``payload_ref``/``secret_refs``/``artifact_ref`` 一律是引用——跨层只传引用
    （4.9 #10）；``parent_lease_id``/``effective_perms`` 来自 offering，
    认领时经 Budget Lease 派生实现"随子任务派生 + 权限收敛"。
    """

    order_id: str
    offering: TaskOffering
    priority: int = 0
    payload_ref: str = ""
    secret_refs: Tuple[str, ...] = ()
    state: str = PENDING
    claimed_by: Optional[str] = None
    claim_assignment_id: Optional[str] = None
    lease_expires_at: Optional[float] = None
    submitted_at: float = 0.0
    artifact_ref: Optional[str] = None
    tenant_id: str = "t0"


class WorkQueue:
    """内存工单队列（真实实现 = glue 工单队列的 Postgres 表，接口一致）。"""

    def __init__(self, *, now: Optional[Callable[[], float]] = None) -> None:
        self._now = now or time.time
        self._orders: Dict[str, WorkOrder] = {}
        self.audit: List[Dict[str, object]] = []

    def submit(self, offering: TaskOffering, *, order_id: Optional[str] = None,
               priority: int = 0, payload_ref: str = "",
               secret_refs: Tuple[str, ...] = ()) -> WorkOrder:
        for ref in secret_refs:
            if not secret_ref_ok(ref):
                raise SecretScopeError(
                    f"secret_refs entries must be scheme-qualified references "
                    f"(like 'bao://kv/data/company/gpu/token'), got {ref!r} — "
                    "raw credential values never enter the queue")
        order = WorkOrder(
            order_id=order_id or uuid.uuid4().hex,
            offering=offering,
            priority=priority,
            payload_ref=payload_ref,
            secret_refs=tuple(secret_refs),
            submitted_at=self._now(),
            tenant_id=offering.tenant_id,
        )
        self._orders[order.order_id] = order
        self.audit.append({"event": "SUBMIT", "order_id": order.order_id,
                           "task_ref": offering.task_ref,
                           "at": order.submitted_at})
        return order

    def get(self, order_id: str) -> WorkOrder:
        try:
            return self._orders[order_id]
        except KeyError:
            raise WorkOrderStateError(f"unknown work order: {order_id}") from None

    def pending(self) -> List[WorkOrder]:
        """PENDING 工单，按（priority 降序, 提交时间升序, order_id 升序）确定性排序。"""
        orders = [o for o in self._orders.values() if o.state == PENDING]
        return sorted(orders, key=lambda o: (-o.priority, o.submitted_at, o.order_id))

    def claimed(self) -> List[WorkOrder]:
        return [o for o in self._orders.values() if o.state == CLAIMED]

    def all_orders(self) -> List[WorkOrder]:
        return list(self._orders.values())

    def mark_completed(self, order_id: str, artifact_ref: Optional[str]) -> WorkOrder:
        order = self.get(order_id)
        if order.state != CLAIMED:
            raise WorkOrderStateError(
                f"order {order_id} is {order.state}, only CLAIMED can complete")
        order.state = COMPLETED
        order.artifact_ref = artifact_ref
        self.audit.append({"event": "COMPLETED", "order_id": order_id,
                           "artifact_ref": artifact_ref, "at": self._now()})
        return order

    def mark_cancelled(self, order_id: str) -> WorkOrder:
        order = self.get(order_id)
        if order.state in (COMPLETED, CANCELLED):
            raise WorkOrderStateError(f"order {order_id} already terminal")
        order.state = CANCELLED
        self.audit.append({"event": "CANCELLED", "order_id": order_id,
                           "at": self._now()})
        return order

    # ── 内部（仅 SelfPickMode 调用）───────────────────────────────────────

    def _mark_claimed(self, order: WorkOrder, *, node_id: str,
                      assignment_id: str, lease_expires_at: float) -> None:
        if order.state != PENDING:
            raise WorkOrderStateError(
                f"order {order.order_id} is {order.state}, not PENDING")
        order.state = CLAIMED
        order.claimed_by = node_id
        order.claim_assignment_id = assignment_id
        order.lease_expires_at = lease_expires_at
        self.audit.append({"event": "CLAIMED", "order_id": order.order_id,
                           "node_id": node_id, "at": self._now()})

    def _mark_pending(self, order: WorkOrder) -> None:
        """回收回 PENDING；幂等（已 PENDING 的工单保持不动）。"""
        if order.state == PENDING:
            return
        if order.state != CLAIMED:
            raise WorkOrderStateError(
                f"order {order.order_id} is {order.state}, only CLAIMED reclaims")
        order.state = PENDING
        order.claimed_by = None
        order.claim_assignment_id = None
        order.lease_expires_at = None
        self.audit.append({"event": "RECLAIMED", "order_id": order.order_id,
                           "at": self._now()})


# ── 调度器与两种取活模式 ──────────────────────────────────────────────────────

class GreedyScheduler:
    """P1 贪心调度器：决策（纯函数 :func:`assign`）+ 落账（ShareLedger）
    + 自取制短时租约（Budget Lease 派生，TTL ≤ 1h）。

    ``budget`` 缺省 None = 不签租约（纯内存演示）；传入 BudgetLedger 时，
    每笔 Assignment 派生一笔额度 1 的租约（自取制带 TTL，调度制无 TTL），
    释放/回收/完成即撤销（级联语义复用既有 revoke）。
    """

    def __init__(self, registry: FleetRegistry, *, now: Optional[Callable[[], float]] = None,
                 budget: Optional[BudgetLedger] = None,
                 max_self_pick_lease: float = MAX_SELF_PICK_LEASE_SECONDS,
                 policy: str = SCHED_POLICY_GREEDY) -> None:
        if policy not in SCHED_POLICIES:
            raise SchedulingError(f"unknown scheduling policy: {policy!r}")
        self._registry = registry
        self._now = now or time.time
        self._budget = budget
        self.max_self_pick_lease = float(max_self_pick_lease)
        self.policy = policy
        self._ledger = ShareLedger()
        self.assignments: Dict[str, Assignment] = {}
        self.audit: List[Dict[str, object]] = []

    # ── 派工（调度制入口）────────────────────────────────────────────────

    def assign(self, offering: TaskOffering, *, dispatch: str = DISPATCH_DIRECT,
               lease_ttl: Optional[float] = None,
               now: Optional[float] = None,
               policy: Optional[str] = None,
               shadow_fefo: Optional[Mapping[str, ResourcePressure]] = None
               ) -> Union[Assignment, Rejected]:
        now = self._now() if now is None else now
        if dispatch not in (DISPATCH_DIRECT, DISPATCH_SELF_PICK):
            raise SchedulingError(f"unknown dispatch mode: {dispatch!r}")
        if dispatch == DISPATCH_SELF_PICK:
            # R7 修复轮（终局，D4-R7 抽查补漏）：非有限 ttl 构造期拒绝——NaN 经
            # ``ttl <= 0`` 与 ``ttl > max_self_pick_lease`` 双比较恒 False 静默
            # 穿过两道闸（TTL≤1h 上限闸对 NaN 失效——执行面 token 短命语义被
            # 旁路，实测 ACCEPTED）；巨型 int 经 float() 抛未归类 OverflowError。
            # 与 ttl_seconds 全库同口径（leases/challenge/_finite 家族）。
            if lease_ttl is None:
                ttl = DEFAULT_SELF_PICK_LEASE_SECONDS
            else:
                try:
                    ttl = float(lease_ttl)
                except (OverflowError, TypeError, ValueError) as exc:
                    raise SchedulingError(
                        f"lease ttl must be a positive finite number, "
                        f"got {lease_ttl!r}") from exc
                if not _finite(ttl) or ttl <= 0:
                    raise SchedulingError(
                        f"lease ttl must be a positive finite number, got {ttl}")
            if ttl > self.max_self_pick_lease:
                return Rejected(
                    task_ref=offering.task_ref, code=REJECT_TTL_CAP,
                    reason=f"self-pick lease ttl {ttl}s exceeds cap "
                           f"{self.max_self_pick_lease}s (execution-worker "
                           "tokens are short-lived, <=1h)")
        else:
            ttl = None
        result = assign(offering, self._registry, ledger=self._ledger, now=now,
                        policy=policy or self.policy, shadow_fefo=shadow_fefo)
        if isinstance(result, Rejected):
            self.audit.append({"event": "ASSIGN_REJECTED", "at": now,
                               "task_ref": offering.task_ref,
                               "code": result.code, "detail": dict(result.detail)})
            return result
        result.dispatch = dispatch
        if self._budget is not None:
            try:
                lease = self._budget.grant(
                    offering.task_ref, 1,
                    parent_lease_id=offering.parent_lease_id,
                    ttl_seconds=ttl,
                    tenant_id=offering.tenant_id,
                    agent_ref=self._registry.get(result.node_id).registration.agent_ref,
                    effective_perms=offering.effective_perms)
            except LeaseError as exc:
                # 派生失败（父额度耗尽 / 权限越界）→ 拒绝并回滚份额账
                self._ledger.release(result.assignment_id)
                self.assignments[result.assignment_id] = result
                result.status = ASSIGN_RELEASED
                result.release_reason = "lease-derivation-failed"
                self.audit.append({"event": "ASSIGN_ROLLED_BACK", "at": now,
                                   "assignment_id": result.assignment_id,
                                   "task_ref": offering.task_ref,
                                   "reason": str(exc)})
                return Rejected(task_ref=offering.task_ref, code=REJECT_LEASE,
                                reason=f"budget lease derivation failed: {exc}")
            result.lease_ref = lease.lease_id
            result.effective_perms = tuple(lease.effective_perms) or _frozen(offering)
            if ttl is not None:
                result.lease_expires_at = now + ttl
        else:
            result.effective_perms = _frozen(offering)
        self.assignments[result.assignment_id] = result
        self.audit.append({"event": "ASSIGNED", "at": now,
                           "assignment_id": result.assignment_id,
                           "task_ref": offering.task_ref,
                           "node_id": result.node_id, "dispatch": dispatch,
                           "gpu_frac": result.gpu_frac_committed,
                           "review_required": result.review_required,
                           "fefo_shadow_choice": (result.choice_trace or {})
                                                 .get("fefo_choice")})
        return result

    # ── 释放 / 完成 / 超时 ───────────────────────────────────────────────

    def release(self, assignment_id: str, *, reason: str,
                status: str = ASSIGN_RELEASED) -> Assignment:
        assignment = self.get_assignment(assignment_id)
        if assignment.status != ASSIGN_ACTIVE:
            return assignment               # 幂等：终态派工不再改账
        assignment.status = status
        assignment.release_reason = reason
        self._ledger.release(assignment_id)
        if assignment.lease_ref is not None and self._budget is not None:
            self._budget.revoke(assignment.lease_ref, reason=reason)
        self.audit.append({"event": "RELEASED", "assignment_id": assignment_id,
                           "status": status, "reason": reason,
                           "at": self._now()})
        return assignment

    def complete(self, assignment_id: str, *, artifact_ref: Optional[str] = None) -> Assignment:
        assignment = self.get_assignment(assignment_id)
        if assignment.status != ASSIGN_ACTIVE:
            return assignment               # 幂等：终态派工不再改账
        assignment.artifact_ref = artifact_ref
        return self.release(assignment_id, reason="completed",
                            status=ASSIGN_COMPLETED)

    def timeout(self, assignment_id: str) -> Assignment:
        return self.release(assignment_id, reason="timeout")

    # ── 观测 ─────────────────────────────────────────────────────────────

    def free_gpu_frac(self, node_id: str) -> float:
        try:
            cap = self._registry.get(node_id).capacity
        except UnknownNodeError:
            raise
        return round(cap.gpu_frac - self._ledger.committed_gpu(node_id), 9)

    def active_slots(self, node_id: str) -> int:
        return self._ledger.active_slots(node_id)

    def get_assignment(self, assignment_id: str) -> Assignment:
        try:
            return self.assignments[assignment_id]
        except KeyError:
            raise UnknownAssignmentError(
                f"unknown assignment: {assignment_id}") from None

    @property
    def registry(self) -> FleetRegistry:
        return self._registry

    @property
    def dispatch_mode(self) -> "DispatchMode":
        return DispatchMode(self)

    @property
    def self_pick(self) -> "SelfPickMode":
        return SelfPickMode(self)


def _frozen(offering: TaskOffering) -> tuple:
    return offering.effective_perms.frozen() if offering.effective_perms else ()


class DispatchMode:
    """调度制（§12.6）：控制台/调度器直接派单到节点，节点被动接单。"""

    def __init__(self, scheduler: GreedyScheduler) -> None:
        self._scheduler = scheduler

    def dispatch(self, offering: TaskOffering, *,
                 now: Optional[float] = None) -> Union[Assignment, Rejected]:
        return self._scheduler.assign(offering, dispatch=DISPATCH_DIRECT, now=now)

    def complete(self, assignment_id: str, *,
                 artifact_ref: Optional[str] = None) -> Assignment:
        return self._scheduler.complete(assignment_id, artifact_ref=artifact_ref)

    def timeout(self, assignment_id: str) -> Assignment:
        return self._scheduler.timeout(assignment_id)


class SelfPickMode:
    """自取制（§12.6）：节点轮询工单队列认领 PENDING——间歇在线的线下机器走这条。

    - ``claim``：按队列确定性顺序找到该节点**可接**的第一单（硬规则与调度制
      同一套过滤链），派生 TTL ≤ 1h 的短时租约并把工单置 CLAIMED；
      TTL 超过上限 → Rejected（fail-closed，不截断）。
    - ``reclaim_expired``：租约 TTL 过期的 CLAIMED 工单回 PENDING 并释放份额/
      撤销租约——心跳 STALE 的兜底死亡语义在工单层的对应物。
    """

    DEFAULT_LEASE_TTL = DEFAULT_SELF_PICK_LEASE_SECONDS

    def __init__(self, scheduler: GreedyScheduler) -> None:
        self._scheduler = scheduler

    def claim(self, node_id: str, queue: WorkQueue, *, ttl_seconds: Optional[float] = None,
              now: Optional[float] = None) -> Union[Assignment, Rejected]:
        scheduler = self._scheduler
        now = scheduler._now() if now is None else now
        record = scheduler._registry.get(node_id)     # 未注册 → UnknownNodeError
        if record.status != NODE_ACTIVE:
            return Rejected(task_ref="-", code=REJECT_STALE,
                            reason=f"node {node_id} is {record.status} — "
                                   "stale nodes cannot claim; re-register first")
        ttl = self.DEFAULT_LEASE_TTL if ttl_seconds is None else float(ttl_seconds)
        if ttl <= 0:
            raise SchedulingError(f"lease ttl must be positive, got {ttl}")
        if ttl > scheduler.max_self_pick_lease:
            return Rejected(
                task_ref="-", code=REJECT_TTL_CAP,
                reason=f"self-pick lease ttl {ttl}s exceeds cap "
                       f"{scheduler.max_self_pick_lease}s (execution-worker "
                       "tokens are short-lived, <=1h)")
        considered = 0
        first_task: Optional[str] = None
        first_code: Optional[str] = None
        for order in queue.pending():
            if order.offering.tenant_id != record.tenant_id:
                continue
            considered += 1
            first_task = first_task or order.offering.task_ref
            reason = _evaluate(order.offering, record,
                               ledger=scheduler._ledger, now=now)
            if reason is not None:
                first_code = first_code or reason
                continue
            result = scheduler.assign(order.offering, dispatch=DISPATCH_SELF_PICK,
                                      lease_ttl=ttl, now=now)
            if isinstance(result, Rejected):     # 评估与落账之间账本/租约恶化
                first_code = first_code or result.code
                continue
            result.order_id = order.order_id
            queue._mark_claimed(order, node_id=node_id,
                                assignment_id=result.assignment_id,
                                lease_expires_at=now + ttl)
            return result
        if considered == 0:
            return Rejected(task_ref="-", code=REJECT_QUEUE_EMPTY,
                            reason="no PENDING work order claimable in queue")
        return Rejected(
            task_ref=first_task or "-", code=first_code or REJECT_NO_NODE,
            reason=f"{considered} pending order(s) not claimable by node {node_id}")

    def complete(self, assignment_id: str, queue: WorkQueue, *,
                 artifact_ref: Optional[str] = None) -> Assignment:
        assignment = self._scheduler.complete(assignment_id, artifact_ref=artifact_ref)
        if assignment.order_id is not None:
            queue.mark_completed(assignment.order_id, artifact_ref)
        return assignment

    def fail(self, assignment_id: str, queue: WorkQueue, *,
             reason: str) -> Assignment:
        """执行失败：释放派工并把工单退回 PENDING（等下一轮认领/回收）。"""
        assignment = self._scheduler.release(assignment_id, reason=reason)
        if assignment.order_id is not None:
            queue._mark_pending(queue.get(assignment.order_id))
        return assignment

    def reclaim_expired(self, queue: WorkQueue, *,
                        now: Optional[float] = None) -> List[WorkOrder]:
        """TTL 过期回收：CLAIMED 且租约到期的工单回 PENDING，释放全部账目。"""
        now = self._scheduler._now() if now is None else now
        reclaimed: List[WorkOrder] = []
        for order in queue.claimed():
            if order.lease_expires_at is not None and now >= order.lease_expires_at:
                assignment_id = order.claim_assignment_id   # 先取引用再清单据
                queue._mark_pending(order)
                if assignment_id is not None:
                    self._scheduler.release(assignment_id,
                                            reason="lease-expired",
                                            status=ASSIGN_RECLAIMED)
                reclaimed.append(order)
        return reclaimed
