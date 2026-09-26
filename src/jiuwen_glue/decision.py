# coding: utf-8
"""决策层 MVP — JevProvider 三原语（WO-0010；PROP-0001 v1.7 §0 总则 6 / v1.6 §4.7）.

JEV 命名口径（如实备案）:
    "JEV"为方案 v1.4 引入的概念名（v1.5 原文已散佚不可考）。本 MVP 按方案 0.6 的
    功能语义固化三原语为 classify（分类）/ score（评分）/ judge（裁决），acronym
    备案解释为 Judge-Evaluate-Verify；若 v1.5 原文找回且定义不同，以 ADR 收敛。

规格来源:
- v1.7 §0 总则 6："决策层原生存在（§4.7）：高频决策与分类走 JevProvider 三原语，
  大模型只做推理与生成。"——本模块是这句的代码落点：模型后端（ModelBackend）只出
  推理结果；"选哪个/打几分"的决策行为本身在决策层收敛，并且**每次落账**。
- v1.6 §4.7：决策层与 TTSE 的边界——TTSE 的 FACT/TIP 归纳与 consult 召回是
  **原生经验通道**，决策层不重复做经验召回，只做第六植入点"晋升候选打分"这
  一个交叉点（:func:`make_score_hook`）。
- handbook §3.3.1 原则三："由确定性系统决策，并在靠近资源的地方执行"——模型可以
  提出意图，决定权在确定性系统。:class:`RuleBasedBackend` 就是"确定性系统"的
  最小形态（声明式规则、零模型调用）。

三原语（返回值全部携带 decision_ref——每次高频决策落一条 DecisionRecord，
"大模型只做推理与生成，决策留痕在决策层"）:
- ``classify(item, labels) -> (label, confidence, decision_ref)``
- ``score(item, rubric)    -> (value, decision_ref)``
- ``judge(question, options, constraints) -> (chosen, rationale_ref, decision_ref)``

UNKNOWN 语义（fail-closed，写死）:
原语未配置后端 / 后端弃权（无规则命中）/ 后端异常 / 返回值越出声明选项空间 /
分值非法（非数值、非有限、置信度越界）→ 返回 ``None`` + 拒绝理由
（:class:`RefusalRecord` 留痕，可观测）；**绝不静默编造**，也绝不落半真半假的
DecisionRecord（决策账本只记真实发生的决策）。

记录编码（复用 decisions.py；context 经其 canonical-JSON + SHA-256 哈希，
原文不落账；item/rubric 应为 JSON 兼容结构——set 等经 default=str 退化后
哈希跨进程不稳定）:
- classify: options=labels, chosen=label, meta.confidence；
- score:    score 是连续量，无离散选项空间——options 退化为 ``(str(value),)``，
            float 值在 meta.value；rubric 与 item 进 context 哈希；
- judge:    options=options, chosen=chosen，rationale_ref 由后端给出并直接返回。

边界（写死）:
- 本层**不做经验召回**（TTSE 边界，v1.6 §4.7）；**不做授权裁决**（授权三态属
  TeamPermissionRail / GuardrailRun——决策点唯一，v1.6 §4.9）；本层不是第二个
  门控，而是把高频小决策从大模型对话里拿回确定性轨道并留痕。
- 选项空间生长必过门禁（v1.7 §12.7"生长必过门禁"）——规则表/后端/rubric 的每次
  变更走 PR + 消融（ablation.py）+ 准入（admission.py）。
- ModelBackend 只留形状，**不发真实模型调用**（WO-0010 边界）。
"""
from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, List, Mapping, Optional, Protocol,
                    Sequence, Tuple, runtime_checkable)

from .decisions import DecisionLog
from .errors import DecisionSchemaError, GlueError

PRIMITIVE_CLASSIFY = "classify"
PRIMITIVE_SCORE = "score"
PRIMITIVE_JUDGE = "judge"
PRIMITIVES = (PRIMITIVE_CLASSIFY, PRIMITIVE_SCORE, PRIMITIVE_JUDGE)

