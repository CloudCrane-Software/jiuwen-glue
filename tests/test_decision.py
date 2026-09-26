# coding: utf-8
"""决策层 MVP 测试：三原语路由与 decision_ref 落账 / UNKNOWN 拒绝路径 /
RuleBased 确定性 / ModelBackend 形状 / make_score_hook 注入 promotion 全链."""
from __future__ import annotations

import copy

import pytest

from jiuwen_glue import (
    GATE_PASS,
    STAGE_PROMOTED,
    STAGE_REJECTED,
    STAGE_SHORTLIST,
    DecisionLayer,
    DecisionLayerError,
    DecisionLog,
    JevBackend,
    JevBackendError,
    JevProvider,
    ModelBackend,
    PrimitiveOutcome,
    RuleBasedBackend,
    context_hash,
    make_score_hook,
    PromotionLedger,
)
from jiuwen_glue.decision import SCORE_RUBRIC_PROMOTION

LABELS = ("chinese", "english", "other")


def _layer(backends=None, *, agent_ref="agent:glue/inst-1/task-1", log=None, clock=None):
    return DecisionLayer(
        decision_log=log or DecisionLog(now=clock), agent_ref=agent_ref,
        backends=backends or {}, now=clock)


def _mixed_backend():
    """一个覆盖三原语的声明式规则后端（声明顺序即优先级）。"""
    return RuleBasedBackend([
        {"primitive": "classify", "when": {"lang": "zh"},
         "label": "chinese", "confidence": 0.9, "rationale_ref": "rule:lang-zh"},
        {"primitive": "classify", "when": {},
         "label": "other", "confidence": 0.5, "rationale_ref": "rule:default"},
        {"primitive": "score", "when": {"asset_key": "skill:video-cut"},
         "rubric": SCORE_RUBRIC_PROMOTION, "value": 0.9,
         "rationale_ref": "rule:promo-video"},
        {"primitive": "judge", "when": {"risk": "low"},
         "chosen": "auto-approve", "rationale_ref": "rule:low-risk"},
        {"primitive": "judge", "when": {},
         "chosen": "escalate", "rationale_ref": "rule:default-escalate"},
    ])


class _ConstBackend(JevBackend):
    """测试替身：返回固定结论（可注入越界值/异常）。"""
    name = "const"

    def __init__(self, *, label="chinese", confidence=None, value=0.8,
                 chosen="approve", raise_on=None):
        self._label, self._conf, self._value, self._chosen = label, confidence, value, chosen
        self._raise_on = raise_on or {}

    def classify(self, item, labels):
        if "classify" in self._raise_on:
            raise RuntimeError("provider down")
        return PrimitiveOutcome(value=self._label, rationale_ref="rule:const",
                                confidence=self._conf)

    def score(self, item, rubric):
        if "score" in self._raise_on:
            raise RuntimeError("provider down")
        return PrimitiveOutcome(value=self._value, rationale_ref="rule:const")

    def judge(self, question, options, constraints=None):
        if "judge" in self._raise_on:
            raise RuntimeError("provider down")
        return PrimitiveOutcome(value=self._chosen, rationale_ref="rule:const")


# ── 三原语路由 + decision_ref 落账 ───────────────────────────────────────────

def test_classify_routes_and_lands_decision_record(clock):
    log = DecisionLog(now=clock)
    layer = _layer({"classify": _mixed_backend()}, log=log, clock=clock)
    result = layer.classify({"lang": "zh", "text": "你好"}, LABELS)
    assert result is not None
    label, confidence, decision_ref = result
    assert (label, confidence) == ("chinese", 0.9)
    rec = log.get(decision_ref)                       # decision_ref 可在账本解析
    assert rec.options == LABELS and rec.chosen == "chinese"
    assert rec.rationale_ref == "rule:lang-zh"
    assert rec.agent_ref == "agent:glue/inst-1/task-1"
    assert rec.meta["backend"] == "rule_based" and rec.meta["confidence"] == 0.9
    # context 经 canonical-JSON 哈希（复用 decisions.context_hash）
    assert rec.context_hash == context_hash(
        {"primitive": "classify", "item": {"lang": "zh", "text": "你好"},
         "labels": list(LABELS)})


def test_score_routes_and_records_value_with_degenerate_option_space(clock):
    log = DecisionLog(now=clock)
    layer = _layer({"score": _mixed_backend()}, log=log, clock=clock)
    result = layer.score({"asset_key": "skill:video-cut"}, SCORE_RUBRIC_PROMOTION)
    assert result is not None
    value, decision_ref = result
    assert value == 0.9
    rec = log.get(decision_ref)
    # score 是连续量：options 退化为 (str(value),)，float 值在 meta.value
    assert rec.options == ("0.9",) and rec.chosen == "0.9"
    assert rec.meta["value"] == 0.9
    assert rec.context_hash == context_hash(
        {"primitive": "score", "item": {"asset_key": "skill:video-cut"},
         "rubric": SCORE_RUBRIC_PROMOTION})


