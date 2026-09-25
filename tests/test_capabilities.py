# coding: utf-8
"""能力元数据注册（五类机读声明 / 版本准入 / 指标单向回流）测试。"""
from __future__ import annotations

import pytest

from jiuwen_glue import (
    AdmissionDeniedError,
    CapabilityDeclaration,
    CapabilityRegistry,
    DeclarationSchemaError,
    EvidenceStore,
)


def _decl(**over):
    base = dict(
        capability_id="skill.search-web",
        version="1.0.0",
        name="web search skill",
        side_effect="read",
        idempotent=True,
        retry_safe=True,
        risk_level=1,
        preconditions=("network.allow(egress:higress)",),
    )
    base.update(over)
    return CapabilityDeclaration(**base)


def test_register_and_roundtrip():
    reg = CapabilityRegistry()
    cap = reg.register(_decl())
    assert cap.status == "CANDIDATE"
    assert reg.get("skill.search-web", "1.0.0") is cap


def test_invalid_side_effect_rejected():
    reg = CapabilityRegistry()
    with pytest.raises(DeclarationSchemaError):
        reg.register(_decl(side_effect="delete-world"))


def test_invalid_risk_level_rejected():
    reg = CapabilityRegistry()
    with pytest.raises(DeclarationSchemaError):
        reg.register(_decl(risk_level=9))
    with pytest.raises(DeclarationSchemaError):
        reg.register(_decl(risk_level=-1))


def test_retry_safe_requires_idempotent():
    """不可幂等却声称可重试 → schema 违规，注册即被检测。"""
    reg = CapabilityRegistry()
    with pytest.raises(DeclarationSchemaError):
        reg.register(_decl(idempotent=False, retry_safe=True))
    # 幂等=False 且 retry_safe=False 是合法组合
    reg.register(_decl(idempotent=False, retry_safe=False,
                       side_effect="external_irreversible", risk_level=4))


def test_duplicate_version_rejected():
    reg = CapabilityRegistry()
    reg.register(_decl())
    with pytest.raises(DeclarationSchemaError):
        reg.register(_decl())


def test_admission_requires_verified_or_finalized_evidence(clock):
    reg = CapabilityRegistry()
    ev_store = EvidenceStore(now=clock)
    reg = CapabilityRegistry(evidence=ev_store)
    reg.register(_decl())
    draft = ev_store.create("capability_version:skill.search-web@1.0.0",
                            {"checks": ["schema", "sandbox smoke"]})
    with pytest.raises(AdmissionDeniedError):
        reg.admit("skill.search-web", "1.0.0", draft.evidence_id)
    assert reg.get("skill.search-web", "1.0.0").status == "REJECTED"
    ev_store.verify(draft.evidence_id, method="eval-gate", checker="ci")
    reg.admit("skill.search-web", "1.0.0", draft.evidence_id)
    assert reg.is_admitted("skill.search-web", "1.0.0")
    assert reg.get("skill.search-web", "1.0.0").admitted_evidence_id == draft.evidence_id


def test_metrics_are_append_only(clock):
    reg = CapabilityRegistry()
    reg.register(_decl())
    for outcome in ("success", "success", "failure", "hit", "hit", "hit", "miss"):
        reg.record_execution("skill.search-web", "1.0.0", outcome)
    m = reg.metrics_of("skill.search-web", "1.0.0")
    assert m["success_rate"] == pytest.approx(2 / 3)
    assert m["hit_rate"] == pytest.approx(3 / 4)
    with pytest.raises(ValueError):
        reg.record_execution("skill.search-web", "1.0.0", "excellent")  # 枚举外拒绝
    with pytest.raises(Exception):
        reg.record_execution("skill.nope", "1.0.0", "success")          # 未注册能力拒绝


def test_no_metric_override_api():
    """单向回流：不存在设置/覆盖指标的接口。"""
    reg = CapabilityRegistry()
    assert not any(name.startswith("set_") or "override" in name
                   for name in dir(reg) if not name.startswith("_"))
