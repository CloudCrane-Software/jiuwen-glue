# coding: utf-8
"""usage_events 计量 + 租约-Higress consumer 绑定（v2.0 §4.4 / M2 计量计费闭环）— W-04.

计量四维度（append-only，只增不改——与 decision_record 同纪律）:

- ``llm_relay``       LLM 中继调用（**按租户 consumer key 维度**——Higress 是唯一
  模型路由决策点，中继计量挂在 consumer 上；consumer key 是引用不是密钥明文）
- ``compute_seconds`` 计算秒
- ``storage_bytes``   存储字节
- ``sandbox_seconds`` 沙箱秒

租约与 Higress consumer key 绑定（§4.4 写死）：**租约过期即网关断流**
（软提醒不算数）。本模块提供绑定声明结构 :class:`LeaseConsumerBinding` 与
纯求值 :func:`cutoff_due` / :func:`cutoff_plan`——读过期租约（含惰性到期：
状态仍 ACTIVE 但 expires_at 已过）算出"该禁用哪些 consumer"；**真实断流的
执行归 srv-1 侧定时检查脚本**（CNB company-ops ``ops/scripts/lease-gateway-guard/``，
默认 dry-run；真实断流 [待 owner 批]）。本模块不做任何网络调用。

边界（写死）:

- 内存版是权威实现；Postgres 持久化对应 CNB company-ops
  ``ops/sql/004_usage_events.sql``（glue.usage_event / glue.lease_consumer_binding /
  glue.v_usage / glue.v_binding_cutoff_due），DDL 触发器是第二道闸（append-only）。
- 凭证纪律：consumer_key 是 Higress consumer **名称引用**，密钥明文永不进计量事件；
  本模块不做金额换算（单位由调用方约定，与 leases 同风格）。
- EXHAUSTED（额度耗尽）是否断流属计费策略，本实现**不含**——cutoff 只认
  EXPIRED / REVOKED / 惰性到期，EXHAUSTED [待 owner 裁]。
"""
from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import UsageSchemaError, UsageStateError

__all__ = [
    "KIND_LLM_RELAY", "KIND_COMPUTE_SECONDS", "KIND_STORAGE_BYTES",
    "KIND_SANDBOX_SECONDS", "USAGE_KINDS",
    "BINDING_ACTIVE", "BINDING_CUTOFF", "BINDING_REVOKED", "BINDING_STATES",
    "CUTOFF_DRY_RUN", "CUTOFF_ENFORCE", "CUTOFF_MODES",
    "REASON_LEASE_EXPIRED", "REASON_LEASE_REVOKED", "REASON_LEASE_LAPSED",
    "UsageEvent", "UsageLedger", "UsageSummary",
    "LeaseConsumerBinding", "BindingLedger",
    "CutoffAction", "cutoff_due", "cutoff_plan",
]

# ── 计量四维度（v2.0 §4.4 写死；新增维度 = 改这里 + DDL CHECK，不走旁路）──────
KIND_LLM_RELAY = "llm_relay"
KIND_COMPUTE_SECONDS = "compute_seconds"
KIND_STORAGE_BYTES = "storage_bytes"
KIND_SANDBOX_SECONDS = "sandbox_seconds"
USAGE_KINDS = (KIND_LLM_RELAY, KIND_COMPUTE_SECONDS, KIND_STORAGE_BYTES,
               KIND_SANDBOX_SECONDS)

# ── 绑定状态（active → cutoff 网关已断流 / revoked 绑定撤销；均终态不可逆）────
BINDING_ACTIVE = "active"
BINDING_CUTOFF = "cutoff"
BINDING_REVOKED = "revoked"
BINDING_STATES = (BINDING_ACTIVE, BINDING_CUTOFF, BINDING_REVOKED)

# 断流模式：dry-run 只出计划不碰网关（默认）；enforce 真实禁用 consumer
# ——脚本侧开关，真实断流 [待 owner 批]，DDL CHECK 同款枚举兜底。
CUTOFF_DRY_RUN = "dry-run"
CUTOFF_ENFORCE = "enforce"
CUTOFF_MODES = (CUTOFF_DRY_RUN, CUTOFF_ENFORCE)

