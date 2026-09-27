# -*- coding: utf-8 -*-
"""glue-governance 包共享内核：三个 harness 工具共享的进程内台账单例。

同进程内只此一份台账（GuardrailRun/Challenge/DecisionRecord），三个工具
经 sys.modules 固定名 glue_governance_core 复用同一模块实例。台账本体
全部来自 jiuwen_glue（本包不复制其源码，不新增第二个决策点）。
"""
from __future__ import annotations

from jiuwen_glue.challenge import ChallengeBoard
from jiuwen_glue.decisions import DecisionLog
from jiuwen_glue.guardrail import GuardrailRunStore

CHALLENGE_BOARD = ChallengeBoard()
RUN_STORE = GuardrailRunStore(challenge_board=CHALLENGE_BOARD)
DECISION_LOG = DecisionLog()
