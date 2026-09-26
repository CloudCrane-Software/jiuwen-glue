# coding: utf-8
"""fleet 调度器 P1 贪心（PROP-0004 / PROP-0001 v1.7 §12.6/§13）— WO-0011.

规格来源（PROP-0004 三阶段：P1 贪心 → P2 利用率优化 → P3 云突发伸缩）:

- **P1 贪心**（本模块）：``assign(offering, registry) -> Assignment | Rejected``
  过滤（can_take / 信任等级 / 在线窗口 / GPU 份额余量 / 并行余量）→
  按（信任等级降序、空闲 gpu_frac 降序、max_parallel 余量降序）排序取首，
  node_id 升序为最终确定性 tie-break。
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

边界（4.9 #10/#13）：任务对象跨层只传引用（task_ref/payload_ref/artifact_ref
一律是引用）；调度器只派工单，**不碰 jiuwenswarm control 实例管理 API**——
实例内部归 control 面，调度面只见工单。
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple, Union

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

__all__ = [
    "GPU_FRAC_EPS",
    "MAX_SELF_PICK_LEASE_SECONDS", "DEFAULT_SELF_PICK_LEASE_SECONDS",
    "DISPATCH_DIRECT", "DISPATCH_SELF_PICK",
    "ASSIGN_ACTIVE", "ASSIGN_COMPLETED", "ASSIGN_RELEASED", "ASSIGN_RECLAIMED",
    "REJECT_NO_NODE", "REJECT_SANDBOX", "REJECT_TRUST", "REJECT_TOOLS",
    "REJECT_TENANT",
    "REJECT_GPU", "REJECT_SLOT", "REJECT_OFFLINE", "REJECT_GUARDRAIL",
    "REJECT_STALE", "REJECT_TTL_CAP", "REJECT_QUEUE_EMPTY", "REJECT_LEASE",
    "TaskOffering", "Assignment", "Rejected", "ShareLedger",
    "assign", "GreedyScheduler", "DispatchMode", "SelfPickMode",
    "WorkOrder", "WorkQueue", "SECRET_REF_PATTERN", "secret_ref_ok",
]

# GPU 份额比较容差（份额是 0.0–1.0 浮点，扣减累计有噪声）
GPU_FRAC_EPS = 1e-9

# 自取制租约 TTL 上限：对齐 execution-worker ≤1h 短时令牌形状（M0 审计 B4）
MAX_SELF_PICK_LEASE_SECONDS = 3600.0
DEFAULT_SELF_PICK_LEASE_SECONDS = 900.0

DISPATCH_DIRECT = "dispatch"        # 调度制：控制台直接派
DISPATCH_SELF_PICK = "self-pick"    # 自取制：节点认领

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
    tenant_id: str = "t0"

    def __post_init__(self) -> None:
        if not self.task_ref or not isinstance(self.task_ref, str):
            raise SchedulingError("task_ref must be a non-empty reference")
        if not isinstance(self.gpu_demand, (int, float)) or \
                not (0.0 <= float(self.gpu_demand) <= 1.0):
            raise SchedulingError(
                f"gpu_demand must be a float in [0.0, 1.0], got {self.gpu_demand!r}")
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
    """贪心排序键：信任等级降序 → 空闲 gpu_frac 降序 → max_parallel 余量降序
    → node_id 升序（确定性 tie-break）。"""
    cap = record.capacity
    committed = ledger.committed_gpu(cap.node_id) if ledger else 0.0
    slots = ledger.active_slots(cap.node_id) if ledger else 0
    free_gpu = round(cap.gpu_frac - committed, 9)
    trust_rank = 1 if cap.trust_level == TRUST_TRUSTED else 0
    return (-trust_rank, -free_gpu, -(cap.max_parallel - slots), cap.node_id)


def assign(offering: TaskOffering, registry: FleetRegistry, *,
           ledger: Optional[ShareLedger] = None,
           now: Optional[float] = None) -> Union[Assignment, Rejected]:
    """P1 贪心派工（纯决策 + 可选落账）。

    过滤 → 排序取首；无可派节点 → Rejected（含各过滤器命中计数）。
    ``ledger`` 缺省为 None = 只决策不落账（dry-run 语义）；
    :class:`GreedyScheduler` 总是传入共享账本完成真实记账。
    """
    now = time.time() if now is None else now
    if offering.guardrail_run_ref is not None and \
            offering.guardrail_verdict != VERDICT_PASS:
        return Rejected(
            task_ref=offering.task_ref, code=REJECT_GUARDRAIL,
            reason=f"guardrail verdict {offering.guardrail_verdict!r} is not "
                   f"{VERDICT_PASS!r} — fail-closed (UNKNOWN included)",
            detail={"guardrail_run_ref": offering.guardrail_run_ref})
    counts: Dict[str, int] = {}
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
        key = _rank_key(record, ledger)
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
    )
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
                 max_self_pick_lease: float = MAX_SELF_PICK_LEASE_SECONDS) -> None:
        self._registry = registry
        self._now = now or time.time
        self._budget = budget
        self.max_self_pick_lease = float(max_self_pick_lease)
        self._ledger = ShareLedger()
        self.assignments: Dict[str, Assignment] = {}
        self.audit: List[Dict[str, object]] = []

    # ── 派工（调度制入口）────────────────────────────────────────────────

    def assign(self, offering: TaskOffering, *, dispatch: str = DISPATCH_DIRECT,
               lease_ttl: Optional[float] = None,
               now: Optional[float] = None) -> Union[Assignment, Rejected]:
        now = self._now() if now is None else now
        if dispatch not in (DISPATCH_DIRECT, DISPATCH_SELF_PICK):
            raise SchedulingError(f"unknown dispatch mode: {dispatch!r}")
        if dispatch == DISPATCH_SELF_PICK:
            ttl = DEFAULT_SELF_PICK_LEASE_SECONDS if lease_ttl is None else float(lease_ttl)
            if ttl <= 0:
                raise SchedulingError(f"lease ttl must be positive, got {ttl}")
            if ttl > self.max_self_pick_lease:
                return Rejected(
                    task_ref=offering.task_ref, code=REJECT_TTL_CAP,
                    reason=f"self-pick lease ttl {ttl}s exceeds cap "
                           f"{self.max_self_pick_lease}s (execution-worker "
                           "tokens are short-lived, <=1h)")
        else:
            ttl = None
        result = assign(offering, self._registry, ledger=self._ledger, now=now)
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
                           "review_required": result.review_required})
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
