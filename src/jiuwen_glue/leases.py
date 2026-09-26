# coding: utf-8
"""Budget Lease（预算租约）— jiuwen-glue 自研对象之一.

规格来源（PROP-0001 v1.6 第 4.1 节；Handbook ch30:110,120）:
- 预算租约"随子任务派生、级联撤销"，是"最容易被忽略、但对可控自治最关键"的对象；
- L3 自治等级要求"预算租约 + 工作目录限定 + 完整调用轨迹与预算消耗"。

最小语义（本实现）:
- **发放 grant**：发一笔 ACTIVE 租约；可指定 ttl（expires_at）；可从父租约派生
  （派生额从父剩余额中划出，父 remaining 相应扣减——"随子任务派生"）。
- **占用 acquire**：按 cost 扣减 remaining。租约必须 ACTIVE 且未过期；
  超额占用被拒绝（BudgetExceededError）；耗尽 → EXHAUSTED。
- **过期 expire**：到达 expires_at 的租约在触碰时惰性转 EXPIRED；也可显式 expire。
  过期只作用于本租约（子租约额度在派生时已从父划出、已承诺，不随父过期回收）。
- **撤销 revoke**：级联——撤销父租约时所有后代租约一并 REVOKED（"级联撤销"），
  未花完的额度随之作废；之后任何占用被拒（LeaseRevokedError）。
- 所有事件（含被拒绝的占用）追加进 audit 日志：检测 = 拒绝 + 留痕。
- **权限交集联动（v1.7 §12.5，WO-0003 返工）**：签发时可传入
  identity.effective_permissions 的求值结果并**固化进租约**（perms 快照 + 三层身份
  引用）——权限交集公式在租约签发路径上被求值，不是口头原则；派生时强制
  **逐级收敛不变式**：子租约权限快照 ⊆ 父租约快照，越界派生被拒绝。

额度为抽象整数单位（例如 1/1000 元或 1K token），由调用方约定；本层不做换算。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional

from .errors import (
    BudgetExceededError,
    LeaseDerivationError,
    LeaseExhaustedError,
    LeaseExpiredError,
    LeaseRevokedError,
    UnknownLeaseError,
)
from .identity import EffectivePerms

ACTIVE = "ACTIVE"
EXHAUSTED = "EXHAUSTED"
EXPIRED = "EXPIRED"
REVOKED = "REVOKED"
_TERMINAL = {EXHAUSTED, EXPIRED, REVOKED}


def _utcnow() -> float:
    import time

    return time.time()


@dataclass(frozen=True)
class LeaseEvent:
    lease_id: str
    event: str            # GRANT / ACQUIRE / EXPIRE / REVOKE / ACQUIRE_REJECTED / GRANT_REJECTED
    occurred_at: float
    detail: Dict[str, object] = field(default_factory=dict)
    tenant_id: str = "t0"


@dataclass
class BudgetLease:
    lease_id: str
    task_ref: str                 # 服务的 Task/Subtask 引用（跨层只传引用，不复制状态）
    amount: int                   # 总额度（>=0）
    remaining: int
    status: str = ACTIVE
    parent_lease_id: Optional[str] = None
    granted_at: float = 0.0
    expires_at: Optional[float] = None
    revoked_at: Optional[float] = None
    revoke_reason: Optional[str] = None
    tenant_id: str = "t0"
    # ── 签发时固化的身份与权限快照（v1.7 §12.5）───────────────────────────
    agent_ref: Optional[str] = None                  # 三层复合身份引用（identity.composite_ref）
    effective_perms: tuple = ()                      # 权限交集快照（签发时求值并冻结）
    perms_provenance: Dict[str, tuple] = field(default_factory=dict)  # 每一分量出处

    def is_expired_at(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at


class BudgetLedger:
    """进程内预算租约台账（Postgres DDL 见 sql/001_glue_objects.sql 的 glue.budget_lease）。"""

    def __init__(self, now: Optional[Callable[[], float]] = None) -> None:
        self._now = now or _utcnow
        self._leases: Dict[str, BudgetLease] = {}
        self.audit: List[LeaseEvent] = []

    # ── 查询 ─────────────────────────────────────────────────────────────

    def get(self, lease_id: str) -> BudgetLease:
        try:
            return self._leases[lease_id]
        except KeyError:
            raise UnknownLeaseError(f"unknown lease: {lease_id}") from None

    def children_of(self, lease_id: str) -> List[BudgetLease]:
        return [l for l in self._leases.values() if l.parent_lease_id == lease_id]

    def _log(self, lease_id: str, event: str, **detail: object) -> None:
        lease = self._leases.get(lease_id)
        tenant = lease.tenant_id if lease is not None else "t0"
        self.audit.append(LeaseEvent(lease_id=lease_id, event=event,
                                     occurred_at=self._now(), detail=dict(detail),
                                     tenant_id=tenant))

    # ── 发放 ─────────────────────────────────────────────────────────────

    def grant(
        self,
        task_ref: str,
        amount: int,
        *,
        parent_lease_id: Optional[str] = None,
        ttl_seconds: Optional[float] = None,
        tenant_id: str = "t0",
        agent_ref: Optional[str] = None,
        effective_perms: Optional[EffectivePerms] = None,
    ) -> BudgetLease:
        """发放租约。parent_lease_id 给出时为"随子任务派生"：额度从父剩余中划出。

        权限交集联动（v1.7 §12.5）：
        - ``effective_perms`` 给出时（identity.effective_permissions 的求值结果），
          其交集快照与分量出处**固化进本租约**——签发即求值，不是口头原则；
        - 派生场景强制**逐级收敛不变式**：子租约快照 ⊆ 父租约快照——
          未给 effective_perms 时继承父快照（⊆ 由构造保证）；显式给出但越界
          （子 ⊄ 父）→ LeaseDerivationError + GRANT_REJECTED 留痕。
        """
        if not isinstance(amount, int) or amount < 0:
            self._log("-", "GRANT_REJECTED", reason="amount must be a non-negative int", amount=amount)
            raise LeaseDerivationError("amount must be a non-negative int")
        now = self._now()
        parent: Optional[BudgetLease] = None
        if parent_lease_id is not None:
            parent = self._refresh(self.get(parent_lease_id), now)
            if parent.status != ACTIVE:
                self._log(parent.lease_id, "GRANT_REJECTED",
                          reason=f"parent not ACTIVE (status={parent.status})")
                raise LeaseDerivationError(
                    f"parent lease {parent.lease_id} is {parent.status}, cannot derive")
            if amount > parent.remaining:
                self._log(parent.lease_id, "GRANT_REJECTED",
                          reason="derivation exceeds parent remaining",
                          requested=amount, parent_remaining=parent.remaining)
                raise LeaseDerivationError(
                    f"derivation {amount} exceeds parent remaining {parent.remaining}")
            parent.remaining -= amount
            self._log(parent.lease_id, "ACQUIRE",
                      reason="carved out for child lease", carved=amount,
                      remaining=parent.remaining)

        # 权限快照固化 + 逐级收敛不变式（子 ⊆ 父）
        frozen: tuple = ()
        provenance: Dict[str, tuple] = {}
        if effective_perms is not None:
            frozen = effective_perms.frozen()
            provenance = {src: tuple(sorted(ps)) for src, ps
                          in (effective_perms.components or {}).items()}
        if parent is not None and parent.effective_perms:
            child_set = set(frozen)
            if effective_perms is not None and not child_set <= set(parent.effective_perms):
                beyond = sorted(child_set - set(parent.effective_perms))
                self._log(parent.lease_id, "GRANT_REJECTED",
                          reason="derived lease perms exceed upstream "
                                 "(permissions only converge, never expand)",
                          beyond_scope=beyond)
                raise LeaseDerivationError(
                    f"derived lease perms exceed parent {parent.lease_id}: "
                    f"out-of-scope {beyond}")
            if effective_perms is None:
                frozen = tuple(parent.effective_perms)      # 继承父快照（⊆ 保证）
                provenance = dict(parent.perms_provenance)

        lease = BudgetLease(
            lease_id=uuid.uuid4().hex,
            task_ref=task_ref,
            amount=amount,
            remaining=amount,
            parent_lease_id=parent_lease_id,
            granted_at=now,
            expires_at=(now + ttl_seconds) if ttl_seconds is not None else None,
            tenant_id=tenant_id or "t0",
            agent_ref=agent_ref,
            effective_perms=frozen,
            perms_provenance=provenance,
        )
        self._leases[lease.lease_id] = lease
        self._log(lease.lease_id, "GRANT", task_ref=task_ref, amount=amount,
                  parent_lease_id=parent_lease_id, expires_at=lease.expires_at,
                  agent_ref=agent_ref, perms=len(frozen))
        return lease

    # ── 过期 ─────────────────────────────────────────────────────────────

    def _refresh(self, lease: BudgetLease, now: float) -> BudgetLease:
        """惰性过期：触碰时若已过 expires_at 则落为 EXPIRED。"""
        if lease.status == ACTIVE and lease.is_expired_at(now):
            lease.status = EXPIRED
            self._log(lease.lease_id, "EXPIRE", at=lease.expires_at)
        return lease

    def expire(self, lease_id: str) -> BudgetLease:
        """显式过期（幂等：已终态的租约保持原状态）。"""
        lease = self.get(lease_id)
        now = self._now()
        if lease.status == ACTIVE:
            lease.status = EXPIRED
            self._log(lease_id, "EXPIRE", explicit=True)
        else:
            lease = self._refresh(lease, now)
        return lease

    # ── 占用 ─────────────────────────────────────────────────────────────

    def acquire(self, lease_id: str, cost: int, *, purpose: str = "") -> int:
        """占用预算：返回扣减后的剩余额度。违规占用被拒绝并留痕。"""
        if not isinstance(cost, int) or cost < 0:
            raise BudgetExceededError("cost must be a non-negative int")
        lease = self._refresh(self.get(lease_id), self._now())
        if lease.status == EXPIRED:
            self._log(lease_id, "ACQUIRE_REJECTED", reason="expired", cost=cost)
            raise LeaseExpiredError(f"lease {lease_id} is expired")
        if lease.status == REVOKED:
            self._log(lease_id, "ACQUIRE_REJECTED", reason="revoked", cost=cost)
            raise LeaseRevokedError(f"lease {lease_id} was revoked (cascade)")
        if lease.status == EXHAUSTED:
            self._log(lease_id, "ACQUIRE_REJECTED", reason="exhausted", cost=cost)
            raise LeaseExhaustedError(f"lease {lease_id} is exhausted")
        if cost > lease.remaining:
            # 超额占用：拒绝，租约保持 ACTIVE（额度未动）。
            self._log(lease_id, "ACQUIRE_REJECTED", reason="overdraft",
                      cost=cost, remaining=lease.remaining, purpose=purpose)
            raise BudgetExceededError(
                f"overdraft on lease {lease_id}: requested {cost} > remaining {lease.remaining}")
        lease.remaining -= cost
        if lease.remaining == 0:
            lease.status = EXHAUSTED
            self._log(lease_id, "ACQUIRE", cost=cost, remaining=0,
                      status_became=EXHAUSTED, purpose=purpose)
        else:
            self._log(lease_id, "ACQUIRE", cost=cost, remaining=lease.remaining, purpose=purpose)
        return lease.remaining

    # ── 撤销（级联） ──────────────────────────────────────────────────────

    def revoke(self, lease_id: str, *, reason: str = "") -> List[str]:
        """撤销租约并级联撤销全部后代租约；返回被撤销的 lease_id 列表。"""
        self.get(lease_id)  # UnknownLeaseError if absent
        revoked: List[str] = []
        stack = [lease_id]
        while stack:
            cur = stack.pop()
            lease = self._leases[cur]
            if lease.status in _TERMINAL:
                continue
            lease.status = REVOKED
            lease.revoked_at = self._now()
            lease.revoke_reason = reason or None
            revoked.append(cur)
            self._log(cur, "REVOKE", reason=reason, cascade=(cur != lease_id))
            stack.extend(l.lease_id for l in self.children_of(cur))
        return revoked

    # ── 状态 ─────────────────────────────────────────────────────────────

    def status_of(self, lease_id: str) -> str:
        """查询状态（含惰性过期判定）。"""
        lease = self.get(lease_id)
        return self._refresh(lease, self._now()).status