# 断流理由码（机器可判定；脚本留痕与 dry-run 报告共用）
REASON_LEASE_EXPIRED = "lease-expired"      # 租约状态已 EXPIRED
REASON_LEASE_REVOKED = "lease-revoked"      # 租约已 REVOKED（级联撤销含）
REASON_LEASE_LAPSED = "lease-lapsed"        # 状态仍 ACTIVE 但 expires_at 已过（惰性到期）


def _utcnow() -> float:
    return time.time()


# ── 计量事件（append-only）────────────────────────────────────────────────────

@dataclass(frozen=True)
class UsageEvent:
    """一条计量事件（append-only；对应 DDL glue.usage_event）。

    ``consumer_key``：Higress consumer **名称引用**（租户维度）；llm_relay 维度
    必填（§4.4"按租户 consumer key"），其余维度可选。``lease_ref``/``node_ref``/
    ``task_ref`` 一律引用，不复制状态（4.9 #10）。
    """

    event_id: str
    kind: str                       # USAGE_KINDS 四选一
    quantity: float                 # >= 0 且有限（负数/NaN/inf 一律拒绝）
    consumer_key: Optional[str] = None
    lease_ref: Optional[str] = None
    node_ref: Optional[str] = None
    task_ref: Optional[str] = None
    tenant_id: str = "t0"
    occurred_at: float = 0.0
    meta: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in USAGE_KINDS:
            raise UsageSchemaError(
                f"usage kind must be one of {USAGE_KINDS}, got {self.kind!r}")
        if isinstance(self.quantity, bool) or \
                not isinstance(self.quantity, (int, float)) or \
                not math.isfinite(float(self.quantity)) or float(self.quantity) < 0:
            raise UsageSchemaError(
                f"quantity must be a finite number >= 0, got {self.quantity!r}")
        if self.kind == KIND_LLM_RELAY and \
                (not self.consumer_key or not isinstance(self.consumer_key, str)):
            raise UsageSchemaError(
                "llm_relay events must carry the tenant consumer key "
                "(v2.0 §4.4: LLM relay is metered per consumer key)")
        if self.consumer_key is not None and \
                (not self.consumer_key or not isinstance(self.consumer_key, str)):
            raise UsageSchemaError("consumer_key must be a non-empty string when given")
        if self.occurred_at < 0:
            raise UsageSchemaError("occurred_at must be a non-negative epoch")


@dataclass(frozen=True)
class UsageSummary:
    """单维度聚合（TUI usage 面板的行模型；pg 模式读 glue.v_usage 同构）。"""

    kind: str
    events: int
    total: float