# promotion.PromotionLedger score_hook 默认 rubric 名（§4.7 第六植入点）
SCORE_RUBRIC_PROMOTION = "promotion:quality"

ClassifyResult = Tuple[str, Optional[float], str]   # (label, confidence, decision_ref)
ScoreResult = Tuple[float, str]                     # (value, decision_ref)
JudgeResult = Tuple[str, str, str]                  # (chosen, rationale_ref, decision_ref)


class DecisionLayerError(GlueError):
    """决策层装配不合法（agent_ref 缺失、决策账本类型错误等）。"""


class JevBackendError(GlueError):
    """后端/规则声明不合法（原语枚举外、规则缺动作字段等）。"""


@dataclass(frozen=True)
class PrimitiveOutcome:
    """单个原语的原始输出（后端返回；尚未落账）。value=弃权时后端直接返回 None。"""

    value: Any                       # classify: label；score: float；judge: chosen
    rationale_ref: str               # 理由的证据引用（规则引用 / 证据 / 轨迹引用）
    confidence: Optional[float] = None
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.rationale_ref or not isinstance(self.rationale_ref, str):
            raise JevBackendError("rationale_ref must be a non-empty evidence reference")


class JevBackend(ABC):
    """后端接口：三原语各一方法；返回 :class:`PrimitiveOutcome`，弃权返回 None。

    后端只"提出结论与理由引用"，不落账、不做门控——落账与 UNKNOWN 收口在
    :class:`DecisionLayer`（决策点唯一：同一原语的执行与留痕只有一条路径）。
    """

    name: str = "backend"

    @abstractmethod
    def classify(self, item: Any, labels: Sequence[str]) -> Optional[PrimitiveOutcome]:
        """把 item 归入 labels 之一；无法判定返回 None。"""

    @abstractmethod
    def score(self, item: Any, rubric: Any) -> Optional[PrimitiveOutcome]:
        """按 rubric 给 item 打连续分；无法判定返回 None。"""

    @abstractmethod
    def judge(self, question: str, options: Sequence[str],
              constraints: Optional[Mapping[str, Any]] = None) -> Optional[PrimitiveOutcome]:
        """在 options 中裁决一个问题；无法判定返回 None。"""


@runtime_checkable
class JevProvider(Protocol):
    """三原语协议（v1.7 §0 总则 6 "JevProvider" 的类型落点）。

    :class:`DecisionLayer` 门面实现本协议；消费方（promotion score_hook /
    eval-gate L1 score_fn / 治理面 TUI）只依赖本协议，不依赖具体后端。
    原语在无法判定时返回 None（UNKNOWN 语义，fail-closed）。
    """

    def classify(self, item: Any, labels: Sequence[str]) -> Optional[ClassifyResult]: ...

    def score(self, item: Any, rubric: Any) -> Optional[ScoreResult]: ...

    def judge(self, question: str, options: Sequence[str],
              constraints: Optional[Mapping[str, Any]] = None) -> Optional[JudgeResult]: ...


# ── 内置后端 1：声明式规则（确定性，零模型调用）─────────────────────────────