def test_judge_routes_and_returns_rationale_ref(clock):
    log = DecisionLog(now=clock)
    layer = _layer({"judge": _mixed_backend()}, log=log, clock=clock)
    result = layer.judge("auto-publish this asset?", ("auto-approve", "escalate"),
                         {"risk": "low"})
    assert result is not None
    chosen, rationale_ref, decision_ref = result
    assert (chosen, rationale_ref) == ("auto-approve", "rule:low-risk")
    rec = log.get(decision_ref)
    assert rec.options == ("auto-approve", "escalate") and rec.chosen == "auto-approve"
    assert rec.context_hash == context_hash(
        {"primitive": "judge", "question": "auto-publish this asset?",
         "options": ["auto-approve", "escalate"], "constraints": {"risk": "low"}})


def test_primitives_are_routed_per_primitive(clock):
    """按原语路由：classify/score 各挂一个后端，互不串线。"""
    log = DecisionLog(now=clock)
    layer = _layer({"classify": _ConstBackend(), "score": _mixed_backend()},
                   log=log, clock=clock)
    assert layer.classify({"any": 1}, LABELS)[0] == "chinese"       # 走 const 后端
    assert layer.score({"asset_key": "skill:video-cut"}, "r") is None  # score 未配置？——已配置 mixed 但规则 rubric 不匹配
    assert layer.refusals[-1].primitive == "score" and "abstained" in layer.refusals[-1].reason
    assert layer.judge("q", ("a", "b")) is None                     # judge 未配置 → 拒绝
    assert "no backend configured" in layer.refusals[-1].reason
    assert layer.backend_of("classify").name == "const"


def test_guardrail_run_ref_and_agent_override_flow_into_record(clock):
    log = DecisionLog(now=clock)
    layer = _layer({"classify": _ConstBackend()}, log=log, clock=clock)
    _label, _conf, ref = layer.classify({"x": 1}, LABELS, agent_ref="agent:other/i/t",
                                        guardrail_run_ref="gr-123", tenant_id="t9")
    rec = log.get(ref)
    assert rec.guardrail_run_ref == "gr-123" and rec.tenant_id == "t9"
    assert rec.agent_ref == "agent:other/i/t"


def test_layer_satisfies_jev_provider_protocol():
    assert isinstance(_layer(), JevProvider)          # runtime_checkable 协议


# ── UNKNOWN 拒绝路径（绝不静默编造）─────────────────────────────────────────

def test_unconfigured_primitive_refuses_without_fabricating_a_record(clock):
    log = DecisionLog(now=clock)
    layer = _layer({}, log=log, clock=clock)          # 什么后端都没配
    assert layer.classify({"a": 1}, LABELS) is None
    assert layer.score({"a": 1}, "rubric") is None
    assert layer.judge("q", ("a", "b")) is None
    assert len(layer.refusals) == 3
    assert all("no backend configured" in r.reason for r in layer.refusals)
    assert log.all() == []                            # 账本不落编造的决策


def test_rule_miss_abstains_and_never_fabricates(clock):
    log = DecisionLog(now=clock)
    backend = RuleBasedBackend([
        {"primitive": "classify", "when": {"lang": "zh"}, "label": "chinese",
         "confidence": 0.9, "rationale_ref": "rule:lang-zh"},
    ])
    layer = _layer({"classify": backend}, log=log, clock=clock)
    assert layer.classify({"lang": "en"}, LABELS) is None     # 无命中 → 弃权
    assert "no rule matched" in layer.refusals[-1].reason
    assert log.all() == []


def test_backend_value_outside_declared_labels_is_refused(clock):
    log = DecisionLog(now=clock)
    layer = _layer({"classify": _ConstBackend(label="klingon")}, log=log, clock=clock)
    assert layer.classify({"x": 1}, LABELS) is None
    assert "outside declared labels" in layer.refusals[-1].reason
    assert log.all() == []                            # 越界返回值不落账


def test_backend_judge_chosen_outside_options_is_refused(clock):
    log = DecisionLog(now=clock)
    layer = _layer({"judge": _ConstBackend(chosen="deploy-to-prod")}, log=log, clock=clock)
    assert layer.judge("q", ("a", "b")) is None
    assert "outside declared options" in layer.refusals[-1].reason
    assert log.all() == []


