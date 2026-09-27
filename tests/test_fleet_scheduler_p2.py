# coding: utf-8
"""fleet 调度器 P2 测试（bin-packing / GPU 优先级 / 抢占标志）— W-04, v2.0 §10 M2.

覆盖：best-fit by gpu_frac（放置后剩余最小者优先，含记账后动态余量）、
平局比 NodeCapacity.gpu_priority、node_id 确定性 tie-break、P1 greedy 排序键
锁定不变（gpu_priority 不影响 greedy）、抢占标志校验（无优先级 = 构造错误；
flag 不触发真实驱逐——抢占语义 [待]）、字段校验（负数/bool 拒绝）、
GreedyScheduler 策略默认值与未知策略 fail-closed。
"""
from __future__ import annotations

import pytest

from jiuwen_glue import (
    CapacitySchemaError,
    TRUST_TRUSTED,
)
from jiuwen_glue.fleet import (
    ASSIGN_ACTIVE,
    REJECT_GPU,
    FleetRegistry,
    GreedyScheduler,
    Rejected,
    SCHED_POLICY_BEST_FIT,
    SCHED_POLICY_GREEDY,
    SCHED_POLICIES,
    SchedulingError,
    TaskOffering,
    Assignment,
    assign,
    rank_key_best_fit,
)

from fleet_utils import make_registration


def _sched(clock, *nodes, policy=SCHED_POLICY_GREEDY):
    registry = FleetRegistry(now=clock)
    for node in nodes:
        registry.register(node)
    return GreedyScheduler(registry, now=clock, policy=policy)


def _trusted(node_id="node-1", **kw):
    return make_registration(node_id, trust_level=TRUST_TRUSTED, **kw)


# ── best-fit by gpu_frac（P2 bin-packing 排序策略）────────────────────────────

def test_best_fit_picks_smallest_leftover(clock):
    """需求 0.3：free 0.5/0.8/1.0 三节点 → 剩余 0.2/0.5/0.7，选 0.5（装得最满）。"""
    sched = _sched(clock,
                   _trusted("n-small", gpu_frac=0.5),
                   _trusted("n-mid", gpu_frac=0.8),
                   _trusted("n-big", gpu_frac=1.0),
                   policy=SCHED_POLICY_BEST_FIT)
    result = sched.assign(TaskOffering(task_ref="task-1", gpu_demand=0.3))
    assert isinstance(result, Assignment)
    assert result.node_id == "n-small"


def test_best_fit_opposite_of_greedy_on_same_nodes(clock):
    """同一拓扑：greedy 选 free 最大者，best-fit 选 free 最小可用者——策略差异可见。"""
    nodes = (_trusted("n-a", gpu_frac=0.4), _trusted("n-b", gpu_frac=0.9))
    greedy = _sched(clock, *nodes, policy=SCHED_POLICY_GREEDY)
    bestfit = _sched(clock, *nodes, policy=SCHED_POLICY_BEST_FIT)
    assert greedy.assign(TaskOffering(task_ref="t", gpu_demand=0.3)).node_id == "n-b"
    assert bestfit.assign(TaskOffering(task_ref="t", gpu_demand=0.3)).node_id == "n-a"


def test_best_fit_accounts_committed_shares_via_ledger(clock):
    """记账动态：0.6 落 n-a（free 0.8，剩余 0.2 最小）→ 0.3 落 n-b（free 0.8，
    剩余 0.5）→ 0.3 再落 n-b（free 0.5，剩余 0.2）→ 末次 0.3：free 均为 0.2
    → REJECT_GPU。best-fit 全程按真实余量装填。"""
    sched = _sched(clock,
                   _trusted("n-a", gpu_frac=0.8),
                   _trusted("n-b", gpu_frac=0.8),
                   policy=SCHED_POLICY_BEST_FIT)
    first = sched.assign(TaskOffering(task_ref="t1", gpu_demand=0.6))
    assert isinstance(first, Assignment) and first.node_id == "n-a"
    second = sched.assign(TaskOffering(task_ref="t2", gpu_demand=0.3))
    assert isinstance(second, Assignment)
    assert second.node_id == "n-b"
    third = sched.assign(TaskOffering(task_ref="t3", gpu_demand=0.3))
    assert isinstance(third, Assignment)
    assert third.node_id == "n-b"
    fourth = sched.assign(TaskOffering(task_ref="t4", gpu_demand=0.3))
    assert isinstance(fourth, Rejected)
    assert fourth.code == REJECT_GPU
    assert sched.free_gpu_frac("n-a") == pytest.approx(0.2)
    assert sched.free_gpu_frac("n-b") == pytest.approx(0.2)