class RuleBasedBackend(JevBackend):
    """声明式规则后端——确定性、零模型调用（内置，handbook 原则三的确定性系统）.

    规则形状（声明式映射；``when`` 缺省 = 恒匹配）::

        {"primitive": "classify", "when": {<item 字段>: 期望值, ...},
         "label": str, "confidence": float(缺省 1.0), "rationale_ref": str}
        {"primitive": "score", "when": {<item 字段>: 期望值, ...},
         "rubric": str(可选, 声明则仅对该 rubric 生效), "value": number,
         "rationale_ref": str}
        {"primitive": "judge", "when": {<约束字段>: 期望值, ...},
         "question_contains": str(可选), "chosen": str, "rationale_ref": str}

    匹配语义（确定性，写死）:
    - ``when`` 各键值**合取**：期望值为 list/tuple/set = 成员测试；否则相等测试；
      字段缺失 = 不匹配；空 ``when`` = 恒匹配；
    - classify/score 的 ``when`` 对 item 求值（item 非 Mapping 时仅空 when 恒匹配）；
    - judge 的 ``when`` 对 constraints 求值，``question_contains`` 对 question 子串匹配；
    - 规则按**声明顺序**求值，第一条命中生效（声明顺序即优先级）；
    - 无命中 → 弃权（返回 None），上层 UNKNOWN——**绝不编造默认答案**。
    - classify 规则未声明 confidence = 规则作者声明满信度 1.0（声明即来源，
      非模型自评）。
    """

    name = "rule_based"

    def __init__(self, rules: Any) -> None:
        validated: List[Dict[str, Any]] = []
        for idx, rule in enumerate(rules):
            if not isinstance(rule, Mapping):
                raise JevBackendError(f"rule #{idx} must be a mapping")
            primitive = rule.get("primitive")
            if primitive not in PRIMITIVES:
                raise JevBackendError(
                    f"rule #{idx}: primitive must be one of {PRIMITIVES}, got {primitive!r}")
            rationale = rule.get("rationale_ref")
            if not rationale or not isinstance(rationale, str):
                raise JevBackendError(
                    f"rule #{idx}: rationale_ref must be a non-empty string")
            when = rule.get("when", {})
            if not isinstance(when, Mapping):
                raise JevBackendError(f"rule #{idx}: when must be a mapping")
            if primitive == PRIMITIVE_CLASSIFY:
                label = rule.get("label")
                if not isinstance(label, str) or not label:
                    raise JevBackendError(
                        f"rule #{idx}: classify rule needs a non-empty string label")
                conf = rule.get("confidence", 1.0)
                if isinstance(conf, bool) or not isinstance(conf, (int, float)) \
                        or not 0.0 <= float(conf) <= 1.0:
                    raise JevBackendError(
                        f"rule #{idx}: confidence must be a number within [0, 1], got {conf!r}")
            elif primitive == PRIMITIVE_SCORE:
                value = rule.get("value")
                if isinstance(value, bool) or not isinstance(value, (int, float)) \
                        or not math.isfinite(float(value)):
                    raise JevBackendError(
                        f"rule #{idx}: score rule needs a finite numeric value, got {value!r}")
                rubric = rule.get("rubric")
                if rubric is not None and not isinstance(rubric, str):
                    raise JevBackendError(
                        f"rule #{idx}: rubric must be a string when declared")
            else:  # judge
                chosen = rule.get("chosen")
                if not isinstance(chosen, str) or not chosen:
                    raise JevBackendError(
                        f"rule #{idx}: judge rule needs a non-empty string chosen")
                qsub = rule.get("question_contains")
                if qsub is not None and not isinstance(qsub, str):
                    raise JevBackendError(
                        f"rule #{idx}: question_contains must be a string")
            validated.append(dict(rule))
        self._rules: Tuple[Dict[str, Any], ...] = tuple(validated)

    @property
    def rules(self) -> Tuple[Dict[str, Any], ...]:
        """规则表只读视图（声明顺序即优先级）。"""
        return tuple(dict(r) for r in self._rules)

    @staticmethod
    def _match(when: Mapping[str, Any], target: Any) -> bool:
        if not when:
            return True
        if not isinstance(target, Mapping):
            return False
        for key, expected in when.items():
            if key not in target:
                return False
            actual = target[key]
            if isinstance(expected, (list, tuple, set)):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    def classify(self, item: Any, labels: Sequence[str]) -> Optional[PrimitiveOutcome]:
        del labels  # labels 空间校验在门面层；规则只负责提出 label
        for rule in self._rules:
            if rule["primitive"] != PRIMITIVE_CLASSIFY:
                continue
            if not self._match(rule.get("when", {}), item):
                continue
            return PrimitiveOutcome(
                value=rule["label"], rationale_ref=rule["rationale_ref"],
                confidence=float(rule.get("confidence", 1.0)))
        return None

    def score(self, item: Any, rubric: Any) -> Optional[PrimitiveOutcome]:
        rubric_name = rubric if isinstance(rubric, str) else (
            rubric.get("name") if isinstance(rubric, Mapping) else None)
        for rule in self._rules:
            if rule["primitive"] != PRIMITIVE_SCORE:
                continue
            declared = rule.get("rubric")
            if declared is not None and rubric_name != declared:
                continue
            if not self._match(rule.get("when", {}), item):
                continue
            return PrimitiveOutcome(value=float(rule["value"]),
                                    rationale_ref=rule["rationale_ref"])
        return None

    def judge(self, question: str, options: Sequence[str],
              constraints: Optional[Mapping[str, Any]] = None) -> Optional[PrimitiveOutcome]:
        del options  # options 空间校验在门面层；规则只负责提出 chosen
        cons = constraints or {}
        for rule in self._rules:
            if rule["primitive"] != PRIMITIVE_JUDGE:
                continue
            qsub = rule.get("question_contains")
            if qsub is not None and (not isinstance(question, str) or qsub not in question):
                continue
            if not self._match(rule.get("when", {}), cons):
                continue
            return PrimitiveOutcome(value=rule["chosen"],
                                    rationale_ref=rule["rationale_ref"])
        return None


