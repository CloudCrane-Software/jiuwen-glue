# coding: utf-8
"""升级阶梯状态机 + 人类就绪包（v2.1 §4.7 升级体系 / 工单 W-06）.

规格来源（建设方案 v2.1 §4.7，逐字裁定）:

- 阶梯六级：L0 执行 → L1 同事 → L2 协调者 → L3 值班工程师(提权租约) →
  L4 守门者(全景只读+跨框批准) → L5 人类；
- **升级的是权限/工具/上下文而非模型**：每次升级显式声明新增上下文范围
  （added_context）+ 工具集（added_tools）+ 时间预算（time_budget_seconds）；
  任何携带"model"增量字段的声明被拒绝（本模块结构上不设 model 字段，
  extras 中出现 model 键即 EscalationSchemaError）；
- 风暴防护三参数（:class:`StormGuard`）：每级最大尝试（max_attempts_per_level，
  同级同签名重试到顶即强制升级，继续原地尝试被拒）/ 向下回退 ≤1
  （max_downgrades，防上下震荡）/ 签名去重限流（dedup_window_seconds，
  同任务同签名在窗口内的重复升级被拒）；另有两条补充防线（grok 红队 R1
  复判后加）：级内总量上限（签名购物防护）与相邻升级最小步进间隔（连跳防护）；
- 组织学习：**同类签名 3 次 → 自动开权限扩展提案对象**
  （:class:`PermissionExpansionProposal`，跨任务累计；提案只登记不授权——
  扩权属人类硬清单，v2.1 §4.5 不对称设计，本模块永远不发放权限）；
- 守门者人类就绪包四件套（:func:`render_readiness`）：事实固定 / 范畴清晰 /
  权限内无解证明 / 可逆性评估——从工单输入与升级台账装配；
  **字段不齐 = BLOCKED 不放行**（聚合复用 guardrail.aggregate，fail-closed）；
- 人类专属四类硬清单（:data:`HARD_LIST`）：金钱 / 法律 ToS / 不可逆 /
  theory.Approval；**不在表内打回 L3**（就绪包范畴件 BLOCKED，裁决卡
  escalate 键打回，见 console-tui 裁决卡）；
- 升级对象挂工单：全对象携带 task_ref 引用（跨层只传引用，不复制任务状态）。

边界（写死）：
- 本模块只做**对象与状态机**，不做决策点："允不允许升级后做某事"仍由
  TeamPermissionRail / OPA / GuardrailRun 判定（决策点唯一，v2.0 §0 原则 3）；
- L5 是阶梯终点但不是自动执行点：升级到 L5 只是把就绪包递到人类面前；
- 异常归属本模块定义（先例 meta_governance.py），不动公共 errors.py；
- [待] Postgres DDL（glue.escalation_case / glue.escalation_event /
  glue.expansion_proposal）与 console-tui pg 视图未落，SQL 接口已留。
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .decisions import DecisionRecord, context_hash
from .errors import GlueError, MissingTaskReferenceError
from .guardrail import VERDICT_BLOCKED, VERDICT_PASS, aggregate

SCOPE = "product"   # v2.1 §4.7：升级体系属控制面（gatekeeper 运行在 srv-1）

# ── 阶梯六级 ──────────────────────────────────────────────────────────────────

LEVEL_L0 = "L0"   # 执行 agent
LEVEL_L1 = "L1"   # 同事
LEVEL_L2 = "L2"   # 协调者
LEVEL_L3 = "L3"   # 值班工程师（提权租约）
LEVEL_L4 = "L4"   # 守门者（全景只读 + 跨框动作批准）
LEVEL_L5 = "L5"   # 人类
LEVELS = (LEVEL_L0, LEVEL_L1, LEVEL_L2, LEVEL_L3, LEVEL_L4, LEVEL_L5)

LEVEL_ROLE = {
    LEVEL_L0: "executor",
    LEVEL_L1: "peer",
    LEVEL_L2: "coordinator",
    LEVEL_L3: "duty_engineer",
    LEVEL_L4: "gatekeeper",
    LEVEL_L5: "human",
}

# 各级能力声明（机读；升级的是权限/工具/上下文，各级能力上限由此表锚定）
LEVEL_CAPABILITIES = {
    LEVEL_L0: ("own-scope",),
    LEVEL_L1: ("peer-review",),
    LEVEL_L2: ("cross-task-coordinate",),
    LEVEL_L3: ("elevated-lease",),
    LEVEL_L4: ("panorama-readonly", "cross-frame-approve"),
    LEVEL_L5: ("human-hard-list",),
}

# 就绪包无解证明覆盖的级别：L4 守门者向 L5 递包时，L0..L3（权限内全部层级）
# 必须每级打到级内限次——机器可判的"权限内已穷尽"。
PROOF_LEVELS = (LEVEL_L0, LEVEL_L1, LEVEL_L2, LEVEL_L3)

# ── 人类专属四类硬清单（v2.1 §4.7；不在表内打回 L3）───────────────────────────

HARD_MONEY = "money"                      # 花钱/承诺付款
HARD_LEGAL_TOS = "legal_tos"              # 法律/供应商 ToS
HARD_IRREVERSIBLE = "irreversible"        # 不可逆动作
HARD_THEORY_APPROVAL = "theory_approval"  # theory.Approval（唯一人工写入点）
HARD_LIST = (HARD_MONEY, HARD_LEGAL_TOS, HARD_IRREVERSIBLE, HARD_THEORY_APPROVAL)


# ── 错误（归属本模块；先例 meta_governance.py）────────────────────────────────

class EscalationError(GlueError):
    """升级阶梯基类错误。"""


class EscalationSchemaError(EscalationError):
    """升级声明不满足 schema（缺 task_ref / 缺时间预算 / 携带 model 增量等）。"""


class EscalationStateError(EscalationError):
    """阶梯状态机非法操作（跳级 / 越 L5 / 未经就绪包升 L5 / 未知 case）。"""


class EscalationStormError(EscalationError):
    """风暴防护触发（签名去重窗口 / 向下回退超限）。"""


class EscalationRequiredError(EscalationError):
    """级内限次已到：原地重试被拒，必须升级（检测 = 拒绝 + 留痕）。"""


class UnknownEscalationCaseError(EscalationError):
    """工单没有升级 case。"""


# ── 风暴防护三参数 + 组织学习阈值 ─────────────────────────────────────────────

@dataclass(frozen=True)
class StormGuard:
    """风暴防护三参数（v2.1 §4.7"级内限次/回退≤1/签名去重"）+ 签名提案阈值
    + 两条补充防线（grok 红队 R1 复判后加，2026-09-28）：

    - ``max_total_attempts_per_level``：级内**总量**上限（不分签名）——堵"换
      blocker 字段刷新签名重置计数"的签名购物路径（R1-F2 实证成立）；
    - ``min_step_interval_seconds``：同一 case 相邻两次升级的最小步进间隔
      （不看签名与层级）——堵去重窗口内 L0→L4 秒级连跳（R1-F3 实证成立；
      逐级爬升本身合法，只是必须给各级留出真实处理时间）。
    """

    max_attempts_per_level: int = 3      # 每级最大尝试（同签名同级）
    max_total_attempts_per_level: int = 9  # 每级总量上限（不分签名；R1-F2 补充防线）
    max_downgrades: int = 1              # 向下回退 ≤1（每个 case 一生）
    dedup_window_seconds: float = 600.0  # 签名去重限流窗口（同任务同签名同源层级）
    min_step_interval_seconds: float = 60.0  # 相邻两次升级最小间隔（R1-F3 补充防线）
    signature_threshold: int = 3         # 同类签名 N 次 → 权限扩展提案（组织学习）

    def __post_init__(self) -> None:
        if not isinstance(self.max_attempts_per_level, int) or self.max_attempts_per_level < 1:
            raise EscalationSchemaError("max_attempts_per_level must be a positive int")
        if (not isinstance(self.max_total_attempts_per_level, int)
                or self.max_total_attempts_per_level < self.max_attempts_per_level):
            raise EscalationSchemaError(
                "max_total_attempts_per_level must be an int >= max_attempts_per_level")
        if not isinstance(self.max_downgrades, int) or self.max_downgrades < 0:
            raise EscalationSchemaError("max_downgrades must be a non-negative int")
        # R6/D1：NaN 经 ``nan < 0`` 恒 False 静默通过，随后 ``(now-last) < nan``
        # 恒 False ——两道风暴闸被静默关闭（fail-open 方向）；±inf 同拒（inf 方向
        # 虽 fail-closed，但非有限窗口不是合法配置，schema 层一并 fail-closed）。
        # 巨型 int（如 10**309）经 math.isfinite 抛 OverflowError——按非有限同拒
        # （grok 红队 R6 二轮发现，schema 错误不得变形为未归类崩溃）。
        def _finite(v: object) -> bool:
            try:
                return math.isfinite(v)  # type: ignore[arg-type]
            except OverflowError:
                return False

        if (not isinstance(self.dedup_window_seconds, (int, float))
                or not _finite(self.dedup_window_seconds)
                or self.dedup_window_seconds < 0):
            raise EscalationSchemaError(
                "dedup_window_seconds must be a finite non-negative number")
        if (not isinstance(self.min_step_interval_seconds, (int, float))
                or not _finite(self.min_step_interval_seconds)
                or self.min_step_interval_seconds < 0):
            raise EscalationSchemaError(
                "min_step_interval_seconds must be a finite non-negative number")
        if not isinstance(self.signature_threshold, int) or self.signature_threshold < 1:
            raise EscalationSchemaError("signature_threshold must be a positive int")


def signature_for(blocker: Mapping[str, Any]) -> str:
    """阻塞签名：blocker 描述经 canonical JSON + SHA-256（复用 decisions.context_hash）。

    blocker 是"卡在哪"的机读描述，如 ``{"error": "PermissionDenied",
    "resource": "prod/release/17", "action": "deploy"}``。同类问题 → 同签名，
    是去重限流与组织学习（3 次 → 提案）的键。
    """
    if not isinstance(blocker, Mapping) or not blocker:
        raise EscalationSchemaError("blocker must be a non-empty mapping")
    return context_hash(dict(blocker))


# ── 升级对象（挂工单：task_ref 引用）──────────────────────────────────────────

@dataclass(frozen=True)
class Escalation:
    """一次升级（对应 [待] DDL glue.escalation_event；frozen——落账后不可变）。"""

    escalation_id: str
    task_ref: str                     # 挂哪张工单（跨层只传引用）
    from_level: str
    to_level: str
    signature: str                    # 阻塞签名（signature_for）
    added_context: Tuple[str, ...]    # 新增上下文范围（显式声明）
    added_tools: Tuple[str, ...]      # 新增工具集（显式声明）
    time_budget_seconds: float        # 时间预算（>0）
    reason: str
    created_at: float
    tenant_id: str = "t0"


@dataclass(frozen=True)
class EscalationEvent:
    """台账审计事件（ATTEMPT / ATTEMPT_REJECTED / ESCALATE / ESCALATE_REJECTED /
    DOWNGRADE / READY_RENDERED / PROPOSAL_OPENED / PROPOSAL_BUMPED）。"""

    event: str
    task_ref: str
    occurred_at: float
    detail: Mapping[str, Any] = field(default_factory=dict)
    tenant_id: str = "t0"


@dataclass
class EscalationCase:
    """一张工单的升级 case（对应 [待] DDL glue.escalation_case；task_ref 主键语义）。"""

    task_ref: str
    level: str = LEVEL_L0
    downgrades_used: int = 0
    attempts: Dict[Tuple[str, str], int] = field(default_factory=dict)  # (level, signature) → 次数
    level_totals: Dict[str, int] = field(default_factory=dict)  # level → 总尝试数（不分签名，R1-F2）
    last_escalation_at: Dict[Tuple[str, str], float] = field(default_factory=dict)
    #                                    ^ (from_level, signature) → 最近升级时刻（签名去重限流键）
    last_escalation_any: Optional[float] = None   # 最近一次升级时刻（步进间隔键，R1-F3）
    history: List[str] = field(default_factory=list)                    # escalation_id 顺序
    opened_at: float = 0.0
    ready_package: Optional["ReadinessPackage"] = None  # 最近一次就绪包（L4→L5 闸）
    tenant_id: str = "t0"


@dataclass(frozen=True)
class PermissionExpansionProposal:
    """权限扩展提案对象（组织学习产物；**只登记不授权**——扩权属人类硬清单）。"""

    proposal_id: str
    signature: str
    occurrences: int                      # 该签名的累计升级次数（≥ threshold 时开）
    task_refs: Tuple[str, ...]            # 触发升级的工单（去重）
    scope_requested: Tuple[str, ...]      # 触发升级声明的上下文增量并集
    tools_requested: Tuple[str, ...]      # 触发升级声明的工具增量并集
    state: str = "OPEN"                   # OPEN（裁决走 PROP 流程，本模块不推进）
    opened_at: float = 0.0
    tenant_id: str = "t0"


# ── 人类就绪包（四件套）───────────────────────────────────────────────────────

@dataclass(frozen=True)
class FrozenFact:
    """固定事实：一句话事实 + 证据引用（引用为空 = 事实未固定）。"""

    statement: str
    evidence_ref: str


@dataclass(frozen=True)
class ReversibilityAssessment:
    """可逆性评估：可逆与否 + 影响面 + 回滚引用（声称可逆就必须给回滚路径）。"""

    reversible: bool
    impact: str
    rollback_ref: str = ""
    assessed_by: str = ""


@dataclass(frozen=True)
class ReadinessTask:
    """就绪包装配输入（从工单与决策记录装配的字段集；由调用方对齐工单对象）。"""

    task_ref: str
    title: str
    hard_list_category: Optional[str] = None   # HARD_LIST 之一；None/表外 = 打回 L3
    facts: Tuple[FrozenFact, ...] = ()
    reversibility: Optional[ReversibilityAssessment] = None


@dataclass(frozen=True)
class ReadinessPiece:
    """四件套之一的机读结论（PASS / BLOCKED；缺料即 BLOCKED，不放行）。"""

    key: str
    verdict: str
    detail: str


PIECE_FACTS = "facts_fixed"
PIECE_CATEGORY = "category_clear"
PIECE_NO_SOLUTION = "in_permission_no_solution"
PIECE_REVERSIBILITY = "reversibility"
PIECE_KEYS = (PIECE_FACTS, PIECE_CATEGORY, PIECE_NO_SOLUTION, PIECE_REVERSIBILITY)


@dataclass(frozen=True)
class ReadinessPackage:
    """人类就绪包（L4 守门者 → L5 人类的唯一合法递包形态）。"""

    task_ref: str
    task_title: str
    signature: str
    category: Optional[str]
    pieces: Tuple[ReadinessPiece, ...]
    decision_refs: Tuple[str, ...] = ()   # 挂联决策记录引用（装配自决策账本）
    generated_at: float = 0.0

    @property
    def verdict(self) -> str:
        """四件套聚合 = guardrail.aggregate（fail-closed：任一 BLOCKED → BLOCKED）。"""
        return aggregate([p.verdict for p in self.pieces])

    @property
    def ready(self) -> bool:
        return self.verdict == VERDICT_PASS

    @property
    def missing(self) -> Tuple[str, ...]:
        return tuple(p.key for p in self.pieces if p.verdict != VERDICT_PASS)

    @property
    def knock_back_to_l3(self) -> bool:
        """范畴不在四类硬清单 → 打回 L3（v2.1 §4.7"不在表内打回 L3"）。"""
        return self.category is None or self.category not in HARD_LIST


def _piece(key: str, ok: bool, ok_detail: str, blocked_detail: str) -> ReadinessPiece:
    return ReadinessPiece(key=key, verdict=VERDICT_PASS if ok else VERDICT_BLOCKED,
                          detail=ok_detail if ok else blocked_detail)


def render_readiness(task: ReadinessTask, *, case: Optional[EscalationCase] = None,
                     max_attempts_per_level: int = 3,
                     decisions: Sequence[DecisionRecord] = (),
                     now: float = 0.0) -> ReadinessPackage:
    """四件套生成器（v2.1 §4.7 守门者人类就绪包；**字段不齐 = BLOCKED 不放行**）。

    装配来源：
    - 事实固定   ← task.facts（每条必须带证据引用）；
    - 范畴清晰   ← task.hard_list_category（不在四类硬清单 = BLOCKED + 打回 L3）；
    - 权限内无解 ← 升级台账 case：L0..L3 每级同签名尝试须打到级内限次
      （机器可判的"权限内已穷尽"；case 缺失 = 无法证明 = BLOCKED）；
    - 可逆性     ← task.reversibility（缺评估 / 缺影响面 / 声称可逆却无回滚引用
      = BLOCKED）。

    decisions：该工单的决策记录（决策账本按调用方过滤后传入），引用入包供人类复核。
    聚合复用 guardrail.aggregate——四件套全 PASS 才 ready（fail-closed 同一门）。
    """
    if not isinstance(task, ReadinessTask):
        raise EscalationSchemaError("task must be a ReadinessTask")
    if not task.task_ref:
        raise MissingTaskReferenceError("readiness package requires a task_ref")

    # 件 1：事实固定
    bad_facts = [f for f in task.facts
                 if not (f.statement and f.statement.strip() and f.evidence_ref
                         and f.evidence_ref.strip())]
    pieces = [_piece(
        PIECE_FACTS, bool(task.facts) and not bad_facts,
        f"{len(task.facts)} facts pinned with evidence refs",
        ("no facts assembled" if not task.facts else
         f"{len(bad_facts)} fact(s) missing statement/evidence_ref"))]

    # 件 2：范畴清晰（不在四类硬清单 → 打回 L3）
    in_hard_list = task.hard_list_category in HARD_LIST
    cat_detail_blocked = (
        "no hard-list category declared" if task.hard_list_category is None else
        f"category {task.hard_list_category!r} is not in the human hard list "
        f"{HARD_LIST} — knock back to L3")
    pieces.append(_piece(PIECE_CATEGORY, in_hard_list,
                         f"hard-list category: {task.hard_list_category}",
                         cat_detail_blocked))

    # 件 3：权限内无解证明（机器可判：L0..L3 每级打到级内限次）
    if case is None:
        sig = ""
        pieces.append(_piece(
            PIECE_NO_SOLUTION, False, "",
            "no escalation case in the ledger — in-permission exhaustion "
            "cannot be proven (fail-closed)"))
    else:
        sig = _case_last_signature(case)
        not_exhausted = [
            lv for lv in PROOF_LEVELS
            if case.attempts.get((lv, sig), 0) < max_attempts_per_level]
        pieces.append(_piece(
            PIECE_NO_SOLUTION, not not_exhausted,
            "levels L0..L3 each hit the per-level attempt cap for this signature",
            "in-permission attempts not exhausted at level(s) "
            f"{not_exhausted} (counts="
            f"{ {lv: case.attempts.get((lv, sig), 0) for lv in PROOF_LEVELS} })"))

    # 件 4：可逆性评估
    rev = task.reversibility
    if rev is None:
        pieces.append(_piece(PIECE_REVERSIBILITY, False, "",
                             "no reversibility assessment"))
    elif not (rev.impact and rev.impact.strip()):
        pieces.append(_piece(PIECE_REVERSIBILITY, False, "",
                             "reversibility assessment has empty impact statement"))
    elif rev.reversible and not (rev.rollback_ref and rev.rollback_ref.strip()):
        pieces.append(_piece(PIECE_REVERSIBILITY, False, "",
                             "claimed reversible but rollback_ref is empty"))
    else:
        pieces.append(_piece(
            PIECE_REVERSIBILITY, True,
            f"reversible={rev.reversible}; rollback={rev.rollback_ref or 'n/a'}", ""))

    return ReadinessPackage(
        task_ref=task.task_ref, task_title=task.title,
        signature=sig if case is not None else "",
        category=task.hard_list_category, pieces=tuple(pieces),
        decision_refs=tuple(d.decision_id for d in decisions)[:10],
        generated_at=now)


def _case_last_signature(case: EscalationCase) -> str:
    """case 最近一次升级的签名（无升级历史 → 空串，"件 3"按 0 次尝试自然判 BLOCKED）。"""
    if not case.last_escalation_at:
        return ""
    (from_level, sig), _ts = max(
        case.last_escalation_at.items(), key=lambda kv: kv[1])
    return sig


# ── 台账（状态机唯一入口）─────────────────────────────────────────────────────

class EscalationLedger:
    """进程内升级台账（对应 [待] DDL glue.escalation_case/event + expansion_proposal）。

    边界（写死）：
    - 状态机只**登记与拦截**升级，不签发任何权限/租约（提权租约在 leases，
      由各执行点按各自决策点发放）；
    - 权限扩展提案只登记（OPEN），永不自动生效——扩权入人类硬清单（v2.1 §4.5）；
    - fail-closed：L4→L5 必须持 READY 就绪包；字段不齐 BLOCKED；风暴防护触发
      一律拒绝并留痕（检测 = 拒绝 + 留痕，全仓同款纪律）。
    """

    def __init__(self, *, guard: Optional[StormGuard] = None,
                 now: Optional[Callable[[], float]] = None) -> None:
        self.guard = guard or StormGuard()
        self._now = now or _utcnow
        self._cases: Dict[str, EscalationCase] = {}
        self._escalations: Dict[str, Escalation] = {}
        self.proposals: Dict[str, PermissionExpansionProposal] = {}   # signature → proposal
        self.audit: List[EscalationEvent] = []
        self._sig_deltas: Dict[str, Dict[str, Any]] = {}              # signature → 聚合增量

    # ── 查询 ─────────────────────────────────────────────────────────────

    def case_of(self, task_ref: str) -> EscalationCase:
        try:
            return self._cases[task_ref]
        except KeyError:
            raise UnknownEscalationCaseError(f"no escalation case for task: {task_ref}") from None

    def level_of(self, task_ref: str) -> str:
        return self.case_of(task_ref).level

    def escalation_of(self, escalation_id: str) -> Escalation:
        try:
            return self._escalations[escalation_id]
        except KeyError:
            raise EscalationStateError(f"unknown escalation: {escalation_id}") from None

    def _log(self, event: str, task_ref: str, **detail: Any) -> None:
        self.audit.append(EscalationEvent(event=event, task_ref=task_ref,
                                          occurred_at=self._now(), detail=detail))

    # ── 级内尝试（级内限次的计数入口）─────────────────────────────────────

    def attempt(self, task_ref: str, blocker: Mapping[str, Any]) -> int:
        """记录一次本级的权限内尝试（同签名）。两级上限：

        - 同签名同级打到 ``max_attempts_per_level`` → 拒（级内限次，v2.1 §4.7）；
        - 本级**总量**（不分签名）打到 ``max_total_attempts_per_level`` → 拒
          （签名购物防护：换 blocker 字段刷新签名无法重置计数，grok 红队 R1-F2）。
        到顶后继续尝试一律 EscalationRequiredError（必须升级，原地重试被拒并留痕）。
        """
        if not task_ref:
            raise MissingTaskReferenceError("attempt requires a task_ref")
        sig = signature_for(blocker)
        case = self._cases.get(task_ref)
        if case is None:
            case = self._open_case(task_ref)
        key = (case.level, sig)
        count = case.attempts.get(key, 0)
        total = case.level_totals.get(case.level, 0)
        if count >= self.guard.max_attempts_per_level:
            self._log("ATTEMPT_REJECTED", task_ref, level=case.level, signature=sig,
                      count=count, cap=self.guard.max_attempts_per_level,
                      reason="per-level attempt cap reached; escalate")
            raise EscalationRequiredError(
                f"task {task_ref} already made {count} attempt(s) at {case.level} for "
                f"signature {sig[:12]}… (cap={self.guard.max_attempts_per_level}); "
                "escalate instead of retrying")
        if total >= self.guard.max_total_attempts_per_level:
            self._log("ATTEMPT_REJECTED", task_ref, level=case.level, signature=sig,
                      total=total, cap_total=self.guard.max_total_attempts_per_level,
                      reason="per-level total attempt cap reached "
                             "(signature-shopping guard, R1-F2)")
            raise EscalationRequiredError(
                f"task {task_ref} already made {total} attempt(s) at {case.level} across "
                f"all signatures (total cap={self.guard.max_total_attempts_per_level}); "
                "varying the blocker does not reset the storm guard")
        case.attempts[key] = count + 1
        case.level_totals[case.level] = total + 1
        self._log("ATTEMPT", task_ref, level=case.level, signature=sig, count=count + 1)
        return count + 1

    # ── 升级（唯一上行入口；显式声明增量）─────────────────────────────────

    def escalate(self, task_ref: str, blocker: Mapping[str, Any], *,
                 added_context: Iterable[str] = (),
                 added_tools: Iterable[str] = (),
                 time_budget_seconds: float = 0.0,
                 reason: str = "",
                 extras: Optional[Mapping[str, Any]] = None) -> Escalation:
        """升级一级（单步；显式声明新增上下文范围 + 工具集 + 时间预算）。

        拦截（全部拒绝 + 留痕，fail-closed）：
        - 缺 task_ref（MissingTaskReferenceError）/ 越过 L5 / 跳级；
        - 增量未声明：time_budget_seconds ≤ 0；extras 携带 ``model`` 键
          （**升级的是权限/工具/上下文而非模型**）；
        - 风暴防护：同任务同签名在去重窗口内重复升级；
        - L4→L5：必须持 READY 的人类就绪包（字段不齐 BLOCKED 不放行）。
        """
        if not task_ref:
            raise MissingTaskReferenceError("escalation requires a task_ref")
        case = self._cases.get(task_ref)
        if case is None:
            case = self._open_case(task_ref)
        now = self._now()
        sig = signature_for(blocker)

        if extras and "model" in {k.lower() for k in extras}:
            self._log("ESCALATE_REJECTED", task_ref, reason="model delta forbidden",
                      signature=sig)
            raise EscalationSchemaError(
                "escalation moves permissions/tools/context, never the model "
                "(v2.1 §4.7): extras must not carry a 'model' key")
        if case.level == LEVEL_L5:
            self._log("ESCALATE_REJECTED", task_ref, reason="already at L5", signature=sig)
            raise EscalationStateError(f"task {task_ref} is already at L5 (human)")
        from_idx = LEVELS.index(case.level)
        to_level = LEVELS[from_idx + 1]

        # 时间预算是显式声明的一部分（>0）；增量字段存在即可（显式空 = 声明不加）。
        if not isinstance(time_budget_seconds, (int, float)) or time_budget_seconds <= 0:
            self._log("ESCALATE_REJECTED", task_ref, reason="time budget not declared",
                      signature=sig)
            raise EscalationSchemaError(
                "escalation must declare a positive time_budget_seconds")

        # 风暴防护补充②（grok 红队 R1-F3）：相邻两次升级的最小步进间隔——
        # 同 case 全局计数，不看签名与层级；去重窗口内"换层级秒级连跳 L0→L4"在此被拦。
        if (case.last_escalation_any is not None
                and (now - case.last_escalation_any) < self.guard.min_step_interval_seconds):
            self._log("ESCALATE_REJECTED", task_ref, reason="min step interval",
                      elapsed=now - case.last_escalation_any,
                      min_interval=self.guard.min_step_interval_seconds)
            raise EscalationStormError(
                f"task {task_ref} escalated {now - case.last_escalation_any:.0f}s ago "
                f"(< min step interval {self.guard.min_step_interval_seconds:.0f}s); "
                "each ladder step needs real processing time (storm guard, R1-F3)")

        # 风暴防护：签名去重限流——键 =（本升级源层级, 签名）。拦截"降级后原级重升"
        # 与"多 worker 重复触发"同签名风暴；逐级爬升（每次 from_level 不同）不受限。
        last = case.last_escalation_at.get((case.level, sig))
        if last is not None and (now - last) < self.guard.dedup_window_seconds:
            self._log("ESCALATE_REJECTED", task_ref, reason="signature dedup window",
                      signature=sig, from_level=case.level,
                      window=self.guard.dedup_window_seconds, elapsed=now - last)
            raise EscalationStormError(
                f"task {task_ref} escalated from {case.level} with the same signature "
                f"{sig[:12]}… {now - last:.0f}s ago (< dedup window "
                f"{self.guard.dedup_window_seconds:.0f}s)")

        # L4→L5 闸：人类就绪包 READY 且**绑定当前 blocker 签名**才放行
        # （grok 红队 R1-F1：签名不匹配的旧包不能为别的 blocker 开门——fail-closed）。
        if to_level == LEVEL_L5:
            pkg = case.ready_package
            if pkg is None or not pkg.ready or pkg.task_ref != task_ref:
                missing = tuple(pkg.missing) if pkg is not None else ("no package rendered",)
                self._log("ESCALATE_REJECTED", task_ref, reason="readiness package not ready",
                          missing=missing, signature=sig)
                raise EscalationStateError(
                    f"L4→L5 requires a READY readiness package for {task_ref}; "
                    f"missing pieces: {missing} (fail-closed)")
            if pkg.signature != sig:
                self._log("ESCALATE_REJECTED", task_ref,
                          reason="readiness package signature mismatch",
                          package_signature=pkg.signature, current_signature=sig)
                raise EscalationStateError(
                    f"readiness package for {task_ref} is bound to blocker signature "
                    f"{pkg.signature[:12]}… but this escalation carries "
                    f"{sig[:12]}… — re-render the package for the current blocker "
                    "(fail-closed, R1-F1)")

        esc = Escalation(
            escalation_id=uuid.uuid4().hex, task_ref=task_ref,
            from_level=case.level, to_level=to_level, signature=sig,
            added_context=tuple(added_context), added_tools=tuple(added_tools),
            time_budget_seconds=float(time_budget_seconds), reason=reason,
            created_at=now, tenant_id=case.tenant_id)
        self._escalations[esc.escalation_id] = esc
        case.level = to_level
        case.history.append(esc.escalation_id)
        case.last_escalation_at[(esc.from_level, sig)] = now
        case.last_escalation_any = now
        self._log("ESCALATE", task_ref, escalation_id=esc.escalation_id,
                  from_level=esc.from_level, to_level=to_level, signature=sig,
                  added_context=esc.added_context, added_tools=esc.added_tools,
                  time_budget_seconds=esc.time_budget_seconds)

        # 组织学习：同类签名跨任务累计，3 次 → 自动开权限扩展提案（只登记不授权）
        self._record_signature(sig, esc, now)
        return esc

    # ── 向下回退（≤1）─────────────────────────────────────────────────────

    def downgrade(self, task_ref: str, *, reason: str = "") -> EscalationCase:
        """降一级（回退 ≤1：每个 case 一生最多一次；L0 不可再降；就绪包作废）。"""
        if not task_ref:
            raise MissingTaskReferenceError("downgrade requires a task_ref")
        case = self.case_of(task_ref)
        if case.level == LEVEL_L0:
            self._log("DOWNGRADE_REJECTED", task_ref, reason="already at L0")
            raise EscalationStateError(f"task {task_ref} is at L0; cannot downgrade")
        if case.downgrades_used >= self.guard.max_downgrades:
            self._log("DOWNGRADE_REJECTED", task_ref, reason="downgrade cap reached",
                      used=case.downgrades_used, cap=self.guard.max_downgrades)
            raise EscalationStormError(
                f"task {task_ref} already used its {case.downgrades_used} downgrade(s) "
                f"(cap={self.guard.max_downgrades}; oscillation is a storm pattern)")
        case.level = LEVELS[LEVELS.index(case.level) - 1]
        case.downgrades_used += 1
        case.ready_package = None   # 回退后现场已变，就绪包必须重render（fail-closed）
        self._log("DOWNGRADE", task_ref, to_level=case.level, reason=reason)
        return case

    # ── 就绪包（渲染 + 闸登记）────────────────────────────────────────────

    def readiness(self, task: ReadinessTask, *,
                  decisions: Sequence[DecisionRecord] = ()) -> ReadinessPackage:
        """渲染就绪包并登记到 case（L4→L5 闸读取最近一次；READY 与否都登记）。"""
        case = self._cases.get(task.task_ref)
        pkg = render_readiness(
            task, case=case, max_attempts_per_level=self.guard.max_attempts_per_level,
            decisions=decisions, now=self._now())
        if case is not None:
            case.ready_package = pkg
            self._log("READY_RENDERED", task.task_ref, verdict=pkg.verdict,
                      missing=pkg.missing, knock_back_to_l3=pkg.knock_back_to_l3)
        return pkg

    # ── 内部 ─────────────────────────────────────────────────────────────

    def _open_case(self, task_ref: str) -> EscalationCase:
        case = EscalationCase(task_ref=task_ref, opened_at=self._now())
        self._cases[task_ref] = case
        self._log("CASE_OPEN", task_ref, level=case.level)
        return case

    def _record_signature(self, sig: str, esc: Escalation, now: float) -> None:
        agg = self._sig_deltas.setdefault(sig, {"tasks": set(), "ctx": set(), "tools": set(),
                                                "count": 0})
        agg["count"] += 1
        agg["tasks"].add(esc.task_ref)
        agg["ctx"].update(esc.added_context)
        agg["tools"].update(esc.added_tools)
        existing = self.proposals.get(sig)
        if agg["count"] < self.guard.signature_threshold:
            return
        proposal = PermissionExpansionProposal(
            proposal_id=(existing.proposal_id if existing else uuid.uuid4().hex),
            signature=sig, occurrences=agg["count"],
            task_refs=tuple(sorted(agg["tasks"])),
            scope_requested=tuple(sorted(agg["ctx"])),
            tools_requested=tuple(sorted(agg["tools"])),
            state="OPEN", opened_at=(existing.opened_at if existing else now),
            tenant_id=esc.tenant_id)
        self.proposals[sig] = proposal
        self._log("PROPOSAL_BUMPED" if existing else "PROPOSAL_OPENED",
                  esc.task_ref, proposal_id=proposal.proposal_id,
                  signature=sig, occurrences=proposal.occurrences)


def _utcnow() -> float:
    import time

    return time.time()
