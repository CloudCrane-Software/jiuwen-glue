# coding: utf-8
"""自取制 worker 客户端协议测试（模拟侧）— WO-0011 / PROP-0003 §12.6.

覆盖：自取制认领（优先级顺序 + 硬规则过滤）、WorkerLoop.tick 全周期
（认领 → 本地执行 → 回报）、TTL 过期回收（工单回 PENDING + 份额释放 +
租约撤销）、TTL 上限（≤1h）、lease_scoped_secrets 只允许引用（执行面
零长期密钥红线在协议层的体现）、执行失败退单重试。
"""
from __future__ import annotations

import pytest

from jiuwen_glue import (
    ACTIVE,
    EffectivePerms,
    EXPIRED,
    PENDING,
    REVOKED,
    TRUST_TRUSTED,
    TRUST_UNTRUSTED,
    BudgetLedger,
)
from jiuwen_glue.fleet import (
    ASSIGN_COMPLETED,
    ASSIGN_RECLAIMED,
    REJECT_QUEUE_EMPTY,
    REJECT_SANDBOX,
    REJECT_STALE,
    REJECT_TTL_CAP,
    Assignment,
    FleetRegistry,
    GreedyScheduler,
    Rejected,
    SecretScopeError,
    SelfPickMode,
    TaskOffering,
    WorkerContext,
    WorkerLoop,
    WorkQueue,
)
from jiuwen_glue.rules import CLAIMED, COMPLETED

from fleet_utils import make_registration


def _setup(clock, node_kwargs=None, budget=False):
    registry = FleetRegistry(now=clock)
    registry.register(make_registration("node-1", now=clock.now_value,
                                        **(node_kwargs or {})))
    scheduler = GreedyScheduler(registry, now=clock,
                                budget=BudgetLedger(now=clock) if budget else None)
    queue = WorkQueue(now=clock)
    return scheduler, queue, scheduler.self_pick


# ── 自取制认领 ───────────────────────────────────────────────────────────────

def test_claim_takes_highest_priority_eligible_order(clock):
    scheduler, queue, mode = _setup(clock, dict(gpu_frac=0.5))
    low = queue.submit(TaskOffering(task_ref="task-low"), order_id="o-low",
                       priority=1)
    high = queue.submit(TaskOffering(task_ref="task-high"), order_id="o-high",
                       priority=5)
    result = mode.claim("node-1", queue)
    assert isinstance(result, Assignment)
    assert result.order_id == "o-high"            # priority 降序优先
    assert queue.get("o-high").state == CLAIMED
    assert queue.get("o-high").claimed_by == "node-1"
    assert queue.get("o-low").state == PENDING    # 一次认领一单
    assert scheduler.active_slots("node-1") == 1
    del low


def test_claim_skips_ineligible_order_and_leaves_it_pending(clock):
    """不可信节点认领非沙箱工单 → 拒绝且工单留在 PENDING（硬规则）。"""
    scheduler, queue, mode = _setup(clock, dict(trust_level=TRUST_UNTRUSTED))
    queue.submit(TaskOffering(task_ref="task-plain"), order_id="o-plain")
    result = mode.claim("node-1", queue)
    assert isinstance(result, Rejected)
    assert result.code == REJECT_SANDBOX
    assert queue.get("o-plain").state == PENDING
    # 随后入队沙箱工单 → 同一节点可认领
    queue.submit(TaskOffering(task_ref="task-sbx", sandbox_class=True),
                 order_id="o-sbx")
    ok = mode.claim("node-1", queue)
    assert isinstance(ok, Assignment)
    assert ok.order_id == "o-sbx"
    assert ok.review_required is True
    assert scheduler.active_slots("node-1") == 1


def test_claim_empty_queue_rejected(clock):
    scheduler, queue, mode = _setup(clock)
    result = mode.claim("node-1", queue)
    assert isinstance(result, Rejected)
    assert result.code == REJECT_QUEUE_EMPTY


