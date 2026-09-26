# coding: utf-8
"""三层复合身份 + 权限交集公式测试（研发手册原则一/二）+ 租约签发联动."""
from __future__ import annotations

import pytest

from jiuwen_glue import (
    AGENT_CAPS,
    DELEGATION,
    DelegationScopeError,
    EffectivePerms,
    IdentitySchemaError,
    LeaseDerivationError,
    PLATFORM_POLICY,
    RUNTIME,
    USER,
    AgentIdentity,
    BudgetLedger,
    Delegation,
    RunInstance,
    TaskContext,
    composite_ref,
    effective_permissions,
    narrow_delegation,
)


def _agent():
    return AgentIdentity(agent_id="dev-core", tenant_id="t0")


def _run():
    return RunInstance(instance_id="i-20260926-01", node="gpumachine-0", ttl_seconds=3600)


def _task():
    return TaskContext(task_id="wo-0003-r1", workspace_uri="file:///ws/jiuwen-glue",
                       pipeline_label="dev", env_ref="env:anolis-23@v7",
                       delegation_ref="dlg-001")


# ── 原则一：三层复合身份 ─────────────────────────────────────────────────────

def test_three_layers_and_composite_ref():
    ref = composite_ref(_agent(), _run(), _task())
    assert ref == "ag:dev-core@t0/run:i-20260926-01/task:wo-0003-r1"
    # 三层缺一不可
    with pytest.raises(IdentitySchemaError):
        composite_ref(None, _run(), _task())
    with pytest.raises(IdentitySchemaError):
        AgentIdentity(agent_id="")
    with pytest.raises(IdentitySchemaError):
        RunInstance(instance_id="")
    with pytest.raises(IdentitySchemaError):
        TaskContext(task_id="")


# ── 原则二：五集交集 ─────────────────────────────────────────────────────────

def test_effective_permissions_is_intersection_with_provenance():
    eps = effective_permissions(
        user={"svc-a:logs:read", "svc-a:restart", "git:push"},
        agent_caps={"svc-a:logs:read", "svc-a:restart"},
        platform_policy={"svc-a:logs:read", "svc-a:restart", "svc-b:logs:read"},
        delegation={"svc-a:logs:read"},
        runtime={"svc-a:logs:read", "net:egress:higress"},
    )
    assert eps.perms == frozenset({"svc-a:logs:read"})
    # 每一分量都有出处引用
    assert set(eps.components) == {USER, AGENT_CAPS, PLATFORM_POLICY, DELEGATION, RUNTIME}
    assert eps.components[USER] == frozenset({"svc-a:logs:read", "svc-a:restart", "git:push"})
    # 用户有权 ≠ Agent 自动有权：git:push 在用户手里但不在交集里
    assert not eps.allows("git:push")
    assert eps.allows("svc-a:logs:read")
    assert eps.frozen() == ("svc-a:logs:read",)


def test_user_permission_alone_is_never_enough():
    """手册案例："合理的过程不是继承用户全部权限"。用户全权，其余四集为空 → 交集为空。"""
    eps = effective_permissions(
        user={"svc-a:logs:read", "svc-a:restart", "prod:deploy"},
        agent_caps=set(), platform_policy=set(),
        delegation=set(), runtime=set())
    assert eps.perms == frozenset()
    assert not eps.allows("svc-a:logs:read")


# ── 原则二：逐级收敛（子委托 ⊆ 上游）────────────────────────────────────────

def test_sub_delegation_must_be_subset_of_upstream():
    parent = narrow_delegation(
        Delegation(delegation_id="dlg-root", subject="ag:orchestrator@t0/run:i0/task:t0",
                   scopes=frozenset({"svc-a:logs:read", "svc-a:restart"})),
        "dlg-child", "ag:research@t0/run:i1/task:t1",
        scopes={"svc-a:logs:read"})
    assert parent.scopes == frozenset({"svc-a:logs:read"})
    assert parent.parent_delegation_id == "dlg-root"
    with pytest.raises(DelegationScopeError):
        narrow_delegation(
            Delegation(delegation_id="dlg-root", subject="s",
                       scopes=frozenset({"svc-a:logs:read"})),
            "dlg-child", "s2", scopes={"svc-a:logs:read", "svc-a:restart"})


# ── 越权尝试 = 目标改服务即拒（手册案例写死）────────────────────────────────

