# coding: utf-8
"""Budget Lease（发放/占用/过期/级联撤销）语义测试。"""
from __future__ import annotations

import pytest

from jiuwen_glue import (
    ACTIVE,
    BudgetExceededError,
    BudgetLedger,
    EXHAUSTED,
    EXPIRED,
    LeaseDerivationError,
    LeaseExhaustedError,
    LeaseExpiredError,
    LeaseRevokedError,
    REVOKED,
)


def test_grant_creates_active_lease_with_full_remaining(clock):
    led = BudgetLedger(now=clock)
    lease = led.grant("task-1", 100)
    assert lease.status == "ACTIVE"
    assert lease.amount == lease.remaining == 100
    assert lease.expires_at is None


def test_acquire_spends_and_exhausts(clock):
    led = BudgetLedger(now=clock)
    lease = led.grant("task-1", 100)
    assert led.acquire(lease.lease_id, 30) == 70
    assert led.acquire(lease.lease_id, 70) == 0
    assert led.status_of(lease.lease_id) == EXHAUSTED
    # 耗尽后再占用被拒（精确异常：LeaseExhaustedError）
    with pytest.raises(LeaseExhaustedError):
        led.acquire(lease.lease_id, 1)
    assert led.audit[-1].event == "ACQUIRE_REJECTED"
    assert led.audit[-1].detail["reason"] == "exhausted"


def test_overdraft_rejected_and_lease_untouched(clock):
    led = BudgetLedger(now=clock)
    lease = led.grant("task-1", 100)
    with pytest.raises(BudgetExceededError):
        led.acquire(lease.lease_id, 101)
    assert led.status_of(lease.lease_id) == "ACTIVE"
    assert led.get(lease.lease_id).remaining == 100
    assert led.audit[-1].detail["reason"] == "overdraft"


def test_lazy_expiry_blocks_acquire(clock):
    led = BudgetLedger(now=clock)
    lease = led.grant("task-1", 100, ttl_seconds=60)
    clock.advance(61)  # 越过过期线
    assert led.status_of(lease.lease_id) == EXPIRED
    with pytest.raises(LeaseExpiredError):
        led.acquire(lease.lease_id, 1)
    assert led.audit[-1].event == "ACQUIRE_REJECTED"
    assert led.audit[-1].detail["reason"] == "expired"


def test_explicit_expire_before_ttl(clock):
    led = BudgetLedger(now=clock)
    lease = led.grant("task-1", 50, ttl_seconds=600)
    clock.advance(10)
    led.expire(lease.lease_id)
    assert led.status_of(lease.lease_id) == EXPIRED


def test_derivation_carves_out_parent_budget(clock):
    led = BudgetLedger(now=clock)
    parent = led.grant("task-root", 100)
    child = led.grant("task-child", 40, parent_lease_id=parent.lease_id)
    assert child.parent_lease_id == parent.lease_id
    assert child.amount == 40
    assert parent.remaining == 60  # 派生即从父划出


def test_derivation_over_parent_remaining_rejected(clock):
    led = BudgetLedger(now=clock)
    parent = led.grant("task-root", 100)
    with pytest.raises(LeaseDerivationError):
        led.grant("task-child", 101, parent_lease_id=parent.lease_id)
    assert led.get(parent.lease_id).remaining == 100  # 未被划走
    assert led.audit[-1].event == "GRANT_REJECTED"


def test_cascade_revoke_kills_descendants(clock):
    led = BudgetLedger(now=clock)
    root = led.grant("task-root", 300)
    mid = led.grant("task-mid", 200, parent_lease_id=root.lease_id)
    leaf = led.grant("task-leaf", 50, parent_lease_id=mid.lease_id)

    revoked = led.revoke(root.lease_id, reason="owner cancelled the run")
    assert set(revoked) == {root.lease_id, mid.lease_id, leaf.lease_id}
    for lid in revoked:
        assert led.status_of(lid) == REVOKED
    for lid in (root.lease_id, mid.lease_id, leaf.lease_id):
        with pytest.raises(LeaseRevokedError):
            led.acquire(lid, 1)
    # 级联撤销留痕
    revoke_events = [e for e in led.audit if e.event == "REVOKE"]
    assert len(revoke_events) == 3


