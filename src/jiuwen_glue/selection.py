# coding: utf-8
"""line.Selection — 扇出择优（v2.0 §5.4 择优公式的参考实现骨架，line 圈）.

圈别（v2.0 §3.1 命名规范）：本模块属 **line.**（生产线）——概念名 ``line.selection``，
物理落位在 glue 单包内（与 e2b_compat=exec 圈、console-tui=product 圈同法：包内模块，
圈别写死在文档与常量，不为圈复制实现）。

术语边界（v2.0 §2，写死）：
- **择优（Selection）** = 从多个**已通过门控**的候选中按机器信号公式选一个；
- 择优**不在门控函数内发生**（§0 总则 1/3：过不过由 GuardrailRun 聚合唯一判定；
  选哪个是本模块——择优唯一决策点落在这里，全体系不设第二处）；
- 本模块只接收已 PASS 的候选：调用方先对每个候选跑 ``GuardrailRunStore.gate()``，
  把 verdict **以值传入**（跨组件只传引用与快照，不缓存状态，§1.2）；本模块
  **不 import guardrail、不查 store**（line 不引用 product 实现的依赖方向纪律）。

择优公式（§5.4，纯机器信号，LLM 评分永不进回路）::

    score = eval*0.45 + mutation*0.20 + conciseness*0.15
          + differential_consensus*0.10 + budget*0.10

护栏（全部 fail-closed）：
- **N > 5 拒绝加载**（权重复算护栏，§5.4）；
- 空候选集拒绝（空输入不放行，§0 总则 2 同构）；
- 信号缺失/越界/非法 → 拒绝（不放行、不猜）；
- **平局裁决是评分进回路的唯一豁免**（R1 纪律，§12 风险表）：同分候选按
  ``candidate_ref`` 字典序取最小——确定性、可复算、无隐藏权重。

边界（写死）：本模块无扇出编排、无执行面、无网络。真实扇出场景（生成 N 候选 →
各自过门控 → 择优）属 line 流水线后续工单；**[待] 公式与护栏已实现，端到端择优
尚未被真实扇出数据验证**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

# ── 常量（写死，§5.4）────────────────────────────────────────────────────────

SCOPE = "line"  # 圈别（v2.0 §3.1；包内落位说明见模块 docstring）

SIGNAL_EVAL = "eval"                              # eval 指标（0.45）
SIGNAL_MUTATION = "mutation"                      # 变异杀死率（0.20）
SIGNAL_CONCISENESS = "conciseness"                # 简洁（0.15）
SIGNAL_DIFF_CONSENSUS = "differential_consensus"  # 差分共识（0.10）
SIGNAL_BUDGET = "budget"                          # 预算（0.10）
SIGNALS = (SIGNAL_EVAL, SIGNAL_MUTATION, SIGNAL_CONCISENESS,
           SIGNAL_DIFF_CONSENSUS, SIGNAL_BUDGET)

SIGNAL_WEIGHTS: Mapping[str, float] = {
    SIGNAL_EVAL: 0.45,
    SIGNAL_MUTATION: 0.20,
    SIGNAL_CONCISENESS: 0.15,
    SIGNAL_DIFF_CONSENSUS: 0.10,
    SIGNAL_BUDGET: 0.10,
}

MAX_CANDIDATES = 5  # N>5 拒绝加载（权重复算护栏，§5.4）

VERDICT_PASS = "PASS"  # 与门控三态对齐（值快照，非 import）


class SelectionError(Exception):
    """择优层错误基类。"""


class SelectionRejectedError(SelectionError):
    """候选集被拒绝（未过门控 / 超载 / 信号非法 / 空集）——fail-closed。"""


# ── 对象 ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Candidate:
    """一个已过门控的候选（跨层只传引用：ref + gate_run_id + 机器信号快照）。"""

    candidate_ref: str                 # 候选引用（如实现 commit / 产物 ID）
    gate_run_id: str                   # 其门控 GuardrailRun 的 run_id（引用，不复制状态）
    signals: Mapping[str, float] = field(default_factory=dict)  # 五信号 ∈ [0,1]

    def __post_init__(self) -> None:
        if not self.candidate_ref or not isinstance(self.candidate_ref, str):
            raise SelectionError("candidate_ref must be a non-empty string")
        if not self.gate_run_id or not isinstance(self.gate_run_id, str):
            raise SelectionError("gate_run_id must be a non-empty string")


@dataclass(frozen=True)
class SelectionResult:
    """择优输出：``winner_ref`` 是唯一结论（§5.4：扇出择优写出 winner_ref）。"""

    winner_ref: str
    winner_gate_run_id: str
    scores: Mapping[str, float]        # candidate_ref → score（全量留痕，可复算）
    tie_broken: bool = False           # True = 触发 R1 平局豁免（字典序）
    n_candidates: int = 0


# ── 公式 ─────────────────────────────────────────────────────────────────────

def score(signals: Mapping[str, float]) -> float:
    """加权求和（纯函数）。信号缺失/多余/越界/非数值 → SelectionRejectedError。"""
    if not isinstance(signals, Mapping):
        raise SelectionRejectedError("signals must be a mapping")
    missing = [k for k in SIGNALS if k not in signals]
    if missing:
        raise SelectionRejectedError(f"missing signals: {sorted(missing)}")
    unknown = [k for k in signals if k not in SIGNAL_WEIGHTS]
    if unknown:
        raise SelectionRejectedError(f"unknown signals: {sorted(unknown)}")
    total = 0.0
    for k in SIGNALS:
        v = signals[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 <= float(v) <= 1.0:
            raise SelectionRejectedError(f"signal {k} must be a number in [0,1], got {v!r}")
        total += SIGNAL_WEIGHTS[k] * float(v)
    return round(total, 12)


# ── 择优（唯一决策点）───────────────────────────────────────────────────────

def select(candidates, gate_verdicts: Mapping[str, str]) -> SelectionResult:
    """从已通过门控的候选中择优，返回 winner_ref。

    gate_verdicts: candidate_ref → 门控 verdict 值快照（调用方从 GuardrailRun
    的 gate() 结果取；本模块不查库——依赖方向纪律，见模块 docstring）。
    拒绝路径（全部 fail-closed）：空集 / N>5 / 任一候选 verdict != PASS / 信号非法。
    平局：R1 唯一豁免——candidate_ref 字典序最小，tie_broken=True 留痕。
    """
    cands = list(candidates)
    if not cands:
        raise SelectionRejectedError("no candidates (empty set is never selected)")
    if len(cands) > MAX_CANDIDATES:
        raise SelectionRejectedError(
            f"{len(cands)} candidates > MAX_CANDIDATES={MAX_CANDIDATES} "
            "(weight recomputation guard, v2.0 §5.4)")
    if not isinstance(gate_verdicts, Mapping):
        raise SelectionRejectedError("gate_verdicts must be a mapping")

    scores: dict = {}
    for c in cands:
        verdict = gate_verdicts.get(c.candidate_ref)
        if verdict is None:
            raise SelectionRejectedError(
                f"candidate {c.candidate_ref} has no gate verdict (fail-closed)")
        if verdict != VERDICT_PASS:
            raise SelectionRejectedError(
                f"candidate {c.candidate_ref} gate verdict is {verdict}, "
                "only PASS candidates enter selection")
        scores[c.candidate_ref] = score(c.signals)

    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    top_score = ordered[0][1]
    tied = [ref for ref, s in ordered if s == top_score]
    winner_ref = tied[0]  # sorted 已按 candidate_ref 字典序（R1 平局豁免，确定性）
    winner = next(c for c in cands if c.candidate_ref == winner_ref)
    return SelectionResult(
        winner_ref=winner_ref,
        winner_gate_run_id=winner.gate_run_id,
        scores=dict(scores),
        tie_broken=len(tied) > 1,
        n_candidates=len(cands),
    )
