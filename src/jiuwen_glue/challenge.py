# coding: utf-8
"""Challenge — 授权三态第三态的结构化对象（WO-0003 返工，v1.7 §12.5）.

规格来源（AI Native 研发手册 §3.3.1"缺少权限时怎么办：授权三态"，逐字见
docs/native-handbook-boundary-check.md）:

- 授权结果不应只有"允许"和"拒绝"：**需要补充授权（Challenge）**时先暂停；
- 它不是一段让模型去猜的 403 报错文本，而是**结构化授权要求**：说明需要谁确认
  （who_confirms）、以什么方式（method）、有效期多久（expires_at）；
- 可信 Runtime 据此在独立界面展示"**哪个 Agent 想对哪个资源做什么**"（to_ask_payload）；
- **模型只收到"等待确认/审批拒绝"这样的高层状态，接触不到授权码和 Token**——
  本模块任何 API 都不返回授权码/token 类字段（见 ChallengeBoard docstring 内写死注释）。

过期即 expired（fail-closed）：pending 的 Challenge 一旦越过 expires_at，
任何裁决请求都被拒绝——缺口必须重新发起 Challenge，而不是用过期的批准。
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .errors import ChallengeStateError, UnknownChallengeError

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"
_TERMINAL = (APPROVED, DENIED, EXPIRED)

# 谁可以确认：用户 / 资源负责人 / 值班负责人（手册案例：重启生产实例不能由 Agent 自行确认）
CONFIRM_USER = "user"
CONFIRM_RESOURCE_OWNER = "resource_owner"
CONFIRM_DUTY_OFFICER = "duty_officer"
_WHO = (CONFIRM_USER, CONFIRM_RESOURCE_OWNER, CONFIRM_DUTY_OFFICER)

# who 维度的强制（不再纯展示）：每类 who_confirms 只允许携带对应身份前缀的
# 裁决人 resolve（resolve 的 by= 必须落在下列前缀域内）：
# - user 类：用户本人（user:*）或治理面控制台操作员（console:*）——ask 审批
#   队列即控制台 ask（method=console.ask），操作员是人类在控制面的操作身份；
# - resource_owner / duty_officer：必须由携带对应前缀的身份在独立界面确认，
#   控制台操作员不得代裁（手册案例：重启生产实例不能被控制台统一裁掉）。
# console-tui 的 state.py 镜像本表，tests 交叉校验防漂移。
CONFIRMER_REALMS = {
    CONFIRM_USER: ("user:", "console:"),
    CONFIRM_RESOURCE_OWNER: ("resource_owner:",),
    CONFIRM_DUTY_OFFICER: ("duty_officer:",),
}


def _utcnow() -> float:
    import time

    return time.time()


def _finite(v: object) -> bool:
    """有限性闸（与 escalation.StormGuard 同口径）：NaN/±inf 一律非有限；
    巨型 int（如 10**309）经 math.isfinite 抛 OverflowError——按非有限同拒，
    schema 错误不得变形为未归类崩溃（D1-R6 PR#15 纪律）。"""
    try:
        return math.isfinite(v)  # type: ignore[arg-type]
    except (OverflowError, TypeError):   # 巨型 int / 非数值类型 → 非有限
        return False


@dataclass
class Challenge:
    """结构化授权要求（对应 DDL glue.challenge）。"""

    challenge_id: str
    who_confirms: str             # user | resource_owner | duty_officer
    resource: str                 # 什么资源
    action: str                   # 什么动作
    method: str                   # 确认方式（如 console.ask / leader.approval）
    created_at: float
    expires_at: float
    state: str = PENDING
    agent_identity_ref: str = ""  # 哪个 Agent（三层复合身份引用）
    guardrail_run_ref: Optional[str] = None   # 由哪个 GuardrailRun 的 permission_rail check 发出
    resolved_at: Optional[float] = None
    resolved_by: Optional[str] = None
    tenant_id: str = "t0"
    meta: Dict[str, Any] = field(default_factory=dict)

    def is_expired_at(self, now: float) -> bool:
        return self.state == PENDING and now >= self.expires_at

    def to_ask_payload(self) -> Dict[str, Any]:
        """映射 TeamPermissionRail ask 路由的载荷：展示"哪个 Agent 想对哪个资源做什么"。

        只含展示与路由字段，**不含任何授权码/token 字段**（见模块 docstring）。
        """
        return {
            "challenge_id": self.challenge_id,
            "agent": self.agent_identity_ref,
            "resource": self.resource,
            "action": self.action,
            "who_confirms": self.who_confirms,
            "method": self.method,
            "expires_at": self.expires_at,
            "state": self.state,
            "guardrail_run_ref": self.guardrail_run_ref,
        }


