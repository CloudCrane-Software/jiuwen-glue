# coding: utf-8
"""fleet 子包：节点池注册协议（PROP-0003）+ 调度器 P1 贪心（PROP-0004）— WO-0011.

模块地图:
- :mod:`jiuwen_glue.fleet.registration` — OpenBao JWT 注册声明（协议级校验，
  不验签）、NodeRegistration/NodeRecord、FleetRegistry（register/heartbeat/
  deregister + STALE 心跳超窗兜底）、可选 SQL 持久化接口（glue.node 对齐）。
- :mod:`jiuwen_glue.fleet.scheduler` — TaskOffering/Assignment/Rejected、
  P1 贪心 :func:`assign`、ShareLedger 份额记账、WorkQueue/WorkOrder 工单队列、
  DispatchMode（调度制）/ SelfPickMode（自取制 + TTL 回收）、Budget Lease 派生。
- :mod:`jiuwen_glue.fleet.worker` — 自取制客户端协议（模拟侧）：WorkerLoop.tick、
  WorkerContext（lease_scoped_secrets 只允许引用）。

规格来源: PROP-0001 v1.7 §12.6（节点池纳管/两种取活模式/不可信节点边界）、
§13（GPU_FRAC → gpu_frac 字段、调度收敛）、PROP-0004 三阶段（P1 实现，
P2/P3 只做设计——见 docs/fleet-design.md）；复用 routes.NodeCapacity /
leases.BudgetLease / identity.EffectivePerms / guardrail 聚合三态，不重造。
"""
from .errors import (
    AttestationError,
    FleetError,
    NodeStateError,
    OnlineWindowError,
    RegistrationError,
    SchedulingError,
    SecretScopeError,
    UnknownAssignmentError,
    UnknownNodeError,
    WorkOrderStateError,
)
from .registration import (
    NODE_ACTIVE,
    NODE_DEREGISTERED,
    NODE_STALE,
    FleetRegistry,
    NodeAttestation,
    NodeRecord,
    NodeRegistration,
    OnlineWindow,
    RegistryPersistence,
    decode_jwt_claims,
    parse_online_window,
)
from .scheduler import (
    ASSIGN_ACTIVE,
    ASSIGN_COMPLETED,
    ASSIGN_RECLAIMED,
    ASSIGN_RELEASED,
    DEFAULT_SELF_PICK_LEASE_SECONDS,
    DISPATCH_DIRECT,
    DISPATCH_SELF_PICK,
    GPU_FRAC_EPS,
    MAX_SELF_PICK_LEASE_SECONDS,
    REJECT_GPU,
    REJECT_GUARDRAIL,
    REJECT_LEASE,
    REJECT_NO_NODE,
    REJECT_OFFLINE,
    REJECT_QUEUE_EMPTY,
    REJECT_SANDBOX,
    REJECT_SLOT,
    REJECT_STALE,
    REJECT_TENANT,
    REJECT_TTL_CAP,
    REJECT_TOOLS,
    REJECT_TRUST,
    Assignment,
    DispatchMode,
    GreedyScheduler,
    Rejected,
    SelfPickMode,
    ShareLedger,
    TaskOffering,
    WorkOrder,
    WorkQueue,
    assign,
    secret_ref_ok,
)
from .worker import TickResult, WorkerContext, WorkerLoop

__all__ = [
    # errors
    "FleetError", "RegistrationError", "AttestationError", "OnlineWindowError",
    "UnknownNodeError", "NodeStateError", "SchedulingError",
    "UnknownAssignmentError", "WorkOrderStateError", "SecretScopeError",
    # registration（PROP-0003）
    "NODE_ACTIVE", "NODE_STALE", "NODE_DEREGISTERED",
    "NodeAttestation", "NodeRegistration", "NodeRecord",
    "FleetRegistry", "RegistryPersistence",
    "OnlineWindow", "parse_online_window", "decode_jwt_claims",
    # scheduler（PROP-0004 P1）
    "GPU_FRAC_EPS", "MAX_SELF_PICK_LEASE_SECONDS", "DEFAULT_SELF_PICK_LEASE_SECONDS",
    "DISPATCH_DIRECT", "DISPATCH_SELF_PICK",
    "ASSIGN_ACTIVE", "ASSIGN_COMPLETED", "ASSIGN_RELEASED", "ASSIGN_RECLAIMED",
    "REJECT_NO_NODE", "REJECT_SANDBOX", "REJECT_TRUST", "REJECT_TOOLS",
    "REJECT_GPU", "REJECT_SLOT", "REJECT_OFFLINE", "REJECT_GUARDRAIL",
    "REJECT_STALE", "REJECT_TENANT", "REJECT_TTL_CAP", "REJECT_QUEUE_EMPTY",
    "REJECT_LEASE",
    "TaskOffering", "Assignment", "Rejected", "ShareLedger", "assign",
    "GreedyScheduler", "DispatchMode", "SelfPickMode",
    "WorkOrder", "WorkQueue", "secret_ref_ok",
    # worker（自取制客户端协议，模拟侧）
    "WorkerContext", "TickResult", "WorkerLoop",
]
