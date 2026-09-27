# coding: utf-8
"""meta_governance 元治理三角色骨架测试（v2.0 §7/§8；W-06）.

覆盖：theory_keeper（前沿→提案、owner 唯一人工写入点、触发→提案）、
trigger_sentinel（四个预注册触发器：阈值边界/多月支出/全量巡检）、
instance_registrar（六模式注册、偏差清单+"别扭"摩擦、bump→建议单）、
theory 文件引用边界（product 只存路径不 import theory）。
"""
from __future__ import annotations

import inspect

import pytest

from jiuwen_glue.meta_governance import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    FrontierInput,
    InstanceRegistrar,
    InstanceSpec,
    MetaGovernanceError,
    THEORY_DIR_REF,
    THEORY_OBJECT_DOCS,
    THRESHOLD_LONG_TASKS_WEEKLY,
    THRESHOLD_SANDBOX_MONTHLY_SPEND,
    THRESHOLD_SERVICES,
    THRESHOLD_XSVC_FAILURES_WEEKLY,
    TheoryBump,
    TheoryKeeper,
    TheoryProposal,
    TriggerCounters,
    TriggerRecord,
    check_all,
    check_cross_service_failures,
    check_long_tasks,
    check_sandbox_spend,
    check_services,
    propose_all,
)
from pathlib import Path

GLUE_ROOT = Path(__file__).resolve().parents[1]


def _frontier(**over):
    base = dict(
        source="handbook-3.3",
        title="手册新增身份原则",
        digest="原则二要求子委托范围只能比上游更小",
        relevant_objects=("guardrail",),
        evidence_ref="build/refs/handbook-3.3-extract.md",
    )
    base.update(over)
    return FrontierInput(**base)


# ── theory_keeper ────────────────────────────────────────────────────────────

def test_keeper_frontier_to_proposal_skeleton():
    """外部前沿 → 提案骨架：PENDING、带预注册决策规则、对象与动机已消化。"""
    keeper = TheoryKeeper()
    p = keeper.propose(_frontier(), decision_rule="shadow-one-cycle-then-promote")
    assert isinstance(p, TheoryProposal)
    assert p.approval == APPROVAL_PENDING
    assert p.objects == ("guardrail",)
    assert p.decision_rule == "shadow-one-cycle-then-promote"
    assert p.source == "handbook-3.3"
    assert "手册新增身份原则" in p.motivation
    assert keeper.proposals == [p]


def test_keeper_rejects_empty_and_unknown_objects():
    """前沿输入缺对象或对象未登记 → 拒绝（fail-closed，不建空壳提案）。"""
    keeper = TheoryKeeper()
    with pytest.raises(MetaGovernanceError):
        keeper.propose(_frontier(relevant_objects=()))
    with pytest.raises(MetaGovernanceError):
        keeper.propose(_frontier(relevant_objects=("nonexistent-object",)))


def test_keeper_owner_decide_is_single_manual_gate():
    """owner 批准/否决是唯一状态写入点；已决提案不可再决（§8 唯一不自动化环节）。"""
    keeper = TheoryKeeper()
    p1 = keeper.propose(_frontier())
    p2 = keeper.propose(_frontier(title="另一条"))
    keeper.owner_decide(p1, approved=True, by="owner")
    assert p1.approval == APPROVAL_APPROVED and p1.approved_by == "owner"
    keeper.owner_decide(p2, approved=False, by="owner")
    assert p2.approval == APPROVAL_REJECTED
    with pytest.raises(MetaGovernanceError):
        keeper.owner_decide(p1, approved=False, by="owner")


def test_keeper_proposal_from_trigger_record():
    """触发记录 → 提案（§8 触发即自动开提案，含预注册决策规则）。"""
    keeper = TheoryKeeper()
    counters = TriggerCounters(long_tasks_weekly=25)
    rec = check_long_tasks(counters)
    assert rec is not None
    p = keeper.propose_from_trigger(rec)
    assert p.source == rec.trigger_id
    assert p.decision_rule == rec.decision_rule
    assert "25 > threshold 20" in p.motivation
    assert p.approval == APPROVAL_PENDING


