# coding: utf-8
"""消融验证测试：配对比较 / 四态 verdict / INCONCLUSIVE 合法结论 / 证据不可重跑."""
from __future__ import annotations

import dataclasses

import pytest

from jiuwen_glue import (
    VERDICT_IMPROVED,
    VERDICT_INCONCLUSIVE,
    VERDICT_NEUTRAL,
    VERDICT_REGRESSED,
    ABLATION_VERDICTS,
    AblationArm,
    AblationError,
    AblationExperiment,
    sign_test_p,
)

INPUTS = list(range(10))


def _experiment(*, control_out=0.0, treatment_out=1.0, inputs=None,
                min_samples=10, alpha=0.05):
    return AblationExperiment(
        inputs=inputs if inputs is not None else INPUTS,
        control=AblationArm(name="control", ref="promoted:skill:video-cut@v2",
                            behavior=lambda item: control_out),
        treatment=AblationArm(name="treatment", ref="skill:video-cut@v3-candidate",
                              behavior=lambda item: treatment_out),
        min_samples=min_samples, alpha=alpha)


def _score_evaluator(item, output):
    return float(output)


# ── 四态 verdict ─────────────────────────────────────────────────────────────

def test_improved_significant(clock):
    result = _experiment().run(_score_evaluator)
    assert result.verdict == VERDICT_IMPROVED
    assert result.wins == 10 and result.losses == 0 and result.ties == 0
    assert result.mean_delta == 1.0
    assert result.p_value < 0.05
    assert result.n_pairs == 10 and result.n_errors == 0
    assert "significant improvement" in result.reason


def test_neutral_when_not_significant(clock):
    """5 胜 +0.1、5 平：n_nonzero=5，符号检验 p=0.0625 > alpha → 持平（未达显著）。"""
    outs = {i: (1.0 if i % 2 == 0 else 0.9) for i in INPUTS}   # 5 胜 +0.1, 5 平 0
    exp = AblationExperiment(
        inputs=INPUTS,
        control=AblationArm(name="c", ref="v2", behavior=lambda i: 0.9),
        treatment=AblationArm(name="t", ref="v3-candidate",
                              behavior=lambda i: outs[i]),
        min_samples=10, alpha=0.05)
    result = exp.run(_score_evaluator)
    assert result.verdict == VERDICT_NEUTRAL
    assert result.wins == 5 and result.losses == 0 and result.ties == 5
    assert "no significant difference" in result.reason


def test_regressed_significant(clock):
    result = _experiment(control_out=1.0, treatment_out=0.0).run(_score_evaluator)
    assert result.verdict == VERDICT_REGRESSED
    assert result.losses == 10 and result.mean_delta == -1.0
    assert "significant regression" in result.reason


def test_inconclusive_insufficient_samples_is_a_legal_rejection(clock):
    result = _experiment(inputs=[1, 2, 3], min_samples=10).run(_score_evaluator)
    assert result.verdict == VERDICT_INCONCLUSIVE
    assert "insufficient samples" in result.reason
    assert result.p_value == 1.0            # 不可判定不报告显著性
    assert result.evidence_ref.startswith("ablation://")


def test_inconclusive_on_evaluator_error_fail_closed(clock):
    def broken_evaluator(item, output):
        if item == 3:
            raise RuntimeError("observer crashed")
        return float(output)

    result = _experiment().run(broken_evaluator)
    assert result.verdict == VERDICT_INCONCLUSIVE
    assert result.n_errors == 1 and result.n_pairs == 9
    assert "observer crashed" in result.reason


def test_inconclusive_on_nonfinite_score(clock):
    def nan_evaluator(item, output):
        return float("nan") if item == 7 else float(output)

    result = _experiment().run(nan_evaluator)
    assert result.verdict == VERDICT_INCONCLUSIVE
    assert result.n_errors == 1


# ── 配对语义与证据 ───────────────────────────────────────────────────────────

def test_paired_same_input_set_runs_both_arms(clock):
    calls = []

    def recording_evaluator(item, output):
        calls.append((item, output))
        return float(output)

    result = _experiment(control_out=0.0, treatment_out=1.0).run(recording_evaluator)
    # 同一输入集双跑：n 个输入 → 2n 次 evaluator 调用，逐对 (control, treatment)
    assert len(calls) == 20
    assert [c[0] for c in calls] == sorted([c[0] for c in calls])
    assert result.pairs[3].item_index == 3 and result.pairs[3].delta == 1.0
    assert all(p.delta == p.treatment - p.control for p in result.pairs)


def test_result_is_frozen_and_single_run(clock):
    exp = _experiment()
    result = exp.run(_score_evaluator)
    assert exp.result is result
    with pytest.raises(AblationError):
        exp.run(_score_evaluator)                 # 结论不可重跑覆盖
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.verdict = VERDICT_NEUTRAL          # type: ignore[misc]


def test_result_carries_both_arm_refs_as_evidence(clock):
    result = _experiment().run(_score_evaluator)
    assert result.control_ref == "promoted:skill:video-cut@v2"
    assert result.treatment_ref == "skill:video-cut@v3-candidate"
    assert result.evidence_ref == f"ablation://{result.experiment_id}"


# ── 统计口径：精确符号检验（零依赖）──────────────────────────────────────────

def test_sign_test_p_exact_values():
    assert sign_test_p(0, 0) == 1.0
    assert abs(sign_test_p(10, 0) - 2 * (1 / 2 ** 10)) < 1e-12
    assert abs(sign_test_p(6, 4) - 0.75390625) < 1e-9      # 2*P(X<=4 | n=10)
    assert sign_test_p(5, 5) == 1.0
    with pytest.raises(AblationError):
        sign_test_p(-1, 0)


def test_alpha_boundary_all_wins_small_n(clock):
    """n=6 全胜：p=2/64=0.03125 < 0.05 → 可判 IMPROVED（alpha 边界可用性）。"""
    result = _experiment(inputs=[1, 2, 3, 4, 5, 6], min_samples=6).run(_score_evaluator)
    assert result.verdict == VERDICT_IMPROVED
    assert abs(result.p_value - 2 / 64) < 1e-12


# ── 装配校验 ─────────────────────────────────────────────────────────────────

def test_arm_and_experiment_validation():
    with pytest.raises(AblationError):
        AblationArm(name="control", ref="", behavior=lambda x: x)
    with pytest.raises(AblationError):
        AblationArm(name="control", ref="r", behavior="not-callable")  # type: ignore[arg-type]
    with pytest.raises(AblationError):
        AblationExperiment(inputs=INPUTS,
                           control=AblationArm(name="c", ref="same", behavior=lambda x: x),
                           treatment=AblationArm(name="t", ref="same", behavior=lambda x: x))
    with pytest.raises(AblationError):
        AblationExperiment(inputs=INPUTS,
                           control=AblationArm(name="c", ref="a", behavior=lambda x: x),
                           treatment=AblationArm(name="t", ref="b", behavior=lambda x: x),
                           alpha=1.5)
    with pytest.raises(AblationError):
        _experiment().run("not-callable")         # type: ignore[arg-type]


def test_verdicts_cover_four_states():
    assert ABLATION_VERDICTS == (
        VERDICT_IMPROVED, VERDICT_NEUTRAL, VERDICT_REGRESSED, VERDICT_INCONCLUSIVE)
