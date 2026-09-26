# coding: utf-8
"""GuardrailRun 协议聚合测试：五步链路 + 聚合三态 + fail-closed + 决策点唯一."""
from __future__ import annotations

import pytest

from jiuwen_glue import (
    BACKEND_EVAL_GATE,
    BACKEND_NATIVE_GUARDRAIL,
    BACKEND_PERMISSION_RAIL,
    BACKEND_SCAN,
    ChallengeBoard,
    CheckSpec,
    GuardrailRunStore,
    GuardrailSchemaError,
    GuardrailSpec,
    GuardrailStateError,
    OUTCOME_ASK,
    OUTCOME_BLOCKED,
    OUTCOME_PASS,
    OUTCOME_UNKNOWN,
    RUN_FINALIZED,
    RUN_OPEN,
    RUN_VOID,
    UnknownGuardrailRunError,
    VERDICT_BLOCKED,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
    aggregate,
)


def _spec(**over):
    base = dict(
        action="release.resume",
        resource="batch:cfg-20260926-01",
        agent_identity_ref="ag:dev-core@t0/run:i-1/task:t-1",
        env_ref="env:anolis-23@v7",
        checks=(
            CheckSpec(check_id="static-scan", backend=BACKEND_SCAN,
                      declaration={"tier": "static"}),
            CheckSpec(check_id="perm-rail", backend=BACKEND_PERMISSION_RAIL),
        ),
    )
    base.update(over)
    return GuardrailSpec(**base)


# ── 聚合语义（纯函数）────────────────────────────────────────────────────────

def test_aggregate_semantics():
    assert aggregate([]) == VERDICT_PASS                       # 无结论不算 BLOCKED/UNKNOWN（由必填检查兜底）
    assert aggregate([VERDICT_PASS, VERDICT_PASS]) == VERDICT_PASS
    assert aggregate([VERDICT_PASS, VERDICT_BLOCKED]) == VERDICT_BLOCKED
    assert aggregate([VERDICT_PASS, VERDICT_UNKNOWN]) == VERDICT_UNKNOWN
    # 优先级：任一 BLOCKED 压过 UNKNOWN
    assert aggregate([VERDICT_UNKNOWN, VERDICT_BLOCKED]) == VERDICT_BLOCKED


# ── 五步链路 ─────────────────────────────────────────────────────────────────

def test_five_step_chain_happy_path(clock):
    store = GuardrailRunStore(now=clock)
    run = store.create_run(_spec())                     # 步骤1+2：提交上下文并固化
    assert run.state == RUN_OPEN
    store.submit_check(run.run_id, "static-scan", OUTCOME_PASS,
                       evidence_ref="ev-static-1")      # 步骤3：提交判断与 Evidence
    store.submit_check(run.run_id, "perm-rail", OUTCOME_PASS,
                       evidence_ref="ev-perm-1")
    store.finalize(run.run_id, seal_ref="transit:eval-signer/xyz")   # 步骤4：验收固化
    assert run.state == RUN_FINALIZED
    result = store.gate(run.run_id)                     # 步骤5：执行前查询
    assert result.verdict == VERDICT_PASS
    assert result.executable is True
    assert result.missing_required == ()


def test_gate_before_finalize_is_unknown_fail_closed(clock):
    """信息不足（未验收固化）→ gate 恒 UNKNOWN；查询方必须拒绝。"""
    store = GuardrailRunStore(now=clock)
    run = store.create_run(_spec())
    store.submit_check(run.run_id, "static-scan", OUTCOME_PASS)
    result = store.gate(run.run_id)
    assert result.verdict == VERDICT_UNKNOWN
    assert result.executable is False
    assert result.state == RUN_OPEN


def test_missing_required_check_is_unknown(clock):
    """必填 check 未提交结论 = 信息不足 → UNKNOWN（fail-closed）。"""
    store = GuardrailRunStore(now=clock)
    run = store.create_run(_spec())
    store.submit_check(run.run_id, "static-scan", OUTCOME_PASS)
    store.finalize(run.run_id, seal_ref="s1")
    result = store.gate(run.run_id)
    assert result.verdict == VERDICT_UNKNOWN
    assert "perm-rail" in result.missing_required


def test_blocked_check_blocks_whole_run(clock):
    store = GuardrailRunStore(now=clock)
    run = store.create_run(_spec())
    store.submit_check(run.run_id, "static-scan", OUTCOME_PASS)
    store.submit_check(run.run_id, "perm-rail", OUTCOME_BLOCKED,
                       note="deny: prod restart needs duty officer")
    store.finalize(run.run_id, seal_ref="s2")
    result = store.gate(run.run_id)
    assert result.verdict == VERDICT_BLOCKED
    assert result.executable is False


def test_submitted_unknown_is_unknown_after_finalize(clock):
    store = GuardrailRunStore(now=clock)
    run = store.create_run(_spec())
    store.submit_check(run.run_id, "static-scan", OUTCOME_UNKNOWN,
                       note="monitor data unavailable")
    store.submit_check(run.run_id, "perm-rail", OUTCOME_PASS)
    store.finalize(run.run_id, seal_ref="s3")
    assert store.gate(run.run_id).verdict == VERDICT_UNKNOWN


# ── 现场变化：原有结论失效 ───────────────────────────────────────────────────