def test_propose_all_batches_proposals_for_every_trigger():
    """一次巡检触发的所有记录各自成案（含触发记录→提案的批量便捷函数）。"""
    keeper = TheoryKeeper()
    counters = TriggerCounters(long_tasks_weekly=21, cross_service_failures_weekly=9,
                               services_count=7,
                               sandbox_monthly_spend=(600.0, 620.0))
    records = propose_all(keeper, counters)
    assert {r.trigger_id for r in records} == {
        "TRG-TEMPORAL-LONGTASKS", "TRG-TEMPORAL-XSVC-FAIL",
        "TRG-ARGOCD-SERVICES", "TRG-E2B-SPEND"}
    assert len(keeper.proposals) == 4


# ── trigger_sentinel：四个预注册触发器 ───────────────────────────────────────

def test_trigger_long_tasks_threshold_boundary():
    """长任务 >20/周：严格大于（=20 不触发，21 触发）。"""
    assert check_long_tasks(TriggerCounters(long_tasks_weekly=THRESHOLD_LONG_TASKS_WEEKLY)) is None
    rec = check_long_tasks(TriggerCounters(long_tasks_weekly=THRESHOLD_LONG_TASKS_WEEKLY + 1))
    assert rec is not None and rec.trigger_id == "TRG-TEMPORAL-LONGTASKS"
    assert rec.threshold == 20 and rec.observed == 21
    assert rec.decision_rule  # 预注册决策规则随记录固化


def test_trigger_cross_service_failures_and_services():
    """跨服务失败 >5/周 与 服务 >5 两个触发器的边界与记录形态。"""
    assert check_cross_service_failures(
        TriggerCounters(cross_service_failures_weekly=THRESHOLD_XSVC_FAILURES_WEEKLY)) is None
    r1 = check_cross_service_failures(TriggerCounters(cross_service_failures_weekly=6))
    assert r1 is not None and r1.trigger_id == "TRG-TEMPORAL-XSVC-FAIL"
    assert check_services(TriggerCounters(services_count=THRESHOLD_SERVICES)) is None
    r2 = check_services(TriggerCounters(services_count=THRESHOLD_SERVICES + 1))
    assert r2 is not None and r2.trigger_id == "TRG-ARGOCD-SERVICES"


def test_trigger_sandbox_spend_requires_two_consecutive_months():
    """云沙箱月支出 >¥500：蓝图 §8 语义=连续 2 个月（单月不触发）。"""
    assert check_sandbox_spend(TriggerCounters(sandbox_monthly_spend=(600.0,))) is None
    assert check_sandbox_spend(TriggerCounters(sandbox_monthly_spend=(400.0, 600.0))) is None
    rec = check_sandbox_spend(TriggerCounters(sandbox_monthly_spend=(600.0, 610.0)))
    assert rec is not None and rec.trigger_id == "TRG-E2B-SPEND"
    assert rec.threshold == THRESHOLD_SANDBOX_MONTHLY_SPEND == 500.0
    # 工单简写字面语义（单月即触发）= consecutive_months=1
    assert check_sandbox_spend(
        TriggerCounters(sandbox_monthly_spend=(600.0,)), consecutive_months=1) is not None
    with pytest.raises(MetaGovernanceError):
        check_sandbox_spend(TriggerCounters(sandbox_monthly_spend=()), consecutive_months=0)


def test_trigger_check_all_returns_only_fired():
    """全量巡检：只返回触发的记录；全部低于阈值 → 空列表。"""
    fired = check_all(TriggerCounters(long_tasks_weekly=30))
    assert [r.trigger_id for r in fired] == ["TRG-TEMPORAL-LONGTASKS"]
    assert check_all(TriggerCounters()) == []


# ── instance_registrar ───────────────────────────────────────────────────────

def test_registrar_register_six_modes_and_reject_bad():
    """实例注册：六种交付模式是枚举（蓝图 §3.1，模式之间无继承）；坏 mode 拒绝。"""
    reg = InstanceRegistrar()
    inst = reg.register(InstanceSpec(instance_id="i-1", name="自家实例", mode="reference"))
    assert inst.registered_at > 0
    for mode in ("shared_hosted", "bespoke_hosted", "bespoke_self",
                 "cloud_hosted", "cloud_self", "reference"):
        reg.register(InstanceSpec(instance_id=f"i-{mode}", name=mode, mode=mode))
    assert len(reg.instances) == 7
    with pytest.raises(MetaGovernanceError):
        InstanceSpec(instance_id="i-x", name="x", mode="franchise")
    with pytest.raises(MetaGovernanceError):
        reg.register(InstanceSpec(instance_id="i-1", name="重复", mode="reference"))


