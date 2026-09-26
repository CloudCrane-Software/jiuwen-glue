# coding: utf-8
"""干预状态机单测（WO-0012）：Challenge 三态转移 / 过期 fail-closed / 暂停二态 / 留痕。

与 glue 主包语义交叉校验：canonical_hash 与 jiuwen_glue.decisions.context_hash
同算法；Challenge 过期拒绝与 jiuwen_glue.challenge.ChallengeBoard 同结果。
"""
from __future__ import annotations

import re

import pytest

from console_tui.state import (CH_APPROVED, CH_DENIED, CH_EXPIRED, CH_PENDING,
                               KIND_PAUSE, KIND_RESUME, KIND_STEER, OPERATOR,
                               ChallengeResolutionError, GovernanceError,
                               IllegalTransitionError, apply_pause, audit_event,
                               canonical_hash, resolve_challenge_state)

HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ── canonical_hash ───────────────────────────────────────────────────────────

def test_canonical_hash_format_and_determinism():
    h1 = canonical_hash({"b": 1, "a": "x"})
    h2 = canonical_hash({"a": "x", "b": 1})          # 键序无关
    assert h1 == h2 and HEX64.match(h1)
    assert canonical_hash({"a": "x"}) != h1


def test_canonical_hash_matches_glue_implementation():
    """交叉校验防漂移：与 jiuwen_glue.decisions.context_hash 同一算法。"""
    try:
        from jiuwen_glue.decisions import context_hash as glue_hash
    except ImportError:                              # pragma: no cover
        pytest.skip("jiuwen_glue not importable")
    ctx = {"intervention": "steer", "target": "alpha-planner-01", "text": "先补测试"}
    assert canonical_hash(ctx) == glue_hash(ctx)


# ── Challenge 裁决状态机（a 干预的核心；语义对齐 002 触发器）────────────────

def test_resolve_pending_to_approved():
    state, at, by = resolve_challenge_state(
        CH_PENDING, expires_at=100.0, now=50.0, approved=True, by=OPERATOR)
    assert (state, at, by) == (CH_APPROVED, 50.0, OPERATOR)


def test_resolve_pending_to_denied():
    state, _, _ = resolve_challenge_state(
        CH_PENDING, expires_at=100.0, now=99.999, approved=False, by="duty_officer")
    assert state == CH_DENIED


def test_resolve_expired_rejected_fail_closed():
    with pytest.raises(ChallengeResolutionError, match="expired"):
        resolve_challenge_state(CH_PENDING, expires_at=100.0, now=100.0,
                                approved=True, by=OPERATOR)
    # 临界之前任意接近也必须在 now >= expires_at 才拒绝
    resolve_challenge_state(CH_PENDING, expires_at=100.0, now=99.9999,
                            approved=True, by=OPERATOR)


def test_resolve_terminal_state_rejected():
    for terminal in (CH_APPROVED, CH_DENIED, CH_EXPIRED):
        with pytest.raises(ChallengeResolutionError, match="terminal"):
            resolve_challenge_state(terminal, expires_at=100.0, now=50.0,
                                    approved=True, by=OPERATOR)


def test_resolve_requires_explicit_confirmer():
    with pytest.raises(ChallengeResolutionError, match="confirmer"):
        resolve_challenge_state(CH_PENDING, expires_at=100.0, now=50.0,
                                approved=True, by="")


def test_challenge_semantics_align_with_glue_board():
    """同一过期场景，glue ChallengeBoard 与本状态机给出同一裁决（拒绝）。"""
    try:
        from jiuwen_glue.challenge import ChallengeBoard
        from jiuwen_glue.errors import ChallengeStateError
    except ImportError:                              # pragma: no cover
        pytest.skip("jiuwen_glue not importable")
    t = [0.0]
    board = ChallengeBoard(now=lambda: t[0])
    ch = board.open(who_confirms="user", resource="r", action="a",
                    method="console.ask", ttl_seconds=10.0)
    t[0] = 11.0                                       # 时钟越过 expires_at
    with pytest.raises(ChallengeStateError):         # glue：过期批准无效
        board.resolve(ch.challenge_id, approved=True, by="user")
    with pytest.raises(ChallengeResolutionError):    # console-tui：同拒
        resolve_challenge_state(CH_PENDING, expires_at=10.0, now=11.0,
                                approved=True, by="user")


# ── p 暂停/恢复二态 ─────────────────────────────────────────────────────────

def test_pause_transitions_legal():
    assert apply_pause(False, KIND_PAUSE) == KIND_PAUSE
    assert apply_pause(True, KIND_RESUME) == KIND_RESUME


def test_pause_illegal_transitions_rejected():
    with pytest.raises(IllegalTransitionError, match="already paused"):
        apply_pause(True, KIND_PAUSE)
    with pytest.raises(IllegalTransitionError, match="not paused"):
        apply_pause(False, KIND_RESUME)
    with pytest.raises(IllegalTransitionError):
        apply_pause(False, "kill")                   # 不存在第三态——三级干预写死


# ── 留痕事件 ────────────────────────────────────────────────────────────────

def test_audit_event_shape_and_kinds():
    ev = audit_event(KIND_STEER, target="alpha-planner-01", at=7.0, text="聚焦")
    assert ev["kind"] == KIND_STEER and ev["target"] == "alpha-planner-01"
    assert ev["by"] == OPERATOR and ev["detail"] == {"text": "聚焦"}
    with pytest.raises(GovernanceError, match="unknown audit kind"):
        audit_event("reboot", target="x")            # 不在 s/a/p 语义内 → 拒绝
