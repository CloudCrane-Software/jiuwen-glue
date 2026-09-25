# coding: utf-8
"""jiuwen-glue — openJiuwen 胶水层最小实现（WO-0003 第 3-5 步）.

四样原生没有的能力（PROP-0001 v1.6 第 0 节第 4 条 / 第 4.1 节）:
- Budget Lease（预算租约：发放/占用/过期 + 级联撤销）  → :mod:`jiuwen_glue.leases`
- Evidence 三态（draft / verified / finalized）        → :mod:`jiuwen_glue.evidence`
- 能力元数据注册（五类机读声明 + 版本准入 + 指标单向回流）→ :mod:`jiuwen_glue.capabilities`
- 协同三铁律的执行与检测                                → :mod:`jiuwen_glue.rules`

边界（写死）: 原生层管"怎么做"，glue 管"准不准进"，控制台管"看得见"；
发现与编排一律用原生 Symphony，本包不自建发现机制。
"""

from .capabilities import (
    ADMITTED,
    CANDIDATE,
    REJECTED,
    CapabilityDeclaration,
    CapabilityRegistry,
    CapabilityVersion,
)
from .errors import (
    AdmissionDeniedError,
    BudgetExceededError,
    DeclarationSchemaError,
    EvidenceImmutableError,
    IllegalTransitionError,
    IronRuleViolation,
    LeaseDerivationError,
    LeaseExhaustedError,
    LeaseExpiredError,
    LeaseRevokedError,
    MissingTaskReferenceError,
    Rule1MessageIsNotClaim,
    Rule2ConversationIsNotState,
    Rule3InternalStepIsNotTask,
    UnknownCapabilityError,
)
from .evidence import DRAFT, FINALIZED, VERIFIED, Evidence, EvidenceStore, EvidenceTransition
from .leases import ACTIVE, EXHAUSTED, EXPIRED, REVOKED, BudgetLedger, BudgetLease
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

__version__ = "0.1.0"

__all__ = [
    # errors
    "IronRuleViolation",
    "Rule1MessageIsNotClaim", "Rule2ConversationIsNotState", "Rule3InternalStepIsNotTask",
    "BudgetExceededError", "LeaseDerivationError", "LeaseExhaustedError",
    "LeaseExpiredError", "LeaseRevokedError",
    "IllegalTransitionError", "EvidenceImmutableError",
    "DeclarationSchemaError", "AdmissionDeniedError", "UnknownCapabilityError",
    "MissingTaskReferenceError",
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
    "__version__",
]