def test_claim_unknown_node_raises(clock):
    scheduler, queue, mode = _setup(clock)
    with pytest.raises(Exception):
        mode.claim("ghost", queue)


def test_stale_node_cannot_claim(clock):
    """STALE 是兜底死亡语义：不可认领，须重注册。"""
    scheduler, queue, mode = _setup(clock, dict(heartbeat_ttl_seconds=10.0))
    queue.submit(TaskOffering(task_ref="task-1"))
    clock.advance(11)
    scheduler.registry.sweep()
    result = mode.claim("node-1", queue)
    assert isinstance(result, Rejected)
    assert result.code == REJECT_STALE
    assert queue.get(queue.all_orders()[0].order_id).state == PENDING


# ── WorkerLoop.tick 全周期 ───────────────────────────────────────────────────

def test_worker_tick_executes_and_reports(clock):
    scheduler, queue, mode = _setup(clock, dict(gpu_frac=0.5))
    queue.submit(TaskOffering(task_ref="task-1", gpu_demand=0.3),
                 order_id="o-1")
    seen = {}

    def executor(ctx, offering):
        seen["ctx"] = ctx
        seen["offering"] = offering
        return "minio://out/task-1.bin"

    worker = WorkerLoop("node-1", mode, queue, executor)
    result = worker.tick()
    assert result.action == "executed"
    assert result.order_id == "o-1"
    assert result.artifact_ref == "minio://out/task-1.bin"
    order = queue.get("o-1")
    assert order.state == COMPLETED
    assert order.artifact_ref == "minio://out/task-1.bin"
    assignment = scheduler.get_assignment(result.assignment_id)
    assert assignment.status == ASSIGN_COMPLETED
    assert scheduler.active_slots("node-1") == 0   # 份额已释放
    assert scheduler.free_gpu_frac("node-1") == 0.5
    # 回调拿到的是范围引用，不是密钥值
    assert seen["ctx"].task_ref == "task-1"
    assert seen["offering"].task_ref == "task-1"


def test_worker_idle_when_queue_empty(clock):
    scheduler, queue, mode = _setup(clock)
    worker = WorkerLoop("node-1", mode, queue, lambda ctx, o: "x://y")
    result = worker.tick()
    assert result.action == "idle"
    assert result.error == REJECT_QUEUE_EMPTY


def test_executor_failure_returns_order_to_pending(clock):
    """执行失败 → 工单退回 PENDING + 份额释放；下一轮可重新认领。"""
    scheduler, queue, mode = _setup(clock, dict(gpu_frac=0.5))
    queue.submit(TaskOffering(task_ref="task-1", gpu_demand=0.2), order_id="o-1")
    state = {"boom": True}

    def executor(ctx, offering):
        if state["boom"]:
            raise RuntimeError("simulated crash")
        return "minio://out/task-1.bin"

    worker = WorkerLoop("node-1", mode, queue, executor)
    first = worker.tick()
    assert first.action == "failed"
    assert first.error == "RuntimeError"           # 只透类型名，不带消息
    assert queue.get("o-1").state == PENDING
    assert scheduler.active_slots("node-1") == 0
    assert scheduler.free_gpu_frac("node-1") == 0.5
    state["boom"] = False
    second = worker.tick()
    assert second.action == "executed"


def test_worker_run_loops_until_idle(clock):
    scheduler, queue, mode = _setup(clock)
    queue.submit(TaskOffering(task_ref="task-1"), order_id="o-1")
    worker = WorkerLoop("node-1", mode, queue, lambda ctx, o: "git://repo/out")
    results = worker.run(ticks=3)
    assert [r.action for r in results] == ["executed", "idle", "idle"]


# ── 租约 TTL：上限与过期回收 ─────────────────────────────────────────────────

def test_claim_ttl_above_cap_rejected_and_order_stays_pending(clock):
    """TTL 超过 1h 上限 → 拒绝（fail-closed，不截断）——对齐 execution-worker
    ≤1h 短时令牌形状。"""
    scheduler, queue, mode = _setup(clock)
    queue.submit(TaskOffering(task_ref="task-1"), order_id="o-1")
    result = mode.claim("node-1", queue, ttl_seconds=3601)
    assert isinstance(result, Rejected)
    assert result.code == REJECT_TTL_CAP
    assert queue.get("o-1").state == PENDING


