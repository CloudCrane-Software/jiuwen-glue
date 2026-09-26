# coding: utf-8
"""fleet 调度器 P1 贪心测试（PROP-0004 / v1.7 §12.6/§13）— WO-0011.

覆盖：贪心排序确定性（信任等级降序 → 空闲 gpu_frac 降序 → max_parallel 余量
降序 → node_id 升序）、不可信节点只接沙箱任务类（硬规则）、GPU 份额扣减与
释放（完成/超时）、并行槽位、在线窗口、GuardrailRun 唯一门控输出消费、
租户隔离、Budget Lease 派生生命周期（含"子 ⊆ 父"越界拒绝与回滚）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_glue import (
    ACTIVE,
    BudgetLedger,
    EffectivePerms,
    REVOKED,
    TRUST_TRUSTED,
    TRUST_UNTRUSTED,
    VERDICT_BLOCKED,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
)
from jiuwen_glue.fleet import (
    ASSIGN_COMPLETED,
    ASSIGN_RELEASED,
    DISPATCH_DIRECT,
    DISPATCH_SELF_PICK,
    REJECT_GPU,
    REJECT_GUARDRAIL,
    REJECT_LEASE,
    REJECT_NO_NODE,
    REJECT_SANDBOX,
    REJECT_SLOT,
    REJECT_TENANT,
    REJECT_TTL_CAP,
    REJECT_TOOLS,
    REJECT_TRUST,
    Assignment,
    DispatchMode,
    FleetRegistry,
    GreedyScheduler,
    Rejected,
    SchedulingError,
    TaskOffering,
    assign,
)

from fleet_utils import make_registration


def _sched(clock, *nodes, budget=None):
    registry = FleetRegistry(now=clock)
    for node in nodes:
        registry.register(node)
    return GreedyScheduler(registry, now=clock, budget=budget)


def _trusted(node_id="node-1", **kw):
    return make_registration(node_id, trust_level=TRUST_TRUSTED, **kw)


def _untrusted(node_id="node-u", **kw):
    return make_registration(node_id, trust_level=TRUST_UNTRUSTED, **kw)


# ── 贪心排序（确定性）────────────────────────────────────────────────────────

def test_greedy_prefers_trusted_over_untrusted_for_sandbox_task(clock):
    """同可派时信任等级优先：不可信节点 gpu 余量更大也输给可信节点。"""
    sched = _sched(clock,
                   _untrusted("node-u", gpu_frac=1.0),
                   _trusted("node-t", gpu_frac=0.4))
    result = sched.assign(TaskOffering(task_ref="task-1", sandbox_class=True))
    assert isinstance(result, Assignment)
    assert result.node_id == "node-t"


def test_greedy_prefers_more_free_gpu_frac(clock):
    sched = _sched(clock,
                   _trusted("node-a", gpu_frac=0.2),
                   _trusted("node-b", gpu_frac=0.9))
    result = sched.assign(TaskOffering(task_ref="task-1", gpu_demand=0.3))
    assert isinstance(result, Assignment)
    assert result.node_id == "node-b"


def test_greedy_prefers_more_parallel_slack(clock):
    """信任与 gpu 都持平 → max_parallel 余量大者优先。"""
    sched = _sched(clock,
                   _trusted("node-a", gpu_frac=0.5, max_parallel=1),
                   _trusted("node-b", gpu_frac=0.5, max_parallel=3))
    result = sched.assign(TaskOffering(task_ref="task-1"))
    assert isinstance(result, Assignment)
    assert result.node_id == "node-b"


def test_greedy_full_tie_breaks_by_node_id_deterministically(clock):
    """完全持平 → node_id 升序；重复派发-释放 10 轮结果完全一致（确定性）。"""
    sched = _sched(clock,
                   _trusted("node-c", gpu_frac=0.5),
                   _trusted("node-a", gpu_frac=0.5),
                   _trusted("node-b", gpu_frac=0.5))
    picks = []
    for i in range(10):
        result = sched.assign(TaskOffering(task_ref=f"task-{i}"))
        assert isinstance(result, Assignment)
        picks.append(result.node_id)
        sched.complete(result.assignment_id)
    assert picks == ["node-a"] * 10


def test_module_level_assign_is_pure_decision(clock):
    """assign(offering, registry) 不落账：同一要约重复决策结果不变（dry-run）。"""
    registry = FleetRegistry(now=clock)
    registry.register(_trusted("node-a", gpu_frac=0.5))
    registry.register(_trusted("node-b", gpu_frac=0.9))
    for _ in range(3):
        result = assign(TaskOffering(task_ref="task-1", gpu_demand=0.5), registry,
                        now=clock.now_value)
        assert isinstance(result, Assignment)
        assert result.node_id == "node-b"     # 无记账 → 余量不耗减


# ── 不可信节点硬规则（12.6 边界，写死）────────────────────────────────────────

def test_untrusted_node_never_takes_non_sandbox_task(clock):
    """池里只有不可信节点 + 非沙箱任务类 → 拒绝（硬规则，无兜底）。"""
    sched = _sched(clock, _untrusted("node-u", gpu_frac=1.0))
    result = sched.assign(TaskOffering(task_ref="task-1"))
    assert isinstance(result, Rejected)
    assert result.code == REJECT_SANDBOX
    assert result.detail.get(REJECT_SANDBOX) == 1


def test_sandbox_task_on_untrusted_marks_isolation_and_review(clock):
    """沙箱任务类可派给不可信节点，Assignment 必须带 sandbox_only + review_required。"""
    sched = _sched(clock, _untrusted("node-u"))
    result = sched.assign(TaskOffering(task_ref="task-1", sandbox_class=True))
    assert isinstance(result, Assignment)
    assert result.sandbox_only is True
    assert result.review_required is True          # 复核流程归控制台 WO-0012


def test_sandbox_task_on_trusted_node_needs_no_review(clock):
    sched = _sched(clock, _trusted("node-t"))
    result = sched.assign(TaskOffering(task_ref="task-1", sandbox_class=True))
    assert isinstance(result, Assignment)
    assert result.sandbox_only is False
    assert result.review_required is False


# ── 份额记账：扣减与释放 ─────────────────────────────────────────────────────

def test_gpu_frac_deducted_and_released_on_complete(clock):
    sched = _sched(clock, _trusted("node-1", gpu_frac=0.5))
    first = sched.assign(TaskOffering(task_ref="task-1", gpu_demand=0.5))
    assert isinstance(first, Assignment)
    assert sched.free_gpu_frac("node-1") == 0.0
    second = sched.assign(TaskOffering(task_ref="task-2", gpu_demand=0.1))
    assert isinstance(second, Rejected)
    assert second.code == REJECT_GPU
    sched.complete(first.assignment_id, artifact_ref="minio://out/a.mp4")
    assert sched.free_gpu_frac("node-1") == 0.5
    third = sched.assign(TaskOffering(task_ref="task-3", gpu_demand=0.4))
    assert isinstance(third, Assignment)
    assert first.status == ASSIGN_COMPLETED
    assert first.artifact_ref == "minio://out/a.mp4"


def test_gpu_frac_released_on_timeout(clock):
    sched = _sched(clock, _trusted("node-1", gpu_frac=0.5))
    assignment = sched.assign(TaskOffering(task_ref="task-1", gpu_demand=0.5))
    sched.timeout(assignment.assignment_id)
    assert assignment.status == ASSIGN_RELEASED
    assert assignment.release_reason == "timeout"
    assert sched.free_gpu_frac("node-1") == 0.5


def test_parallel_slot_exhaustion_and_release(clock):
    sched = _sched(clock, _trusted("node-1", gpu_frac=1.0, max_parallel=1))
    first = sched.assign(TaskOffering(task_ref="task-1"))
    second = sched.assign(TaskOffering(task_ref="task-2"))
    assert isinstance(first, Assignment)
    assert isinstance(second, Rejected)
    assert second.code == REJECT_SLOT
    assert sched.active_slots("node-1") == 1
    sched.complete(first.assignment_id)
    assert sched.active_slots("node-1") == 0
    assert isinstance(sched.assign(TaskOffering(task_ref="task-3")), Assignment)


# ── 过滤器：STALE / 工具标签 / 信任要求 / 在线窗口 / 租户 ─────────────────────

def test_stale_node_is_not_scheduled(clock):
    registry = FleetRegistry(now=clock)
    registry.register(make_registration("node-1", now=clock.now_value,
                                        heartbeat_ttl_seconds=10.0))
    sched = GreedyScheduler(registry, now=clock)
    clock.advance(11)
    registry.sweep()
    result = sched.assign(TaskOffering(task_ref="task-1"))
    assert isinstance(result, Rejected)
    assert result.code == REJECT_NO_NODE


def test_required_tools_filter(clock):
    sched = _sched(clock, _trusted("node-1", tools=("shell",)))
    missing = sched.assign(TaskOffering(task_ref="task-1", required_tools=("vllm",)))
    assert isinstance(missing, Rejected)
    assert missing.code == REJECT_TOOLS


def test_required_tools_matched_assigns(clock):
    sched = _sched(clock, _trusted("node-1", tools=("shell", "vllm")))
    result = sched.assign(TaskOffering(task_ref="task-1", required_tools=("vllm",)))
    assert isinstance(result, Assignment)


def test_trust_required_filters_untrusted(clock):
    sched = _sched(clock, _untrusted("node-u"))
    result = sched.assign(TaskOffering(task_ref="task-1",
                                       trust_required=TRUST_TRUSTED,
                                       sandbox_class=True))
    assert isinstance(result, Rejected)
    assert result.code == REJECT_TRUST


def test_offline_window_blocks_assignment(clock):
    """在线窗口外不派（窗口按节点本地墙钟；声明带 +08 时区偏移）。"""
    local8 = timezone(timedelta(hours=8))
    online_local = datetime(2026, 1, 5, 10, 0, tzinfo=local8).timestamp()   # 窗口内
    offline_local = datetime(2026, 1, 5, 19, 30, tzinfo=local8).timestamp() # 窗口外
    registry = FleetRegistry(now=clock)
    registry.register(make_registration("node-1", now=clock.now_value,
                                        online_window="09:00-18:00+08"))
    sched = GreedyScheduler(registry, now=clock)
    assert isinstance(sched.assign(TaskOffering(task_ref="task-1"), now=online_local),
                      Assignment)
    blocked = sched.assign(TaskOffering(task_ref="task-2"), now=offline_local)
    assert isinstance(blocked, Rejected)
    assert blocked.code == "NODE_OFFLINE_WINDOW"


def test_tenant_isolation(clock):
    sched = _sched(clock, _trusted("node-1", gpu_frac=1.0))
    result = sched.assign(TaskOffering(task_ref="task-1", tenant_id="t1"))
    assert isinstance(result, Rejected)
    assert result.code == REJECT_NO_NODE
    assert result.detail.get(REJECT_TENANT) == 1


# ── GuardrailRun 唯一门控输出被消费（不新增第二个决策点）───────────────────────

def test_guardrail_verdict_gates_assignment(clock):
    sched = _sched(clock, _trusted("node-1"))
    blocked = sched.assign(TaskOffering(task_ref="task-1",
                                        guardrail_run_ref="gr-1",
                                        guardrail_verdict=VERDICT_BLOCKED))
    unknown = sched.assign(TaskOffering(task_ref="task-2",
                                        guardrail_run_ref="gr-2",
                                        guardrail_verdict=VERDICT_UNKNOWN))
    assert blocked.code == REJECT_GUARDRAIL
    assert unknown.code == REJECT_GUARDRAIL        # UNKNOWN fail-closed
    passed = sched.assign(TaskOffering(task_ref="task-3",
                                       guardrail_run_ref="gr-3",
                                       guardrail_verdict=VERDICT_PASS))
    assert isinstance(passed, Assignment)


def test_offering_verdict_without_run_ref_is_schema_error(clock):
    with pytest.raises(SchedulingError):
        TaskOffering(task_ref="task-1", guardrail_verdict=VERDICT_PASS)


# ── Budget Lease 派生（自取制短时租约，子 ⊆ 父收敛）───────────────────────────

def test_budget_lease_derived_on_self_pick_and_revoked_on_complete(clock):
    budget = BudgetLedger(now=clock)
    parent = budget.grant("batch-1", 1)
    sched = _sched(clock,
                   _trusted("node-1", gpu_frac=0.5,
                            agent_ref="ag:runner@t0/run:i-1/task:t-1"),
                   budget=budget)
    offering = TaskOffering(task_ref="task-1", parent_lease_id=parent.lease_id)
    assignment = sched.assign(offering, dispatch=DISPATCH_SELF_PICK, lease_ttl=600)
    assert isinstance(assignment, Assignment)
    assert assignment.lease_ref is not None
    assert budget.status_of(assignment.lease_ref) == ACTIVE
    sched.complete(assignment.assignment_id)
    assert budget.status_of(assignment.lease_ref) == REVOKED   # 撤销复用既有 revoke


def test_lease_derivation_failure_rejects_and_rolls_back(clock):
    """父额度耗尽 → 派生失败 → 拒绝 + 份额账回滚（不留幽灵占用）。"""
    budget = BudgetLedger(now=clock)
    parent = budget.grant("batch-1", 1)            # 父只剩 1 份额
    sched = _sched(clock, _trusted("node-1", gpu_frac=1.0), budget=budget)
    first = sched.assign(TaskOffering(task_ref="task-1",
                                      parent_lease_id=parent.lease_id),
                         dispatch=DISPATCH_SELF_PICK, lease_ttl=600)
    assert isinstance(first, Assignment)
    second = sched.assign(TaskOffering(task_ref="task-2",
                                       parent_lease_id=parent.lease_id),
                          dispatch=DISPATCH_SELF_PICK, lease_ttl=600)
    assert isinstance(second, Rejected)
    assert second.code == REJECT_LEASE
    assert sched.active_slots("node-1") == 1       # 回滚后只剩第一笔
    assert sched.free_gpu_frac("node-1") == 1.0    # 第二笔 gpu_demand=0，回滚无损


def test_derived_lease_perms_converge_never_expand(clock):
    """权限交集快照固化进派生租约；越界派生（子 ⊄ 父）被既有不变式拒绝。"""
    budget = BudgetLedger(now=clock)
    parent = budget.grant("batch-1", 2,
                          effective_perms=EffectivePerms(
                              perms=frozenset({"kv/data/company/gpu/*:read"})))
    sched = _sched(clock, _trusted("node-1"), budget=budget)
    ok = sched.assign(TaskOffering(
        task_ref="task-1",
        parent_lease_id=parent.lease_id,
        effective_perms=EffectivePerms(
            perms=frozenset({"kv/data/company/gpu/*:read"}))),
        dispatch=DISPATCH_SELF_PICK, lease_ttl=600)
    assert isinstance(ok, Assignment)
    assert ok.effective_perms == ("kv/data/company/gpu/*:read",)
    beyond = sched.assign(TaskOffering(
        task_ref="task-2",
        parent_lease_id=parent.lease_id,
        effective_perms=EffectivePerms(
            perms=frozenset({"kv/data/company/*:write"}))),
        dispatch=DISPATCH_SELF_PICK, lease_ttl=600)
    assert isinstance(beyond, Rejected)
    assert beyond.code == REJECT_LEASE


def test_self_pick_ttl_above_cap_rejected(clock):
    sched = _sched(clock, _trusted("node-1"))
    result = sched.assign(TaskOffering(task_ref="task-1"),
                          dispatch=DISPATCH_SELF_PICK, lease_ttl=3601)
    assert isinstance(result, Rejected)
    assert result.code == REJECT_TTL_CAP


def test_dispatch_mode_direct_flow(clock):
    """调度制：DispatchMode 直接派 → Assignment.dispatch == "dispatch"。"""
    sched = _sched(clock, _trusted("node-1"))
    mode = DispatchMode(sched)
    result = mode.dispatch(TaskOffering(task_ref="task-1"))
    assert isinstance(result, Assignment)
    assert result.dispatch == DISPATCH_DIRECT
    assert result.order_id is None                 # 调度制不来自工单队列
    assert result.lease_ref is None                # 未接 BudgetLedger → 无租约
    mode.complete(result.assignment_id, artifact_ref="git://repo/abc")
    assert result.status == ASSIGN_COMPLETED
