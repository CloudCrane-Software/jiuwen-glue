# coding: utf-8
"""记忆晋升管线测试：阶段机 / 质量门 / 脱敏门 UNKNOWN 拒绝 / score_hook / 版本化 / tombstone."""
from __future__ import annotations

import pytest

from jiuwen_glue import (
    GATE_PASS,
    STAGE_PROMOTED,
    STAGE_REJECTED,
    STAGE_SHORTLIST,
    STAGE_WITHDRAWN,
    STAGE_WORKING,
    PromotionLedger,
    PromotionTransitionError,
    UnknownPromotionAssetError,
)


def _nominate(led, asset_key="skill:video-cut", content=None):
    return led.nominate(asset_key, content or {"sop": "cut → encode → publish"},
                        origin_ref="ttse:bank/fact-42")


def _feed_eval(led, asset_key, successes=4, failures=0):
    for _ in range(successes):
        led.record_eval(asset_key, "success")
    for _ in range(failures):
        led.record_eval(asset_key, "failure")


def _redaction_pass(content):
    return GATE_PASS


# ── 阶段机 ───────────────────────────────────────────────────────────────────

def test_stage_machine_forward_path_with_reason_and_evidence(clock):
    led = PromotionLedger(redaction_checker=_redaction_pass, now=clock)
    cand = _nominate(led)
    assert cand.stage == STAGE_WORKING
    _feed_eval(led, cand.asset_key)
    led.transition(cand.promotion_id, STAGE_SHORTLIST, reason="ablation passed",
                   evidence_ref="ev-ablation-1")
    assert cand.stage == STAGE_SHORTLIST
    led.transition(cand.promotion_id, STAGE_PROMOTED, reason="gates passed",
                   evidence_ref="ev-redaction-1")
    assert cand.stage == STAGE_PROMOTED
    kinds = [(t.from_stage, t.to_stage, t.kind) for t in led.history(cand.promotion_id)]
    assert kinds == [(None, STAGE_WORKING, "CREATE"),
                     (STAGE_WORKING, STAGE_SHORTLIST, "TRANSITION"),
                     (STAGE_SHORTLIST, STAGE_PROMOTED, "TRANSITION")]
    # 每次转移带理由与证据引用
    for t in led.history(cand.promotion_id):
        assert t.reason


def test_skip_and_backward_transitions_rejected(clock):
    led = PromotionLedger(redaction_checker=_redaction_pass, now=clock)
    cand = _nominate(led)
    with pytest.raises(PromotionTransitionError):
        led.transition(cand.promotion_id, STAGE_PROMOTED, reason="skip shortlist")
    led.transition(cand.promotion_id, STAGE_SHORTLIST, reason="ok")
    with pytest.raises(PromotionTransitionError):
        led.transition(cand.promotion_id, STAGE_WORKING, reason="backward")
    # 违规留痕
    assert any(t.kind == "VIOLATION" for t in led.history(cand.promotion_id))
    # 无理由的转移被拒绝
    with pytest.raises(PromotionTransitionError):
        led.transition(cand.promotion_id, STAGE_REJECTED, reason="")


def test_content_frozen_after_working(clock):
    led = PromotionLedger(redaction_checker=_redaction_pass, now=clock)
    cand = _nominate(led)
    led.update_content(cand.promotion_id, {"sop": "v2"})
    led.transition(cand.promotion_id, STAGE_SHORTLIST, reason="ok")
    with pytest.raises(PromotionTransitionError):
        led.update_content(cand.promotion_id, {"sop": "v3-drift"})
    assert cand.content == {"sop": "v2"}


# ── 质量门：eval 指标（append-only 单向回流）────────────────────────────────

def test_quality_gate_unknown_when_no_eval_data(clock):
    """样本不足 = UNKNOWN = 拒绝晋升（fail-closed）。"""
    led = PromotionLedger(redaction_checker=_redaction_pass, now=clock)
    cand = _nominate(led)
    led.transition(cand.promotion_id, STAGE_SHORTLIST, reason="ok")
    led.transition(cand.promotion_id, STAGE_PROMOTED, reason="try")
    assert cand.stage == STAGE_REJECTED
    rejects = [t for t in led.history(cand.promotion_id) if t.kind == "GATE_REJECT"]
    assert len(rejects) == 1 and "quality" in rejects[0].reason


