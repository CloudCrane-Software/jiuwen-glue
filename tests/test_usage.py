# coding: utf-8
"""usage_events 计量四维度 + 租约-Higress consumer 绑定测试（v2.0 §4.4）— W-04.

覆盖：四维度追加与聚合、llm_relay 强制 consumer key、负/非有限数量拒绝、
append-only 纪律（无 update/delete 写路径）、绑定状态机（active → cutoff/
revoked 终态不可逆）、重复绑定拒绝、断流求值（EXPIRED/REVOKED/惰性到期，
EXHAUSTED 不断流）、缺租约数据跳过、dry-run 恒为默认（真实断流 [待 owner 批]）。
"""
from __future__ import annotations

import math

import pytest

from jiuwen_glue import (
    BINDING_ACTIVE,
    BINDING_CUTOFF,
    BINDING_REVOKED,
    CUTOFF_DRY_RUN,
    CUTOFF_ENFORCE,
    KIND_COMPUTE_SECONDS,
    KIND_LLM_RELAY,
    KIND_SANDBOX_SECONDS,
    KIND_STORAGE_BYTES,
    REASON_LEASE_EXPIRED,
    REASON_LEASE_LAPSED,
    REASON_LEASE_REVOKED,
    USAGE_KINDS,
    BindingLedger,
    LeaseConsumerBinding,
    UsageEvent,
    UsageLedger,
    UsageSchemaError,
    UsageStateError,
    cutoff_due,
    cutoff_plan,
)

NOW = 1_700_000_000.0


def _clock():
    class _C:
        now_value = NOW

        def __call__(self):
            return self.now_value

        def advance(self, s):
            self.now_value += s

    return _C()


# ── 四维度追加与聚合 ─────────────────────────────────────────────────────────

def test_four_dimensions_record_and_summarize():
    ledger = UsageLedger(now=_clock())
    ledger.record_llm_relay(1200, "cons-alpha", lease_ref="lease-1")
    ledger.record_llm_relay(800, "cons-alpha", lease_ref="lease-1")
    ledger.record_compute_seconds(90.5, node_ref="node-gpu")
    ledger.record_storage_bytes(1024)
    ledger.record_sandbox_seconds(30)
    s = ledger.summarize()
    assert set(s) == set(USAGE_KINDS)
    assert s[KIND_LLM_RELAY].events == 2
    assert s[KIND_LLM_RELAY].total == 2000
    assert s[KIND_COMPUTE_SECONDS].total == pytest.approx(90.5)
    assert s[KIND_STORAGE_BYTES].total == 1024
    assert s[KIND_SANDBOX_SECONDS].total == 30


def test_summarize_filters_by_tenant_and_kind():
    ledger = UsageLedger(now=_clock())
    ledger.record_llm_relay(10, "cons-a", tenant_id="t0")
    ledger.record_llm_relay(20, "cons-a", tenant_id="t1")
    ledger.record_compute_seconds(5, tenant_id="t0")
    s = ledger.summarize(tenant_id="t1")
    assert set(s) == {KIND_LLM_RELAY} and s[KIND_LLM_RELAY].total == 20
    s0 = ledger.summarize(tenant_id="t0", kind=KIND_LLM_RELAY)
    assert set(s0) == {KIND_LLM_RELAY} and s0[KIND_LLM_RELAY].events == 1


def test_llm_relay_requires_consumer_key():
    ledger = UsageLedger(now=_clock())
    with pytest.raises(UsageSchemaError, match="consumer key"):
        ledger.record_llm_relay(100, "")
    with pytest.raises(UsageSchemaError, match="consumer key"):
        UsageEvent(event_id="e1", kind=KIND_LLM_RELAY, quantity=1, consumer_key=None)


def test_other_dimensions_do_not_require_consumer_key():
    ledger = UsageLedger(now=_clock())
    e = ledger.record_compute_seconds(1.0)
    assert e.consumer_key is None


def test_negative_or_nonfinite_quantity_rejected():
    ledger = UsageLedger(now=_clock())
    for bad in (-1, -0.001, float("nan"), float("inf"), True, "100"):
        with pytest.raises(UsageSchemaError):
            ledger.record_compute_seconds(bad)


def test_unknown_kind_rejected():
    with pytest.raises(UsageSchemaError, match="usage kind"):
        UsageEvent(event_id="e", kind="gpu_hours", quantity=1)


def test_events_only_for_llm_relay_carry_consumer_key_shape():
    ledger = UsageLedger(now=_clock())
    ledger.record_llm_relay(5, "cons-a")
    ledger.record_llm_relay(7, "cons-b")
    got = ledger.events(consumer_key="cons-a")
    assert len(got) == 1 and got[0].quantity == 5


# ── append-only 纪律 ─────────────────────────────────────────────────────────

def test_ledger_is_append_only_no_mutation_methods():
    write_names = [n for n in dir(UsageLedger)
                   if n.startswith(("update", "delete", "remove", "amend", "rewrite"))]
    assert write_names == []
    ledger = UsageLedger(now=_clock())
    ledger.record_storage_bytes(1)
    before = tuple(ledger.events())
    assert before == tuple(ledger.events())      # 回放只读且稳定
    assert len(ledger) == 1


def test_recorded_events_carry_timestamp_from_injected_clock():
    clock = _clock()
    ledger = UsageLedger(now=clock)
    e = ledger.record_storage_bytes(1)
    assert e.occurred_at == NOW
    clock.advance(60)
    e2 = ledger.record_storage_bytes(1)
    assert e2.occurred_at == NOW + 60