def test_best_fit_exact_fit_wins(clock):
    """精确贴合（leftover=0）优先于更大剩余：demand 0.5 → free 0.5 的节点。"""
    sched = _sched(clock,
                   _trusted("n-exact", gpu_frac=0.5),
                   _trusted("n-roomy", gpu_frac=1.0),
                   policy=SCHED_POLICY_BEST_FIT)
    result = sched.assign(TaskOffering(task_ref="t", gpu_demand=0.5))
    assert isinstance(result, Assignment)
    assert result.node_id == "n-exact"


def test_best_fit_tie_breaks_by_gpu_priority_then_node_id(clock):
    """剩余相同 → gpu_priority 大者优先；再同 → node_id 升序。"""
    sched = _sched(clock,
                   _trusted("n-lo", gpu_frac=0.6, gpu_priority=1),
                   _trusted("n-hi", gpu_frac=0.6, gpu_priority=9),
                   policy=SCHED_POLICY_BEST_FIT)
    result = sched.assign(TaskOffering(task_ref="t", gpu_demand=0.2))
    assert isinstance(result, Assignment)
    assert result.node_id == "n-hi"

    sched2 = _sched(clock,
                    _trusted("n-b", gpu_frac=0.6, gpu_priority=5),
                    _trusted("n-a", gpu_frac=0.6, gpu_priority=5),
                    policy=SCHED_POLICY_BEST_FIT)
    result2 = sched2.assign(TaskOffering(task_ref="t", gpu_demand=0.2))
    assert isinstance(result2, Assignment)
    assert result2.node_id == "n-a"


def test_rank_key_best_fit_shape(clock):
    """排序键形状直测：剩余升序、优先级降序、node_id 升序。"""
    ledger = None
    r_hi = _trusted("n-hi", gpu_frac=0.5, gpu_priority=7)
    r_lo = _trusted("n-lo", gpu_frac=0.5, gpu_priority=7)
    r_big = _trusted("n-big", gpu_frac=0.9, gpu_priority=0)
    k_hi = rank_key_best_fit(r_hi, ledger, 0.3)
    k_lo = rank_key_best_fit(r_lo, ledger, 0.3)
    k_big = rank_key_best_fit(r_big, ledger, 0.3)
    assert k_hi < k_lo                       # 同剩余同优先级 → node_id
    assert k_hi[0] == pytest.approx(0.2)     # leftover
    assert k_big[0] == pytest.approx(0.6)    # 更大剩余排后
    assert k_hi < k_big


# ── P1 greedy 排序键锁定不变 ─────────────────────────────────────────────────

def test_greedy_policy_ignores_gpu_priority(clock):
    """P1 语义锁定：greedy 仍按空闲 gpu_frac 降序，高 gpu_priority 不改变结果。"""
    sched = _sched(clock,
                   _trusted("n-less-free", gpu_frac=0.4, gpu_priority=100),
                   _trusted("n-more-free", gpu_frac=0.9, gpu_priority=0))
    result = sched.assign(TaskOffering(task_ref="t", gpu_demand=0.3))
    assert isinstance(result, Assignment)
    assert result.node_id == "n-more-free"


# ── 抢占标志（语义 [待]；本工单只加字段与校验）───────────────────────────────

