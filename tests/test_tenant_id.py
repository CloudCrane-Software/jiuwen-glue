# coding: utf-8
"""tenant_id 全对象字段测试（v1.7 §4.1）：默认 "t0" 与显式覆盖，覆盖既有五模块 + 新六模块
（D1-R2 补：ablation 四对象——v2.0 §4.2 明列模块）."""
from __future__ import annotations

import pytest

from jiuwen_glue import (
    BudgetLedger,
    CapabilityDeclaration,
    CapabilityRegistry,
    EvidenceStore,
    PromotionLedger,
    TaskLedger,
)


def test_lease_default_and_explicit_tenant(clock):
    led = BudgetLedger(now=clock)
    assert led.grant("t-1", 10).tenant_id == "t0"          # 默认
    assert led.grant("t-2", 10, tenant_id="t1").tenant_id == "t1"
    assert led.audit[-1].tenant_id == "t1"                 # 审计事件同样携带


def test_evidence_default_and_explicit(clock):
    st = EvidenceStore(now=clock)
    assert st.create("s", {"a": 1}).tenant_id == "t0"
    ev = st.create("s", {"a": 1}, tenant_id="t2")
    assert ev.tenant_id == "t2"
    st.verify(ev.evidence_id, method="m", checker="c")
    assert st.history(ev.evidence_id)[-1].tenant_id == "t2"   # 迁移留痕同样携带


def test_capability_tenant_scoped_registry(clock):
    reg = CapabilityRegistry()

    def decl(tenant):
        return CapabilityDeclaration(capability_id="skill.web", version="1.0.0",
                                     name="web skill", side_effect="read",
                                     idempotent=True, retry_safe=True, risk_level=1,
                                     tenant_id=tenant)
    reg.register(decl("t0"))
    reg.register(decl("t1"))                               # 同 capability@version，不同租户
    assert reg.get("skill.web", "1.0.0").tenant_id == "t0"
    assert reg.get("skill.web", "1.0.0", tenant_id="t1").tenant_id == "t1"
    with pytest.raises(Exception):
        reg.get("skill.web", "1.0.0", tenant_id="t9")
    with pytest.raises(Exception):
        reg.register(decl("t0"))                           # 同租户同版本重复被拒


def test_task_default_and_explicit(clock):
    led = TaskLedger(now=clock)
    assert led.create("任务A").tenant_id == "t0"
    task = led.create("任务B", tenant_id="t3")
    assert task.tenant_id == "t3"
    receipt = led.post_message(task.task_id, "orchestrator", "hi", tenant_id="t3")
    assert receipt.tenant_id == "t3"


def test_promotion_default_and_explicit(clock):
    led = PromotionLedger(now=clock)
    assert led.nominate("skill:x", {}).tenant_id == "t0"
    cand = led.nominate("skill:y", {}, tenant_id="t5")
    assert cand.tenant_id == "t5"


def test_new_modules_carry_tenant(clock):
    """新六模块（guardrail/challenge/decisions/routes/identity/…）数据类同样带 tenant_id。"""
    from jiuwen_glue import (
        AgentIdentity,
        ArtifactRoute,
        ChallengeBoard,
        CheckSpec,
        DecisionLog,
        GuardrailRunStore,
        GuardrailSpec,
        RunInstance,
        TaskContext,
        BACKEND_SCAN,
    )
    assert AgentIdentity(agent_id="a").tenant_id == "t0"
    assert RunInstance(instance_id="i").tenant_id == "t0"
    assert TaskContext(task_id="t").tenant_id == "t0"

    board = ChallengeBoard(now=clock)
    ch = board.open(who_confirms="user", resource="r", action="a", method="m",
                    ttl_seconds=60, tenant_id="t7")
    assert ch.tenant_id == "t7"

    store = GuardrailRunStore(now=clock)
    spec = GuardrailSpec(action="act", resource="res",
                         agent_identity_ref="ag:a@t9/run:i/task:t",
                         tenant_id="t9",
                         checks=(CheckSpec(check_id="c1", backend=BACKEND_SCAN),))
    run = store.create_run(spec)
    assert run.tenant_id == "t9"
    assert run.spec.tenant_id == "t9"

    log = DecisionLog(now=clock)
    assert log.append(agent_ref="ag:a@t0/run:i/task:t", context={}, options=["x"],
                      chosen="x", rationale_ref="e", tenant_id="t8").tenant_id == "t8"

    assert ArtifactRoute("*", "code", "git", tenant_id="t6").tenant_id == "t6"


def test_ablation_default_and_explicit_tenant(clock):
    """ablation 四对象带 tenant_id（v2.0 §4.2/D-09 ④；D1-R2 回填）：
    Arm 独立默认/覆盖；Experiment→Result→pairs 全链继承实验租户。"""
    from jiuwen_glue import AblationArm, AblationExperiment

    arm_c = AblationArm(name="control", ref="promoted:skill:video-cut@v2",
                        behavior=lambda i: 0.5)
    assert arm_c.tenant_id == "t0"                                  # 默认
    arm_t = AblationArm(name="treatment", ref="skill:video-cut@v3-candidate",
                        behavior=lambda i: 0.6, tenant_id="t4")
    assert arm_t.tenant_id == "t4"                                  # 显式覆盖

    exp = AblationExperiment(inputs=list(range(10)), control=arm_c,
                             treatment=arm_t, min_samples=10, tenant_id="t4")
    assert exp.tenant_id == "t4"
    res = exp.run(lambda item, out: float(out))
    assert res.tenant_id == "t4"                                    # 结果证据继承
    assert all(p.tenant_id == "t4" for p in res.pairs)              # 配对分继承

    res0 = AblationExperiment(                                      # 缺省实验 → t0
        inputs=list(range(10)), control=arm_c,
        treatment=AblationArm(name="t2", ref="skill:other@v1",
                              behavior=lambda i: 0.7),
        min_samples=10).run(lambda item, out: float(out))
    assert res0.tenant_id == "t0"
    assert all(p.tenant_id == "t0" for p in res0.pairs)