# ── 绑定声明结构与状态机 ─────────────────────────────────────────────────────

def test_binding_requires_refs_and_defaults_to_dry_run():
    b = LeaseConsumerBinding(binding_id="b1", lease_ref="lease-1",
                             consumer_key="cons-alpha", created_at=NOW)
    assert b.state == BINDING_ACTIVE
    assert b.cutoff_mode == CUTOFF_DRY_RUN       # 真实断流 [待 owner 批]，默认 dry-run
    bad_sets = [{"binding_id": ""}, {"lease_ref": ""}, {"consumer_key": None}]
    for bad in bad_sets:
        kwargs = dict(binding_id="b", lease_ref="l", consumer_key="c", created_at=NOW)
        kwargs.update(bad)
        with pytest.raises(UsageSchemaError):
            LeaseConsumerBinding(**kwargs)


def test_binding_states_enumeration_and_guards():
    with pytest.raises(UsageSchemaError):
        LeaseConsumerBinding(binding_id="b", lease_ref="l", consumer_key="c",
                             state="paused", created_at=NOW)
    with pytest.raises(UsageSchemaError):
        LeaseConsumerBinding(binding_id="b", lease_ref="l", consumer_key="c",
                             state=BINDING_CUTOFF, created_at=NOW)  # cutoff 缺 cutoff_at


def test_binding_ledger_lifecycle_and_terminal_states():
    clock = _clock()
    led = BindingLedger(now=clock)
    b = led.bind("lease-1", "cons-alpha", cutoff_mode=CUTOFF_ENFORCE)
    assert led.get(b.binding_id).state == BINDING_ACTIVE
    # 重复活跃绑定拒绝
    with pytest.raises(UsageStateError, match="already exists"):
        led.bind("lease-1", "cons-alpha")
    done = led.mark_cutoff(b.binding_id)
    assert done.state == BINDING_CUTOFF and done.cutoff_at == NOW
    with pytest.raises(UsageStateError):
        led.mark_cutoff(b.binding_id)            # 终态不可逆
    with pytest.raises(UsageStateError):
        led.revoke(b.binding_id)
    b2 = led.bind("lease-2", "cons-beta")
    rv = led.revoke(b2.binding_id, reason="offboard")
    assert rv.state == BINDING_REVOKED and rv.revoke_reason == "offboard"
    with pytest.raises(UsageStateError):
        led.get("nope")
    assert len(led.audit) == 5                  # BIND + BIND_REJECTED + CUTOFF + BIND + REVOKE


# ── 断流求值（读过期租约 → 计划；真实断流 [待 owner 批]）─────────────────────

def _binding(bid, lease, consumer="cons-x", state=BINDING_ACTIVE, **kw):
    return LeaseConsumerBinding(binding_id=bid, lease_ref=lease,
                                consumer_key=consumer, state=state,
                                created_at=NOW, **kw)


def test_cutoff_due_reasons_expired_revoked_lapsed():
    b = _binding("b1", "lease-1")
    assert cutoff_due(b, "EXPIRED", None, now=NOW) == (True, REASON_LEASE_EXPIRED)
    assert cutoff_due(b, "REVOKED", None, now=NOW) == (True, REASON_LEASE_REVOKED)
    # 惰性到期：状态仍 ACTIVE 但 expires_at 已过
    assert cutoff_due(b, "ACTIVE", NOW - 1, now=NOW) == (True, REASON_LEASE_LAPSED)
    assert cutoff_due(b, "ACTIVE", NOW + 60, now=NOW) == (False, "")
    assert cutoff_due(b, "ACTIVE", None, now=NOW) == (False, "")
    # EXHAUSTED 不断流（计费策略 [待 owner 裁]）
    assert cutoff_due(b, "EXHAUSTED", None, now=NOW) == (False, "")
    # 非 ACTIVE 绑定不再出计划
    done = _binding("b2", "lease-1", state=BINDING_CUTOFF, cutoff_at=NOW)
    assert cutoff_due(done, "EXPIRED", None, now=NOW) == (False, "")


def test_cutoff_plan_defaults_to_dry_run_and_skips_unknown_leases():
    bindings = [
        _binding("b1", "lease-dead"),
        _binding("b2", "lease-alive", consumer="cons-y"),
        _binding("b3", "lease-ghost"),        # 租约视图缺声明 → 跳过
    ]
    lease_view = {
        "lease-dead": ("EXPIRED", NOW - 100),
        "lease-alive": ("ACTIVE", NOW + 100),
    }
    plan = cutoff_plan(bindings, lease_view, now=NOW)
    assert [a.binding_id for a in plan] == ["b1"]
    a = plan[0]
    assert a.consumer_key == "cons-x" and a.reason == REASON_LEASE_EXPIRED
    assert a.dry_run is True                   # 默认恒 dry-run
    enforced = cutoff_plan(bindings, lease_view, now=NOW, enforce=True)
    assert enforced[0].dry_run is False
    assert cutoff_plan(bindings, {"lease-ghost": ("ACTIVE", None)}, now=NOW) == []


def test_cutoff_plan_deterministic_order():
    bindings = [
        _binding("b2", "l2", consumer="cons-b"),
        _binding("b1", "l1", consumer="cons-a"),
    ]
    view = {"l1": ("REVOKED", None), "l2": ("ACTIVE", NOW - 1)}
    plan = cutoff_plan(bindings, view, now=NOW)
    assert [(a.consumer_key, a.binding_id) for a in plan] == \
        [("cons-a", "b1"), ("cons-b", "b2")]