def test_lease_ttl_expiry_reclaims_order(clock):
    """租约 TTL 过期 → 工单回 PENDING、份额释放、租约终结（自取制兜底死亡）。"""
    scheduler, queue, mode = _setup(clock, dict(gpu_frac=0.5), budget=True)
    queue.submit(TaskOffering(task_ref="task-1", gpu_demand=0.3), order_id="o-1")
    assignment = mode.claim("node-1", queue, ttl_seconds=100)
    assert isinstance(assignment, Assignment)
    assert assignment.lease_ref is not None
    assert scheduler.active_slots("node-1") == 1
    clock.advance(101)
    reclaimed = mode.reclaim_expired(queue)
    assert [o.order_id for o in reclaimed] == ["o-1"]
    assert queue.get("o-1").state == PENDING
    assert scheduler.get_assignment(assignment.assignment_id).status == ASSIGN_RECLAIMED
    assert scheduler.active_slots("node-1") == 0
    assert scheduler.free_gpu_frac("node-1") == 0.5
    assert scheduler.registry.get("node-1").status == "ACTIVE"   # 节点本身还活着


def test_expired_lease_is_terminated_in_budget_ledger(clock):
    scheduler, queue, mode = _setup(clock, budget=True)
    queue.submit(TaskOffering(task_ref="task-1"), order_id="o-1")
    assignment = mode.claim("node-1", queue, ttl_seconds=50)
    assert budget_status(scheduler, assignment) == ACTIVE
    clock.advance(51)
    mode.reclaim_expired(queue)
    assert budget_status(scheduler, assignment) in (REVOKED, EXPIRED)


def budget_status(scheduler, assignment):
    ledger = scheduler._budget
    return ledger.status_of(assignment.lease_ref)


# ── lease_scoped_secrets：只允许引用（零长期密钥红线的协议层体现）─────────────

VALID_REF = "bao://kv/data/company/gpu/token-ref-1"


def test_worker_context_rejects_raw_token_secret():
    with pytest.raises(SecretScopeError):
        WorkerContext(node_id="node-1", order_id="o-1", task_ref="task-1",
                      pipeline_label="", assignment_id="a-1",
                      lease_ref="lease-1", lease_expires_at=None,
                      lease_scoped_secrets=("bare-raw-token-value",))


def test_queue_submit_rejects_raw_secret_values(clock):
    scheduler, queue, mode = _setup(clock)
    with pytest.raises(SecretScopeError):
        queue.submit(TaskOffering(task_ref="task-1"),
                     secret_refs=("another-raw-value",))


def test_worker_context_accepts_scoped_refs_and_worker_sees_only_refs(clock):
    """worker 可见 = 范围引用 + 租约引用 + 固化权限快照；不可信节点带复核标记。"""
    scheduler, queue, mode = _setup(clock, dict(trust_level=TRUST_UNTRUSTED),
                                   budget=True)
    perms = EffectivePerms(perms=frozenset({"kv/data/company/gpu/*:read"}))
    queue.submit(TaskOffering(task_ref="task-sbx", sandbox_class=True,
                              effective_perms=perms),
                 order_id="o-1", secret_refs=(VALID_REF,))
    seen = {}
    WorkerLoop("node-1", mode, queue,
               lambda ctx, o: (seen.setdefault("ctx", ctx),
                               "minio://out/sbx.bin")[1]).tick()
    ctx = seen["ctx"]
    assert ctx.lease_scoped_secrets == (VALID_REF,)   # 引用，不是值
    assert ctx.lease_ref is not None                  # 租约引用（短时）
    assert ctx.review_required is True                # 不可信节点 → 人工复核
    assert ctx.effective_perms == ("kv/data/company/gpu/*:read",)  # 快照固化进租约
