# coding: utf-8
"""三层复合身份 + 权限交集公式 — jiuwen-glue 自研对象之一（WO-0003 返工，v1.7 §12.5）.

规格来源（AI Native 研发手册 §3.3.1 原则一/二，逐字见 docs/native-handbook-boundary-check.md）:
- 原则一：一次请求至少区分三层主体——**稳定 Agent**（"这是哪个受治理的软件主体"）、
  **运行实例**（"当前是哪台设备、Pod 或沙箱在执行"）、**任务上下文**（"这次为何执行"）；
  Agent 代表用户时还要带上经过验证的用户身份和委托依据。
- 原则二：**权限只能逐级收敛**。有效权限 = 用户权限 ∩ Agent 能力上限 ∩ 平台策略 ∩
  本次委托范围 ∩ 运行时约束。子委托的范围只能比上游更小。

本模块只承载**身份对象、交集公式与收敛不变式**；不是 PDP：
- "允不允许"的最终判断仍由各执行点（TeamPermissionRail / OPA / 原生 guardrail）做出，
  本模块的交集结果是它们的输入之一——**不新增第二个决策点**（PROP-0001 v1.6 §4.9）。
- 参照系 = 阿里体系（RRSA OIDC + STS、两层交集、实例级 policy），不发明新东西；
  密码学验证（签名/OIDC）由可信 Runtime 与 OpenBao 承担，glue 只做对象与引用（如实偏差，
  见四边界核对文档"凭证"行）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, Mapping, Optional

from .errors import DelegationScopeError, IdentitySchemaError

USER = "user"                  # 用户权限
AGENT_CAPS = "agent_caps"      # Agent 能力上限
PLATFORM_POLICY = "platform_policy"  # 平台策略
DELEGATION = "delegation"      # 本次委托范围
RUNTIME = "runtime"            # 运行时约束
_SOURCES = (USER, AGENT_CAPS, PLATFORM_POLICY, DELEGATION, RUNTIME)


def _utcnow() -> float:
    import time

    return time.time()


# ── 三层复合身份（原则一）─────────────────────────────────────────────────────

@dataclass(frozen=True)
class AgentIdentity:
    """第一层：稳定 Agent——"这是哪个受治理的软件主体"。"""

    agent_id: str
    tenant_id: str = "t0"

    def __post_init__(self) -> None:
        if not self.agent_id or not isinstance(self.agent_id, str):
            raise IdentitySchemaError("agent_id must be a non-empty string")


@dataclass(frozen=True)
class RunInstance:
    """第二层：运行实例——"当前是哪台设备、Pod 或沙箱在执行"。"""

    instance_id: str
    node: str = ""                 # 设备 / Pod / 沙箱标识
    ttl_seconds: Optional[float] = None   # 实例短时性：TTL 到期即失效（可撤销窗口）
    tenant_id: str = "t0"

    def __post_init__(self) -> None:
        if not self.instance_id or not isinstance(self.instance_id, str):
            raise IdentitySchemaError("instance_id must be a non-empty string")


@dataclass(frozen=True)
class TaskContext:
    """第三层：任务上下文——"这次为何执行"（贯穿审计、撤销事件、风险信号）。"""

    task_id: str
    workspace_uri: str = ""        # 租约绑定的工作区 URI（v1.7 §13：工作区切换=认领不同工单）
    pipeline_label: str = ""       # 流水线标签（治理面只见 pipeline 标签不同的工单）
    env_ref: str = ""              # 环境定义引用（12.2 环境即交接物）
    delegation_ref: str = ""       # 委托依据引用（Delegation.delegation_id）
    tenant_id: str = "t0"

    def __post_init__(self) -> None:
        if not self.task_id or not isinstance(self.task_id, str):
            raise IdentitySchemaError("task_id must be a non-empty string")


def composite_ref(agent: AgentIdentity, run: RunInstance, task: TaskContext) -> str:
    """三层复合身份引用（单串，供 Lease.agent_ref / DecisionRecord.agent_ref /
    GuardrailSpec.agent_identity_ref 等引用）。

    形如 ``ag:<agent_id>@<tenant>/run:<instance_id>/task:<task_id>``。
    决策记录与门控上下文只传引用，不复制身份状态（跨层只传引用，4.9 #10）。
    """
    for obj, name in ((agent, "agent"), (run, "run"), (task, "task")):
        if obj is None:
            raise IdentitySchemaError(f"composite_ref requires all three layers, missing {name}")
    return (f"ag:{agent.agent_id}@{agent.tenant_id}"
            f"/run:{run.instance_id}/task:{task.task_id}")


# ── 委托与逐级收敛不变式（原则二后半）────────────────────────────────────────

@dataclass(frozen=True)
class Delegation:
    """委托依据："Agent 为什么可以代表用户"。scopes 为本次委托范围（权限表达式集合）。"""

    delegation_id: str
    subject: str                       # 被委托方（三层复合身份引用）
    scopes: FrozenSet[str] = frozenset()
    parent_delegation_id: Optional[str] = None   # 上游委托（多 Agent 协作时每一跳建立自己的身份）
    tenant_id: str = "t0"

    def __post_init__(self) -> None:
        if not self.delegation_id or not isinstance(self.delegation_id, str):
            raise IdentitySchemaError("delegation_id must be a non-empty string")
        if not self.subject or not isinstance(self.subject, str):
            raise IdentitySchemaError("subject must be a non-empty string")


def narrow_delegation(parent: Delegation, delegation_id: str, subject: str,
                      scopes: Iterable[str], *, tenant_id: str = "t0") -> Delegation:
    """派生子委托：**子委托范围只能比上游更小**（原则二）。

    scopes ⊄ parent.scopes 时抛 DelegationScopeError——目标超出本次委托范围即越权尝试
    （手册案例："模型把操作目标从当前服务改成另一个服务……目标超出本次委托范围时拒绝执行"）。
    """
    child_scopes = frozenset(scopes)
    illegal = child_scopes - parent.scopes
    if illegal:
        raise DelegationScopeError(
            f"sub-delegation {delegation_id} exceeds upstream {parent.delegation_id}: "
            f"out-of-scope {sorted(illegal)} (permissions only converge, never expand)")
    return Delegation(delegation_id=delegation_id, subject=subject,
                      scopes=child_scopes, parent_delegation_id=parent.delegation_id,
                      tenant_id=tenant_id or parent.tenant_id)


# ── 权限交集公式（原则二前半）────────────────────────────────────────────────

@dataclass(frozen=True)
class EffectivePerms:
    """五集交集结果 + 每一分量的出处引用。

    冻结进 Lease（leases.BudgetLease.effective_perms）后成为该租约的权限快照：
    签发后运行时约束变化不影响已固化快照，但逐级收敛不变式在派生时强制
    （子租约快照 ⊆ 父租约快照）。
    """

    perms: FrozenSet[str] = frozenset()
    components: Mapping[str, FrozenSet[str]] = field(default_factory=dict)
    computed_at: float = 0.0

    def allows(self, required: str) -> bool:
        """确定性成员判断：required 是否在交集内。

        这是交集公式的消费接口（PEP 落实决策时调用），不是新决策点——
        判定逻辑只有集合成员判断，无任何模型参与。
        """
        return required in self.perms

    def subset_of(self, upstream: "EffectivePerms") -> bool:
        """逐级收敛判定：本快照是否 ⊆ 上游快照。"""
        return self.perms <= upstream.perms

    def frozen(self) -> tuple:
        """固化为不可变 tuple（存入 Lease / JSONB DDL 列）。"""
        return tuple(sorted(self.perms))


def effective_permissions(user: Iterable[str], agent_caps: Iterable[str],
                          platform_policy: Iterable[str], delegation: Iterable[str],
                          runtime: Iterable[str], *,
                          now: Optional[float] = None) -> EffectivePerms:
    """有效权限 = 用户权限 ∩ Agent 能力上限 ∩ 平台策略 ∩ 本次委托范围 ∩ 运行时约束。

    用户有权执行某个操作，**不等于 Agent 自动有权代为执行**——五集交集缺一不可。
    返回结果集与每一分量的出处引用（出处 = 五个来源集合各自收敛后的内容）。
    """
    components: Dict[str, FrozenSet[str]] = {
        USER: frozenset(user),
        AGENT_CAPS: frozenset(agent_caps),
        PLATFORM_POLICY: frozenset(platform_policy),
        DELEGATION: frozenset(delegation),
        RUNTIME: frozenset(runtime),
    }
    perms: FrozenSet[str] = frozenset.intersection(*[components[s] for s in _SOURCES])
    return EffectivePerms(perms=perms, components=components,
                          computed_at=now if now is not None else _utcnow())