class UsageLedger:
    """进程内计量台账（内存权威）。

    append-only 纪律：只有 ``record*`` 追加入口与只读查询/聚合——本类**不提供**
    任何 update/delete 方法；DDL 触发器（004）是落库后的第二道闸。
    """

    def __init__(self, *, now: Optional[Callable[[], float]] = None) -> None:
        self._now = now or _utcnow
        self._events: List[UsageEvent] = []

    # ── 追加（唯一写路径）────────────────────────────────────────────────
    def record(self, event: UsageEvent) -> UsageEvent:
        if not isinstance(event, UsageEvent):
            raise UsageSchemaError("record expects a UsageEvent")
        if event.occurred_at == 0.0:
            event = replace_occurred_at(event, self._now())
        self._events.append(event)
        return event

    def record_llm_relay(self, quantity: float, consumer_key: str, *,
                         lease_ref: Optional[str] = None,
                         task_ref: Optional[str] = None,
                         tenant_id: str = "t0",
                         meta: Optional[Mapping[str, object]] = None) -> UsageEvent:
        """LLM 中继调用计量（按租户 consumer key 维度，§4.4）。"""
        return self.record(UsageEvent(
            event_id=uuid.uuid4().hex, kind=KIND_LLM_RELAY, quantity=quantity,
            consumer_key=consumer_key, lease_ref=lease_ref, task_ref=task_ref,
            tenant_id=tenant_id, meta=meta or {}))

    def record_compute_seconds(self, quantity: float, *,
                               node_ref: Optional[str] = None,
                               lease_ref: Optional[str] = None,
                               tenant_id: str = "t0",
                               meta: Optional[Mapping[str, object]] = None) -> UsageEvent:
        return self.record(UsageEvent(
            event_id=uuid.uuid4().hex, kind=KIND_COMPUTE_SECONDS, quantity=quantity,
            node_ref=node_ref, lease_ref=lease_ref, tenant_id=tenant_id,
            meta=meta or {}))

    def record_storage_bytes(self, quantity: float, *,
                             lease_ref: Optional[str] = None,
                             tenant_id: str = "t0",
                             meta: Optional[Mapping[str, object]] = None) -> UsageEvent:
        return self.record(UsageEvent(
            event_id=uuid.uuid4().hex, kind=KIND_STORAGE_BYTES, quantity=quantity,
            lease_ref=lease_ref, tenant_id=tenant_id, meta=meta or {}))

    def record_sandbox_seconds(self, quantity: float, *,
                               lease_ref: Optional[str] = None,
                               tenant_id: str = "t0",
                               meta: Optional[Mapping[str, object]] = None) -> UsageEvent:
        return self.record(UsageEvent(
            event_id=uuid.uuid4().hex, kind=KIND_SANDBOX_SECONDS, quantity=quantity,
            lease_ref=lease_ref, tenant_id=tenant_id, meta=meta or {}))

    # ── 只读查询 / 聚合 ──────────────────────────────────────────────────
    def events(self, *, tenant_id: Optional[str] = None, kind: Optional[str] = None,
               consumer_key: Optional[str] = None,
               lease_ref: Optional[str] = None) -> Tuple[UsageEvent, ...]:
        """过滤回放（均只读；返回元组防调用方改内部状态）。"""
        out = []
        for e in self._events:
            if tenant_id is not None and e.tenant_id != tenant_id:
                continue
            if kind is not None and e.kind != kind:
                continue
            if consumer_key is not None and e.consumer_key != consumer_key:
                continue
            if lease_ref is not None and e.lease_ref != lease_ref:
                continue
            out.append(e)
        return tuple(out)

    def summarize(self, *, tenant_id: str = "t0",
                  kind: Optional[str] = None) -> Dict[str, UsageSummary]:
        """按维度聚合（事件数 + 总量）；TUI usage 面板与 pg glue.v_usage 同构。"""
        totals: Dict[str, List[float]] = {}
        for e in self.events(tenant_id=tenant_id, kind=kind):
            acc = totals.setdefault(e.kind, [0, 0.0])
            acc[0] += 1
            acc[1] += float(e.quantity)
        return {k: UsageSummary(kind=k, events=int(v[0]), total=v[1])
                for k, v in sorted(totals.items())}

    def __len__(self) -> int:
        return len(self._events)


def replace_occurred_at(event: UsageEvent, at: float) -> UsageEvent:
    """签发辅助：补 occurred_at（append-only 语义不变——构造期定稿，无更新路径）。"""
    return UsageEvent(
        event_id=event.event_id, kind=event.kind, quantity=event.quantity,
        consumer_key=event.consumer_key, lease_ref=event.lease_ref,
        node_ref=event.node_ref, task_ref=event.task_ref,
        tenant_id=event.tenant_id, occurred_at=at, meta=event.meta)


# ── 租约-Higress consumer 绑定（租约过期即网关断流，§4.4 写死）────────────────