def test_registrar_deviation_list_declares_delta_not_theory():
    """偏差清单：只声明差异；"别扭"摩擦标注可登记（§8 演进输入：实例摩擦）。"""
    reg = InstanceRegistrar()
    reg.register(InstanceSpec(instance_id="i-9", name="分叉实例", mode="bespoke_self"))
    d1 = reg.record_deviation("i-9", "leases", "额度单位用美元分，非抽象整数")
    d2 = reg.record_deviation("i-9", "guardrail", "强制 seal_ref 接内部对象存储，别扭",
                              friction=True)
    assert d1.friction is False and d2.friction is True
    assert [d.deviation_id for d in reg.deviations_of("i-9")] == [d1.deviation_id, d2.deviation_id]
    assert reg.deviations_of("i-9", object_name="leases") == [d1]
    with pytest.raises(MetaGovernanceError):
        reg.record_deviation("unknown-i", "leases", "delta")
    with pytest.raises(MetaGovernanceError):
        reg.record_deviation("i-9", "no-such-object", "delta")


def test_registrar_bump_generates_suggestions_with_deviation_refs():
    """理论 bump → 每个未跟随新版本的实例生成建议单；偏差引用与摩擦单独回流（§7/§8）。"""
    reg = InstanceRegistrar()
    reg.register(InstanceSpec(instance_id="i-a", name="A", mode="reference",
                              theory_versions={"leases": "0.1.0"}))
    reg.register(InstanceSpec(instance_id="i-b", name="B", mode="cloud_self",
                              theory_versions={"leases": "0.2.0"}))
    reg.record_deviation("i-a", "leases", "未实现惰性过期")
    friction = reg.record_deviation("i-a", "leases", "派生划扣时机语义别扭", friction=True)

    bump = TheoryBump(object_name="leases", from_version="0.1.0", to_version="0.2.0",
                      approved_by="owner")
    suggestions = reg.on_theory_bump(bump)
    by_instance = {s.instance_id: s for s in suggestions}
    # i-a（0.1.0）得到建议单，附带偏差与摩擦引用
    assert by_instance["i-a"].current_version == "0.1.0"
    assert by_instance["i-a"].to_version == "0.2.0"
    assert friction.deviation_id in by_instance["i-a"].deviation_refs
    assert by_instance["i-a"].friction_deviation_refs == (friction.deviation_id,)
    # i-b 已在 0.2.0 → 无建议单（蓝图 §7：给"每个实例"生成的是升级建议单，
    # 已跟随者无需建议）
    assert "i-b" not in by_instance
    # 从未登记该对象的实例同样得到建议单（current=(none)）
    reg.register(InstanceSpec(instance_id="i-c", name="C", mode="shared_hosted"))
    suggestions2 = reg.on_theory_bump(bump)
    assert any(s.instance_id == "i-c" and s.current_version == "(none)"
               for s in suggestions2)


def test_registrar_bump_requires_owner_approval():
    """未 owner 批准的 bump 不得生成建议单（§8：唯一不自动化环节）。"""
    reg = InstanceRegistrar()
    reg.register(InstanceSpec(instance_id="i-1", name="X", mode="reference"))
    with pytest.raises(MetaGovernanceError):
        reg.on_theory_bump(TheoryBump(object_name="leases", to_version="9.9.9"))


# ── theory 文件引用边界（product 只存路径，不 import theory 代码）─────────────

def test_theory_doc_path_constants_point_to_real_files():
    """文件引用形态的边界证据：THEORY_OBJECT_DOCS 登记的路径真实存在三件套。"""
    assert THEORY_DIR_REF == "theory/"
    for obj, rel in THEORY_OBJECT_DOCS.items():
        base = GLUE_ROOT / rel
        for piece in ("spec.md", "eval.md", "contract.md"):
            assert (base / piece).is_file(), f"missing {base / piece}"


def test_meta_governance_never_imports_theory():
    """import 边界：本模块源码不出现 import theory / from theory（蓝图 §1.2）。"""
    import jiuwen_glue.meta_governance as mg

    src = inspect.getsource(mg)
    for line in src.splitlines():
        s = line.strip()
        assert not (s.startswith("import theory") or s.startswith("from theory")), s