# ── 后端 2：模型后端（只留形状，不发真实模型调用）───────────────────────────

class ModelBackend(JevBackend):
    """模型后端——**只留形状（WO-0010 边界：不发真实模型调用）**.

    字段约定（Higress 唯一模型入口，PROP-0001 v1.6 §4.9 #7 / v1.7 §12.1）:
    - ``endpoint_ref``: Higress 模型网关端点的**引用名**（如 ``higress:model-gateway``），
      解析发生在运行时配置层；本类只存引用，不存 URL 值；
    - ``api_key_ref``: 凭据引用（如 ``openbao:secret/credentials/higress#api_key``），
      本类**不存密钥值**（handbook §3.3.1 原则四：长期凭证不进入 Agent；
      EXECUTION-PROTOCOL 红线 3：密钥值不进代码/日志/git）；
    - ``model``: 经 Higress 上游路由的模型名。

    三原语方法一律 raise NotImplementedError——真实接入属后续工单
    （需真机联调 + 经 OpenBao 注入的短时凭据），本 MVP 只固化形状与注入约定。
    """

    name = "model"

    def __init__(self, *, endpoint_ref: str, api_key_ref: str, model: str) -> None:
        for field_name, value in (("endpoint_ref", endpoint_ref),
                                  ("api_key_ref", api_key_ref), ("model", model)):
            if not value or not isinstance(value, str):
                raise JevBackendError(
                    f"ModelBackend.{field_name} must be a non-empty reference string")
        self.endpoint_ref = endpoint_ref    # 引用名，非 URL 值
        self.api_key_ref = api_key_ref      # 凭据引用，非密钥值
        self.model = model

    def classify(self, item: Any, labels: Sequence[str]) -> Optional[PrimitiveOutcome]:
        raise NotImplementedError(
            "ModelBackend is a shape-only stub (WO-0010); real model calls are a "
            "separate work order — wire via Higress single entry, inject creds at runtime")

    def score(self, item: Any, rubric: Any) -> Optional[PrimitiveOutcome]:
        raise NotImplementedError(
            "ModelBackend is a shape-only stub (WO-0010); real model calls are a "
            "separate work order — wire via Higress single entry, inject creds at runtime")

    def judge(self, question: str, options: Sequence[str],
              constraints: Optional[Mapping[str, Any]] = None) -> Optional[PrimitiveOutcome]:
        raise NotImplementedError(
            "ModelBackend is a shape-only stub (WO-0010); real model calls are a "
            "separate work order — wire via Higress single entry, inject creds at runtime")


# ── 门面：按原语路由 + 决策留痕 + UNKNOWN 收口 ──────────────────────────────

@dataclass(frozen=True)
class RefusalRecord:
    """一次弃权的留痕（UNKNOWN 可观测——弃权不是错误，但必须看得见）。"""

    ts: float
    primitive: str
    reason: str
    agent_ref: str
    tenant_id: str