def test_void_invalidates_finalized_conclusion(clock):
    """任何关键状态变化 → 原有结论失效；void 后 gate 恒 UNKNOWN。"""
    store = GuardrailRunStore(now=clock)
    run = store.create_run(_spec())
    store.submit_check(run.run_id, "static-scan", OUTCOME_PASS)
    store.submit_check(run.run_id, "perm-rail", OUTCOME_PASS)
    store.finalize(run.run_id, seal_ref="s4")
    assert store.gate(run.run_id).verdict == VERDICT_PASS
    store.void(run.run_id, reason="production target changed")
    assert run.state == RUN_VOID
    result = store.gate(run.run_id)
    assert result.verdict == VERDICT_UNKNOWN
    assert result.executable is False


# ── 固化与状态保护 ───────────────────────────────────────────────────────────

def test_frozen_after_finalize(clock):
    store = GuardrailRunStore(now=clock)
    run = store.create_run(_spec())
    store.submit_check(run.run_id, "static-scan", OUTCOME_PASS)
    store.finalize(run.run_id, seal_ref="s5")
    with pytest.raises(GuardrailStateError):
        store.submit_check(run.run_id, "perm-rail", OUTCOME_PASS)
    with pytest.raises(GuardrailStateError):
        store.finalize(run.run_id, seal_ref="s5-again")


def test_undeclared_check_rejected(clock):
    store = GuardrailRunStore(now=clock)
    run = store.create_run(_spec())
    with pytest.raises(GuardrailSchemaError):
        store.submit_check(run.run_id, "not-in-spec", OUTCOME_PASS)


def test_spec_validation():
    with pytest.raises(GuardrailSchemaError):
        GuardrailSpec(action="", resource="r", agent_identity_ref="ag:x/run:y/task:z")
    with pytest.raises(GuardrailSchemaError):
        _spec(checks=(CheckSpec(check_id="a", backend="magic-backend"),))
    with pytest.raises(GuardrailSchemaError):
        _spec(checks=(CheckSpec(check_id="dup", backend=BACKEND_SCAN),
                      CheckSpec(check_id="dup", backend=BACKEND_SCAN)))
    with pytest.raises(UnknownGuardrailRunError):
        GuardrailRunStore().gate("nope")


def test_native_guardrail_backend_declaration_only(clock):
    """模块只存声明：native_guardrail 型 check 的执行在原生 core.security.guardrail。"""
    store = GuardrailRunStore(now=clock)
    spec = _spec(checks=(CheckSpec(check_id="injection-guard",
                                   backend=BACKEND_NATIVE_GUARDRAIL,
                                   declaration={"rule": "prompt-injection", "risk": "high"}),))
    run = store.create_run(spec)
    store.submit_check(run.run_id, "injection-guard", OUTCOME_PASS,
                       evidence_ref="ev-native-1")
    store.finalize(run.run_id, seal_ref="s6")
    assert store.gate(run.run_id).verdict == VERDICT_PASS


# ── ASK 语义：可 emit Challenge（授权三态第三态）─────────────────────────────

def test_ask_emits_challenge_and_gate_stays_unknown(clock):
    board = ChallengeBoard(now=clock)
    store = GuardrailRunStore(challenge_board=board, now=clock)
    run = store.create_run(_spec())
    store.submit_check(run.run_id, "perm-rail", OUTCOME_ASK,
                       note="need user confirmation for resume")
    result = store.gate(run.run_id)
    assert result.verdict == VERDICT_UNKNOWN            # 未决 ask = UNKNOWN（fail-closed）
    pending = store.pending_challenges(run.run_id)
    assert len(pending) == 1
    payload = pending[0].to_ask_payload()
    assert payload["agent"] == run.spec.agent_identity_ref
    assert payload["resource"] == "batch:cfg-20260926-01"
    assert payload["action"] == "release.resume"
    # 确认人批准后以 PASS 重新提交 → 门控放行
    ch = pending[0]
    board.resolve(ch.challenge_id, approved=True, by="user-88")
    store.submit_check(run.run_id, "perm-rail", OUTCOME_PASS,
                       evidence_ref=f"challenge:{ch.challenge_id}")
    store.submit_check(run.run_id, "static-scan", OUTCOME_PASS)
    store.finalize(run.run_id, seal_ref="s7")
    assert store.gate(run.run_id).verdict == VERDICT_PASS


def test_ask_only_valid_for_permission_rail(clock):
    board = ChallengeBoard(now=clock)
    store = GuardrailRunStore(challenge_board=board, now=clock)
    run = store.create_run(_spec())
    with pytest.raises(GuardrailSchemaError):
        store.submit_check(run.run_id, "static-scan", OUTCOME_ASK)


# ── 决策点唯一（写死的注释与代码事实一致）────────────────────────────────────

def test_decision_point_unicity():
    """本模块不新增第二个决策点：唯一门控输出是 GuardrailResult.verdict；
    本模块没有任何'执行检查'的方法（只有 submit/finalize/gate 协议动作）。"""
    import inspect
    from jiuwen_glue import guardrail as g
    public = [n for n, _ in inspect.getmembers(GuardrailRunStore, inspect.isfunction)
              if not n.startswith("_")]
    assert set(public) == {"create_run", "submit_check", "finalize", "void",
                           "gate", "get", "pending_challenges"}
    assert "execute" not in " ".join(public)
    assert "本模块不新增第二个决策点" in inspect.getdoc(g)
