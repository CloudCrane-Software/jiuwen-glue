# coding: utf-8
"""line.Selection 择优骨架测试：公式、护栏（只收 PASS / N>5 / 空集）、winner_ref、R1 平局豁免."""
from __future__ import annotations

import pytest

from jiuwen_glue import (
    MAX_CANDIDATES,
    SIGNAL_WEIGHTS,
    SelectionCandidate as Candidate,
    SelectionRejectedError,
    SelectionResult,
    selection_score as score,
    select_winner as select,
)


def _cand(ref: str, *, eval_=0.9, mutation=0.8, concise=0.5, diff=0.7, budget=0.6,
          run_id: str = "run-1") -> Candidate:
    return Candidate(
        candidate_ref=ref, gate_run_id=run_id,
        signals={"eval": eval_, "mutation": mutation, "conciseness": concise,
                 "differential_consensus": diff, "budget": budget},
    )


# ── 公式 ─────────────────────────────────────────────────────────────────────

def test_weights_sum_to_one():
    """§5.4 五权重之和必须为 1（公式不变式）。"""
    assert abs(sum(SIGNAL_WEIGHTS.values()) - 1.0) < 1e-12
    assert set(SIGNAL_WEIGHTS) == {"eval", "mutation", "conciseness",
                                   "differential_consensus", "budget"}


def test_score_weighted_sum():
    """纯机器信号加权：eval 0.45 主导（LLM 评分不进回路，公式只认五信号）。"""
    assert score({"eval": 1.0, "mutation": 0.0, "conciseness": 0.0,
                  "differential_consensus": 0.0, "budget": 0.0}) == pytest.approx(0.45)
    assert score({"eval": 0.0, "mutation": 1.0, "conciseness": 0.0,
                  "differential_consensus": 0.0, "budget": 0.0}) == pytest.approx(0.20)
    assert score({"eval": 1.0, "mutation": 1.0, "conciseness": 1.0,
                  "differential_consensus": 1.0, "budget": 1.0}) == pytest.approx(1.0)


def test_score_missing_signal_rejected():
    """信号缺失 → 拒绝（fail-closed，不猜不补）。"""
    with pytest.raises(SelectionRejectedError):
        score({"eval": 1.0})  # 缺其余四信号


def test_score_unknown_and_out_of_range_rejected():
    with pytest.raises(SelectionRejectedError):
        score({"eval": 1.0, "mutation": 0.5, "conciseness": 0.5,
               "differential_consensus": 0.5, "budget": 0.5, "llm_score": 0.9})  # 多余信号
    with pytest.raises(SelectionRejectedError):
        score({"eval": 1.5, "mutation": 0.0, "conciseness": 0.0,
               "differential_consensus": 0.0, "budget": 0.0})  # 越界
    with pytest.raises(SelectionRejectedError):
        score({"eval": "0.9", "mutation": 0.0, "conciseness": 0.0,
               "differential_consensus": 0.0, "budget": 0.0})  # 非数值


# ── 护栏 ─────────────────────────────────────────────────────────────────────

def test_select_returns_winner_ref():
    """最高分者胜出，winner_ref + gate_run_id 引用随结果留痕。"""
    result = select(
        [_cand("cand-a", eval_=0.5), _cand("cand-b", eval_=0.9, run_id="run-2")],
        gate_verdicts={"cand-a": "PASS", "cand-b": "PASS"},
    )
    assert isinstance(result, SelectionResult)
    assert result.winner_ref == "cand-b"
    assert result.winner_gate_run_id == "run-2"
    assert result.n_candidates == 2
    assert set(result.scores) == {"cand-a", "cand-b"}


def test_select_rejects_non_pass_candidate():
    """只接收已 PASS 候选——UNKNOWN/BLOCKED/缺 verdict 一律拒绝（fail-closed）。"""
    for verdict in ("UNKNOWN", "BLOCKED"):
        with pytest.raises(SelectionRejectedError):
            select([_cand("cand-a")], gate_verdicts={"cand-a": verdict})
    with pytest.raises(SelectionRejectedError):
        select([_cand("cand-a")], gate_verdicts={})  # 无门控记录


def test_select_rejects_empty_and_overload():
    """空集不放行；N>5 拒绝加载（权重复算护栏）。"""
    with pytest.raises(SelectionRejectedError):
        select([], gate_verdicts={})
    cands = [_cand(f"cand-{i}") for i in range(MAX_CANDIDATES + 1)]  # 6 个
    verdicts = {f"cand-{i}": "PASS" for i in range(MAX_CANDIDATES + 1)}
    with pytest.raises(SelectionRejectedError):
        select(cands, gate_verdicts=verdicts)
    # N=5 恰好放行
    result = select(cands[:MAX_CANDIDATES], gate_verdicts=verdicts)
    assert result.n_candidates == MAX_CANDIDATES


def test_select_tie_break_is_deterministic():
    """R1 平局豁免：同分按 candidate_ref 字典序，确定性可复算，tie_broken 留痕。"""
    sig = dict(eval_=0.8, mutation=0.7, concise=0.6, diff=0.5, budget=0.4)
    cands = [_cand("cand-c", **sig), _cand("cand-a", **sig), _cand("cand-b", **sig)]
    verdicts = {c.candidate_ref: "PASS" for c in cands}
    r1 = select(cands, gate_verdicts=verdicts)
    r2 = select(list(reversed(cands)), gate_verdicts=verdicts)  # 输入顺序无关
    assert r1.winner_ref == "cand-a"
    assert r2.winner_ref == "cand-a"
    assert r1.tie_broken is True and r2.tie_broken is True


def test_select_no_tie_when_scores_differ():
    result = select([_cand("cand-a", eval_=0.4), _cand("cand-b", eval_=0.9)],
                    gate_verdicts={"cand-a": "PASS", "cand-b": "PASS"})
    assert result.tie_broken is False


def test_select_rejects_bad_signals_on_pass_candidate():
    """已 PASS 但信号非法 → 拒绝（门控不管信号合法性，择优层自守）。"""
    bad = Candidate(candidate_ref="cand-a", gate_run_id="run-1",
                    signals={"eval": 0.9})  # 缺四信号
    with pytest.raises(SelectionRejectedError):
        select([bad], gate_verdicts={"cand-a": "PASS"})
