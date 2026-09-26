# coding: utf-8
"""mock 数据层全面板取数 + 三级干预留痕（s/a/p）。"""
from __future__ import annotations

import pytest

from console_tui.data import (ChallengeRow, DecisionRow, LeaseRow, NodeRow,
                              PoolSummary, TaskRow)
from console_tui.state import (KIND_APPROVE, KIND_DENY, KIND_PAUSE, KIND_RESUME,
                               KIND_STEER, ChallengeResolutionError,
                               GovernanceError, IllegalTransitionError,
                               UnknownTargetError)

MINUTE = 60.0


# ── 五个数据面板（12.3）取数 ─────────────────────────────────────────────────

def test_all_five_panels_have_data(store):
    assert store.tasks() and all(isinstance(t, TaskRow) for t in store.tasks())
    assert store.leases() and all(isinstance(x, LeaseRow) for x in store.leases())
    assert store.challenges() and all(isinstance(c, ChallengeRow) for c in store.challenges())
    assert store.decisions() and all(isinstance(d, DecisionRow) for d in store.decisions())
    assert store.nodes() and all(isinstance(n, NodeRow) for n in store.nodes())
    assert isinstance(store.pool_summary(), PoolSummary)


def test_task_board_fields_complete(store):
    t = store.tasks()[0]
    assert t.task_id and t.title and t.owner and t.state in (
        "PENDING", "CLAIMED", "COMPLETED", "BLOCKED", "CANCELLED")
    assert t.last_transition_at >= t.created_at
    assert t.paused is False                           # 种子数据无暂停


def test_lease_panel_exposes_utilization(store):
    leases = {x.lease_id: x for x in store.leases()}
    active = leases["lease-100"]
    assert active.status == "ACTIVE"
    assert active.remaining <= active.amount
    assert 0.0 < active.utilization < 1.0
    assert leases["lease-101"].parent_lease_id == "lease-100"   # 派生关系可见


def test_challenge_queue_sorted_by_deadline(store):
    rows = store.challenges()
    assert len(rows) == 2
    assert rows == sorted(rows, key=lambda c: c.expires_at)
    assert all(c.seconds_left(store.now()) > 0 for c in rows)


def test_node_panel_and_pool_summary(store):
    nodes = store.nodes()
    summary = store.pool_summary()
    assert summary.node_count == 3
    assert summary.gpu_frac_total == pytest.approx(1.5)   # 0.5 + 1.0 + 0.0
    assert summary.gpu_frac_trusted == pytest.approx(1.5)
    assert summary.untrusted_count == 1                    # edge-relay
    assert summary.max_parallel_total == 7


def test_tower_columns_derived_from_tasks(store):
    cols = {a.agent_ref: a for a in store.agents()}
    assert set(cols) == {"alpha-planner-01", "beta-reviewer-01",
                         "gamma-ops-01", "delta-runner-02"}
    assert cols["alpha-planner-01"].status == "working"    # tsk-001 CLAIMED
    assert cols["gamma-ops-01"].status == "idle"           # 只有 PENDING/COMPLETED
    assert cols["delta-runner-02"].current_task == "租约到期巡检"
    assert all(a.heartbeat_seconds >= 0 for a in cols.values())
    assert cols["beta-reviewer-01"].open_tasks == 1


# ── s = steer（可逆干预，留痕）───────────────────────────────────────────────

def test_steer_writes_decision_record_and_audit(store):
    before = len(store.decisions())
    event = store.steer("alpha-planner-01", "先补 guardrail 测试")
    assert event.kind == KIND_STEER and event.target == "alpha-planner-01"
    assert event.detail["text"] == "先补 guardrail 测试"
    dec = store.decisions()[-1]
    assert len(store.decisions()) == before + 1
    assert dec.chosen == KIND_STEER and "steer" in dec.options
    assert dec.meta["intervention"] == KIND_STEER and dec.meta["target"] == "alpha-planner-01"
    assert len(dec.context_hash) == 64
    assert store.audit_trail()[-1].kind == KIND_STEER