def test_quality_gate_blocked_below_threshold(clock):
    led = PromotionLedger(redaction_checker=_redaction_pass, min_success_rate=0.8, now=clock)
    cand = _nominate(led)
    _feed_eval(led, cand.asset_key, successes=2, failures=2)   # 0.5 < 0.8
    led.transition(cand.promotion_id, STAGE_SHORTLIST, reason="ok")
    led.transition(cand.promotion_id, STAGE_PROMOTED, reason="try")
    assert cand.stage == STAGE_REJECTED
    assert "quality gate BLOCKED" in [t.reason for t in led.history(cand.promotion_id)][-1]


def test_metrics_are_append_only(clock):
    led = PromotionLedger(now=clock)
    led.record_eval("skill:x", "success")
    with pytest.raises(ValueError):
        led.record_eval("skill:x", "excellent")
    # 无手改接口（单向回流）
    assert not any(name.startswith("set_") or "override" in name
                   for name in dir(led) if not name.startswith("_"))


# ── 脱敏门：未注入 checker → UNKNOWN → 拒绝（fail-closed 写死）───────────────

def test_redaction_unknown_by_default_rejects_promotion(clock):
    led = PromotionLedger(now=clock)                      # 未注入 redaction checker
    cand = _nominate(led)
    _feed_eval(led, cand.asset_key)
    led.transition(cand.promotion_id, STAGE_SHORTLIST, reason="ok")
    led.transition(cand.promotion_id, STAGE_PROMOTED, reason="try")
    assert cand.stage == STAGE_REJECTED
    last = led.history(cand.promotion_id)[-1]
    assert "redaction" in last.reason and "UNKNOWN" in last.reason


def test_redaction_blocked_rejects(clock):
    def leaky(content):
        return "BLOCKED"
    led = PromotionLedger(redaction_checker=leaky, now=clock)
    cand = _nominate(led, content={"note": "token=sk-xxxxxxxxxxxxxxxx"})
    _feed_eval(led, cand.asset_key)
    led.transition(cand.promotion_id, STAGE_SHORTLIST, reason="ok")
    led.transition(cand.promotion_id, STAGE_PROMOTED, reason="try")
    assert cand.stage == STAGE_REJECTED
    assert cand.redaction_verdict is None


def test_redaction_pass_promotes(clock):
    led = PromotionLedger(redaction_checker=_redaction_pass, now=clock)
    cand = _nominate(led)
    _feed_eval(led, cand.asset_key)
    led.transition(cand.promotion_id, STAGE_SHORTLIST, reason="ok")
    led.transition(cand.promotion_id, STAGE_PROMOTED, reason="gates passed")
    assert cand.stage == STAGE_PROMOTED
    assert cand.redaction_verdict == GATE_PASS


# ── 决策层交叉点：score_hook（§4.7 第六植入点，唯一交叉点）────────────────────

def test_score_hook_participates_when_injected(clock):
    calls = []

    def hook(snapshot):
        calls.append(snapshot)
        return 0.42

    led = PromotionLedger(redaction_checker=_redaction_pass, score_hook=hook,
                          min_score=0.6, now=clock)
    cand = _nominate(led)
    _feed_eval(led, cand.asset_key)
    led.transition(cand.promotion_id, STAGE_SHORTLIST, reason="ok")
    led.transition(cand.promotion_id, STAGE_PROMOTED, reason="try")
    assert cand.stage == STAGE_REJECTED                    # 0.42 < 0.6 → 拒绝
    assert len(calls) == 1                                 # hook 收到候选快照
    assert calls[0]["asset_key"] == cand.asset_key


def test_score_hook_none_is_unknown_fail_closed(clock):
    led = PromotionLedger(redaction_checker=_redaction_pass,
                          score_hook=lambda s: None, now=clock)
    cand = _nominate(led)
    _feed_eval(led, cand.asset_key)
    led.transition(cand.promotion_id, STAGE_SHORTLIST, reason="ok")
    led.transition(cand.promotion_id, STAGE_PROMOTED, reason="try")
    assert cand.stage == STAGE_REJECTED
    assert "score" in led.history(cand.promotion_id)[-1].reason


