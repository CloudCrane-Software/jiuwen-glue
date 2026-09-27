# coding: utf-8
"""旧实现快照（红证明靶子）——**不可用于生产**，仅 spec-gate 红证明维度使用.

来源：git 6f4674c（main，W-01 修复前）src/jiuwen_glue/guardrail.py 的 aggregate，
逐字拷贝（去掉相对 import，改用本地常量）。快照纪律见 evals/guardrail-aggregate/README.md：
这里的一切缺陷是**有意保留**的历史事实——`aggregate_v_pre_w01([]) == "PASS"`（fail-open）
正是 W-01 缺陷 #2 的原始形态，红反例 agg-red-001 必须永远能在它身上复现断言失败
（v2.0 §5.2 红证明：新 case 在旧实现断言失败）。
"""
from __future__ import annotations

from typing import Iterable

VERDICT_PASS = "PASS"
VERDICT_BLOCKED = "BLOCKED"
VERDICT_UNKNOWN = "UNKNOWN"


def aggregate_v_pre_w01(verdicts: Iterable[str]) -> str:
    """聚合语义（纯函数）：任一 BLOCKED → BLOCKED；否则任一 UNKNOWN → UNKNOWN；否则 PASS。

    （6f4674c 原始 docstring，原样保留——注意其中没有空集条款，空输入落到 PASS，
    即 W-01 缺陷 #2 的 fail-open。）
    """
    vs = list(verdicts)
    if any(v == VERDICT_BLOCKED for v in vs):
        return VERDICT_BLOCKED
    if any(v == VERDICT_UNKNOWN for v in vs):
        return VERDICT_UNKNOWN
    return VERDICT_PASS
