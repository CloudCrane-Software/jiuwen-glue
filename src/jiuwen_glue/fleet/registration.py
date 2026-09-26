# coding: utf-8
"""节点池注册协议（PROP-0003 / PROP-0001 v1.7 §12.6）— WO-0011.

规格来源（v1.7 §12.6 节点池纳管）:

- 任何实体机经 **OpenBao JWT 注册**为 worker 节点，声明：能力（CPU/GPU 份额/
  工具/在线窗口）+ 信任等级 + max_parallel —— 能力声明直接复用
  :class:`jiuwen_glue.routes.NodeCapacity`（字段与 CNB company-ops
  ``ops/sql/002_glue_v2.sql`` 的 ``glue.node`` 表对齐），本模块不重造。
- **不可信节点只派沙箱任务类**（硬规则在 scheduler 强制）；本模块在注册入口
  保证声明自洽（attestation 的 trust_level 声明与 capacity 声明不一致 → 拒绝）。
- 心跳超窗 → 节点标记 **STALE**（自取制租约过期的兜底死亡语义）；
  STALE 节点重新出现在调度候选的唯一路径 = 带新 attestation 重新注册。

协议边界（如实声明）:

- **本模块不连真实 OpenBao**：只定义协议与校验——JWT payload claims 解码后做
  **协议级校验**（必填 claims 齐全性、有效期、sub/trust_level 与节点声明一致性）。
  JWT **签名验证归真实 OpenBao**（jwt auth role，M0 审计 B3 骨架已就位），
  真实接入属后续工单，标注 [待真实 bao 接入]。
- **零密钥落盘**：注册时 JWT 明文只经内存参数传递，解码校验后仅保留派生的
  非敏感字段（iat/exp/sub/trust/aud）与 ``jwt_ref`` 引用——原始 token 永不存储
  （跨层只传引用，4.9 #10）。
- 持久化：内存版为权威实现；SQL 持久化只定义接口
  （:class:`RegistryPersistence`，对应 ``glue.node`` 表），不连真实 Postgres。
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from ..routes import NodeCapacity, TRUST_TRUSTED, TRUST_UNTRUSTED
from .errors import (
    AttestationError,
    NodeStateError,
    OnlineWindowError,
    RegistrationError,
    UnknownNodeError,
)

__all__ = [
    "NODE_ACTIVE", "NODE_STALE", "NODE_DEREGISTERED",
    "TRUST_LEVELS", "OnlineWindow", "parse_online_window",
    "decode_jwt_claims", "NodeAttestation", "NodeRegistration", "NodeRecord",
    "FleetRegistry", "RegistryPersistence",
]

NODE_ACTIVE = "ACTIVE"
NODE_STALE = "STALE"
NODE_DEREGISTERED = "DEREGISTERED"

TRUST_LEVELS = (TRUST_TRUSTED, TRUST_UNTRUSTED)

# attestation JWT 必填 claims：主体 + 过期时间。缺失即拒绝注册（fail-closed）。
_REQUIRED_CLAIMS = ("sub", "exp")


# ── 在线窗口（声明可解析性在注册入口把关；调度器消费 is_online）────────────────

@dataclass(frozen=True)
class OnlineWindow:
    """解析后的在线窗口。``start_minutes is None`` = 永远在线（"always"）。

    支持 ``HH:MM-HH:MM`` 与带整点时区偏移的 ``HH:MM-HH:MM+HH``（如
    ``09:00-18:00+08``）；窗口按节点本地墙钟判定；跨午夜窗口（start > end）合法。
    """

    start_minutes: Optional[int] = None
    end_minutes: Optional[int] = None
    offset_minutes: int = 0

    def is_online(self, now: float) -> bool:
        if self.start_minutes is None:
            return True
        tz = timezone(timedelta(minutes=self.offset_minutes))
        local = datetime.fromtimestamp(now, tz=tz)
        minute_of_day = local.hour * 60 + local.minute
        start, end = self.start_minutes, self.end_minutes
        if start < end:
            return start <= minute_of_day < end
        return minute_of_day >= start or minute_of_day < end   # 跨午夜


_WINDOW_RE = re.compile(
    r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})(?:\s*([+-])(\d{1,2}))?$")


def parse_online_window(value: str) -> OnlineWindow:
    """解析在线窗口声明；不可解析 → OnlineWindowError（fail-closed，不猜）。"""
    if not isinstance(value, str) or not value.strip():
        raise OnlineWindowError("online_window must be a non-empty string")
    text = value.strip()
    if text.lower() == "always":
        return OnlineWindow()
    m = _WINDOW_RE.match(text)
    if m is None:
        raise OnlineWindowError(
            f"online_window {value!r} not parseable — "
            "expected 'always' or 'HH:MM-HH:MM[+HH]'")
    sh, sm, eh, em, sign, off = m.groups()
    start = int(sh) * 60 + int(sm)
    end = int(eh) * 60 + int(em)
    if int(sh) > 23 or int(eh) > 23 or int(sm) > 59 or int(em) > 59:
        raise OnlineWindowError(f"online_window {value!r} has out-of-range time")
    if start == end:
        raise OnlineWindowError(
            f"online_window {value!r} is degenerate (start == end)")
    offset = int(off) * 60 if off else 0
    if offset and int(off) > 14:
        raise OnlineWindowError(f"online_window {value!r} offset out of range")
    return OnlineWindow(start, end, (-offset if sign == "-" else offset))


# ── OpenBao JWT 注册声明（协议级校验；签名验证归真实 bao，[待真实 bao 接入]）──

def decode_jwt_claims(token: str) -> Dict[str, object]:
    """解码 JWT payload（仅解码，**不验签**——签名验证归真实 OpenBao）。

    格式非法 / payload 不是 JSON 对象 → AttestationError。
    """
    if not isinstance(token, str) or not token.strip():
        raise AttestationError("attestation jwt must be a non-empty string")
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise AttestationError(
            f"attestation jwt must have 3 segments, got {len(parts)}")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise AttestationError("attestation jwt payload is not decodable") from None
    if not isinstance(claims, dict):
        raise AttestationError("attestation jwt payload must be a JSON object")
    return claims


@dataclass(frozen=True)
class NodeAttestation:
    """注册声明（attestation）——只保存 JWT 的**派生字段与引用**，永不保存 token。

    构造入口 :meth:`from_jwt`：解码 claims 并做协议级校验——
    必填 claims（``sub``/``exp``）缺失 → 拒绝；``exp`` 已过 → 拒绝。
    """

    jwt_ref: str                       # JWT 引用（如 bao audit/acl 句柄），不是 token
    issued_at: Optional[float]         # iat claim（可缺省）
    expires_at: float                  # exp claim（必填）
    trust_level: Optional[str]         # trust_level claim（可选；与 capacity 声明交叉校验）
    subject: Optional[str]             # sub claim（可选；与 node_id 交叉校验）
    audience: Optional[str] = None     # aud claim（M0 B3 为占位，如实）

    @classmethod
    def from_jwt(cls, token: str, *, jwt_ref: str, now: float) -> "NodeAttestation":
        if not jwt_ref or not isinstance(jwt_ref, str):
            raise AttestationError("jwt_ref must be a non-empty reference string")
        claims = decode_jwt_claims(token)
        missing = [c for c in _REQUIRED_CLAIMS if c not in claims]
        if missing:
            raise AttestationError(
                f"attestation jwt missing required claims: {missing}")
        exp = claims["exp"]
        if not isinstance(exp, (int, float)) or isinstance(exp, bool):
            raise AttestationError(f"exp claim must be a number, got {exp!r}")
        if now >= float(exp):
            raise AttestationError(
                f"attestation jwt expired (exp={exp}, now={now}) — re-attest")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise AttestationError("sub claim must be a non-empty string")
        trust = claims.get("trust_level")
        if trust is not None and trust not in TRUST_LEVELS:
            raise AttestationError(
                f"trust_level claim must be one of {TRUST_LEVELS}, got {trust!r}")
        iat = claims.get("iat")
        if iat is not None and (not isinstance(iat, (int, float)) or isinstance(iat, bool)):
            raise AttestationError(f"iat claim must be a number, got {iat!r}")
        aud = claims.get("aud")
        if aud is not None and not isinstance(aud, str):
            raise AttestationError(f"aud claim must be a string, got {aud!r}")
        return cls(jwt_ref=jwt_ref, issued_at=None if iat is None else float(iat),
                   expires_at=float(exp), trust_level=trust, subject=sub,
                   audience=aud)

    def validate_at(self, now: float) -> None:
        """以注册方时钟复检有效期（惰性过期判定，与 leases 同风格）。"""
        if now >= self.expires_at:
            raise AttestationError(
                f"attestation jwt expired (exp={self.expires_at}, now={now})")


@dataclass(frozen=True)
class NodeRegistration:
    """一次节点注册：身份 + 能力声明（复用 NodeCapacity）+ attestation。

    ``agent_ref`` 是该节点执行运行时的**三层复合身份引用**
    （identity.composite_ref 产出，形如 ``ag:.../run:.../task:...``）——
    只传引用，不复制身份状态（4.9 #10）。
    """

    node_id: str
    capacity: NodeCapacity
    attestation: NodeAttestation
    heartbeat_ttl_seconds: float = 300.0   # 心跳超窗即 STALE（兜底死亡语义的窗口）
    agent_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.node_id or not isinstance(self.node_id, str):
            raise RegistrationError("node_id must be a non-empty string")
        if not isinstance(self.capacity, NodeCapacity):
            raise RegistrationError("capacity must be a NodeCapacity (reuse, do not rebuild)")
        if self.capacity.node_id != self.node_id:
            raise RegistrationError(
                f"capacity.node_id {self.capacity.node_id!r} != registration "
                f"node_id {self.node_id!r}")
        if not isinstance(self.attestation, NodeAttestation):
            raise RegistrationError("attestation must be a NodeAttestation")
        if not isinstance(self.heartbeat_ttl_seconds, (int, float)) or \
                self.heartbeat_ttl_seconds <= 0:
            raise RegistrationError(
                f"heartbeat_ttl_seconds must be a positive number, "
                f"got {self.heartbeat_ttl_seconds!r}")
        if self.agent_ref is not None and (not isinstance(self.agent_ref, str)
                                           or not self.agent_ref):
            raise RegistrationError("agent_ref must be None or a non-empty reference")


@dataclass
class NodeRecord:
    """注册后的节点台账记录（内存权威；SQL 持久化经 RegistryPersistence）。"""

    registration: NodeRegistration
    status: str = NODE_ACTIVE
    registered_at: float = 0.0
    last_seen_at: float = 0.0
    online: OnlineWindow = field(default_factory=OnlineWindow)

    @property
    def capacity(self) -> NodeCapacity:
        return self.registration.capacity

    @property
    def node_id(self) -> str:
        return self.registration.node_id

    @property
    def tenant_id(self) -> str:
        return self.registration.capacity.tenant_id


class RegistryPersistence:
    """可选 SQL 持久化接口（内存版为权威实现；真实 Postgres 接入属后续工单）。

    目标表 = CNB company-ops ``ops/sql/002_glue_v2.sql`` 的 ``glue.node``
    （tenant_id / node_id / cpu_frac / gpu_frac / tools JSONB / trust_level /
    max_parallel / online_window / registered_at）。DDL 未含状态与心跳列——
    真实实现需另加 ``glue.node_status``（或 ALTER 加列），schema 变更走 PR
    （company-ops，方案纪律：实例配置变更走 PR）。方法默认抛 NotImplementedError。
    """

    def upsert_node(self, record: "NodeRecord",
                    registration: "NodeRegistration") -> None:
        raise NotImplementedError

    def record_heartbeat(self, node_id: str, at: float) -> None:
        raise NotImplementedError

    def update_status(self, node_id: str, status: str, at: float) -> None:
        raise NotImplementedError


class FleetRegistry:
    """节点池注册台账（内存版）。

    - ``register``：校验 attestation（缺失/过期/主体与信任等级不一致 → 拒绝）
      + 在线窗口可解析（fail-closed）→ 入池 ACTIVE；重复注册 = upsert
      （STALE 节点重新入池的**唯一**路径：带新 attestation 重新注册）。
    - ``heartbeat``：刷新 last_seen；对 STALE/已注销节点心跳 → NodeStateError
      （STALE 是死亡语义，不许悄悄复活）。
    - ``sweep``/惰性判定：心跳超窗（last_seen + heartbeat_ttl_seconds）→ STALE。
    - 所有事件追加 audit（检测 = 拒绝/标记 + 留痕）。
    """

    def __init__(self, *, now: Optional[Callable[[], float]] = None,
                 persistence: Optional[RegistryPersistence] = None) -> None:
        self._now = now or time.time
        self._persistence = persistence
        self._nodes: Dict[str, NodeRecord] = {}
        self.audit: List[Dict[str, object]] = []

    # ── 注册 ─────────────────────────────────────────────────────────────

    def register(self, registration: NodeRegistration, *,
                 now: Optional[float] = None) -> NodeRecord:
        if not isinstance(registration, NodeRegistration):
            raise RegistrationError("register expects a NodeRegistration")
        now = self._now() if now is None else now
        # 协议级校验：声明缺失/过期即拒（签名验证归真实 bao，[待真实 bao 接入]）
        registration.attestation.validate_at(now)
        att = registration.attestation
        if att.trust_level is not None and att.trust_level != registration.capacity.trust_level:
            raise AttestationError(
                f"attestation trust_level {att.trust_level!r} conflicts with "
                f"declared capacity trust_level "
                f"{registration.capacity.trust_level!r}")
        if att.subject is not None and att.subject != registration.node_id:
            raise AttestationError(
                f"attestation subject {att.subject!r} does not match node_id "
                f"{registration.node_id!r}")
        window = parse_online_window(registration.capacity.online_window)
        record = NodeRecord(registration=registration, status=NODE_ACTIVE,
                            registered_at=now, last_seen_at=now, online=window)
        self._nodes[registration.node_id] = record      # upsert：重新注册即复活
        self._persist("upsert_node", record, registration)
        self._audit("REGISTER", registration.node_id, at=now,
                    trust_level=registration.capacity.trust_level,
                    jwt_ref=att.jwt_ref)
        return record

    # ── 心跳 / 注销 ──────────────────────────────────────────────────────

    def heartbeat(self, node_id: str, *, now: Optional[float] = None) -> NodeRecord:
        now = self._now() if now is None else now
        record = self.get(node_id)                      # 含惰性 STALE 判定
        if record.status != NODE_ACTIVE:
            raise NodeStateError(
                f"node {node_id} is {record.status} — heartbeat refused; "
                "re-register with a fresh attestation to rejoin")
        record.last_seen_at = now
        self._persist("record_heartbeat", node_id=node_id, at=now)
        self._audit("HEARTBEAT", node_id, at=now)
        return record

    def deregister(self, node_id: str, *, reason: str = "",
                   now: Optional[float] = None) -> NodeRecord:
        now = self._now() if now is None else now
        record = self.get(node_id)
        record.status = NODE_DEREGISTERED
        self._persist("update_status", node_id=node_id, status=NODE_DEREGISTERED,
                      at=now)
        self._audit("DEREGISTER", node_id, at=now, reason=reason)
        return record

    # ── 查询（含惰性 STALE 判定）──────────────────────────────────────────

    def get(self, node_id: str) -> NodeRecord:
        try:
            record = self._nodes[node_id]
        except KeyError:
            raise UnknownNodeError(f"unknown node: {node_id}") from None
        return self._refresh(record)

    def all_nodes(self) -> List[NodeRecord]:
        return [self._refresh(r) for r in list(self._nodes.values())]

    def active_nodes(self) -> List[NodeRecord]:
        return [r for r in self.all_nodes() if r.status == NODE_ACTIVE]

    def sweep(self, *, now: Optional[float] = None) -> List[str]:
        """显式巡检：把全部心跳超窗的 ACTIVE 节点标记 STALE，返回本次标记列表。"""
        now = self._now() if now is None else now
        newly_stale: List[str] = []
        for record in list(self._nodes.values()):
            if record.status != NODE_ACTIVE:
                continue
            if now > record.last_seen_at + record.registration.heartbeat_ttl_seconds:
                self._mark_stale(record, now)
                newly_stale.append(record.node_id)
        return newly_stale

    # ── 内部 ─────────────────────────────────────────────────────────────

    def _refresh(self, record: NodeRecord) -> NodeRecord:
        if record.status == NODE_ACTIVE:
            deadline = record.last_seen_at + record.registration.heartbeat_ttl_seconds
            if self._now() > deadline:
                self._mark_stale(record, self._now())
        return record

    def _mark_stale(self, record: NodeRecord, now: float) -> None:
        record.status = NODE_STALE
        self._persist("update_status", node_id=record.node_id, status=NODE_STALE,
                      at=now)
        self._audit("STALE", record.node_id, at=now)

    def _persist(self, op: str, *args: object, **kwargs: object) -> None:
        if self._persistence is None:
            return
        getattr(self._persistence, op)(*args, **kwargs)

    def _audit(self, event: str, node_id: str, **detail: object) -> None:
        self.audit.append({"event": event, "node_id": node_id, **detail})
