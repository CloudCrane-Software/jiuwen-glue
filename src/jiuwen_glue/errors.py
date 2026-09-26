# coding: utf-8
"""jiuwen-glue: 胶水层异常类型.

所有违规异常都携带 ``rule_no``/``code`` 便于机器判定；所有异常在触发时
都会被对应 store 记录进 violation/audit 日志（检测 = 抛出 + 留痕）。
"""
from __future__ import annotations

from typing import Optional


class GlueError(Exception):
    """jiuwen-glue 基础异常。"""


# ── Budget Lease ──────────────────────────────────────────────────────────────

class LeaseError(GlueError):
    pass


class UnknownLeaseError(LeaseError):
    pass


class LeaseExpiredError(LeaseError):
    pass


class LeaseRevokedError(LeaseError):
    pass


class LeaseExhaustedError(LeaseError):
    pass


class BudgetExceededError(LeaseError):
    """占用超过剩余预算（超额占用被拒绝）。"""


class LeaseDerivationError(LeaseError):
    """子租约派生不合法（例如派生额超过父剩余额）。"""


# ── Evidence 三态 ─────────────────────────────────────────────────────────────

class EvidenceError(GlueError):
    pass


class UnknownEvidenceError(EvidenceError):
    pass


class IllegalTransitionError(EvidenceError):
    """非法状态迁移（跳态 / 回退 / 终态修改）。"""


class EvidenceImmutableError(EvidenceError):
    """非 DRAFT 状态的 Evidence 内容不可修改；FINALIZED 不可再做任何变更。"""


# ── 能力元数据 ────────────────────────────────────────────────────────────────

class CapabilityError(GlueError):
    pass


class DeclarationSchemaError(CapabilityError):
    """能力声明不满足机读 schema（五类声明字段非法）。"""


class AdmissionDeniedError(CapabilityError):
    """版本准入被拒（缺 VERIFIED/FINALIZED 证据或证据不达标）。"""


class UnknownCapabilityError(CapabilityError):
    pass


# ── 协同三铁律 ────────────────────────────────────────────────────────────────

class IronRuleViolation(GlueError):
    """三条铁律违规基类：违规必被检测（抛出 + 记入 violation_log）。"""

    rule_no: int = 0
    code: str = "IRON_RULE_VIOLATION"


class Rule1MessageIsNotClaim(IronRuleViolation):
    """铁律 1：消息发送成功不代表任务已被承接。"""

    rule_no = 1
    code = "MESSAGE_IS_NOT_CLAIM"


class Rule2ConversationIsNotState(IronRuleViolation):
    """铁律 2：不能把对话历史当作 Team State。"""

    rule_no = 2
    code = "CONVERSATION_IS_NOT_STATE"


class Rule3InternalStepIsNotTask(IronRuleViolation):
    """铁律 3：同一成员连续完成的内部步骤不建任务（无独立交付/不同责任/明确依赖不得拆）。"""

    rule_no = 3
    code = "INTERNAL_STEP_IS_NOT_TASK"


class UnknownTaskError(GlueError):
    pass


class MissingTaskReferenceError(GlueError):
    """完成状态变更缺少 TaskRun / Artifact 引用（协作事实必须有台账承载）。"""


# ── 三层复合身份与权限交集（WO-0003 返工：研发手册原则一/二）─────────────────

class IdentityError(GlueError):
    pass


class IdentitySchemaError(IdentityError):
    """三层身份对象字段不合法（缺稳定 Agent id / 实例 id / 任务 id 等）。"""


class DelegationScopeError(IdentityError):
    """子委托范围超出上游（原则二：权限只能逐级收敛，子委托 ⊆ 上游）。"""


# ── GuardrailRun 协议聚合（WO-0003 返工：手册 §3.3.2）────────────────────────

class GuardrailError(GlueError):
    pass


class UnknownGuardrailRunError(GuardrailError):
    pass


class GuardrailSchemaError(GuardrailError):
    """GuardrailSpec / CheckSpec 不满足声明 schema（backend 枚举、必填上下文等）。"""


class GuardrailStateError(GuardrailError):
    """GuardrailRun 非法状态操作（固化后提交 / 重复 finalize 等）。"""


# ── Challenge（授权三态第三态：结构化授权要求）───────────────────────────────

class ChallengeError(GlueError):
    pass


class UnknownChallengeError(ChallengeError):
    pass


class ChallengeStateError(ChallengeError):
    """Challenge 非法状态操作（对已决/已过期的 Challenge 再裁决等）。"""


# ── 记忆晋升管线（v1.6 4.9 #4：只做"个人→组织"资产晋升）──────────────────────

class PromotionError(GlueError):
    pass


class UnknownPromotionAssetError(PromotionError):
    pass


class PromotionTransitionError(PromotionError):
    """晋升阶段机非法迁移（跳态 / 回退 / 终态再迁移）。"""


# ── 决策记录（append-only）────────────────────────────────────────────────────

class DecisionError(GlueError):
    pass


class DecisionSchemaError(DecisionError):
    """决策记录字段不合法（chosen 不在 options 中、缺 agent_ref 等）。"""


class UnknownDecisionError(DecisionError):
    pass


# ── 产物路由 / 节点容量（便宜三件：environments/、artifact_routes、GPU 份额）──

class RouteError(GlueError):
    pass


class ArtifactRouteUndeclaredError(RouteError):
    """(pipeline, artifact_kind) 未声明路由——防产物误入 git / 误入 DAM（v1.7 §13）。"""


class RouteSchemaError(RouteError):
    """路由声明字段不合法。"""


class CapacitySchemaError(GlueError):
    """节点容量声明不合法（份额越界 / max_parallel < 1 / 信任等级枚举外）。"""