def test_backend_exception_is_fail_closed_refusal(clock):
    log = DecisionLog(now=clock)
    layer = _layer({"classify": _ConstBackend(raise_on=("classify",)),
                    "score": _ConstBackend(raise_on=("score",))}, log=log, clock=clock)
    assert layer.classify({"x": 1}, LABELS) is None
    assert "raised" in layer.refusals[-1].reason
    assert layer.score({"x": 1}, "r") is None
    assert log.all() == []


def test_out_of_range_confidence_and_nonfinite_score_refused(clock):
    log = DecisionLog(now=clock)
    layer = _layer({"classify": _ConstBackend(confidence=1.5),
                    "score": _ConstBackend(value=float("nan"))}, log=log, clock=clock)
    assert layer.classify({"x": 1}, LABELS) is None
    assert "confidence" in layer.refusals[-1].reason
    assert layer.score({"x": 1}, "r") is None
    assert "non-finite/non-numeric" in layer.refusals[-1].reason
    assert log.all() == []


def test_malformed_call_arguments_raise_loudly(clock):
    """调用方参数不合法 = 调用方 bug，大声失败（不算"无法判定"）。"""
    layer = _layer({"classify": _ConstBackend()})
    with pytest.raises(Exception):
        layer.classify({"x": 1}, ())
    with pytest.raises(Exception):
        layer.judge("", ("a",))
    with pytest.raises(Exception):
        layer.judge("q", ())
    with pytest.raises(DecisionLayerError):
        DecisionLayer(decision_log=object(), agent_ref="a")  # type: ignore[arg-type]


# ── RuleBased 后端：确定性 + 规则语义 ────────────────────────────────────────

def test_rule_based_backend_is_deterministic_zero_model(clock):
    log = DecisionLog(now=clock)
    layer = _layer({"classify": _mixed_backend(), "judge": _mixed_backend()},
                   log=log, clock=clock)
    first = layer.classify({"lang": "zh"}, LABELS)
    second = layer.classify({"lang": "zh"}, LABELS)
    assert first[:2] == second[:2]                    # 同输入 → 同 label/confidence
    j1 = layer.judge("q?", ("auto-approve", "escalate"), {"risk": "low"})
    j2 = layer.judge("q?", ("auto-approve", "escalate"), {"risk": "low"})
    assert j1[:2] == j2[:2]
    assert len(log.all()) == 4                        # 每次决策各落一条
    # 声明顺序即优先级：lang=zh 同时命中第一条与恒匹配兜底，取第一条
    assert log.all()[0].chosen == "chinese"


def test_rule_based_judge_question_contains_and_score_rubric_scoping(clock):
    backend = RuleBasedBackend([
        {"primitive": "score", "when": {}, "rubric": "promo", "value": 0.7,
         "rationale_ref": "rule:promo-only"},
        {"primitive": "judge", "when": {}, "question_contains": "delete",
         "chosen": "ask-human", "rationale_ref": "rule:deletion"},
    ])
    promo = backend.score({"x": 1}, "promo")
    assert promo is not None and promo.value == 0.7
    assert backend.score({"x": 1}, "other-rubric") is None    # rubric 限定生效
    assert backend.judge("should I delete prod?", ("a", "b")) is not None
    assert backend.judge("should I keep prod?", ("a", "b")) is None


def test_rule_schema_validation(clock):
    with pytest.raises(JevBackendError):
        RuleBasedBackend([{"primitive": "divine", "label": "x"}])
    with pytest.raises(JevBackendError):
        RuleBasedBackend([{"primitive": "classify", "when": {}, "rationale_ref": "r"}])
    with pytest.raises(JevBackendError):
        RuleBasedBackend([{"primitive": "classify", "when": {}, "label": "x"}])  # 缺 rationale
    with pytest.raises(JevBackendError):
        RuleBasedBackend([{"primitive": "score", "when": {}, "value": "high",
                           "rationale_ref": "r"}])
    with pytest.raises(JevBackendError):
        RuleBasedBackend([{"primitive": "judge", "when": {}, "chosen": "a",
                           "rationale_ref": "r", "question_contains": 5}])
    with pytest.raises(JevBackendError):
        PrimitiveOutcome(value="x", rationale_ref="")   # 理由引用必填


# ── ModelBackend：只留形状，引用不存值 ───────────────────────────────────────

def test_model_backend_is_shape_only_refs_not_values():
    backend = ModelBackend(endpoint_ref="higress:model-gateway",
                           api_key_ref="openbao:secret/credentials/higress#api_key",
                           model="qwen-max")
    assert backend.endpoint_ref == "higress:model-gateway"       # 只存引用名
    assert backend.api_key_ref.startswith("openbao:")            # 只存凭据引用
    with pytest.raises(NotImplementedError):
        backend.classify({"x": 1}, LABELS)
    with pytest.raises(NotImplementedError):
        backend.score({"x": 1}, "r")
    with pytest.raises(NotImplementedError):
        backend.judge("q", ("a", "b"))