# ── v2.0 §3.3（W-01 缺陷 #1 修复）：级联遍历独立于本节点状态 ──────────────────

def test_revoke_cascades_from_expired_parent(clock):
    """回归（v2.0 §3.3）：EXPIRED 父 + ACTIVE 子 → revoke 后子必为 REVOKED。

    此前实现对终态父租约直接 continue 跳过级联，ACTIVE 子租约成孤儿仍可扣费。
    """
    led = BudgetLedger(now=clock)
    parent = led.grant("task-root", 100, ttl_seconds=60)
    child = led.grant("task-child", 40, parent_lease_id=parent.lease_id)  # 子无 TTL
    clock.advance(61)                                    # 父越过过期线
    assert led.status_of(parent.lease_id) == EXPIRED     # 父惰性过期
    assert led.status_of(child.lease_id) == ACTIVE       # 子仍 ACTIVE（过期只作用于本租约）
    revoked = led.revoke(parent.lease_id, reason="owner cancelled the run")
    assert child.lease_id in revoked
    assert led.status_of(child.lease_id) == REVOKED      # 子必为 REVOKED，不再是孤儿
    with pytest.raises(LeaseRevokedError):
        led.acquire(child.lease_id, 1)                   # 孤儿扣费通道被关闭


def test_revoke_cascades_when_parent_lazily_expires_at_revoke(clock):
    """父租约惰性过期在撤销调用内同步生效并级联（v2.0 §3.3）：
    父已越过 expires_at 但尚未被触碰（台账仍记 ACTIVE）时，revoke 仍级联到子。"""
    led = BudgetLedger(now=clock)
    parent = led.grant("task-root", 100, ttl_seconds=60)
    child = led.grant("task-child", 40, parent_lease_id=parent.lease_id)
    clock.advance(61)                                    # 未触碰，父在台账仍 ACTIVE
    revoked = led.revoke(parent.lease_id)
    assert led.status_of(child.lease_id) == REVOKED
    assert led.status_of(parent.lease_id) in (EXPIRED, REVOKED)


def test_revoke_cascades_from_exhausted_parent(clock):
    """终态不止 EXPIRED：EXHAUSTED 父下的 ACTIVE 子同样必须被级联撤销。"""
    led = BudgetLedger(now=clock)
    parent = led.grant("task-root", 100)
    child = led.grant("task-child", 40, parent_lease_id=parent.lease_id)
    led.acquire(parent.lease_id, 60)                     # 父耗尽（剩余 60 已被派生划出）→ EXHAUSTED
    assert led.status_of(parent.lease_id) == EXHAUSTED
    assert led.status_of(child.lease_id) == ACTIVE
    led.revoke(parent.lease_id, reason="kill tree")
    assert led.status_of(child.lease_id) == REVOKED


def test_revoke_cascades_deep_under_expired_mid(clock):
    """EXPIRED 只在中间层也不阻断：root → EXPIRED mid → ACTIVE leaf，
    revoke(root) 时 leaf 必须被触达并 REVOKED。"""
    led = BudgetLedger(now=clock)
    root = led.grant("task-root", 300)
    mid = led.grant("task-mid", 200, parent_lease_id=root.lease_id, ttl_seconds=60)
    leaf = led.grant("task-leaf", 50, parent_lease_id=mid.lease_id)
    clock.advance(61)                                    # 仅 mid 过期
    assert led.status_of(mid.lease_id) == EXPIRED
    assert led.status_of(leaf.lease_id) == ACTIVE
    led.revoke(root.lease_id)
    assert led.status_of(root.lease_id) == REVOKED
    assert led.status_of(leaf.lease_id) == REVOKED