@dataclass(frozen=True)
class LeaseConsumerBinding:
    """绑定声明：一笔 Budget Lease ↔ 一个 Higress consumer key。

    语义：租约活着 = 网关放行该 consumer 的流量并按其计量；租约
    EXPIRED / REVOKED / 惰性到期 → consumer 必须被网关禁用（断流）。
    ``cutoff_mode`` 声明执行方式：dry-run（默认，只出计划）/ enforce（真实禁用，
    [待 owner 批]）。状态机：active → cutoff | revoked（终态不可逆，与
    leases 终态同风格；DDL 触发器第二道闸兜底）。
    """

    binding_id: str
    lease_ref: str                  # glue Budget Lease 引用（UUID 字符串）
    consumer_key: str               # Higress consumer 名称引用（非密钥明文）
    tenant_id: str = "t0"
    state: str = BINDING_ACTIVE
    cutoff_mode: str = CUTOFF_DRY_RUN
    created_at: float = 0.0
    cutoff_at: Optional[float] = None   # 网关断流执行/计划时间（脚本回填）
    revoked_at: Optional[float] = None
    revoke_reason: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("binding_id", "lease_ref", "consumer_key"):
            v = getattr(self, name)
            if not v or not isinstance(v, str):
                raise UsageSchemaError(f"{name} must be a non-empty string, got {v!r}")
        if self.state not in BINDING_STATES:
            raise UsageSchemaError(
                f"binding state must be one of {BINDING_STATES}, got {self.state!r}")
        if self.cutoff_mode not in CUTOFF_MODES:
            raise UsageSchemaError(
                f"cutoff_mode must be one of {CUTOFF_MODES}, got {self.cutoff_mode!r}")
        if self.state == BINDING_CUTOFF and self.cutoff_at is None:
            raise UsageSchemaError("cutoff binding requires cutoff_at")


class BindingLedger:
    """绑定台账（内存权威）：bind / revoke / mark_cutoff + 只读查询。

    无网络语义——Higress API 调用归 srv-1 脚本；本类只管声明与状态。
    """

    def __init__(self, *, now: Optional[Callable[[], float]] = None) -> None:
        self._now = now or _utcnow
        self._bindings: Dict[str, LeaseConsumerBinding] = {}
        self.audit: List[Dict[str, object]] = []

    def bind(self, lease_ref: str, consumer_key: str, *,
             binding_id: Optional[str] = None,
             cutoff_mode: str = CUTOFF_DRY_RUN,
             tenant_id: str = "t0") -> LeaseConsumerBinding:
        """建立绑定；(lease_ref, consumer_key) 重复 → 拒绝（一份声明管一条线，
        拒绝同样留痕——与 leases 的 GRANT_REJECTED 同纪律）。"""
        if any(b.lease_ref == lease_ref and b.consumer_key == consumer_key
               and b.state == BINDING_ACTIVE for b in self._bindings.values()):
            self.audit.append({"event": "BIND_REJECTED", "at": self._now(),
                               "lease_ref": lease_ref, "consumer_key": consumer_key,
                               "reason": "duplicate-active-binding"})
            raise UsageStateError(
                f"active binding for lease {lease_ref} x consumer "
                f"{consumer_key!r} already exists")
        binding = LeaseConsumerBinding(
            binding_id=binding_id or uuid.uuid4().hex,
            lease_ref=lease_ref, consumer_key=consumer_key,
            tenant_id=tenant_id, state=BINDING_ACTIVE, cutoff_mode=cutoff_mode,
            created_at=self._now())
        self._bindings[binding.binding_id] = binding
        self.audit.append({"event": "BIND", "at": binding.created_at,
                           "binding_id": binding.binding_id, "lease_ref": lease_ref,
                           "consumer_key": consumer_key, "cutoff_mode": cutoff_mode})
        return binding

    def get(self, binding_id: str) -> LeaseConsumerBinding:
        try:
            return self._bindings[binding_id]
        except KeyError:
            raise UsageStateError(f"unknown binding: {binding_id}") from None

    def mark_cutoff(self, binding_id: str, *, at: Optional[float] = None) -> LeaseConsumerBinding:
        """网关断流回填（脚本执行后调用；dry-run 计划不走这里）。"""
        cur = self.get(binding_id)
        if cur.state != BINDING_ACTIVE:
            raise UsageStateError(
                f"binding {binding_id} is {cur.state}; only ACTIVE can be marked cutoff")
        new = LeaseConsumerBinding(
            binding_id=cur.binding_id, lease_ref=cur.lease_ref,
            consumer_key=cur.consumer_key, tenant_id=cur.tenant_id,
            state=BINDING_CUTOFF, cutoff_mode=cur.cutoff_mode,
            created_at=cur.created_at, cutoff_at=self._now() if at is None else at)
        self._bindings[binding_id] = new
        self.audit.append({"event": "CUTOFF", "at": new.cutoff_at,
                           "binding_id": binding_id, "consumer_key": cur.consumer_key})
        return new

    def revoke(self, binding_id: str, *, reason: str = "") -> LeaseConsumerBinding:
        cur = self.get(binding_id)
        if cur.state != BINDING_ACTIVE:
            raise UsageStateError(
                f"binding {binding_id} is {cur.state}; only ACTIVE can be revoked")
        new = LeaseConsumerBinding(
            binding_id=cur.binding_id, lease_ref=cur.lease_ref,
            consumer_key=cur.consumer_key, tenant_id=cur.tenant_id,
            state=BINDING_REVOKED, cutoff_mode=cur.cutoff_mode,
            created_at=cur.created_at, revoked_at=self._now(),
            revoke_reason=reason or None)
        self._bindings[binding_id] = new
        self.audit.append({"event": "REVOKE", "at": new.revoked_at,
                           "binding_id": binding_id, "reason": reason})
        return new

    def bindings(self, *, state: Optional[str] = None,
                 tenant_id: Optional[str] = None) -> Tuple[LeaseConsumerBinding, ...]:
        out = []
        for b in self._bindings.values():
            if state is not None and b.state != state:
                continue
            if tenant_id is not None and b.tenant_id != tenant_id:
                continue
            out.append(b)
        return tuple(out)


