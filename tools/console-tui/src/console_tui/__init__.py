# coding: utf-8
"""jiuwen-console-tui — 治理面作战室 TUI（WO-0012 / PROP-0005，v1.7 §12.3）.

公开面：``MockConsoleStore`` / ``PgConsoleStore`` / ``connect_pg``（数据层）、
``ConsoleApp``（Textual 塔式布局）、state 模块（干预状态机）。
分层边界：执行面交互属于 jiuwenswarm 自带 TUI；本包只做治理面只读展示 +
s/a/p 三级受控干预，全部干预经控制台留痕。
"""
from .data import (AgentColumn, AuditEntry, ChallengeRow, ConsoleStore,
                   DecisionRow, LeaseRow, MockConsoleStore, NodeRow,
                   PgConsoleStore, PoolSummary, TaskRow, connect_pg)
from .state import (CH_PENDING, KIND_APPROVE, KIND_DENY, KIND_PAUSE, KIND_RESUME,
                    KIND_STEER, OPERATOR, GovernanceError, IllegalTransitionError,
                    ChallengeResolutionError, UnknownTargetError, apply_pause,
                    audit_event, canonical_hash, resolve_challenge_state)

__version__ = "0.1.0"

__all__ = [
    "AgentColumn", "AuditEntry", "ChallengeRow", "ConsoleStore", "DecisionRow",
    "LeaseRow", "MockConsoleStore", "NodeRow", "PgConsoleStore", "PoolSummary",
    "TaskRow", "connect_pg",
    "CH_PENDING", "KIND_APPROVE", "KIND_DENY", "KIND_PAUSE", "KIND_RESUME",
    "KIND_STEER", "OPERATOR", "GovernanceError", "IllegalTransitionError",
    "ChallengeResolutionError", "UnknownTargetError", "apply_pause",
    "audit_event", "canonical_hash", "resolve_challenge_state",
]