class ChallengeBoard:
    """进程内 Challenge 台账（Postgres DDL 见 ops/sql/002_glue_v2.sql 的 glue.challenge）。

    安全边界（写死）：本台账对模型侧只暴露 ``state_of`` / ``to_ask_payload`` 这样的
    **高层状态**（approved / denied / pending / expired）；批准结果如何变成可执行的
    凭证/令牌，由可信 Runtime 与 OpenBao（Credential Broker）按需兑换，本模块
    **不签发、不持有、不返回任何授权码**——模型永远接触不到。
    """

    def __init__(self, now: Optional[Callable[[], float]] = None) -> None:
        self._now = now or _utcnow
        self._items: Dict[str, Challenge] = {}
        self.audit: List[Dict[str, Any]] = []

    # ── 查询 ─────────────────────────────────────────────────────────────

    def get(self, challenge_id: str) -> Challenge:
        try:
            return self._items[challenge_id]
        except KeyError:
            raise UnknownChallengeError(f"unknown challenge: {challenge_id}") from None

    def state_of(self, challenge_id: str) -> str:
        """模型可见的高层状态。pending 越过 expires_at 即惰性转 expired（fail-closed）。"""
        ch = self.get(challenge_id)
        if ch.is_expired_at(self._now()):
            ch.state = EXPIRED
            self._log(ch, "EXPIRE", lazy=True)
        return ch.state

    def pending_for(self, who_confirms: str) -> List[Challenge]:
        """某类确认人视角的待审批队列（治理面 TUI ask 审批队列的数据源）。"""
        now = self._now()
        out: List[Challenge] = []
        for ch in self._items.values():
            if ch.is_expired_at(now):
                ch.state = EXPIRED
                self._log(ch, "EXPIRE", lazy=True)
            if ch.who_confirms == who_confirms and ch.state == PENDING:
                out.append(ch)
        return out

    def _log(self, ch: Challenge, event: str, **detail: Any) -> None:
        self.audit.append({"challenge_id": ch.challenge_id, "event": event,
                           "at": self._now(), **detail})

    # ── 发起 / 裁决 ──────────────────────────────────────────────────────

    def open(self, *, who_confirms: str, resource: str, action: str, method: str,
             ttl_seconds: float, agent_identity_ref: str = "",
             guardrail_run_ref: Optional[str] = None,
             tenant_id: str = "t0",
             meta: Optional[Dict[str, Any]] = None) -> Challenge:
        """发起一条结构化授权要求。ttl_seconds 必须为正的有限数（无有效期/
        永不过期的 Challenge 不允许存在——NaN/±inf 使过期闸 fail-open）。"""
        if who_confirms not in _WHO:
            raise ChallengeStateError(
                f"who_confirms must be one of {_WHO}, got {who_confirms!r}")
        if not resource or not action or not method:
            raise ChallengeStateError("resource / action / method are required")
        # R7/D1：NaN 经 ``nan <= 0`` 恒 False 静默通过 → expires_at=nan →
        # is_expired_at（now >= nan 恒 False）永不触发，过期闸 fail-open；
        # ±inf 同理永不过期；巨型 int 经 now+ttl 的 float 转换抛未归类
        # OverflowError——三者按「非有限正数」同拒（fail-closed）。
        if not isinstance(ttl_seconds, (int, float)) or not _finite(ttl_seconds) \
                or ttl_seconds <= 0:
            raise ChallengeStateError(
                "ttl_seconds must be a positive finite number "
                "(NaN/inf never expire — an endless challenge is not allowed)")
        now = self._now()
        ch = Challenge(
            challenge_id=uuid.uuid4().hex, who_confirms=who_confirms,
            resource=resource, action=action, method=method,
            created_at=now, expires_at=now + ttl_seconds,
            agent_identity_ref=agent_identity_ref,
            guardrail_run_ref=guardrail_run_ref, tenant_id=tenant_id,
            meta=dict(meta or {}))
        self._items[ch.challenge_id] = ch
        self._log(ch, "OPEN", resource=resource, action=action, who=who_confirms)
        return ch

    def resolve(self, challenge_id: str, *, approved: bool, by: str) -> Challenge:
        """裁决（由 who_confirms 对应的人在独立界面完成；Agent 不得代为裁决）。

        - **who 维度强制**：by 必须落在 who_confirms 对应的身份前缀域内
          （CONFIRMER_REALMS），错域裁决一律拒绝 → ChallengeStateError；
        - 已过期（含惰性过期）的 Challenge 一律拒绝裁决 → ChallengeStateError
          （fail-closed：过期批准不产生任何效力）；
        - 终态不可再改（approved/denied 不回退）。
        """
        ch = self.get(challenge_id)
        now = self._now()
        if ch.state == PENDING and now >= ch.expires_at:
            ch.state = EXPIRED
            self._log(ch, "EXPIRE", lazy=True)
        if ch.state != PENDING:
            raise ChallengeStateError(
                f"challenge {challenge_id} is {ch.state}; only PENDING can be resolved")
        if not by:
            raise ChallengeStateError("resolve requires an explicit confirmer (by=...)")
        realms = CONFIRMER_REALMS.get(ch.who_confirms, ())
        if not any(by.startswith(prefix) for prefix in realms):
            raise ChallengeStateError(
                f"confirmer {by!r} is not allowed for who_confirms={ch.who_confirms!r} "
                f"(allowed realms: {realms}); the challenge must be confirmed by the "
                "corresponding person in an independent interface")
        ch.state = APPROVED if approved else DENIED
        ch.resolved_at = now
        ch.resolved_by = by
        self._log(ch, "RESOLVE", approved=approved, by=by, who=ch.who_confirms)
        return ch
