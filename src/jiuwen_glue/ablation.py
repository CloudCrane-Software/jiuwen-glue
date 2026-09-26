# coding: utf-8
"""消融验证 — WO-0007 收缩版件 1（PROP-0001 v1.6 §4.9 #9 / 工单 WO-0007 收缩）.

原生自演进通道（Skill 自演进审批流 / SwarmSkills 演进轨 / TTSE / AutoHarness）
只有置信度阈值与审批，**没有对照实验**——本模块补这件事：经验/技能包准入前的
A/B 对照（control=现状 vs treatment=候选），**同一输入集双跑、配对比较**。

verdict 四态:
- IMPROVED      提升显著（配对符号检验 p < alpha 且平均配对差 > 0）
- NEUTRAL       持平（未达显著，或平均配对差为零）
- REGRESSED     劣化显著（p < alpha 且平均配对差 < 0）
- INCONCLUSIVE  不可判定（样本不足 / 求值出错）——**拒绝准入的合法结论**（WO-0007
  原文："样本不足 → INCONCLUSIVE（拒绝准入的合法结论）"）

统计口径（零第三方依赖，写死）:
- 配对差 d_i = treatment_i - control_i（evaluator 分数，越高越好）；
- 显著性 = 双侧精确符号检验（exact sign test，H0: P(+)=0.5，
  :func:`sign_test_p` 纯标准库 math.comb 实现）；
- 判定需"方向 × 显著"同时成立：方向 = 平均配对差符号；平均差为零 → NEUTRAL；
- 求值链上任一异常 / 非有限分数 → 该对丢弃；出现任何错误对 → 整体
  INCONCLUSIVE（fail-closed——带洞的证据不能支撑准入）。

结果即证据（append-only）: ``run()`` 只允许调用一次，:class:`AblationResult`
frozen 且自带 ``evidence_ref``（``ablation://<experiment_id>``）——准入
（admission.py）绑定该证据引用，结论不可重跑覆盖（准入证据不可漂移）。
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence, Tuple

from .errors import GlueError

VERDICT_IMPROVED = "IMPROVED"
VERDICT_NEUTRAL = "NEUTRAL"
VERDICT_REGRESSED = "REGRESSED"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"
ABLATION_VERDICTS = (VERDICT_IMPROVED, VERDICT_NEUTRAL,
                     VERDICT_REGRESSED, VERDICT_INCONCLUSIVE)

ARM_CONTROL = "control"
ARM_TREATMENT = "treatment"

Behavior = Callable[[Any], Any]            # 输入 → 该臂行为（现状/候选）的输出
Evaluator = Callable[[Any, Any], float]    # (item, 输出) → 分数，越高越好


class AblationError(GlueError):
    """消融实验定义/执行不合法（重复 run、臂引用为空、evaluator 缺失等）。"""


def sign_test_p(wins: int, losses: int) -> float:
    """双侧精确符号检验 p 值（H0: P(+) = 0.5）；纯标准库，零第三方依赖。"""
    if wins < 0 or losses < 0:
        raise AblationError("wins/losses must be non-negative")
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2.0 * tail)


@dataclass(frozen=True)
class AblationArm:
    """一个实验臂：name（标签）/ ref（引用：现状配置或候选经验·技能包）/ behavior。"""

    name: str
    ref: str          # 如 "promoted:skill:video-cut@v2"（现状）/ "skill:video-cut@v3-candidate"
    behavior: Behavior

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise AblationError("arm name must be a non-empty string")
        if not self.ref or not isinstance(self.ref, str):
            raise AblationError("arm ref must be a non-empty reference")
        if not callable(self.behavior):
            raise AblationError("arm behavior must be callable(item) -> output")


@dataclass(frozen=True)
class PairScore:
    """一对配对结果（同一输入在两臂下的分数与差）。"""

    item_index: int
    control: float
    treatment: float
    delta: float      # treatment - control


@dataclass(frozen=True)
class AblationResult:
    """消融结论（frozen；即准入绑定的证据本体）。"""

    experiment_id: str
    verdict: str                      # ABLATION_VERDICTS 之一
    reason: str
    control_ref: str
    treatment_ref: str
    n_inputs: int
    n_pairs: int                      # 成对求值成功的对数
    n_errors: int                     # 求值失败的对数（>0 → INCONCLUSIVE）
    wins: int                         # delta > 0 的对数
    ties: int                         # delta == 0 的对数
    losses: int                       # delta < 0 的对数
    mean_delta: float
    p_value: float                    # INCONCLUSIVE 时恒为 1.0（不可判定不报告显著性）
    evidence_ref: str                 # "ablation://<experiment_id>"
    pairs: Tuple[PairScore, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.verdict not in ABLATION_VERDICTS:
            raise AblationError(
                f"verdict must be one of {ABLATION_VERDICTS}, got {self.verdict!r}")


class AblationExperiment:
    """一次消融实验：control/treatment 两组定义 + run(evaluator) 配对比较.

    边界（写死）: 本模块只做**对照实验与配对比较**；候选由原生自演进通道产生
    （不自建经验库/发现机制，v1.6 §4.9 #1/#4/#9）；结论供 admission.py 的
    硬规则消费，本模块不做准入判定。
    """

    def __init__(self, *, inputs: Sequence[Any], control: AblationArm,
                 treatment: AblationArm, min_samples: int = 10, alpha: float = 0.05,
                 experiment_id: Optional[str] = None) -> None:
        self._inputs: List[Any] = list(inputs)
        if not isinstance(control, AblationArm) or not isinstance(treatment, AblationArm):
            raise AblationError("control/treatment must be AblationArm instances")
        if control.ref == treatment.ref:
            raise AblationError("control and treatment must reference different artifacts")
        if min_samples < 1:
            raise AblationError("min_samples must be >= 1")
        if not 0.0 < alpha < 1.0:
            raise AblationError("alpha must be within (0, 1)")
        self._control = control
        self._treatment = treatment
        self._min_samples = min_samples
        self._alpha = alpha
        self._experiment_id = experiment_id or uuid.uuid4().hex
        self._result: Optional[AblationResult] = None

    # ── 审计视图 ─────────────────────────────────────────────────────────

    @property
    def experiment_id(self) -> str:
        return self._experiment_id

    @property
    def control(self) -> AblationArm:
        return self._control

    @property
    def treatment(self) -> AblationArm:
        return self._treatment

    @property
    def alpha(self) -> float:
        return self._alpha

    @property
    def min_samples(self) -> int:
        return self._min_samples

    @property
    def result(self) -> Optional[AblationResult]:
        return self._result

    # ── 执行（一次定论）──────────────────────────────────────────────────

    def run(self, evaluator: Evaluator) -> AblationResult:
        """同一输入集双跑 + 配对比较（重复调用抛错——结论不可重跑覆盖）。"""
        if self._result is not None:
            raise AblationError(
                "experiment already ran; its result is append-only admission evidence "
                "— build a new experiment to re-test")
        if not callable(evaluator):
            raise AblationError("evaluator must be callable(item, output) -> float")

        n = len(self._inputs)
        pairs: List[PairScore] = []
        errors = 0
        first_error = ""
        if n >= self._min_samples:
            for idx, item in enumerate(self._inputs):
                try:
                    out_c = self._control.behavior(item)
                    out_t = self._treatment.behavior(item)
                    score_c = float(evaluator(item, out_c))
                    score_t = float(evaluator(item, out_t))
                    if not (math.isfinite(score_c) and math.isfinite(score_t)):
                        raise ValueError(
                            f"non-finite score: control={score_c!r}, treatment={score_t!r}")
                except Exception as exc:               # noqa: BLE001 — 求值故障 = 带洞证据
                    errors += 1
                    if not first_error:
                        first_error = repr(exc)
                    continue
                pairs.append(PairScore(item_index=idx, control=score_c,
                                       treatment=score_t, delta=score_t - score_c))

        wins = sum(1 for p in pairs if p.delta > 0)
        losses = sum(1 for p in pairs if p.delta < 0)
        ties = len(pairs) - wins - losses
        mean_delta = (sum(p.delta for p in pairs) / len(pairs)) if pairs else 0.0

        if n < self._min_samples:
            verdict = VERDICT_INCONCLUSIVE
            reason = (f"insufficient samples: {n} < min_samples={self._min_samples} "
                      "— INCONCLUSIVE is a legal rejection conclusion (WO-0007)")
            p_value = 1.0
        elif errors:
            verdict = VERDICT_INCONCLUSIVE
            reason = (f"evaluator/behavior errors on {errors} pair(s) "
                      f"(first: {first_error}) — fail-closed INCONCLUSIVE")
            p_value = 1.0
        else:
            p_value = sign_test_p(wins, losses)
            if p_value < self._alpha and mean_delta > 0:
                verdict = VERDICT_IMPROVED
                reason = (f"significant improvement: mean paired delta {mean_delta:+.4g}, "
                          f"sign-test p={p_value:.4g} < alpha={self._alpha} "
                          f"(wins={wins}, losses={losses}, ties={ties})")
            elif p_value < self._alpha and mean_delta < 0:
                verdict = VERDICT_REGRESSED
                reason = (f"significant regression: mean paired delta {mean_delta:+.4g}, "
                          f"sign-test p={p_value:.4g} < alpha={self._alpha} "
                          f"(wins={wins}, losses={losses}, ties={ties})")
            else:
                verdict = VERDICT_NEUTRAL
                reason = (f"no significant difference: mean paired delta {mean_delta:+.4g}, "
                          f"sign-test p={p_value:.4g} (alpha={self._alpha}) "
                          f"(wins={wins}, losses={losses}, ties={ties})")

        self._result = AblationResult(
            experiment_id=self._experiment_id, verdict=verdict, reason=reason,
            control_ref=self._control.ref, treatment_ref=self._treatment.ref,
            n_inputs=n, n_pairs=len(pairs), n_errors=errors,
            wins=wins, ties=ties, losses=losses, mean_delta=mean_delta,
            p_value=p_value, evidence_ref=f"ablation://{self._experiment_id}",
            pairs=tuple(pairs))
        return self._result