def _utcnow() -> float:
    return time.time()


class DecisionLayer:
    """决策层门面：按原语路由后端 + 每次决策落一条 DecisionRecord。

    - ``backends``: {primitive: JevBackend}；按原语路由，未配置的原语调用
      → UNKNOWN（返回 None + 拒绝理由），**绝不静默编造**；
    - 每次成功决策经 ``decisions.DecisionLog.append`` 落一条 append-only
      DecisionRecord（context 经 canonical-JSON 哈希，原文不落账）；
    - 后端异常 / 弃权 / 越界返回值 → None + :class:`RefusalRecord`（可观测），
      不落 DecisionRecord；调用方参数不合法（如空 labels）→ 直接抛
      DecisionSchemaError（调用方 bug 要大声失败，不算"无法判定"）。

    边界（写死）: 不做经验召回（TTSE 边界）；不做授权裁决（决策点唯一）；
    选项空间生长必过门禁（v1.7 §12.7）。
    """

    def __init__(self, *, decision_log: DecisionLog, agent_ref: str,
                 backends: Optional[Mapping[str, JevBackend]] = None,
                 tenant_id: str = "t0",
                 now: Optional[Callable[[], float]] = None) -> None:
        if not isinstance(decision_log, DecisionLog):
            raise DecisionLayerError("decision_log must be a decisions.DecisionLog")
        if not agent_ref or not isinstance(agent_ref, str):
            raise DecisionLayerError("agent_ref must be a non-empty identity reference")
        if now is None:
            now = _utcnow
        self._log = decision_log
        self._agent_ref = agent_ref
        self._tenant_id = tenant_id or "t0"
        self._now = now
        self.refusals: List[RefusalRecord] = []   # 进程内可观测；生产侧由观测系统承接
        self._backends: Dict[str, JevBackend] = {}
        for primitive, backend in dict(backends or {}).items():
            self.set_backend(primitive, backend)

    # ── 装配 ─────────────────────────────────────────────────────────────

    def set_backend(self, primitive: str, backend: JevBackend) -> None:
        """按原语装配后端（仅限启动装配；选项空间生长必过门禁，v1.7 §12.7）。"""
        if primitive not in PRIMITIVES:
            raise JevBackendError(
                f"primitive must be one of {PRIMITIVES}, got {primitive!r}")
        if not isinstance(backend, JevBackend):
            raise JevBackendError("backend must subclass JevBackend")
        self._backends[primitive] = backend

    def backend_of(self, primitive: str) -> Optional[JevBackend]:
        return self._backends.get(primitive)

    @property
    def decision_log(self) -> DecisionLog:
        return self._log

    @property
    def agent_ref(self) -> str:
        return self._agent_ref

    @property
    def last_refusal(self) -> Optional[RefusalRecord]:
        return self.refusals[-1] if self.refusals else None

    # ── 内部 ─────────────────────────────────────────────────────────────

    def _refuse(self, primitive: str, reason: str, *, agent_ref: str,
                tenant_id: str) -> None:
        self.refusals.append(RefusalRecord(
            ts=self._now(), primitive=primitive, reason=reason,
            agent_ref=agent_ref, tenant_id=tenant_id))

    def _route(self, primitive: str, *, agent_ref: str, tenant_id: str) -> Optional[JevBackend]:
        backend = self._backends.get(primitive)
        if backend is None:
            self._refuse(
                primitive,
                f"no backend configured for primitive {primitive!r} — UNKNOWN (fail-closed)",
                agent_ref=agent_ref, tenant_id=tenant_id)
        return backend

    # ── 原语 1：classify ─────────────────────────────────────────────────

    def classify(self, item: Any, labels: Sequence[str], *, agent_ref: Optional[str] = None,
                 tenant_id: Optional[str] = None,
                 guardrail_run_ref: Optional[str] = None) -> Optional[ClassifyResult]:
        """分类：返回 (label, confidence, decision_ref)；无法判定返回 None。"""
        labels_t = tuple(labels)
        who = agent_ref or self._agent_ref
        tid = tenant_id or self._tenant_id
        if not labels_t or not all(isinstance(x, str) and x for x in labels_t):
            raise DecisionSchemaError(
                "labels must be a non-empty sequence of non-empty strings")
        backend = self._route(PRIMITIVE_CLASSIFY, agent_ref=who, tenant_id=tid)
        if backend is None:
            return None
        try:
            outcome = backend.classify(item, labels_t)
        except Exception as exc:                       # noqa: BLE001 — 后端故障必须 fail-closed
            self._refuse(PRIMITIVE_CLASSIFY,
                         f"classify backend {backend.name!r} raised: {exc!r}",
                         agent_ref=who, tenant_id=tid)
            return None
        if outcome is None:
            self._refuse(PRIMITIVE_CLASSIFY,
                         f"classify backend {backend.name!r} abstained: no rule matched "
                         "— UNKNOWN (fail-closed)", agent_ref=who, tenant_id=tid)
            return None
        if outcome.value not in labels_t:
            self._refuse(PRIMITIVE_CLASSIFY,
                         f"classify backend returned label {outcome.value!r} outside "
                         "declared labels — refused, never fabricate",
                         agent_ref=who, tenant_id=tid)
            return None
        confidence = outcome.confidence
        if confidence is not None and (isinstance(confidence, bool)
                                       or not isinstance(confidence, (int, float))
                                       or not 0.0 <= float(confidence) <= 1.0):
            self._refuse(PRIMITIVE_CLASSIFY,
                         f"classify backend returned out-of-range confidence {confidence!r}",
                         agent_ref=who, tenant_id=tid)
            return None
        meta: Dict[str, Any] = dict(outcome.meta)
        meta.setdefault("backend", backend.name)
        meta["confidence"] = None if confidence is None else float(confidence)
        rec = self._log.append(
            agent_ref=who,
            context={"primitive": PRIMITIVE_CLASSIFY, "item": item,
                     "labels": list(labels_t)},
            options=labels_t, chosen=outcome.value,
            rationale_ref=outcome.rationale_ref, guardrail_run_ref=guardrail_run_ref,
            tenant_id=tid, meta=meta)
        return (outcome.value, None if confidence is None else float(confidence),
                rec.decision_id)

    # ── 原语 2：score ────────────────────────────────────────────────────

    def score(self, item: Any, rubric: Any, *, agent_ref: Optional[str] = None,
              tenant_id: Optional[str] = None,
              guardrail_run_ref: Optional[str] = None) -> Optional[ScoreResult]:
        """评分：返回 (value, decision_ref)；无法判定返回 None。

        rubric 为 str 或 Mapping（一并进 context 哈希）；value 必须是有限数值
        （与 eval-gate 漏斗对非数值/NaN 的 fail-closed 语义同形）。
        """
        who = agent_ref or self._agent_ref
        tid = tenant_id or self._tenant_id
        backend = self._route(PRIMITIVE_SCORE, agent_ref=who, tenant_id=tid)
        if backend is None:
            return None
        try:
            outcome = backend.score(item, rubric)
        except Exception as exc:                       # noqa: BLE001
            self._refuse(PRIMITIVE_SCORE,
                         f"score backend {backend.name!r} raised: {exc!r}",
                         agent_ref=who, tenant_id=tid)
            return None
        if outcome is None:
            self._refuse(PRIMITIVE_SCORE,
                         f"score backend {backend.name!r} abstained: no rule matched "
                         "— UNKNOWN (fail-closed)", agent_ref=who, tenant_id=tid)
            return None
        value = outcome.value
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(float(value)):
            self._refuse(PRIMITIVE_SCORE,
                         f"score backend returned non-finite/non-numeric score {value!r} "
                         "— refused, never fabricate", agent_ref=who, tenant_id=tid)
            return None
        meta = dict(outcome.meta)
        meta.setdefault("backend", backend.name)
        meta["value"] = float(value)
        rec = self._log.append(
            agent_ref=who,
            context={"primitive": PRIMITIVE_SCORE, "item": item, "rubric": rubric},
            options=(str(float(value)),), chosen=str(float(value)),
            rationale_ref=outcome.rationale_ref, guardrail_run_ref=guardrail_run_ref,
            tenant_id=tid, meta=meta)
        return (float(value), rec.decision_id)

    # ── 原语 3：judge ────────────────────────────────────────────────────

    def judge(self, question: str, options: Sequence[str],
              constraints: Optional[Mapping[str, Any]] = None, *,
              agent_ref: Optional[str] = None, tenant_id: Optional[str] = None,
              guardrail_run_ref: Optional[str] = None) -> Optional[JudgeResult]:
        """裁决：返回 (chosen, rationale_ref, decision_ref)；无法判定返回 None。"""
        opts = tuple(options)
        cons = dict(constraints or {})
        who = agent_ref or self._agent_ref
        tid = tenant_id or self._tenant_id
        if not isinstance(question, str) or not question:
            raise DecisionSchemaError("question must be a non-empty string")
        if not opts or not all(isinstance(x, str) and x for x in opts):
            raise DecisionSchemaError(
                "options must be a non-empty sequence of non-empty strings")
        backend = self._route(PRIMITIVE_JUDGE, agent_ref=who, tenant_id=tid)
        if backend is None:
            return None
        try:
            outcome = backend.judge(question, opts, cons)
        except Exception as exc:                       # noqa: BLE001
            self._refuse(PRIMITIVE_JUDGE,
                         f"judge backend {backend.name!r} raised: {exc!r}",
                         agent_ref=who, tenant_id=tid)
            return None
        if outcome is None:
            self._refuse(PRIMITIVE_JUDGE,
                         f"judge backend {backend.name!r} abstained: no rule matched "
                         "— UNKNOWN (fail-closed)", agent_ref=who, tenant_id=tid)
            return None
        if outcome.value not in opts:
            self._refuse(PRIMITIVE_JUDGE,
                         f"judge backend returned chosen {outcome.value!r} outside "
                         "declared options — refused, never fabricate",
                         agent_ref=who, tenant_id=tid)
            return None
        meta = dict(outcome.meta)
        meta.setdefault("backend", backend.name)
        rec = self._log.append(
            agent_ref=who,
            context={"primitive": PRIMITIVE_JUDGE, "question": question,
                     "options": list(opts), "constraints": cons},
            options=opts, chosen=outcome.value,
            rationale_ref=outcome.rationale_ref, guardrail_run_ref=guardrail_run_ref,
            tenant_id=tid, meta=meta)
        return (outcome.value, outcome.rationale_ref, rec.decision_id)


