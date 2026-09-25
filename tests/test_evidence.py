# coding: utf-8
"""Evidence 三态（DRAFT/VERIFIED/FINALIZED）状态机测试。"""
from __future__ import annotations

import pytest

from jiuwen_glue import (
    DRAFT,
    FINALIZED,
    EvidenceImmutableError,
    EvidenceStore,
    IllegalTransitionError,
    VERIFIED,
)


def test_full_forward_path(clock):
    st = EvidenceStore(now=clock)
    ev = st.create("task_run:42", {"summary": "did the thing"})
    assert ev.state == DRAFT
    st.verify(ev.evidence_id, method="eval-gate run #7", checker="ci-bot")
    assert st.get(ev.evidence_id).state == VERIFIED
    st.finalize(ev.evidence_id, seal_ref="transit:eval-signer/abc")
    assert st.get(ev.evidence_id).state == FINALIZED
    kinds = [(t.from_state, t.to_state) for t in st.history(ev.evidence_id)
             if t.kind == "TRANSITION"]
    assert kinds == [(DRAFT, VERIFIED), (VERIFIED, FINALIZED)]


def test_skip_state_rejected(clock):
    st = EvidenceStore(now=clock)
    ev = st.create("task_run:42", {"summary": "x"})
    with pytest.raises(IllegalTransitionError):
        st.finalize(ev.evidence_id, seal_ref="s1")   # DRAFT → FINALIZED 跳态
    assert st.get(ev.evidence_id).state == DRAFT
    assert st.history(ev.evidence_id)[-1].kind == "VIOLATION"


def test_backward_transition_rejected(clock):
    st = EvidenceStore(now=clock)
    ev = st.create("s", {})
    st.verify(ev.evidence_id, method="m", checker="c")
    with pytest.raises(IllegalTransitionError):
        st.demote(ev.evidence_id, DRAFT)             # VERIFIED → DRAFT 回退
    assert st.get(ev.evidence_id).state == VERIFIED
    st.finalize(ev.evidence_id, seal_ref="s")
    with pytest.raises(IllegalTransitionError):
        st.demote(ev.evidence_id, VERIFIED)          # FINALIZED → VERIFIED 回退
    assert st.get(ev.evidence_id).state == FINALIZED
    assert st.history(ev.evidence_id)[-1].kind == "VIOLATION"


def test_finalize_requires_verified(clock):
    st = EvidenceStore(now=clock)
    ev = st.create("s", {})
    with pytest.raises(IllegalTransitionError):
        st.finalize(ev.evidence_id, seal_ref="s")
    st.verify(ev.evidence_id, method="m", checker="c")
    st.finalize(ev.evidence_id, seal_ref="seal-1")
    with pytest.raises(IllegalTransitionError):
        st.finalize(ev.evidence_id, seal_ref="seal-2")  # 终态再 finalize 拒绝


def test_content_edit_only_in_draft(clock):
    st = EvidenceStore(now=clock)
    ev = st.create("s", {"v": 1})
    st.update_content(ev.evidence_id, {"v": 2})       # DRAFT 可编辑
    assert st.get(ev.evidence_id).content == {"v": 2}
    st.verify(ev.evidence_id, method="m", checker="c")
    with pytest.raises(EvidenceImmutableError):
        st.update_content(ev.evidence_id, {"v": 3})   # VERIFIED 冻结
    st.finalize(ev.evidence_id, seal_ref="s")
    with pytest.raises(EvidenceImmutableError):
        st.update_content(ev.evidence_id, {"v": 4})   # FINALIZED 不可变
    assert st.history(ev.evidence_id)[-2:].__len__() == 2  # 两次违规都留痕


def test_verify_requires_method_and_checker(clock):
    st = EvidenceStore(now=clock)
    ev = st.create("s", {})
    with pytest.raises(IllegalTransitionError):
        st.verify(ev.evidence_id, method="", checker="c")
    with pytest.raises(IllegalTransitionError):
        st.verify(ev.evidence_id, method="m", checker="")
    assert st.get(ev.evidence_id).state == DRAFT


def test_admit_ready_gate(clock):
    st = EvidenceStore(now=clock)
    ev = st.create("s", {})
    assert not st.admit_ready(ev.evidence_id)
    st.verify(ev.evidence_id, method="m", checker="c")
    assert st.admit_ready(ev.evidence_id)
    st.finalize(ev.evidence_id, seal_ref="s")
    assert st.admit_ready(ev.evidence_id)
