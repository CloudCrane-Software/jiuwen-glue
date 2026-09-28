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


# ── W-02 复核修复：空快照也是快照（逐级收敛不变式的空集边界）──────────────────

def test_derived_perms_rejected_when_parent_snapshot_evaluated_empty(clock):
    """回归（W-02）：父租约权限快照**求值为空集**（交集=空=什么都不允许）时，
    携带非空快照的派生必须被拒绝——空快照扩张即越界（permissions only converge）。
    此前实现以真值判断 parent.effective_perms 把空快照当"无快照"，越界派生被放行。"""
    from jiuwen_glue import EffectivePerms

    led = BudgetLedger(now=clock)
    parent = led.grant("task-root", 100,
                       effective_perms=EffectivePerms(perms=frozenset()))
    assert parent.effective_perms == () and parent.perms_frozen is True
    with pytest.raises(LeaseDerivationError):
        led.grant("task-child", 10, parent_lease_id=parent.lease_id,
                  effective_perms=EffectivePerms(perms=frozenset({"admin:all"})))
    assert led.children_of(parent.lease_id) == []          # 子未落账
    assert any(e.event == "GRANT_REJECTED" for e in led.audit)  # 拒绝留痕


def test_child_without_perms_inherits_empty_snapshot(clock):
    """父快照求值为空集、子未声明快照 → 子继承空快照（⊆ 由构造保证，fail-closed）。"""
    from jiuwen_glue import EffectivePerms

    led = BudgetLedger(now=clock)
    parent = led.grant("task-root", 100,
                       effective_perms=EffectivePerms(perms=frozenset()))
    child = led.grant("task-child", 10, parent_lease_id=parent.lease_id)
    assert child.effective_perms == () and child.perms_frozen is True


def test_derived_perms_under_never_frozen_parent_declares_own_baseline(clock):
    """如实边界（W-02 固化）：父租约签发时从未传入 effective_perms（无收敛基准），
    子可声明自己的快照并成为其后代的收敛基线——该情形不做收敛校验，
    见 leases 模块 docstring"空集边界"说明。"""
    from jiuwen_glue import EffectivePerms

    led = BudgetLedger(now=clock)
    parent = led.grant("task-root", 100)                   # 未传 effective_perms
    assert parent.perms_frozen is False
    child = led.grant("task-child", 10, parent_lease_id=parent.lease_id,
                      effective_perms=EffectivePerms(perms=frozenset({"deploy:prod"})))
    assert child.effective_perms == ("deploy:prod",)
    grandchild = led.grant("task-gc", 5, parent_lease_id=child.lease_id)
    assert grandchild.effective_perms == ("deploy:prod",)  # 基线向下继承
    with pytest.raises(LeaseDerivationError):              # 基线以下照常收敛
        led.grant("task-beyond", 5, parent_lease_id=child.lease_id,
                  effective_perms=EffectivePerms(perms=frozenset({"deploy:prod", "admin:all"})))


def test_rejected_derivation_leaves_parent_budget_intact(clock):
    """回归（W-02）：越界派生被拒后父租约剩余额**原封不动**——
    此前实现先划扣后校验权限，拒绝路径不回滚，父预算凭空蒸发。"""
    from jiuwen_glue import EffectivePerms

    led = BudgetLedger(now=clock)
    parent = led.grant("task-root", 100,
                       effective_perms=EffectivePerms(perms=frozenset({"read:a"})))
    with pytest.raises(LeaseDerivationError):
        led.grant("task-child", 30, parent_lease_id=parent.lease_id,
                  effective_perms=EffectivePerms(perms=frozenset({"write:b"})))
    assert parent.remaining == 100                      # 零副作用
    assert led.children_of(parent.lease_id) == []
    # 合法派生仍正常划扣
    ok = led.grant("task-ok", 30, parent_lease_id=parent.lease_id,
                   effective_perms=EffectivePerms(perms=frozenset({"read:a"})))
    assert parent.remaining == 70 and ok.remaining == 30


def test_cross_tenant_derivation_rejected(clock):
    """租户边界（D1-R4）：派生必须同租户——显式声明他租 tenant_id 被拒绝，
    父剩余额零副作用（校验先于划扣），GRANT_REJECTED 留痕。"""
    led = BudgetLedger(now=clock)
    parent = led.grant("task-A", 100, tenant_id="tA")
    with pytest.raises(LeaseDerivationError) as ei:
        led.grant("task-B", 50, parent_lease_id=parent.lease_id, tenant_id="tB")
    assert "cross-tenant derivation rejected" in str(ei.value)
    assert parent.remaining == 100                          # 拒绝路径零副作用
    assert led.children_of(parent.lease_id) == []
    assert led.audit[-1].event == "GRANT_REJECTED"
    assert led.audit[-1].detail["reason"].startswith("cross-tenant derivation")
    assert led.audit[-1].detail["parent_tenant"] == "tA"
    assert led.audit[-1].detail["requested_tenant"] == "tB"
    # 同租户派生照常放行
    ok = led.grant("task-C", 50, parent_lease_id=parent.lease_id, tenant_id="tA")
    assert parent.remaining == 50 and ok.remaining == 50


def test_derivation_without_tenant_inherits_parent_tenant(clock):
    """租户边界（D1-R4）：派生未声明 tenant_id → 继承父租约租户
    （"随子任务派生"：子任务与父任务同属一方），而非静默落到默认 t0。"""
    led = BudgetLedger(now=clock)
    parent = led.grant("task-A", 100, tenant_id="tA")
    child = led.grant("task-B", 50, parent_lease_id=parent.lease_id)
    assert child.tenant_id == "tA"
    assert led.audit[-1].tenant_id == "tA"                  # GRANT 事件携带
    # 根发放未声明仍默认 t0（既有语义不变）
    assert led.grant("task-root", 10).tenant_id == "t0"


def test_grant_ttl_must_be_positive_finite(clock):
    """R7 修复轮（终局补漏，#16 推广到 leases 模块）：ttl_seconds 非有限/非正数
    构造期拒绝——NaN 经 ``ttl <= 0`` 恒 False 静默通过 → expires_at=nan →
    is_expired_at（now >= nan 恒 False）永不惰性过期，「租约过期即断流」
    fail-open；±inf 同理永不过期；巨型 int 经 now+ttl 的 float 转换抛未归类
    OverflowError；负 ttl 使租约出生即过期（静默，此前仅 DB 层
    CHECK(expires_at>granted_at) 会拒，进程内参考语义层不拒）。与
    challenge.open 的 ttl_seconds 闸（challenge.py 同款 _finite 口径）对齐。"""
    led = BudgetLedger(now=clock)
    for bad in (float("nan"), float("inf"), float("-inf"), 10**400, 0, -5, "60"):
        with pytest.raises(LeaseDerivationError, match="ttl_seconds"):
            led.grant("task-1", 100, ttl_seconds=bad)
    # 拒绝路径零副作用：无租约被创建，且每次拒绝都有 GRANT_REJECTED 留痕
    assert led._leases == {}
    assert sum(1 for e in led.audit if e.event == "GRANT_REJECTED") == 7


def test_grant_ttl_none_still_means_no_expiry(clock):
    """ttl_seconds=None（无有效期语义）保持既有接受面不变。"""
    led = BudgetLedger(now=clock)
    lease = led.grant("task-1", 100, ttl_seconds=None)
    assert lease.expires_at is None
    assert led.status_of(lease.lease_id) == ACTIVE
