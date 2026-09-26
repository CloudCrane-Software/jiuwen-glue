# coding: utf-8
"""jiuwen-glue — openJiuwen 胶水层最小实现（WO-0003 + M0.5 返工补齐）.

原生没有的能力（PROP-0001 v1.6 §0 总则 4 / §4.1；v1.7 §12.5 JIT 身份三件）:
- Budget Lease（预算租约：发放/占用/过期 + 级联撤销 + 签发时固化权限交集）→ :mod:`jiuwen_glue.leases`
- Evidence 三态（draft / verified / finalized）                          → :mod:`jiuwen_glue.evidence`
- 能力元数据注册（五类机读声明 + 版本准入 + 指标单向回流）               → :mod:`jiuwen_glue.capabilities`
- 协同三铁律的执行与检测                                                 → :mod:`jiuwen_glue.rules`
- GuardrailRun 门控协议聚合薄层（五步链路 + 聚合三态 fail-closed）       → :mod:`jiuwen_glue.guardrail`
- 三层复合身份 + 权限交集公式（研发手册原则一/二）                       → :mod:`jiuwen_glue.identity`
- Challenge 结构化对象（授权三态第三态）                                 → :mod:`jiuwen_glue.challenge`
- 记忆晋升管线（个人→组织资产晋升：质量门+脱敏门+版本化，不回写执行面）  → :mod:`jiuwen_glue.promotion`
- 决策记录（append-only）                                                → :mod:`jiuwen_glue.decisions`
- 产物路由表 + 节点容量模型（便宜三件 Python 侧）                        → :mod:`jiuwen_glue.routes`

边界（写死）: 原生层管"怎么做"，glue 管"准不准进"，控制台管"看得见"；
发现与编排一律用原生 Symphony，本包不自建发现机制；
**glue 不新增第二个决策点**（GuardrailRun 聚合 verdict 是唯一门控输出）。
"""
from .capabilities import (
    ADMITTED,
    CANDIDATE,
    REJECTED,
    CapabilityDeclaration,
    CapabilityRegistry,
    CapabilityVersion,
)
from .challenge import (
    APPROVED,
    CONFIRM_DUTY_OFFICER,
    CONFIRM_RESOURCE_OWNER,
    CONFIRM_USER,
    DENIED,
    EXPIRED as CHALLENGE_EXPIRED,
    PENDING as CHALLENGE_PENDING,
    Challenge,
    ChallengeBoard,
)
from .decisions import DecisionLog, DecisionRecord, context_hash
from .errors import (
    AdmissionDeniedError,
    ArtifactRouteUndeclaredError,
    BudgetExceededError,
    CapacitySchemaError,
    ChallengeStateError,
    DecisionSchemaError,
    DeclarationSchemaError,
    DelegationScopeError,
    EvidenceImmutableError,
    GuardrailSchemaError,
    GuardrailStateError,
    IdentitySchemaError,
    IllegalTransitionError,
    IronRuleViolation,
    LeaseDerivationError,
    LeaseExhaustedError,
    LeaseExpiredError,
    LeaseRevokedError,
    MissingTaskReferenceError,
    PromotionTransitionError,
    RouteSchemaError,
    Rule1MessageIsNotClaim,
    Rule2ConversationIsNotState,
    Rule3InternalStepIsNotTask,
    UnknownCapabilityError,
    UnknownChallengeError,
    UnknownDecisionError,
    UnknownGuardrailRunError,
    UnknownPromotionAssetError,
)
from .evidence import DRAFT, FINALIZED, VERIFIED, Evidence, EvidenceStore, EvidenceTransition
from .guardrail import (
    BACKEND_EVAL_GATE,
    BACKEND_NATIVE_GUARDRAIL,
    BACKEND_PERMISSION_RAIL,
    BACKEND_SCAN,
    OUTCOME_ASK,
    OUTCOME_BLOCKED,
    OUTCOME_PASS,
    OUTCOME_UNKNOWN,
    RUN_FINALIZED,
    RUN_OPEN,
    RUN_VOID,
    VERDICT_BLOCKED,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
    CheckResult,
    CheckSpec,
    GuardrailResult,
    GuardrailRun,
    GuardrailRunStore,
    GuardrailSpec,
    aggregate,
)
from .identity import (
    AGENT_CAPS,
    DELEGATION,
    PLATFORM_POLICY,
    RUNTIME,
    USER,
    AgentIdentity,
    Delegation,
    EffectivePerms,
    RunInstance,
    TaskContext,
    composite_ref,
    effective_permissions,
    narrow_delegation,
)
from .leases import ACTIVE, EXHAUSTED, EXPIRED, REVOKED, BudgetLedger, BudgetLease
from .promotion import (
    GATE_BLOCKED,
    GATE_PASS,
    GATE_UNKNOWN,
    STAGE_PROMOTED,
    STAGE_REJECTED,
    STAGE_SHORTLIST,
    STAGE_WITHDRAWN,
    STAGE_WORKING,
    PromotionCandidate,
    PromotionLedger,
    PromotionTransition,
)
from .routes import (
    KIND_CODE,
    KIND_EVAL,
    KIND_VIDEO,
    PIPELINE_DEFAULT,
    SINK_EVAL_ASSETS,
    SINK_GIT,
    SINK_MINIO,
    TRUST_TRUSTED,
    TRUST_UNTRUSTED,
    ArtifactRoute,
    ArtifactRouteTable,
    NodeCapacity,
)
from .rules import (
    BLOCKED,
    CANCELLED,
    CLAIMED,
    COMPLETED,
    PENDING,
    MessageReceipt,
    SubtaskSpec,
    Task,
    TaskLedger,
)

