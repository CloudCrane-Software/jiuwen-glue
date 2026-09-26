# coding: utf-8
"""准入记录测试：硬规则矩阵（消融 2 态 × 门控 3 态 = 6 组合）+ INCONCLUSIVE 合法拒绝 /
Spec 版本与证据绑定 / append-only / skill-pack review 段外发形状."""
from __future__ import annotations

import dataclasses
import re

import pytest

from jiuwen_glue import (
    BACKEND_EVAL_GATE,
    DECISION_ALLOWED,
    DECISION_HELD,
    DECISION_REJECTED,
    VERDICT_IMPROVED,
    VERDICT_INCONCLUSIVE,
    VERDICT_NEUTRAL,
    VERDICT_REGRESSED,
    AblationArm,
    AblationExperiment,
    CheckSpec,
    GuardrailRunStore,
    GuardrailSpec,
    AdmissionError,
    AdmissionLedger,
    export_to_skillpack,
    guardrail_verdict_of,
    OUTCOME_PASS,
    OUTCOME_BLOCKED,
)
from jiuwen_glue.guardrail import VERDICT_PASS, VERDICT_BLOCKED, VERDICT_UNKNOWN

INPUTS = list(range(10))
CANDIDATE = "skill:video-cut@v3-candidate"


# ── 装配助手：真实 GuardrailRun（经 store 走完整协议）与真实消融结论 ─────────

def _spec():
    return GuardrailSpec(
        action="experience.admit", resource=CANDIDATE,
        agent_identity_ref="agent:glue/inst-1/task-1", spec_version="spec-v3",
        checks=(CheckSpec(check_id="ablation-gate", backend=BACKEND_EVAL_GATE),))


def _guardrail_run(*, verdict):
    """经 GuardrailRunStore 走五步协议，产出指定聚合 verdict 的 run。"""
    store = GuardrailRunStore()
    run = store.create_run(_spec())
    if verdict == VERDICT_PASS:
        store.submit_check(run.run_id, "ablation-gate", OUTCOME_PASS,
                           evidence_ref="ev-evalgate-1")
        store.finalize(run.run_id, seal_ref="seal-admit-1")
    elif verdict == VERDICT_BLOCKED:
        store.submit_check(run.run_id, "ablation-gate", OUTCOME_BLOCKED,
                           evidence_ref="ev-evalgate-blocked")
        store.finalize(run.run_id, seal_ref="seal-admit-2")
    else:  # UNKNOWN：协议未闭合（OPEN，未 finalize）
        pass
    return run


def _ablation_result(verdict):
    """用真实消融实验产出指定 verdict 的结论（证据引用真实可溯）。"""
    if verdict == VERDICT_IMPROVED:
        c_out, t_out = 0.0, 1.0
    elif verdict == VERDICT_REGRESSED:
        c_out, t_out = 1.0, 0.0
    elif verdict == VERDICT_NEUTRAL:
        c_out, t_out = 0.9, 0.9
    else:  # INCONCLUSIVE：样本不足
        exp = AblationExperiment(
            inputs=[1, 2], min_samples=10,
            control=AblationArm(name="c", ref="promoted:skill:video-cut@v2",
                                behavior=lambda i: 0.0),
            treatment=AblationArm(name="t", ref=CANDIDATE, behavior=lambda i: 1.0))
        return exp.run(lambda item, out: float(out))
    exp = AblationExperiment(
        inputs=INPUTS, min_samples=10,
        control=AblationArm(name="c", ref="promoted:skill:video-cut@v2",
                            behavior=lambda i: c_out),
        treatment=AblationArm(name="t", ref=CANDIDATE, behavior=lambda i: t_out))
    return exp.run(lambda item, out: float(out))


# ── 硬规则矩阵：消融 {通过, 未通过} × 门控 {PASS, BLOCKED, UNKNOWN} = 6 组合 ──

