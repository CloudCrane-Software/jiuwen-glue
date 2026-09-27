# coding: utf-8
"""jiuwen-console-tui — 治理面作战室 TUI（WO-0012 / PROP-0005，v1.7 §12.3）.

公开面：``MockConsoleStore`` / ``PgConsoleStore`` / ``connect_pg``（数据层）、
``ConsoleApp``（Textual 塔式布局）、state 模块（干预状态机）。
分层边界：执行面交互属于 jiuwenswarm 自带 TUI；本包只做治理面只读展示 +
s/a/p 三级受控干预，全部干预经控制台留痕。
"""
from .data import (AgentColumn, AuditEntry, ChallengeRow, ConsoleStore,
                   DecisionRow, HARD_LIST_CATEGORIES, LeaseRow, MockConsoleStore,
                   NodeRow, PgConsoleStore, PoolSummary, ReadinessCardRow,
                   TaskRow, connect_pg)
from .adjudication import AdjudicationCardScreen, PIECE_LABELS, render_readiness_card
from .state import (CARD_APPROVED, CARD_PENDING, CARD_RETURNED, CH_PENDING,
                    KIND_APPROVE, KIND_DENY, KIND_ESCALATE_BACK, KIND_PAUSE,
                    KIND_RESUME, KIND_STEER, OPERATOR, GovernanceError,
                    AdjudicationResolutionError, IllegalTransitionError,
                    ChallengeResolutionError, UnknownTargetError, apply_pause,
                    audit_event, canonical_hash, resolve_adjudication_state,
                    resolve_challenge_state)

__version__ = "0.1.0"

__all__ = [
    "AgentColumn", "AuditEntry", "ChallengeRow", "ConsoleStore", "DecisionRow",
    "LeaseRow", "MockConsoleStore", "NodeRow", "PgConsoleStore", "PoolSummary",
    "TaskRow", "ReadinessCardRow", "HARD_LIST_CATEGORIES", "connect_pg",
    "AdjudicationCardScreen", "PIECE_LABELS", "render_readiness_card",
    "CARD_PENDING", "CARD_APPROVED", "CARD_RETURNED",
    "CH_PENDING", "KIND_APPROVE", "KIND_DENY", "KIND_ESCALATE_BACK", "KIND_PAUSE",
    "KIND_RESUME", "KIND_STEER", "OPERATOR", "GovernanceError", "IllegalTransitionError",
    "ChallengeResolutionError", "AdjudicationResolutionError", "UnknownTargetError",
    "apply_pause", "audit_event", "canonical_hash",
    "resolve_challenge_state", "resolve_adjudication_state",
]