def test_steer_rejects_unknown_agent_and_empty_text(store):
    with pytest.raises(UnknownTargetError):
        store.steer("ghost-agent", "x")
    with pytest.raises(GovernanceError):
        store.steer("alpha-planner-01", "   ")
    assert all(a.kind != KIND_STEER for a in store.audit_trail()[-2:])   # 拒绝也不留成功痕


# ── a = 审批 ask 队列（走 Challenge 状态机，不绕过）────────────────────────

def _pending(store):
    return store.challenges()[0].challenge_id


def test_approve_challenge_flow(store):
    cid = _pending(store)
    event = store.resolve_challenge(cid, approved=True)
    assert event.kind == KIND_APPROVE
    assert cid not in {c.challenge_id for c in store.challenges()}      # 出队
    dec = store.decisions()[-1]
    assert dec.chosen == KIND_APPROVE and dec.meta["challenge_state"] == "approved"
    assert store.audit_trail()[-1].kind == KIND_APPROVE


def test_deny_challenge_flow(store):
    cid = _pending(store)
    event = store.resolve_challenge(cid, approved=False)
    assert event.kind == KIND_DENY
    assert store.decisions()[-1].meta["challenge_state"] == "denied"


def test_expired_challenge_rejected_fail_closed(store, clock):
    cid = _pending(store)
    clock.advance(3600.0)                               # 越过全部有效期
    with pytest.raises(ChallengeResolutionError, match="expired"):
        store.resolve_challenge(cid, approved=True)
    assert store.challenges() == []                     # 惰性过期 → 出队（fail-closed）


def test_resolved_challenge_is_terminal(store):
    cid = _pending(store)
    store.resolve_challenge(cid, approved=True)
    with pytest.raises(ChallengeResolutionError, match="terminal"):
        store.resolve_challenge(cid, approved=False)
    with pytest.raises(UnknownTargetError):
        store.resolve_challenge("ch-9999", approved=True)


# ── p = 暂停 / 恢复（同一键位，可逆）────────────────────────────────────────

def test_pause_resume_cycle_writes_audit(store):
    before = len(store.decisions())
    ev1 = store.set_task_pause("tsk-001", pause=True)
    assert ev1.kind == KIND_PAUSE
    assert store.tasks()[0].paused is True              # tsk-001 是看板首行
    ev2 = store.set_task_pause("tsk-001", pause=False)
    assert ev2.kind == KIND_RESUME
    assert store.tasks()[0].paused is False
    assert len(store.decisions()) == before + 2         # 每次干预一条留痕
    kinds = [a.kind for a in store.audit_trail()[-2:]]
    assert kinds == [KIND_PAUSE, KIND_RESUME]


def test_pause_illegal_transitions_rejected(store):
    with pytest.raises(IllegalTransitionError, match="already paused"):
        store.set_task_pause("tsk-001", pause=True)
        store.set_task_pause("tsk-001", pause=True)
    with pytest.raises(IllegalTransitionError, match="not paused"):
        store.set_task_pause("tsk-002", pause=False)
    with pytest.raises(UnknownTargetError):
        store.set_task_pause("tsk-ghost", pause=True)


def test_pause_state_survives_restart_like_reinstantiation(store):
    """暂停态由决策留痕推导：新实例从同一留痕重建后仍可见（003 paused 推导语义）。"""
    store.set_task_pause("tsk-001", pause=True)
    decisions = store.decisions()
    # 模拟重建：从留痕重放 pause/resume 推导（与 v_task_board.paused 同规则）
    replay = [d for d in decisions
              if d.meta.get("intervention") in ("pause", "resume")
              and d.meta.get("target") == "tsk-001"]
    assert replay and replay[-1].meta["intervention"] == KIND_PAUSE