# ── 断流求值（纯函数；srv-1 脚本的"脑"，在本包可测）──────────────────────────

@dataclass(frozen=True)
class CutoffAction:
    """一条断流计划项：脚本按它调 Higress 控制台 API（或 dry-run 只打印）。"""

    binding_id: str
    consumer_key: str
    lease_ref: str
    reason: str                     # REASON_LEASE_* 之一
    tenant_id: str = "t0"
    dry_run: bool = True            # 真实断流 [待 owner 批]：默认恒为 True


def cutoff_due(binding: LeaseConsumerBinding,
               lease_status: str,
               lease_expires_at: Optional[float], *,
               now: float) -> Tuple[bool, str]:
    """单条绑定的断流判定：返回 (是否到期, 理由码)。

    到期 = 租约 EXPIRED / REVOKED，或状态仍 ACTIVE 但 expires_at 已过
    （惰性到期，与 leases 同风格）。EXHAUSTED 不在内（计费策略 [待 owner 裁]）。
    绑定非 ACTIVE 一律不到期（终态已处理过）。
    """
    if binding.state != BINDING_ACTIVE:
        return False, ""
    if lease_status == "EXPIRED":
        return True, REASON_LEASE_EXPIRED
    if lease_status == "REVOKED":
        return True, REASON_LEASE_REVOKED
    if lease_status == "ACTIVE" and lease_expires_at is not None \
            and now >= lease_expires_at:
        return True, REASON_LEASE_LAPSED
    return False, ""


def cutoff_plan(bindings: Sequence[LeaseConsumerBinding],
                lease_view: Mapping[str, Tuple[str, Optional[float]]], *,
                now: float, enforce: bool = False) -> List[CutoffAction]:
    """算断流计划：读租约视图（lease_ref → (status, expires_at)），产出计划项。

    ``enforce=False``（默认）恒产 dry-run 计划——**本工单只交骨架，真实断流
    [待 owner 批]**；enforce=True 也只是把 dry_run 标志翻开，真正的网关调用
    在 srv-1 脚本里（本包零网络）。缺租约声明的绑定直接跳过，由脚本侧对账
    报缺——本层不做无依据猜测（fail-closed 的如实边界）。
    """
    actions: List[CutoffAction] = []
    for b in bindings:
        view = lease_view.get(b.lease_ref)
        if view is None:
            continue                  # 无租约数据 → 脚本侧对账报缺，不在本层猜测
        due, reason = cutoff_due(b, view[0], view[1], now=now)
        if due:
            actions.append(CutoffAction(
                binding_id=b.binding_id, consumer_key=b.consumer_key,
                lease_ref=b.lease_ref, reason=reason, tenant_id=b.tenant_id,
                dry_run=not enforce))
    return sorted(actions, key=lambda a: (a.consumer_key, a.binding_id))