@pytest.mark.parametrize("abl_verdict,gate_verdict,expected", [
    (VERDICT_IMPROVED, VERDICT_PASS, DECISION_ALLOWED),
    (VERDICT_IMPROVED, VERDICT_BLOCKED, DECISION_REJECTED),
    (VERDICT_IMPROVED, VERDICT_UNKNOWN, DECISION_HELD),
    (VERDICT_REGRESSED, VERDICT_PASS, DECISION_REJECTED),
    (VERDICT_REGRESSED, VERDICT_BLOCKED, DECISION_REJECTED),
    (VERDICT_REGRESSED, VERDICT_UNKNOWN, DECISION_HELD),
])
def test_hard_rule_matrix(clock, abl_verdict, gate_verdict, expected):
    ledger = AdmissionLedger(now=clock)
    rec = ledger.admit(CANDIDATE, _guardrail_run(verdict=gate_verdict),
                       _ablation_result(abl_verdict))
    assert rec.decision == expected
    assert rec.candidate_ref == CANDIDATE
    assert rec.guardrail_verdict == gate_verdict
    assert rec.ablation_verdict == abl_verdict


def test_inconclusive_ablation_with_pass_guardrail_is_a_legal_rejection(clock):
    ledger = AdmissionLedger(now=clock)
    rec = ledger.admit(CANDIDATE, _guardrail_run(verdict=VERDICT_PASS),
                       _ablation_result(VERDICT_INCONCLUSIVE))
    assert rec.decision == DECISION_REJECTED
    assert "legal rejection" in rec.reason


def test_held_reason_references_readmission_not_final(clock):
    ledger = AdmissionLedger(now=clock)
    rec = ledger.admit(CANDIDATE, _guardrail_run(verdict=VERDICT_UNKNOWN),
                       _ablation_result(VERDICT_IMPROVED))
    assert rec.decision == DECISION_HELD
    assert "re-admission" in rec.reason


# ── 记录绑定 Spec 版本与证据引用 ─────────────────────────────────────────────

def test_record_binds_spec_version_seal_and_evidence(clock):
    ledger = AdmissionLedger(now=clock)
    abl = _ablation_result(VERDICT_IMPROVED)
    run = _guardrail_run(verdict=VERDICT_PASS)
    rec = ledger.admit(CANDIDATE, run, abl)
    assert rec.guardrail_spec_version == "spec-v3"       # WO-0007：生效绑定 Spec 版本
    assert rec.guardrail_seal_ref == "seal-admit-1"
    assert rec.guardrail_run_ref == run.run_id
    assert rec.ablation_experiment_id == abl.experiment_id
    assert rec.ablation_evidence_ref == abl.evidence_ref
    assert rec.ablation_evidence_ref.startswith("ablation://")
    assert rec.tenant_id == "t0"


def test_verdict_of_mirrors_store_gate_semantics(clock):
    store = GuardrailRunStore()
    run = store.create_run(_spec())
    assert guardrail_verdict_of(run) == VERDICT_UNKNOWN          # OPEN → UNKNOWN
    store.submit_check(run.run_id, "ablation-gate", OUTCOME_PASS, evidence_ref="e")
    store.finalize(run.run_id, seal_ref="s")
    assert guardrail_verdict_of(run) == VERDICT_PASS             # FINALIZED + 全 PASS
    store.void(run.run_id, reason="production changed")
    assert guardrail_verdict_of(run) == VERDICT_UNKNOWN          # VOID → UNKNOWN
    with pytest.raises(AdmissionError):
        guardrail_verdict_of("not-a-run")                        # type: ignore[arg-type]


# ── 默认接受集与放宽（放宽本身是门禁配置变更，走 PR）────────────────────────