__version__ = "0.2.0"

__all__ = [
    # errors
    "IronRuleViolation",
    "Rule1MessageIsNotClaim", "Rule2ConversationIsNotState", "Rule3InternalStepIsNotTask",
    "BudgetExceededError", "LeaseDerivationError", "LeaseExhaustedError",
    "LeaseExpiredError", "LeaseRevokedError",
    "IllegalTransitionError", "EvidenceImmutableError",
    "DeclarationSchemaError", "AdmissionDeniedError", "UnknownCapabilityError",
    "MissingTaskReferenceError",
    "DelegationScopeError", "GuardrailSchemaError", "GuardrailStateError",
    "IdentitySchemaError",
    "UnknownGuardrailRunError", "ChallengeStateError", "UnknownChallengeError",
    "PromotionTransitionError", "UnknownPromotionAssetError",
    "DecisionSchemaError", "UnknownDecisionError",
    "ArtifactRouteUndeclaredError", "RouteSchemaError", "CapacitySchemaError",
    # leases
    "BudgetLedger", "BudgetLease",
    "ACTIVE", "EXHAUSTED", "EXPIRED", "REVOKED",
    # evidence
    "Evidence", "EvidenceStore", "EvidenceTransition",
    "DRAFT", "VERIFIED", "FINALIZED",
    # capabilities
    "CapabilityDeclaration", "CapabilityRegistry", "CapabilityVersion",
    "CANDIDATE", "ADMITTED", "REJECTED",
    # rules
    "Task", "TaskLedger", "MessageReceipt", "SubtaskSpec",
    "PENDING", "CLAIMED", "COMPLETED", "BLOCKED", "CANCELLED",
    # guardrail（聚合三态 = 唯一门控输出）
    "GuardrailSpec", "CheckSpec", "CheckResult", "GuardrailRun", "GuardrailRunStore",
    "GuardrailResult", "aggregate",
    "BACKEND_NATIVE_GUARDRAIL", "BACKEND_PERMISSION_RAIL", "BACKEND_EVAL_GATE", "BACKEND_SCAN",
    "VERDICT_PASS", "VERDICT_BLOCKED", "VERDICT_UNKNOWN",
    "OUTCOME_PASS", "OUTCOME_BLOCKED", "OUTCOME_UNKNOWN", "OUTCOME_ASK",
    "RUN_OPEN", "RUN_FINALIZED", "RUN_VOID",
    # identity（三层复合身份 + 权限交集公式）
    "AgentIdentity", "RunInstance", "TaskContext", "Delegation",
    "composite_ref", "effective_permissions", "EffectivePerms", "narrow_delegation",
    "USER", "AGENT_CAPS", "PLATFORM_POLICY", "DELEGATION", "RUNTIME",
    # challenge（授权三态第三态）
    "Challenge", "ChallengeBoard",
    "CHALLENGE_PENDING", "CHALLENGE_EXPIRED",
    "APPROVED", "DENIED",
    "CONFIRM_USER", "CONFIRM_RESOURCE_OWNER", "CONFIRM_DUTY_OFFICER",
    # promotion（记忆晋升管线）
    "PromotionLedger", "PromotionCandidate", "PromotionTransition",
    "STAGE_WORKING", "STAGE_SHORTLIST", "STAGE_PROMOTED", "STAGE_REJECTED", "STAGE_WITHDRAWN",
    "GATE_PASS", "GATE_BLOCKED", "GATE_UNKNOWN",
    # decisions（决策记录，append-only）
    "DecisionLog", "DecisionRecord", "context_hash",
    # routes（便宜三件 Python 侧）
    "ArtifactRoute", "ArtifactRouteTable", "NodeCapacity",
    "SINK_GIT", "SINK_MINIO", "SINK_EVAL_ASSETS",
    "KIND_CODE", "KIND_VIDEO", "KIND_EVAL", "PIPELINE_DEFAULT",
    "TRUST_TRUSTED", "TRUST_UNTRUSTED",
    "__version__",
]