def test_target_service_change_is_privilege_escalation_not_new_phase():
    """模型把操作目标从 svc-a 改成 svc-b：不是新任务阶段，而是越权尝试——拒绝。"""
    eps = effective_permissions(
        user={"svc-a:logs:read", "svc-b:logs:read"},       # 用户对 svc-b 也有权
        agent_caps={"svc-a:logs:read"},
        platform_policy={"svc-a:logs:read", "svc-b:logs:read"},
        delegation={"svc-a:logs:read"},                    # 本次委托只对准 svc-a
        runtime={"svc-a:logs:read"})
    assert eps.allows("svc-a:logs:read")
    assert not eps.allows("svc-b:logs:read")               # 目标改服务即拒


# ── 租约签发联动：交集公式在签发路径上求值并固化 ─────────────────────────────

def test_lease_freezes_effective_perms_at_grant(clock):
    led = BudgetLedger(now=clock)
    eps = effective_permissions(
        user={"svc-a:logs:read", "svc-a:restart"},
        agent_caps={"svc-a:logs:read"},
        platform_policy={"svc-a:logs:read", "svc-a:restart"},
        delegation={"svc-a:logs:read"},
        runtime={"svc-a:logs:read"})
    lease = led.grant("wo-0003-r1", 100, agent_ref=composite_ref(_agent(), _run(), _task()),
                      effective_perms=eps)
    assert lease.effective_perms == ("svc-a:logs:read",)           # 固化进租约
    assert lease.perms_provenance[DELEGATION] == ("svc-a:logs:read",)
    assert lease.perms_provenance[USER] == ("svc-a:logs:read", "svc-a:restart")
    assert lease.agent_ref == "ag:dev-core@t0/run:i-20260926-01/task:wo-0003-r1"
    # 无 effective_perms 时租约字段为空（兼容既有调用）
    plain = led.grant("t2", 10)
    assert plain.effective_perms == () and plain.agent_ref is None


def test_derived_lease_perms_must_be_subset_of_parent(clock):
    """逐级收敛不变式与派生租约的联动：子租约快照 ⊆ 父租约快照。"""
    led = BudgetLedger(now=clock)
    parent_eps = effective_permissions(
        user={"svc-a:logs:read", "svc-a:restart"},
        agent_caps={"svc-a:logs:read", "svc-a:restart"},
        platform_policy={"svc-a:logs:read", "svc-a:restart"},
        delegation={"svc-a:logs:read", "svc-a:restart"},
        runtime={"svc-a:logs:read", "svc-a:restart"})
    parent = led.grant("root", 100, effective_perms=parent_eps)

    # 子集派生：合法（显式给出更小的快照）
    child_eps = effective_permissions(
        user={"svc-a:logs:read", "svc-a:restart"},
        agent_caps={"svc-a:logs:read", "svc-a:restart"},
        platform_policy={"svc-a:logs:read", "svc-a:restart"},
        delegation={"svc-a:logs:read"},                    # 委托进一步收窄
        runtime={"svc-a:logs:read", "svc-a:restart"})
    child = led.grant("child", 40, parent_lease_id=parent.lease_id,
                      effective_perms=child_eps)
    assert set(child.effective_perms) <= set(parent.effective_perms)
    assert child.effective_perms == ("svc-a:logs:read",)

    # 未显式给出 → 继承父快照（⊆ 由构造保证）
    inherited = led.grant("child2", 10, parent_lease_id=parent.lease_id)
    assert inherited.effective_perms == parent.effective_perms

    # 越界派生：子 ⊄ 父 → 拒绝 + 留痕
    rogue_eps = effective_permissions(
        user={"svc-b:write"}, agent_caps={"svc-b:write"},
        platform_policy={"svc-b:write"}, delegation={"svc-b:write"},
        runtime={"svc-b:write"})
    with pytest.raises(LeaseDerivationError) as ei:
        led.grant("rogue", 10, parent_lease_id=parent.lease_id,
                  effective_perms=rogue_eps)
    assert "out-of-scope" in str(ei.value)
    assert led.audit[-1].event == "GRANT_REJECTED"
    assert led.audit[-1].detail["reason"].startswith("derived lease perms exceed upstream")


def test_effective_perms_subset_helper():
    big = EffectivePerms(perms=frozenset({"a", "b"}))
    small = EffectivePerms(perms=frozenset({"a"}))
    assert small.subset_of(big)
    assert not big.subset_of(small)