def test_model_backend_via_layer_degrades_to_unknown_refusal(clock):
    """模型后端未接线时整链 fail-closed：None + 拒绝理由，不编造。"""
    log = DecisionLog(now=clock)
    layer = _layer({"classify": ModelBackend(endpoint_ref="higress:m", api_key_ref="ob:k",
                                             model="m")}, log=log, clock=clock)
    assert layer.classify({"x": 1}, LABELS) is None
    assert "shape-only stub" in layer.refusals[-1].reason
    assert log.all() == []
    with pytest.raises(JevBackendError):
        ModelBackend(endpoint_ref="", api_key_ref="k", model="m")   # 引用必填


# ── 注入点 1：make_score_hook → promotion 全链（真调用 glue.promotion）───────

def test_score_hook_injected_promotion_full_chain_promotes(clock):
    log = DecisionLog(now=clock)
    layer = _layer({"score": _mixed_backend()}, log=log, clock=clock)
    led = PromotionLedger(redaction_checker=lambda c: GATE_PASS,
                          score_hook=make_score_hook(layer), now=clock)
    cand = led.nominate("skill:video-cut", {"sop": "cut -> encode"},
                        origin_ref="ttse:bank/fact-42")
    for _ in range(4):
        led.record_eval(cand.asset_key, "success")
    led.transition(cand.promotion_id, STAGE_SHORTLIST, reason="ablation passed",
                   evidence_ref="ev-ablation-1")
    led.transition(cand.promotion_id, STAGE_PROMOTED, reason="gates passed",
                   evidence_ref="ev-redaction-1")
    assert cand.stage == STAGE_PROMOTED
    assert cand.score == 0.9                                   # 决策层分数进了晋升
    # 决策留痕在决策层：一条 score DecisionRecord，chosen=str(value)
    recs = log.all()
    assert len(recs) == 1 and recs[0].chosen == "0.9"
    snapshot = {"promotion_id": cand.promotion_id, "asset_key": cand.asset_key,
                "origin_ref": "ttse:bank/fact-42",
                "content": {"sop": "cut -> encode"}, "tenant_id": "t0"}
    assert recs[0].context_hash == context_hash(
        {"primitive": "score", "item": snapshot, "rubric": SCORE_RUBRIC_PROMOTION})
    assert recs[0].rationale_ref == "rule:promo-video"


def test_score_hook_abstain_fails_promotion_closed(clock):
    """决策层无命中规则 → hook 返回 None → promotion UNKNOWN 拒绝（fail-closed）。"""
    log = DecisionLog(now=clock)
    layer = _layer({"score": _mixed_backend()}, log=log, clock=clock)
    led = PromotionLedger(redaction_checker=lambda c: GATE_PASS,
                          score_hook=make_score_hook(layer), now=clock)
    cand = led.nominate("skill:other-asset", {"sop": "x"})     # 规则只覆盖 video-cut
    for _ in range(4):
        led.record_eval(cand.asset_key, "success")
    led.transition(cand.promotion_id, STAGE_SHORTLIST, reason="ok")
    led.transition(cand.promotion_id, STAGE_PROMOTED, reason="try")
    assert cand.stage == STAGE_REJECTED
    last = led.history(cand.promotion_id)[-1]
    assert "score" in last.reason and "UNKNOWN" in last.reason
    assert "no rule matched" in layer.refusals[-1].reason
    assert log.all() == []                                     # 弃权不落决策账


def test_snapshot_passed_to_score_primitive_is_protected_from_mutation(clock):
    """promotion 快照传入层时被浅拷贝为 dict：hook 侧改动不污染评审对象。"""
    seen = []

    class _Spy(JevBackend):
        name = "spy"

        def classify(self, item, labels):
            return None

        def score(self, item, rubric):
            seen.append(item)
            item["tampered"] = True                            # 试图改写快照
            return PrimitiveOutcome(value=0.9, rationale_ref="rule:spy")

        def judge(self, question, options, constraints=None):
            return None

    log = DecisionLog(now=clock)
    layer = _layer({"score": _Spy()}, log=log, clock=clock)
    hook = make_score_hook(layer)
    original = {"asset_key": "skill:x", "content": {"a": 1}}
    frozen = copy.deepcopy(original)
    assert hook(original) == 0.9
    assert original == frozen                                  # 原快照未被改写
    assert seen[0] is not original                             # 层内另存副本