def test_preempt_flag_requires_positive_priority():
    with pytest.raises(SchedulingError, match="preempt_ok"):
        TaskOffering(task_ref="t", preempt_ok=True)                       # 无优先级
    with pytest.raises(SchedulingError, match="preempt_ok"):
        TaskOffering(task_ref="t", preempt_ok=True, gpu_priority=0)
    ok = TaskOffering(task_ref="t", preempt_ok=True, gpu_priority=3)
    assert ok.preempt_ok is True and ok.gpu_priority == 3


def test_preempt_flag_does_not_evict_active_assignments(clock):
    """抢占语义 [待]：preempt_ok=True 也绝不驱逐 ACTIVE 派工——满节点照样拒绝。"""
    sched = _sched(clock, _trusted("n-only", gpu_frac=0.5),
                   policy=SCHED_POLICY_BEST_FIT)
    low = sched.assign(TaskOffering(task_ref="low", gpu_demand=0.5, gpu_priority=1))
    assert isinstance(low, Assignment)
    high = sched.assign(TaskOffering(task_ref="high", gpu_demand=0.5,
                                     gpu_priority=9, preempt_ok=True))
    assert isinstance(high, Rejected)
    assert high.code == REJECT_GPU            # 不驱逐，走常规 GPU 不足拒绝
    assert low.status == ASSIGN_ACTIVE        # 低优先级派工未受影响


def test_preempt_flag_echoed_on_assignment(clock):
    sched = _sched(clock, _trusted("n-1", gpu_frac=0.5),
                   policy=SCHED_POLICY_BEST_FIT)
    result = sched.assign(TaskOffering(task_ref="t", gpu_demand=0.5,
                                       gpu_priority=2, preempt_ok=True))
    assert isinstance(result, Assignment)
    assert result.preempt_ok is True


# ── 字段校验与策略参数 fail-closed ───────────────────────────────────────────

def test_gpu_priority_validation_on_capacity():
    with pytest.raises(CapacitySchemaError):
        make_registration("n-x", gpu_priority=-1)
    with pytest.raises(CapacitySchemaError):
        make_registration("n-x", gpu_priority=True)   # bool 冒充 int → 拒
    with pytest.raises(CapacitySchemaError):
        make_registration("n-x", gpu_priority=1.5)
    assert make_registration("n-x", gpu_priority=3).capacity.gpu_priority == 3


def test_task_gpu_priority_validation():
    with pytest.raises(SchedulingError):
        TaskOffering(task_ref="t", gpu_priority=-2)
    with pytest.raises(SchedulingError):
        TaskOffering(task_ref="t", gpu_priority=True)
    with pytest.raises(SchedulingError):
        TaskOffering(task_ref="t", gpu_priority=0.5)
    assert TaskOffering(task_ref="t", gpu_priority=4).gpu_priority == 4


def test_unknown_policy_fails_closed(clock):
    registry = FleetRegistry(now=clock)
    registry.register(_trusted("n-1"))
    with pytest.raises(SchedulingError, match="unknown scheduling policy"):
        GreedyScheduler(registry, now=clock, policy="spread")
    with pytest.raises(SchedulingError, match="unknown scheduling policy"):
        assign(TaskOffering(task_ref="t"), registry, policy="random")
    assert set(SCHED_POLICIES) == {SCHED_POLICY_GREEDY, SCHED_POLICY_BEST_FIT}


def test_scheduler_default_policy_is_p1_greedy(clock):
    registry = FleetRegistry(now=clock)
    registry.register(_trusted("n-1"))
    sched = GreedyScheduler(registry, now=clock)
    assert sched.policy == SCHED_POLICY_GREEDY
    result = sched.assign(TaskOffering(task_ref="t", gpu_priority=5))
    assert isinstance(result, Assignment)


def test_best_fit_zero_demand_still_prefers_tightest_node(clock):
    """纯 CPU 任务（demand 0）也按 best-fit：free 最小者先填满。"""
    sched = _sched(clock,
                   _trusted("n-a", gpu_frac=0.9),
                   _trusted("n-b", gpu_frac=0.1),
                   policy=SCHED_POLICY_BEST_FIT)
    result = sched.assign(TaskOffering(task_ref="t"))
    assert isinstance(result, Assignment)
    assert result.node_id == "n-b"