# ── 注入点 1：promotion.score_hook 适配（§4.7 第六植入点，唯一交叉点）────────

def make_score_hook(layer: DecisionLayer, *,
                    rubric: Any = SCORE_RUBRIC_PROMOTION
                    ) -> Callable[[Mapping[str, Any]], Optional[float]]:
    """产出可直接注入 ``promotion.PromotionLedger(score_hook=...)`` 的适配函数.

    两端形状（都写死，注入即用）:
    - promotion 侧 ScoreHook: ``Callable[[候选快照 Mapping], Optional[float]]``，
      None = 决策层弃权 = UNKNOWN = 拒绝晋升（fail-closed）；
    - 决策层侧: 每次打分经 :meth:`DecisionLayer.score` 落一条 DecisionRecord
      （"决策留痕在决策层"），rubric 缺省 ``promotion:quality``。

    本适配器不抛异常——后端异常/弃权已在层内收口为 None（refusal 留痕）。
    """
    if not isinstance(layer, DecisionLayer):
        raise DecisionLayerError("make_score_hook requires a DecisionLayer")

    def _hook(snapshot: Mapping[str, Any]) -> Optional[float]:
        result = layer.score(dict(snapshot), rubric)
        if result is None:
            return None
        value, _decision_ref = result
        return float(value)

    return _hook
