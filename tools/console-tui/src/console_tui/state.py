# coding: utf-8
"""干预状态机 — console-tui 三级干预的纯逻辑层（WO-0012，v1.7 §12.3）.

规格来源（PROP-0001 v1.7 §12.3 / §12.5；AI Native 研发手册 §3.3.1 授权三态）:

- 干预只有三级（12.3 写死，不得增加）：``s``=steer（``_steer`` 指令注入，可逆）、
  ``a``=审批 ask 队列（接管）、``p``=暂停（任务级 pause 标记，可恢复）；
- **全部干预经控制台留痕**：每次 s/a/p 产生一条可审计事件 + 一条 append-only
  决策记录（pg 模式落 glue.decision_record + challenge 状态转移）；
- ``a`` **走 Challenge 结构化对象的状态机，不绕过**：pending → approved/denied；
  pending 越过 expires_at 一律惰性转 expired（fail-closed），过期后任何裁决拒绝；
  终态不可再改。语义与 src/jiuwen_glue/challenge.py + ops/sql/002 的
  trg_challenge_guard 触发器逐条对齐（tests 有交叉校验）；
- 本模块不 import textual、不碰数据库——只定义状态机与校验，pg/mock 两个后端共用。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, Mapping, Optional, Tuple

# ── 常量 ─────────────────────────────────────────────────────────────────────

# 干预人（治理面操作员）。Agent 不得自确认 Challenge（challenge.py 同款纪律）。
OPERATOR = "console:operator"

# 干预动作（三级干预对应的审计 kind；p 键在暂停态复用为恢复，不新增键）
KIND_STEER = "steer"
KIND_APPROVE = "approve"
KIND_DENY = "deny"
KIND_PAUSE = "pause"
KIND_RESUME = "resume"
# 裁决卡（W-06，v2.1 §4.7）的第二键：escalate = 就绪包打回 L3（非 s/a/p 干预，
# 属升级阶梯裁决面；卡片两键 approve/escalate 的留痕 kind）
KIND_ESCALATE_BACK = "escalate_back"
AUDIT_KINDS = (KIND_STEER, KIND_APPROVE, KIND_DENY, KIND_PAUSE, KIND_RESUME,
               KIND_ESCALATE_BACK)

# Challenge 状态（与 002 glue.challenge CHECK 约束一致）
CH_PENDING = "pending"
CH_APPROVED = "approved"
CH_DENIED = "denied"
CH_EXPIRED = "expired"
CH_TERMINAL = (CH_APPROVED, CH_DENIED, CH_EXPIRED)

# 就绪包裁决卡状态（W-06，v2.1 §4.7；escalate 键 = 打回 L3）
CARD_PENDING = "pending"
CARD_APPROVED = "approved"        # approve 键：批准递呈人类（L5）
CARD_RETURNED = "returned_l3"     # escalate 键：打回 L3
CARD_TERMINAL = (CARD_APPROVED, CARD_RETURNED)


# ── 错误（与 glue errors.py 的层级同风格，独立定义避免运行时依赖 jiuwen_glue）──

class GovernanceError(Exception):
    """治理干预被拒绝的基类（拒绝 + 留痕，检测语义同 glue 三铁律）。"""


class UnknownTargetError(GovernanceError):
    """干预目标（agent / task / challenge）不存在。"""


class IllegalTransitionError(GovernanceError):
    """非法状态转移（重复暂停、未暂停即恢复、终态再裁决）。"""


class ChallengeResolutionError(GovernanceError):
    """Challenge 裁决被拒（未知 / 已过期 fail-closed / 已是终态）。"""


class AdjudicationResolutionError(GovernanceError):
    """就绪包裁决卡被拒（未知卡 / 已是终态 / 包非 READY 却要 approve——fail-closed）。"""


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def canonical_hash(context: Mapping[str, Any]) -> str:
    """canonical JSON（排序键、无空白）+ SHA-256 hex，64 位。

    与 jiuwen_glue.decisions.context_hash 同一算法（tests 交叉校验防漂移）；
    decision_record.context_hash 列的 CHECK 约束要求 ^[0-9a-f]{64}$。
    """
    canonical = json.dumps(context, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def utcnow() -> float:
    return time.time()


def resolve_challenge_state(state: str, *, expires_at: float, now: float,
                            approved: bool, by: str) -> Tuple[str, float, str]:
    """Challenge 裁决的纯状态机（mock/pg 两后端共用；pg 侧另有 DDL 触发器第二道闸）。

    返回 (new_state, resolved_at, resolved_by)。规则（与 002 trg_challenge_guard
    + challenge.py.resolve 逐条对齐）:

    1. 未知裁决人（by 为空）拒绝——Agent 不得自确认；
    2. 已是终态（approved/denied/expired）拒绝再改；
    3. pending 且 now >= expires_at → 惰性转 expired 并拒绝裁决（fail-closed：
       过期批准不产生任何效力，缺口必须重新发起 Challenge）；
    4. pending 且未过期 → approved/denied，resolved_at/resolved_by 必填。
    """
    if not by or not isinstance(by, str):
        raise ChallengeResolutionError("resolving a challenge requires an explicit confirmer (by=...)")
    if state not in (CH_PENDING, CH_APPROVED, CH_DENIED, CH_EXPIRED):
        raise ChallengeResolutionError(f"unknown challenge state: {state!r}")
    if state in CH_TERMINAL:
        raise ChallengeResolutionError(
            f"challenge is {state} (terminal); only PENDING can be resolved")
    # state == pending
    if now >= expires_at:
        raise ChallengeResolutionError(
            "challenge expired at deadline; late approval is void (fail-closed) "
            "— re-open a new Challenge instead")
    return (CH_APPROVED if approved else CH_DENIED), now, by


def apply_pause(current: bool, target: str) -> str:
    """任务级暂停标记的纯状态机：pause/resume 二态，禁止重复暂停 / 无暂停恢复。

    target ∈ {pause, resume}；返回生效后的 kind（pause → KIND_PAUSE，resume →
    KIND_RESUME）。与 s/a 不同，p 的留痕只落一条决策记录（任务对象本身在
    glue.team_task 无 pause 列——暂停态由 decision_record 最新一条 pause/resume
    推导，见 sql/003 v_task_board.paused）。
    """
    if target == KIND_PAUSE:
        if current:
            raise IllegalTransitionError("task is already paused")
        return KIND_PAUSE
    if target == KIND_RESUME:
        if not current:
            raise IllegalTransitionError("task is not paused; nothing to resume")
        return KIND_RESUME
    raise IllegalTransitionError(f"pause target must be 'pause' or 'resume', got {target!r}")


def resolve_adjudication_state(state: str, *, ready: bool, approved: bool,
                               by: str) -> Tuple[str, str]:
    """就绪包裁决卡的纯状态机（W-06，v2.1 §4.7；mock/pg 两后端共用）。

    返回 (new_state, resolved_by)。规则（与 glue.escalation 就绪包闸逐条对齐）:

    1. 未知裁决人（by 为空）拒绝——Agent 不得自确认（challenge 同款纪律）；
    2. 已是终态（approved / returned_l3）拒绝再改；
    3. **approve 键 fail-closed**：包非 READY（四件套任一 BLOCKED / 范畴不在
       四类硬清单）时批准被拒——要么先补齐就绪包，要么用 escalate 键打回 L3；
    4. escalate 键（打回 L3）对 pending 卡一律放行（打回本身就是安全方向）。
    """
    if not by or not isinstance(by, str):
        raise AdjudicationResolutionError(
            "resolving a readiness card requires an explicit adjudicator (by=...)")
    if state not in (CARD_PENDING,) + CARD_TERMINAL:
        raise AdjudicationResolutionError(f"unknown readiness card state: {state!r}")
    if state in CARD_TERMINAL:
        raise AdjudicationResolutionError(
            f"readiness card is {state} (terminal); only PENDING can be resolved")
    if approved and not ready:
        raise AdjudicationResolutionError(
            "readiness package is not READY; approving a human handoff is rejected "
            "(fail-closed) — complete the four pieces or knock it back to L3 with "
            "the escalate key")
    return (CARD_APPROVED if approved else CARD_RETURNED), by


def audit_event(kind: str, *, target: str, by: str = OPERATOR,
                at: Optional[float] = None, **detail: Any) -> Dict[str, Any]:
    """构造一条可审计干预事件（留痕语义：s/a/p 每次都产生一条）。"""
    if kind not in AUDIT_KINDS:
        raise GovernanceError(f"unknown audit kind: {kind!r}")
    return {"ts": at if at is not None else utcnow(), "kind": kind,
            "target": target, "by": by, "detail": dict(detail)}
