# coding: utf-8
"""meta_governance — 元治理三角色骨架（v2.0 §8；W-06）.

三个角色（蓝图 v2.0 §8：运行在 srv-1，此处为其**纯对象+函数骨架**）：

1. **theory_keeper（理论守门人）**：消化外部前沿为理论提案——
   ``TheoryKeeper.propose(FrontierInput) -> TheoryProposal``；
   也可由触发记录直接开提案（§8"触发即自动开提案"）。
2. **trigger_sentinel（触发哨兵）**：巡检预注册触发器——
   ``check_all(counters) -> List[TriggerRecord]``。四个预注册触发器（W-06 范围；
   蓝图 §8 全集另含"环境>3"与"Nacos 多部门租户"，标【待扩展】）：
   - TRG-TEMPORAL-LONGTASKS：长任务 >20/周（蓝图 §8 Temporal）
   - TRG-TEMPORAL-XSVC-FAIL：跨服务状态失败 >5/周（蓝图 §8 Temporal）
   - TRG-ARGOCD-SERVICES：服务 >5（蓝图 §8 ArgoCD）
   - TRG-E2B-SPEND：云沙箱月支出 >¥500 连续 2 个月（蓝图 §8 E2B 自托管
     "云沙箱月支出>¥500×2 月"；W-06 工单简写为">¥500"，`consecutive_months`
     参数默认 2 为蓝图语义，传 1 即工单字面语义）
3. **instance_registrar（实例注册员）**：实例注册表条目（六种交付模式，
   蓝图 §3.1）+ 偏差清单（§2 术语：不复制理论内容，只声明差异；含
   §8"别扭"摩擦标注）+ 理论 bump → 每实例升级建议单（蓝图 §7）。

流程对齐（蓝图 §8）：提案 → **owner 批准（递归结构里唯一不自动化的环节）** →
影子 → 参考实现 → 实例建议单 → 各实例按节奏合入。

**[待接线]** 本模块是纯骨架：全部输入以普通对象传参，不接任何真实数据源
（无 srv-1 Postgres / usage_events / 计费系统查询）。接线工单归属后续工单。

**import 边界（theory 圈隔离，蓝图 §1.2）**：product 不 import theory 的实现——
本模块对 theory 层只以**文件路径常量**（``THEORY_DIR_REF`` / ``THEORY_OBJECT_DOCS``）
做文件引用形态的登记，不存在 ``import theory``；theory 文档（theory/*.md）亦不
import 任何圈的代码。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import GlueError

# ── theory 层文件引用（只存路径，不 import——蓝图 §1.2 依赖方向）─────────────

THEORY_DIR_REF = "theory/"
THEORY_OBJECT_DOCS: Dict[str, str] = {
    "guardrail": "theory/guardrail/",
    "leases": "theory/leases/",
}

# ── 错误 ─────────────────────────────────────────────────────────────────────


class MetaGovernanceError(GlueError):
    """元治理骨架的 schema/状态错误（W-06 新增，归属本模块）。"""


# ── 通用 ─────────────────────────────────────────────────────────────────────

def _utcnow() -> float:
    import time

    return time.time()


def _require_non_empty(value: str, name: str) -> str:
    if not value or not isinstance(value, str):
        raise MetaGovernanceError(f"{name} must be a non-empty string")
    return value


# ══════════════════════════════════════════════════════════════════════════════
# 角色一：theory_keeper（理论守门人）——外部前沿 → 理论提案骨架
# ══════════════════════════════════════════════════════════════════════════════

APPROVAL_PENDING = "PENDING"
APPROVAL_APPROVED = "APPROVED"
APPROVAL_REJECTED = "REJECTED"


@dataclass(frozen=True)
class FrontierInput:
    """外部前沿输入（§8 演进输入第一级：外部前沿）。[待接线：无自动采集]"""

    source: str            # 来源（如 "handbook-3.3" / "openjiwen-release" / "paper"）
    title: str             # 一句话主题
    digest: str            # 消化后的要点（keeper 的"消化"产物）
    relevant_objects: Tuple[str, ...] = ()   # 涉及的治理对象（theory 对象名）
    evidence_ref: str = ""                   # 证据引用（文件/URL/轨迹）


@dataclass
class TheoryProposal:
    """理论提案对象骨架（§8 流程起点；含预注册决策规则）。

    状态推进只有一个人工写入点：owner 批准/否决（§8"唯一不自动化的环节"；
    §7 唯一人工写入点 = theory.Approval）。影子/合入字段仅登记，不推进。
    """

    proposal_id: str
    objects: Tuple[str, ...]        # 提案变更的 theory 对象（如 ("guardrail",)）
    motivation: str                 # 动机（来自前沿消化或触发记录）
    decision_rule: str              # 预注册决策规则（§8：提案含预注册决策规则）
    source: str                     # "frontier" / 触发器 ID / "owner-strategy"
    evidence_ref: str = ""
    approval: str = APPROVAL_PENDING   # PENDING / APPROVED / REJECTED（owner 写入点）
    approved_by: Optional[str] = None
    shadow_status: str = "not-started"  # [待接线] 影子期登记位（§8：批准后影子）
    created_at: float = 0.0


class TheoryKeeper:
    """theory-keeper 骨架：FrontierInput / TriggerRecord → TheoryProposal。

    [待接线] 真实部署时前端接外部前沿采集（论文/厂商 release/手册更新巡检），
    后端接提案流水线（同流水线同门控，蓝图 §0 原则 11）。
    """

    def __init__(self, *, now: Optional[Callable[[], float]] = None) -> None:
        self._now = now or _utcnow
        self.proposals: List[TheoryProposal] = []

    def propose(self, frontier: FrontierInput, *,
                decision_rule: str = "owner-approval-then-shadow") -> TheoryProposal:
        """外部前沿 → 理论提案骨架。relevant_objects 必须是已登记的 theory 对象。"""
        _require_non_empty(frontier.source, "frontier.source")
        _require_non_empty(frontier.title, "frontier.title")
        _require_non_empty(frontier.digest, "frontier.digest")
        if not frontier.relevant_objects:
            raise MetaGovernanceError(
                "frontier.relevant_objects must name at least one theory object")
        unknown = [o for o in frontier.relevant_objects if o not in THEORY_OBJECT_DOCS]
        if unknown:
            raise MetaGovernanceError(
                f"unknown theory objects {unknown}; registered: {sorted(THEORY_OBJECT_DOCS)}")
        proposal = TheoryProposal(
            proposal_id=uuid.uuid4().hex,
            objects=tuple(frontier.relevant_objects),
            motivation=f"{frontier.title}: {frontier.digest}",
            decision_rule=_require_non_empty(decision_rule, "decision_rule"),
            source=frontier.source,
            evidence_ref=frontier.evidence_ref,
            created_at=self._now(),
        )
        self.proposals.append(proposal)
        return proposal

    def propose_from_trigger(self, record: "TriggerRecord") -> TheoryProposal:
        """触发记录 → 提案（§8"触发即自动开提案（含预注册决策规则）"）。"""
        proposal = TheoryProposal(
            proposal_id=uuid.uuid4().hex,
            objects=(record.object_scope,) if record.object_scope else ("process",),
            motivation=f"trigger {record.trigger_id} fired: "
                       f"observed {record.observed} > threshold {record.threshold}",
            decision_rule=record.decision_rule,
            source=record.trigger_id,
            evidence_ref=record.source_ref,
            created_at=self._now(),
        )
        self.proposals.append(proposal)
        return proposal

    def owner_decide(self, proposal: TheoryProposal, *, approved: bool,
                     by: str) -> TheoryProposal:
        """owner 批准/否决——递归结构里唯一不自动化的环节（§8；§7 theory.Approval）。"""
        if proposal.approval != APPROVAL_PENDING:
            raise MetaGovernanceError(
                f"proposal {proposal.proposal_id} already decided ({proposal.approval})")
        _require_non_empty(by, "by")
        proposal.approval = APPROVAL_APPROVED if approved else APPROVAL_REJECTED
        proposal.approved_by = by
        return proposal


# ══════════════════════════════════════════════════════════════════════════════
# 角色二：trigger_sentinel（触发哨兵）——预注册触发器巡检
# ══════════════════════════════════════════════════════════════════════════════

TRG_TEMPORAL_LONGTASKS = "TRG-TEMPORAL-LONGTASKS"
TRG_TEMPORAL_XSVC_FAIL = "TRG-TEMPORAL-XSVC-FAIL"
TRG_ARGOCD_SERVICES = "TRG-ARGOCD-SERVICES"
TRG_E2B_SPEND = "TRG-E2B-SPEND"

# 预注册阈值（蓝图 §8；改动即理论变更，须走提案——预注册的意义就是防随手改阈值）
THRESHOLD_LONG_TASKS_WEEKLY = 20
THRESHOLD_XSVC_FAILURES_WEEKLY = 5
THRESHOLD_SERVICES = 5
THRESHOLD_SANDBOX_MONTHLY_SPEND = 500.0     # ¥
SANDBOX_SPEND_CONSECUTIVE_MONTHS = 2        # 蓝图 §8 "×2 月"

# 预注册决策规则（触发即按此开提案，§8）
_DECISION_RULES = {
    TRG_TEMPORAL_LONGTASKS: "harness 长任务协议升级提案（预注册）",
    TRG_TEMPORAL_XSVC_FAIL: "跨服务状态一致性提案（预注册）",
    TRG_ARGOCD_SERVICES: "GitOps 演进提案（预注册）",
    TRG_E2B_SPEND: "E2B 自托管提案（预注册）",
}
# 触发器影响的 theory 对象范围（None=流程级提案，不落单一治理对象）
_OBJECT_SCOPE = {
    TRG_TEMPORAL_LONGTASKS: None,
    TRG_TEMPORAL_XSVC_FAIL: None,
    TRG_ARGOCD_SERVICES: None,
    TRG_E2B_SPEND: None,
}


@dataclass(frozen=True)
class TriggerCounters:
    """触发器计数对象（sentinel 的唯一输入形态）。[待接线：计数值由数据面产生]"""

    long_tasks_weekly: int = 0              # 本周长任务数（Temporal）
    cross_service_failures_weekly: int = 0  # 本周跨服务状态失败数（Temporal）
    services_count: int = 0                 # 当前服务数（ArgoCD）
    sandbox_monthly_spend: Tuple[float, ...] = ()  # 云沙箱月支出，按月升序，最后一位=当月（E2B）


@dataclass(frozen=True)
class TriggerRecord:
    """触发记录：一次触发的固化证据（谁、阈值、观测值、下一步规则）。"""

    trigger_id: str
    threshold: float
    observed: float
    decision_rule: str          # 预注册决策规则
    object_scope: Optional[str] # 触发器涉及的 theory 对象（None=流程级）
    source_ref: str = ""        # 计数来源引用 [待接线：数据面证据链]
    triggered_at: float = 0.0


def _record(trigger_id: str, threshold: float, observed: float, *,
            now: Callable[[], float]) -> TriggerRecord:
    return TriggerRecord(
        trigger_id=trigger_id, threshold=threshold, observed=observed,
        decision_rule=_DECISION_RULES[trigger_id],
        object_scope=_OBJECT_SCOPE[trigger_id], triggered_at=now())


def check_long_tasks(counters: TriggerCounters, *, now: Optional[Callable[[], float]] = None
                     ) -> Optional[TriggerRecord]:
    """Temporal：长任务 >20/周（严格大于；=20 不触发）。"""
    if counters.long_tasks_weekly > THRESHOLD_LONG_TASKS_WEEKLY:
        return _record(TRG_TEMPORAL_LONGTASKS, THRESHOLD_LONG_TASKS_WEEKLY,
                       counters.long_tasks_weekly, now=now or _utcnow)
    return None


def check_cross_service_failures(counters: TriggerCounters, *,
                                 now: Optional[Callable[[], float]] = None
                                 ) -> Optional[TriggerRecord]:
    """Temporal：跨服务状态失败 >5/周（严格大于）。"""
    if counters.cross_service_failures_weekly > THRESHOLD_XSVC_FAILURES_WEEKLY:
        return _record(TRG_TEMPORAL_XSVC_FAIL, THRESHOLD_XSVC_FAILURES_WEEKLY,
                       counters.cross_service_failures_weekly, now=now or _utcnow)
    return None


def check_services(counters: TriggerCounters, *,
                   now: Optional[Callable[[], float]] = None) -> Optional[TriggerRecord]:
    """ArgoCD：服务 >5（严格大于；蓝图 §8 同族触发器"环境>3"【待扩展】）。"""
    if counters.services_count > THRESHOLD_SERVICES:
        return _record(TRG_ARGOCD_SERVICES, THRESHOLD_SERVICES,
                       counters.services_count, now=now or _utcnow)
    return None


def check_sandbox_spend(counters: TriggerCounters, *,
                        consecutive_months: int = SANDBOX_SPEND_CONSECUTIVE_MONTHS,
                        now: Optional[Callable[[], float]] = None
                        ) -> Optional[TriggerRecord]:
    """E2B 自托管：云沙箱月支出 >¥500 连续 N 个月（蓝图 §8 "×2 月"，默认 N=2）。

    W-06 工单简写为"月支出>¥500"——传 ``consecutive_months=1`` 即得该字面语义。
    月序列不足 N 个月时不触发（证据不足，fail-closed 同精神：不凭半个证据开提案）。
    """
    if consecutive_months < 1:
        raise MetaGovernanceError("consecutive_months must be >= 1")
    months = counters.sandbox_monthly_spend[-consecutive_months:]
    if len(months) < consecutive_months:
        return None
    if all(spend > THRESHOLD_SANDBOX_MONTHLY_SPEND for spend in months):
        return _record(TRG_E2B_SPEND, THRESHOLD_SANDBOX_MONTHLY_SPEND,
                       max(months), now=now or _utcnow)
    return None


_CHECKS = (check_long_tasks, check_cross_service_failures,
           check_services, check_sandbox_spend)


def check_all(counters: TriggerCounters, *,
              now: Optional[Callable[[], float]] = None) -> List[TriggerRecord]:
    """全量巡检：返回本次触发的全部记录（未触发的触发器不产生记录）。"""
    out: List[TriggerRecord] = []
    for check in _CHECKS:
        rec = check(counters, now=now)
        if rec is not None:
            out.append(rec)
    return out


def propose_all(keeper: "TheoryKeeper", counters: TriggerCounters, *,
                now: Optional[Callable[[], float]] = None) -> List[TriggerRecord]:
    """巡检+自动开案的便捷接线：全量巡检，并对每条触发记录开提案（§8
    "触发即自动开提案（含预注册决策规则）"）。返回本次触发的全部记录。
    [待接线] 真实部署由 trigger-sentinel 巡检任务周期调用。"""
    records = check_all(counters, now=now)
    for rec in records:
        keeper.propose_from_trigger(rec)
    return records


# ══════════════════════════════════════════════════════════════════════════════
# 角色三：instance_registrar（实例注册员）——注册表 + 偏差清单 + 建议
# ══════════════════════════════════════════════════════════════════════════════

MODE_SHARED_HOSTED = "shared_hosted"
MODE_BESPOKE_HOSTED = "bespoke_hosted"
MODE_BESPOKE_SELF = "bespoke_self"
MODE_CLOUD_HOSTED = "cloud_hosted"
MODE_CLOUD_SELF = "cloud_self"
MODE_REFERENCE = "reference"
INSTANCE_MODES = (MODE_SHARED_HOSTED, MODE_BESPOKE_HOSTED, MODE_BESPOKE_SELF,
                  MODE_CLOUD_HOSTED, MODE_CLOUD_SELF, MODE_REFERENCE)


@dataclass
class InstanceSpec:
    """实例注册表条目：参考实现的一个部署/分叉（§2 术语"实例"）。

    六种交付模式只是实例的 mode 枚举，模式之间**无继承**（蓝图 §3.1）。
    ``theory_versions``：theory 对象名 → 该实例当前跟随的 semver。
    [待接线] contact/endpoints 等部署事实由实例方登记，本骨架不采集。
    """

    instance_id: str
    name: str
    mode: str                            # INSTANCE_MODES 之一
    theory_versions: Dict[str, str] = field(default_factory=dict)
    contact: str = ""                    # [待接线]
    registered_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.instance_id or not isinstance(self.instance_id, str):
            raise MetaGovernanceError("instance_id must be a non-empty string")
        _require_non_empty(self.name, "name")
        if self.mode not in INSTANCE_MODES:
            raise MetaGovernanceError(
                f"mode must be one of {INSTANCE_MODES}, got {self.mode!r}")


@dataclass(frozen=True)
class DeviationEntry:
    """偏差清单条目：实例相对其注册表条目的差量声明（§2 术语"偏差清单"）。

    **不复制理论内容，只声明差异**（§2 禁止的混用）；``friction=True`` 即
    §8 演进输入第三级"实例摩擦（偏差清单中的'别扭'标注）"。
    """

    deviation_id: str
    instance_id: str
    object_name: str                     # 偏差涉及的 theory 对象
    delta: str                           # 差量声明（只写"与理论差在哪"）
    friction: bool = False               # "别扭"标注（演进输入：实例摩擦）
    opened_at: float = 0.0


@dataclass(frozen=True)
class TheoryBump:
    """一次理论版本 bump（对象级）。owner Approval 前不得生成建议单（§7/§8）。"""

    object_name: str
    to_version: str
    from_version: str = ""
    proposal_ref: Optional[str] = None   # 关联 TheoryProposal.proposal_id
    approved_by: Optional[str] = None    # owner 写入点留痕（theory.Approval）


@dataclass(frozen=True)
class UpgradeSuggestion:
    """升级建议单（蓝图 §7：理论版本 bump → 实例注册表给每个实例生成升级建议单）。"""

    suggestion_id: str
    instance_id: str
    object_name: str
    current_version: str
    to_version: str
    deviation_refs: Tuple[str, ...] = ()  # 该实例在此对象上的偏差（升级时需复核）
    friction_deviation_refs: Tuple[str, ...] = ()  # 其中带"别扭"标注的（§8 实例摩擦回流）


class InstanceRegistrar:
    """instance-registrar 骨架：注册表 + 偏差清单 + bump→建议单。

    [待接线] 真实部署时注册表落 srv-1 Postgres（三接口纪律，蓝图 §4.3）；
    本骨架进程内存储，只证明对象语义。
    """

    def __init__(self, *, now: Optional[Callable[[], float]] = None) -> None:
        self._now = now or _utcnow
        self._instances: Dict[str, InstanceSpec] = {}
        self._deviations: List[DeviationEntry] = []

    # ── 注册表 ───────────────────────────────────────────────────────────

    def register(self, instance: InstanceSpec) -> InstanceSpec:
        """登记实例（重复 instance_id 拒绝）。"""
        if instance.instance_id in self._instances:
            raise MetaGovernanceError(
                f"instance {instance.instance_id} already registered")
        instance.registered_at = self._now()
        self._instances[instance.instance_id] = instance
        return instance

    def get(self, instance_id: str) -> InstanceSpec:
        try:
            return self._instances[instance_id]
        except KeyError:
            raise MetaGovernanceError(f"unknown instance: {instance_id}") from None

    @property
    def instances(self) -> List[InstanceSpec]:
        return list(self._instances.values())

    # ── 偏差清单 ─────────────────────────────────────────────────────────

    def record_deviation(self, instance_id: str, object_name: str, delta: str, *,
                         friction: bool = False) -> DeviationEntry:
        """登记一条偏差（只声明差异，不复制理论内容）。"""
        self.get(instance_id)  # unknown instance 检查
        _require_non_empty(object_name, "object_name")
        if object_name not in THEORY_OBJECT_DOCS:
            raise MetaGovernanceError(
                f"unknown theory object {object_name!r}; "
                f"registered: {sorted(THEORY_OBJECT_DOCS)}")
        _require_non_empty(delta, "delta")
        entry = DeviationEntry(
            deviation_id=uuid.uuid4().hex, instance_id=instance_id,
            object_name=object_name, delta=delta, friction=friction,
            opened_at=self._now())
        self._deviations.append(entry)
        return entry

    def deviations_of(self, instance_id: str, *,
                      object_name: Optional[str] = None) -> List[DeviationEntry]:
        return [d for d in self._deviations
                if d.instance_id == instance_id
                and (object_name is None or d.object_name == object_name)]

    # ── 理论 bump → 建议单（蓝图 §7）──────────────────────────────────────

    def on_theory_bump(self, bump: TheoryBump) -> List[UpgradeSuggestion]:
        """理论版本 bump → 给每个**未跟随新版本**的实例生成升级建议单。

        - 未 owner 批准的 bump 拒绝生成（§8：owner 批准是唯一不自动化环节）；
        - 每条建议单附该实例在此对象上的偏差引用（含"别扭"标注的单独提出——
          实例摩擦是下一轮演进的输入，§8 演进输入第三级）。
        """
        _require_non_empty(bump.object_name, "bump.object_name")
        if bump.object_name not in THEORY_OBJECT_DOCS:
            raise MetaGovernanceError(f"unknown theory object {bump.object_name!r}")
        _require_non_empty(bump.to_version, "bump.to_version")
        if not bump.approved_by:
            raise MetaGovernanceError(
                "theory bump requires owner approval before suggestions "
                "(owner approval is the only non-automated step, v2.0 §8)")
        suggestions: List[UpgradeSuggestion] = []
        for inst in self._instances.values():
            current = inst.theory_versions.get(bump.object_name, "(none)")
            if current == bump.to_version:
                continue  # 已跟随新版本，无需建议单
            devs = self.deviations_of(inst.instance_id, object_name=bump.object_name)
            suggestions.append(UpgradeSuggestion(
                suggestion_id=uuid.uuid4().hex,
                instance_id=inst.instance_id,
                object_name=bump.object_name,
                current_version=current,
                to_version=bump.to_version,
                deviation_refs=tuple(d.deviation_id for d in devs),
                friction_deviation_refs=tuple(d.deviation_id for d in devs if d.friction),
            ))
        return suggestions
