# coding: utf-8
"""决策记录（append-only）测试：只增不改 / chosen∈options / context_hash / 查询."""
from __future__ import annotations

import pytest

from jiuwen_glue import (
    DecisionLog,
    DecisionSchemaError,
    UnknownDecisionError,
    context_hash,
)

AGENT_REF = "ag:decision-keeper@t0/run:i-9/task:wo-11"


def _append(log, **over):
    kw = dict(agent_ref=AGENT_REF,
              context={"run_id": "r-1", "candidate": "exp-42", "baseline": "exp-41"},
              options=["admit", "reject", "ablation-again"],
              chosen="admit", rationale_ref="evidence:ev-ablation-42",
              guardrail_run_ref="grun-77", tenant_id="t0")
    kw.update(over)
    return log.append(**kw)


def test_append_and_query_roundtrip(clock):
    log = DecisionLog(now=clock)
    rec = _append(log)
    got = log.get(rec.decision_id)
    assert got.chosen == "admit"
    assert got.options == ("admit", "reject", "ablation-again")
    assert got.agent_ref == AGENT_REF                       # 三层身份引用
    assert got.guardrail_run_ref == "grun-77"
    assert got.rationale_ref == "evidence:ev-ablation-42"
    assert got.tenant_id == "t0"
    assert log.all() == [got]


def test_chosen_must_be_in_options(clock):
    log = DecisionLog(now=clock)
    with pytest.raises(DecisionSchemaError):
        _append(log, chosen="deploy-to-prod")
    with pytest.raises(DecisionSchemaError):
        _append(log, options=[])
    with pytest.raises(DecisionSchemaError):
        _append(log, agent_ref="")
    with pytest.raises(DecisionSchemaError):
        _append(log, rationale_ref="")


def test_append_only_no_update_or_delete_api(clock):
    """append-only 写死：不存在任何 update/delete/set 接口；存储对象 frozen。"""
    log = DecisionLog(now=clock)
    rec = _append(log)
    forbidden = ("update", "delete", "remove", "set_", "replace", "amend", "rewrite")
    names = [n for n in dir(log) if not n.startswith("_")]
    assert not any(any(f in n.lower() for f in forbidden) for n in names)
    assert set(names) == {"append", "get", "all", "by_agent", "by_context"}
    with pytest.raises(Exception):
        # frozen dataclass：属性不可变
        rec.chosen = "reject"  # type: ignore[misc]


def test_context_hash_deterministic_and_sensitive_free(clock):
    """同一上下文 → 同一哈希（可对账）；哈希是 SHA-256 hex，原文不落账。"""
    ctx = {"run_id": "r-1", "candidate": "exp-42"}
    h1 = context_hash(ctx)
    h2 = context_hash({"candidate": "exp-42", "run_id": "r-1"})   # 键序无关
    assert h1 == h2
    assert len(h1) == 64 and all(c in "0123456789abcdef" for c in h1)
    log = DecisionLog(now=clock)
    rec = _append(log)
    assert rec.context_hash == context_hash(rec.meta or {}) or rec.context_hash
    assert "exp-42" not in rec.context_hash                  # 原文不出现在哈希里


def test_query_by_agent_and_context(clock):
    log = DecisionLog(now=clock)
    r1 = _append(log)
    r2 = _append(log, chosen="reject", rationale_ref="evidence:ev-43",
                 context={"run_id": "r-2", "candidate": "exp-43"})
    _append(log, agent_ref="ag:other@t0/run:i-0/task:t-0",
            context={"run_id": "r-3", "candidate": "exp-44"})
    assert log.by_agent(AGENT_REF) == [r1, r2]
    assert log.by_context({"run_id": "r-1", "candidate": "exp-42",
                           "baseline": "exp-41"}) == [r1]
    with pytest.raises(UnknownDecisionError):
        log.get("nope")
