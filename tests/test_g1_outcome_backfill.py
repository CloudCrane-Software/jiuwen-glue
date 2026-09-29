"""G-1 outcome 回填全链路（v2.0 §4.2 / W-02 缺口 G-1）。

语义：append-only 账本内的受控单字段终态登记——NULL=未回填，首写即冻结，
无改写路径；接线点=DecisionLayer.record_outcome（三原语调用方显式回填）。
"""
import pytest

from jiuwen_glue.decisions import DecisionLog, DecisionSchemaError, UnknownDecisionError
from jiuwen_glue.decision import DecisionLayer, RuleBasedBackend


def _log() -> DecisionLog:
    return DecisionLog()


def _rec(log: DecisionLog, chosen="a"):
    return log.append(
        agent_ref="agent:dev-core", context={"q": "pick"},
        options=("a", "b"), chosen=chosen, rationale_ref="ev://1")


OUTCOME = {"outcome": "success", "evidence_ref": "task://t1/run"}


# ── DecisionLog.record_outcome ────────────────────────────────────────────

def test_outcome_backfill_once():
    log = _log()
    rec = _rec(log)
    assert rec.outcome is None                      # 未回填=NULL
    got = log.record_outcome(rec.decision_id, OUTCOME)
    assert got.outcome["outcome"] == "success"      # 首写生效
    assert log.get(rec.decision_id).outcome is not None


def test_outcome_rewrite_rejected():
    log = _log()
    rec = _rec(log)
    log.record_outcome(rec.decision_id, OUTCOME)
    with pytest.raises(DecisionSchemaError):
        log.record_outcome(rec.decision_id, {"outcome": "failure", "evidence_ref": "x"})  # 终态不可改


def test_outcome_shape_enforced():
    log = _log()
    rec = _rec(log)
    with pytest.raises(DecisionSchemaError):
        log.record_outcome(rec.decision_id, {"evidence_ref": "x"})            # 缺 outcome
    with pytest.raises(DecisionSchemaError):
        log.record_outcome(rec.decision_id, "success")                         # 非映射
    with pytest.raises(UnknownDecisionError):
        log.record_outcome("no-such-id", OUTCOME)                              # 未知决策


def test_outcome_superseded_shape_also_ok():
    log = _log()
    rec = _rec(log)
    log.record_outcome(rec.decision_id, {"outcome": "superseded", "evidence_ref": "adr://7"})
    assert log.get(rec.decision_id).outcome["outcome"] == "superseded"


# ── DecisionLayer 门面接线 ─────────────────────────────────────────────────

def _layer() -> DecisionLayer:
    layer = DecisionLayer(decision_log=_log(), agent_ref="agent:dev-core")
    layer.set_backend("classify", RuleBasedBackend([
        {"primitive": "classify", "when": {"kind": "triage"},
         "label": "batch", "confidence": 1.0, "rationale_ref": "rule:triage"}]))
    return layer


def test_layer_roundtrip_classify_and_backfill():
    layer = _layer()
    label, conf, ref = layer.classify({"kind": "triage"}, ["batch", "interactive"])
    assert label == "batch"
    assert layer.outcome_of(ref) is None
    layer.record_outcome(ref, OUTCOME)
    assert layer.outcome_of(ref)["outcome"] == "success"          # 原语→落账→回填 全链


def test_layer_refusal_has_no_decision_to_backfill():
    layer = _layer()
    # 未配置 judge 后端 → 弃权（None），无从回填（弃权不落账语义保持）
    assert layer.judge("q", ("a", "b")) is None
