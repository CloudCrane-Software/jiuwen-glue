# coding: utf-8
"""节点池注册协议测试（PROP-0003 / v1.7 §12.6）— WO-0011.

覆盖：注册校验（JWT 声明缺失/过期/主体与信任等级不符 → 拒绝）、在线窗口
fail-closed 解析、心跳超窗 → STALE（自取制租约过期的兜底死亡语义）、
STALE 只能重注册复活、注销即出池、可选 SQL 持久化钩子。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jiuwen_glue import TRUST_TRUSTED, TRUST_UNTRUSTED
from jiuwen_glue.fleet import (
    NODE_ACTIVE,
    NODE_DEREGISTERED,
    NODE_STALE,
    AttestationError,
    FleetRegistry,
    NodeStateError,
    OnlineWindowError,
    RegistryPersistence,
    UnknownNodeError,
    parse_online_window,
)

from fleet_utils import make_attestation, make_jwt, make_registration


def _registry(clock):
    return FleetRegistry(now=clock)


def _registered(clock, node_id="node-1", **kwargs):
    registry = FleetRegistry(now=clock)
    registry.register(make_registration(node_id, now=clock.now_value, **kwargs))
    return registry


# ── 注册校验（fail-closed：协议不满足即拒绝）──────────────────────────────────

def test_register_valid_node_is_active_and_capacity_reused(clock):
    reg = make_registration("node-1", now=clock.now_value)
    record = _registry(clock).register(reg)
    assert record.status == NODE_ACTIVE
    assert record.capacity is reg.capacity            # 复用 NodeCapacity，不复制
    assert record.registered_at == clock.now_value
    assert record.last_seen_at == clock.now_value


def test_register_audit_trail_records_event(clock):
    registry = _registered(clock)
    events = [e["event"] for e in registry.audit]
    assert events == ["REGISTER"]


def test_register_missing_exp_claim_rejected(clock):
    """必填 claims 缺失（exp）→ 拒绝（协议级；签名验证归真实 bao，[待真实 bao 接入]）。"""
    with pytest.raises(AttestationError):
        make_attestation({"sub": "node-1"},          # 无 exp
                         jwt_ref="bao://audit/jwt/node-1", now=clock.now_value)


def test_register_missing_sub_claim_rejected(clock):
    with pytest.raises(AttestationError):
        make_attestation({"exp": int(clock.now_value) + 600},   # 无 sub
                         jwt_ref="bao://audit/jwt/node-1", now=clock.now_value)


def test_register_expired_jwt_rejected(clock):
    """JWT 已过期 → 拒绝注册。"""
    with pytest.raises(AttestationError):
        make_attestation({"sub": "node-1", "exp": int(clock.now_value) - 1},
                         jwt_ref="bao://audit/jwt/node-1", now=clock.now_value)


def test_register_rechecks_expiry_with_registry_clock(clock):
    """签发时未过期、注册时注册方时钟已越过 exp → 注册侧仍拒绝（惰性复检）。"""
    reg = make_registration("node-1", now=clock.now_value,
                            claims={"exp": int(clock.now_value) + 10})
    clock.advance(11)
    with pytest.raises(AttestationError):
        _registry(clock).register(reg)


def test_register_malformed_jwt_rejected(clock):
    from jiuwen_glue.fleet import NodeAttestation
    with pytest.raises(AttestationError):
        NodeAttestation.from_jwt(make_jwt({"sub": "n"}, segments=2),
                                 jwt_ref="bao://audit/jwt/x", now=clock.now_value)
    with pytest.raises(AttestationError):
        NodeAttestation.from_jwt(make_jwt({}, payload_text="@@@not-base64@@@"),
                                 jwt_ref="bao://audit/jwt/x", now=clock.now_value)


def test_register_trust_level_conflict_rejected(clock):
    """attestation 信任等级与 capacity 声明不一致 → 拒绝（声明自洽性把关）。"""
    reg = make_registration("node-1", now=clock.now_value,
                            trust_level=TRUST_TRUSTED,
                            claims={"trust_level": TRUST_UNTRUSTED})
    with pytest.raises(AttestationError):
        _registry(clock).register(reg)


def test_register_subject_mismatch_rejected(clock):
    """JWT 主体与 node_id 不符 → 拒绝（防声明嫁接到别的节点身份）。"""
    reg = make_registration("node-1", now=clock.now_value,
                            claims={"sub": "someone-else"})
    with pytest.raises(AttestationError):
        _registry(clock).register(reg)


# ── 在线窗口（注册入口 fail-closed；调度消费 is_online）───────────────────────

def test_online_window_unparseable_rejected_at_registration(clock):
    """窗口声明不可解析 → 注册入口拒绝（fail-closed，不让调度器猜）。"""
    reg = make_registration("node-1", now=clock.now_value,
                            online_window="whenever")
    with pytest.raises(OnlineWindowError):
        _registry(clock).register(reg)
    with pytest.raises(OnlineWindowError):
        parse_online_window("09:00-09:00")            # 退化窗口（start == end）


def test_online_window_semantics():
    offset8 = timezone(timedelta(hours=8))
    online_ts = datetime(2026, 1, 5, 10, 0, tzinfo=offset8).timestamp()
    offline_ts = datetime(2026, 1, 5, 20, 0, tzinfo=offset8).timestamp()
    win = parse_online_window("09:00-18:00+08")
    assert win.is_online(online_ts)
    assert not win.is_online(offline_ts)
    overnight = parse_online_window("22:00-06:00")    # 跨午夜窗口
    night = datetime(2026, 1, 5, 23, 30, tzinfo=timezone.utc).timestamp()
    noon = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    assert overnight.is_online(night)
    assert not overnight.is_online(noon)
    assert parse_online_window("always").is_online(0.0)


# ── 心跳与 STALE（自取制租约过期的兜底死亡语义）───────────────────────────────

def test_heartbeat_updates_and_sweep_marks_stale(clock):
    registry = _registered(clock, heartbeat_ttl_seconds=300.0)
    clock.advance(100)
    record = registry.heartbeat("node-1")
    assert record.last_seen_at == clock.now_value
    clock.advance(301)                                # ttl=300s 内无心跳
    assert registry.sweep() == ["node-1"]
    assert registry.get("node-1").status == NODE_STALE
    assert registry.active_nodes() == []
    assert [e["event"] for e in registry.audit] == ["REGISTER", "HEARTBEAT", "STALE"]


def test_lazy_stale_on_get_without_sweep(clock):
    """不跑 sweep、直接 get 也判 STALE（惰性过期，与 leases 同风格）。"""
    registry = _registered(clock, heartbeat_ttl_seconds=300.0)
    clock.advance(301)
    assert registry.get("node-1").status == NODE_STALE


def test_stale_node_heartbeat_refused_reregister_reactivates(clock):
    """STALE 不许悄悄复活：心跳拒绝；唯一回池路径 = 带新 attestation 重注册。"""
    registry = _registered(clock, heartbeat_ttl_seconds=300.0)
    clock.advance(301)
    registry.get("node-1")
    with pytest.raises(NodeStateError):
        registry.heartbeat("node-1")
    record = registry.register(make_registration("node-1", now=clock.now_value))
    assert record.status == NODE_ACTIVE
    registry.heartbeat("node-1")                      # 复活后心跳恢复正常
    assert registry.get("node-1").status == NODE_ACTIVE


def test_heartbeat_unknown_node_raises(clock):
    with pytest.raises(UnknownNodeError):
        _registry(clock).heartbeat("ghost")


def test_deregister_excludes_node_and_refuses_heartbeat(clock):
    registry = _registered(clock)
    record = registry.deregister("node-1", reason="decommissioned")
    assert record.status == NODE_DEREGISTERED
    assert registry.active_nodes() == []
    with pytest.raises(NodeStateError):
        registry.heartbeat("node-1")


def test_persistence_hooks_called(clock):
    """可选 SQL 持久化钩子按事件被调用（glue.node 对齐；真实实现属后续工单）。"""

    class Spy(RegistryPersistence):
        def __init__(self):
            self.calls = []

        def upsert_node(self, record, registration):
            self.calls.append(("upsert", record.node_id, record.status))

        def record_heartbeat(self, node_id, at):
            self.calls.append(("heartbeat", node_id, at))

        def update_status(self, node_id, status, at):
            self.calls.append(("status", node_id, status))

    spy = Spy()
    registry = FleetRegistry(now=clock, persistence=spy)
    registry.register(make_registration("node-1", now=clock.now_value,
                                        heartbeat_ttl_seconds=10.0))
    registry.heartbeat("node-1")
    clock.advance(11)
    registry.sweep()
    registry.deregister("node-1")
    assert spy.calls == [
        ("upsert", "node-1", NODE_ACTIVE),
        ("heartbeat", "node-1", clock.now_value - 11.0),
        ("status", "node-1", NODE_STALE),
        ("status", "node-1", NODE_DEREGISTERED),
    ]