def test_score_hook_raising_is_fail_closed(clock):
    def broken(snapshot):
        raise RuntimeError("provider down")
    led = PromotionLedger(redaction_checker=_redaction_pass, score_hook=broken, now=clock)
    cand = _nominate(led)
    _feed_eval(led, cand.asset_key)
    led.transition(cand.promotion_id, STAGE_SHORTLIST, reason="ok")
    led.transition(cand.promotion_id, STAGE_PROMOTED, reason="try")
    assert cand.stage == STAGE_REJECTED


def test_default_no_score_hook_walks_pure_quality_gates(clock):
    """score_hook 默认 None：走纯质量门 + 脱敏门，不打分。"""
    led = PromotionLedger(redaction_checker=_redaction_pass, now=clock)
    cand = _nominate(led)
    _feed_eval(led, cand.asset_key)
    led.transition(cand.promotion_id, STAGE_SHORTLIST, reason="ok")
    led.transition(cand.promotion_id, STAGE_PROMOTED, reason="gates passed")
    assert cand.stage == STAGE_PROMOTED
    assert cand.score is None                              # 从未打分


# ── 版本化入库 + tombstone 撤回 ──────────────────────────────────────────────

def test_promoted_versions_are_monotonic(clock):
    led = PromotionLedger(redaction_checker=_redaction_pass, now=clock)
    v1 = _nominate(led, content={"sop": "v1"})
    _feed_eval(led, v1.asset_key)
    led.transition(v1.promotion_id, STAGE_SHORTLIST, reason="ok")
    led.transition(v1.promotion_id, STAGE_PROMOTED, reason="gates")
    v2 = _nominate(led, content={"sop": "v2-improved"})
    led.transition(v2.promotion_id, STAGE_SHORTLIST, reason="ok")
    led.transition(v2.promotion_id, STAGE_PROMOTED, reason="gates")
    assert (v1.version, v2.version) == (1, 2)
    assert v1.promoted_version_key == "skill:video-cut@v1"
    assert v2.promoted_version_key == "skill:video-cut@v2"
    assert led.org_versions_of("skill:video-cut") == {1: v1.promotion_id,
                                                      2: v2.promotion_id}


def test_tombstone_withdraws_without_version_reuse(clock):
    led = PromotionLedger(redaction_checker=_redaction_pass, now=clock)
    cand = _nominate(led)
    _feed_eval(led, cand.asset_key)
    led.transition(cand.promotion_id, STAGE_SHORTLIST, reason="ok")
    led.transition(cand.promotion_id, STAGE_PROMOTED, reason="gates")
    led.tombstone(cand.promotion_id, reason="policy change: asset obsolete")
    assert cand.stage == STAGE_WITHDRAWN
    assert led.is_tombstoned(cand.asset_key, cand.version)
    assert led.latest_live_version(cand.asset_key) is None
    # 墓碑后新提名晋升 → 版本号继续单调（不复用 1）
    nxt = _nominate(led, content={"sop": "v2"})
    _feed_eval(led, nxt.asset_key)
    led.transition(nxt.promotion_id, STAGE_SHORTLIST, reason="ok")
    led.transition(nxt.promotion_id, STAGE_PROMOTED, reason="gates")
    assert nxt.version == 2
    assert led.latest_live_version(cand.asset_key) == 2
    # 非晋升态不可墓碑（working 阶段的候选没有已入库版本可撤）
    fresh = _nominate(led, content={"sop": "v3"})
    with pytest.raises(PromotionTransitionError):
        led.tombstone(fresh.promotion_id, reason="not promoted yet")
    assert any(t.kind == "VIOLATION" for t in led.history(fresh.promotion_id))


def test_unknown_candidate_query(clock):
    led = PromotionLedger(now=clock)
    with pytest.raises(UnknownPromotionAssetError):
        led.get("nope")
