# coding: utf-8
"""Evidence 三态（draft / verified / finalized）— jiuwen-glue 自研对象之一.

规格来源（PROP-0001 v1.6 第 4.1 节；Handbook L4 自治要"区分计划/已发出操作/已确认结果"、
组织级记忆"可复用经验经过验证后才进入记忆"；本工单 WO-0003 将 Evidence 三态定为
draft → verified → finalized）:

- **DRAFT**：草稿。内容可编辑；等待验证。
- **VERIFIED**：已验证。只能从 DRAFT 进入，必须携带验证方式与验证人/器；
  进入后内容冻结（不可再编辑）。
- **FINALIZED**：定稿。只能从 VERIFIED 进入，带 seal 引用；终态、不可变、
  不允许任何回退。能力版本准入（capabilities.admit）只认 VERIFIED/FINALIZED 证据。

不变量（违规必被检测）:
1. 只允许前向迁移 DRAFT→VERIFIED→FINALIZED；跳态与回退抛 IllegalTransitionError。
2. 内容仅 DRAFT 可编辑；VERIFIED/FINALIZED 编辑抛 EvidenceImmutableError。
3. FINALIZED 后一切变更被拒绝（终态）。
4. 每次成功迁移与每次被拒绝的违规尝试都追加进 transitions/audit（检测 = 拒绝 + 留痕）。
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .errors import (
    EvidenceImmutableError,
    IllegalTransitionError,
    UnknownEvidenceError,
)

DRAFT = "DRAFT"
VERIFIED = "VERIFIED"
FINALIZED = "FINALIZED"
_LEGAL = {(DRAFT, VERIFIED), (VERIFIED, FINALIZED)}


@dataclass(frozen=True)
class EvidenceTransition:
    evidence_id: str
    from_state: Optional[str]
    to_state: Optional[str]
    kind: str                      # CREATE / TRANSITION / EDIT / VIOLATION
    occurred_at: float
    method: Optional[str] = None   # 验证方式（verify 时必填）
    checker: Optional[str] = None  # 验证人/器（verify 时必填）
    refs: Dict[str, Any] = field(default_factory=dict)
    note: Optional[str] = None


@dataclass
class Evidence:
    evidence_id: str
    subject: str                       # 证据主体（如 task_run / capability_version / experience）
    content: Dict[str, Any]
    state: str = DRAFT
    created_at: float = 0.0
    verified_at: Optional[float] = None
    verified_by: Optional[str] = None
    verified_method: Optional[str] = None
    finalized_at: Optional[float] = None
    seal_ref: Optional[str] = None


class EvidenceStore:
    """进程内 Evidence 台账（Postgres DDL 见 sql/001_glue_objects.sql 的 glue.evidence）。"""

    def __init__(self, now: Optional[Any] = None) -> None:
        if now is None:
            import time

            now = time.time
        self._now = now
        self._items: Dict[str, Evidence] = {}
        self.transitions: List[EvidenceTransition] = []

    # ── 查询 ─────────────────────────────────────────────────────────────

    def get(self, evidence_id: str) -> Evidence:
        try:
            return self._items[evidence_id]
        except KeyError:
            raise UnknownEvidenceError(f"unknown evidence: {evidence_id}") from None

    def history(self, evidence_id: str) -> List[EvidenceTransition]:
        self.get(evidence_id)
        return [t for t in self.transitions if t.evidence_id == evidence_id]

    def has_reached(self, evidence_id: str, state: str) -> bool:
        return self.get(evidence_id).state == state

    # ── 内部 ─────────────────────────────────────────────────────────────

    def _record(self, ev: Evidence, kind: str, *, from_state: Optional[str] = None,
                to_state: Optional[str] = None, method: Optional[str] = None,
                checker: Optional[str] = None, refs: Optional[Dict[str, Any]] = None,
                note: Optional[str] = None) -> None:
        self.transitions.append(EvidenceTransition(
            evidence_id=ev.evidence_id, from_state=from_state, to_state=to_state,
            kind=kind, occurred_at=self._now(), method=method, checker=checker,
            refs=dict(refs or {}), note=note))

    # ── 生命周期 ─────────────────────────────────────────────────────────

    def create(self, subject: str, content: Dict[str, Any]) -> Evidence:
        ev = Evidence(evidence_id=uuid.uuid4().hex, subject=subject,
                      content=copy.deepcopy(content), created_at=self._now())
        self._items[ev.evidence_id] = ev
        self._record(ev, "CREATE", to_state=DRAFT)
        return ev

    def update_content(self, evidence_id: str, content: Dict[str, Any]) -> Evidence:
        """仅 DRAFT 可编辑；否则拒绝并留痕。"""
        ev = self.get(evidence_id)
        if ev.state != DRAFT:
            self._record(ev, "VIOLATION", from_state=ev.state, to_state=ev.state,
                         note="edit rejected: content frozen after DRAFT")
            raise EvidenceImmutableError(
                f"evidence {evidence_id} is {ev.state}; only DRAFT content is editable")
        ev.content = copy.deepcopy(content)
        self._record(ev, "EDIT", from_state=DRAFT, to_state=DRAFT)
        return ev

    def verify(self, evidence_id: str, *, method: str, checker: str,
               refs: Optional[Dict[str, Any]] = None) -> Evidence:
        """DRAFT → VERIFIED：必须声明验证方式与验证人/器。"""
        ev = self.get(evidence_id)
        if not method or not checker:
            self._record(ev, "VIOLATION", from_state=ev.state, to_state=VERIFIED,
                         note="verify rejected: method/checker required")
            raise IllegalTransitionError("verify requires method and checker")
        if ev.state != DRAFT:
            self._record(ev, "VIOLATION", from_state=ev.state, to_state=VERIFIED,
                         note=f"verify rejected: illegal from {ev.state}")
            raise IllegalTransitionError(
                f"verify requires state DRAFT, evidence {evidence_id} is {ev.state}")
        ev.state = VERIFIED
        ev.verified_at = self._now()
        ev.verified_method = method
        ev.verified_by = checker
        self._record(ev, "TRANSITION", from_state=DRAFT, to_state=VERIFIED,
                     method=method, checker=checker, refs=refs)
        return ev

    def finalize(self, evidence_id: str, *, seal_ref: str) -> Evidence:
        """VERIFIED → FINALIZED：终态封存。"""
        ev = self.get(evidence_id)
        if ev.state != VERIFIED:
            self._record(ev, "VIOLATION", from_state=ev.state, to_state=FINALIZED,
                         note=f"finalize rejected: illegal from {ev.state}")
            raise IllegalTransitionError(
                f"finalize requires state VERIFIED, evidence {evidence_id} is {ev.state}")
        if not seal_ref:
            self._record(ev, "VIOLATION", from_state=VERIFIED, to_state=FINALIZED,
                         note="finalize rejected: seal_ref required")
            raise IllegalTransitionError("finalize requires seal_ref")
        ev.state = FINALIZED
        ev.finalized_at = self._now()
        ev.seal_ref = seal_ref
        self._record(ev, "TRANSITION", from_state=VERIFIED, to_state=FINALIZED,
                     refs={"seal_ref": seal_ref})
        return ev

    def demote(self, evidence_id: str, to_state: str) -> Evidence:
        """显式的回退请求：一律拒绝并留痕（终态不可变、只进不退）。"""
        ev = self.get(evidence_id)
        self._record(ev, "VIOLATION", from_state=ev.state, to_state=to_state,
                     note="demote rejected: transitions are forward-only")
        raise IllegalTransitionError(
            f"backward transition {ev.state} -> {to_state} is not allowed")

    def admit_ready(self, evidence_id: str) -> bool:
        """能力版本准入可用的证据：VERIFIED 或 FINALIZED。"""
        return self.get(evidence_id).state in (VERIFIED, FINALIZED)