def test_neutral_ablation_rejected_by_default_and_relaxable(clock):
    ledger = AdmissionLedger(now=clock)
    rec = ledger.admit(CANDIDATE, _guardrail_run(verdict=VERDICT_PASS),
                       _ablation_result(VERDICT_NEUTRAL))
    assert rec.decision == DECISION_REJECTED             # 默认只收 IMPROVED
    relaxed = AdmissionLedger(now=clock,
                              accept_ablation_verdicts=(VERDICT_IMPROVED, VERDICT_NEUTRAL))
    rec2 = relaxed.admit(CANDIDATE, _guardrail_run(verdict=VERDICT_PASS),
                         _ablation_result(VERDICT_NEUTRAL))
    assert rec2.decision == DECISION_ALLOWED
    with pytest.raises(AdmissionError):
        AdmissionLedger(accept_ablation_verdicts=("SUPERIOR",))


# ── append-only ─────────────────────────────────────────────────────────────

def test_ledger_is_append_only_and_records_frozen(clock):
    ledger = AdmissionLedger(now=clock)
    rec = ledger.admit(CANDIDATE, _guardrail_run(verdict=VERDICT_PASS),
                       _ablation_result(VERDICT_IMPROVED))
    # 无 update/delete/撤销接口（append-only）
    public = [n for n in dir(ledger) if not n.startswith("_")]
    assert not any(w in n.lower() for n in public
                   for w in ("update", "delete", "remove", "revoke", "set_"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.decision = DECISION_REJECTED                 # type: ignore[misc]
    assert ledger.get(rec.admission_id) is rec
    assert [r.admission_id for r in ledger.all()] == [rec.admission_id]
    assert ledger.by_decision(DECISION_ALLOWED) == [rec]
    with pytest.raises(AdmissionError):
        ledger.get("nope")
    with pytest.raises(AdmissionError):
        ledger.by_decision("MAYBE")


def test_admit_argument_validation(clock):
    ledger = AdmissionLedger(now=clock)
    with pytest.raises(AdmissionError):
        ledger.admit("", _guardrail_run(verdict=VERDICT_PASS),
                     _ablation_result(VERDICT_IMPROVED))
    with pytest.raises(AdmissionError):
        ledger.admit(CANDIDATE, "not-a-run",             # type: ignore[arg-type]
                     _ablation_result(VERDICT_IMPROVED))
    with pytest.raises(AdmissionError):
        ledger.admit(CANDIDATE, _guardrail_run(verdict=VERDICT_PASS),
                     "not-an-ablation")                  # type: ignore[arg-type]


# ── 外发：skill-pack OKF review 段形状 ───────────────────────────────────────

def test_export_shape_matches_okf_review_section(clock):
    ledger = AdmissionLedger(now=clock)
    rec = ledger.admit(CANDIDATE, _guardrail_run(verdict=VERDICT_PASS),
                       _ablation_result(VERDICT_IMPROVED))
    pack_review = export_to_skillpack(rec)
    assert set(pack_review.keys()) == {"gate_ref", "evidence_ref", "approved_at"}
    assert pack_review["gate_ref"] == f"guardrail://admission/{rec.admission_id}"
    assert pack_review["gate_ref"].startswith("guardrail://")   # okf-spec §6 格式强制
    assert pack_review["evidence_ref"] == rec.ablation_evidence_ref
    assert pack_review["evidence_ref"]                          # 非空（okf-spec §6）
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", pack_review["approved_at"])
    assert pack_review["approved_at"] == "2023-11-14"           # FakeClock 起点 1.7e9


def test_export_rejects_non_allowed_records(clock):
    ledger = AdmissionLedger(now=clock)
    held = ledger.admit(CANDIDATE, _guardrail_run(verdict=VERDICT_UNKNOWN),
                        _ablation_result(VERDICT_IMPROVED))
    with pytest.raises(AdmissionError):
        export_to_skillpack(held)
    rejected = ledger.admit(CANDIDATE, _guardrail_run(verdict=VERDICT_PASS),
                            _ablation_result(VERDICT_REGRESSED))
    with pytest.raises(AdmissionError):
        export_to_skillpack(rejected)
    with pytest.raises(AdmissionError):
        export_to_skillpack("not-a-record")              # type: ignore[arg-type]
