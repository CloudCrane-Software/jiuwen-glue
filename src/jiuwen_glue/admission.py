# coding: utf-8
"""准入记录 — WO-0007 收缩版件 2（PROP-0001 v1.6 §4.9 #9；v1.7 §12.7）.

每条经验/技能包的生效**绑定 Spec 版本与证据引用**（WO-0007 收缩版第 2 件）。

硬规则（写死）——**消融通过 且 GuardrailRun 聚合 verdict == PASS 才 ALLOWED**:
- GuardrailRun BLOCKED → REJECTED（门控明确阻断）；
- GuardrailRun UNKNOWN → HELD（门控协议信息不足：补齐后重新准入，不终审——
  fail-closed；HELD 不是放行，也不是终审拒绝）；
- 消融 IMPROVED 才算"通过"（默认；NEUTRAL / REGRESSED / INCONCLUSIVE → REJECTED，
  其中 INCONCLUSIVE 是 WO-0007 明文的"拒绝准入的合法结论"）；
- 优先级：**门控协议完备性优先**——UNKNOWN 一律 HELD，消融结论只在门控
  PASS / BLOCKED 时参与终审（协议未闭合时任何终审都不可靠）。

append-only：:class:`AdmissionLedger` 只有 ``admit()`` 与查询，无 update/delete；
记录 frozen。外发：:func:`export_to_skillpack` 产出 skill-pack 仓 OKF spec
review 段形状的 dict（形状对齐对方仓 docs/okf-spec.md §3/§6——本仓**不 import
对方仓**，以文档形状为契约）。

边界（写死）: 本模块只做**准入判定与留痕**；门控的执行在原生
core.security.guardrail 与各执行点（TeamPermissionRail / eval-gate / 扫描器），
聚合输出属 GuardrailRunStore（决策点唯一，v1.6 §4.9）；候选由原生自演进通道
产生，本模块不自建经验库。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .ablation import ABLATION_VERDICTS, VERDICT_IMPROVED, AblationResult
from .errors import GlueError
from .guardrail import (RUN_FINALIZED, VERDICT_BLOCKED, VERDICT_PASS,
                        VERDICT_UNKNOWN, aggregate, GuardrailRun)

DECISION_ALLOWED = "ALLOWED"
DECISION_REJECTED = "REJECTED"
DECISION_HELD = "HELD"
ADMISSION_DECISIONS = (DECISION_ALLOWED, DECISION_REJECTED, DECISION_HELD)


class AdmissionError(GlueError):
    """准入判定/外发不合法（对象类型错误、非 ALLOWED 记录外发等）。"""


def _utcnow() -> float:
    return time.time()


def guardrail_verdict_of(run: GuardrailRun) -> str:
    """从 GuardrailRun 派生聚合 verdict（镜像 ``GuardrailRunStore.gate()`` 语义）.

    聚合用同一个 ``guardrail.aggregate``，语义与 store 完全一致：
    OPEN / VOID / 必填 check 未提交 → UNKNOWN；FINALIZED → 聚合三态。
    生产中进程内直接持有 run 对象时用本函数；跨进程应以 store.gate() 的
    GuardrailResult 为准（聚合输出的唯一来源是 GuardrailRunStore）。
    """
    if not isinstance(run, GuardrailRun):
        raise AdmissionError(
            f"guardrail_run must be a GuardrailRun, got {type(run).__name__}")
    if run.state != RUN_FINALIZED:
        return VERDICT_UNKNOWN   # OPEN = 协议未闭合；VOID = 现场已变化（均 fail-closed）
    missing = [c.check_id for c in run.spec.checks
               if c.required and c.check_id not in run.results]
    if missing:
        return VERDICT_UNKNOWN
    return aggregate([r.verdict for r in run.results.values()])


@dataclass(frozen=True)
class AdmissionRecord:
    """一条准入记录（append-only；对应 DDL 层 glue.admission_record 形状）。"""

    admission_id: str
    ts: float
    decision: str                      # ALLOWED / REJECTED / HELD
    reason: str
    candidate_ref: str                 # 候选引用（如 skill:video-cut@v3-candidate）
    guardrail_run_ref: str             # GuardrailRun run_id
    guardrail_verdict: str             # 聚合 verdict（PASS/BLOCKED/UNKNOWN）
    guardrail_spec_version: str        # 绑定的 Spec 版本（WO-0007：生效绑定 Spec 版本）
    guardrail_seal_ref: Optional[str]  # 验收固化 seal 引用（未固化 = None）
    ablation_experiment_id: str
    ablation_verdict: str
    ablation_evidence_ref: str         # 消融证据引用（ablation://<id>）
    tenant_id: str = "t0"


class AdmissionLedger:
    """进程内准入台账（append-only：只有 admit() 与查询，无 update/delete）。"""

    def __init__(self, *, accept_ablation_verdicts: Any = (VERDICT_IMPROVED,),
                 now: Optional[Callable[[], float]] = None) -> None:
        self._accept = frozenset(accept_ablation_verdicts)
        unknown = self._accept - set(ABLATION_VERDICTS)
        if unknown:
            raise AdmissionError(
                f"accept_ablation_verdicts contains unknown verdicts: {sorted(unknown)}")
        self._now = now or _utcnow
        self._records: Dict[str, AdmissionRecord] = {}
        self._order: List[str] = []

    # ── 硬规则判定 + 落账（唯一写入口）────────────────────────────────────

    def admit(self, candidate: str, guardrail_run: GuardrailRun,
              ablation_result: AblationResult) -> AdmissionRecord:
        """硬规则：消融通过 且 GuardrailRun 聚合 verdict==PASS 才 ALLOWED。

        candidate 为候选引用串（如 ``skill:video-cut@v3-candidate``）；
        guardrail_run 为 GuardrailRun 对象（verdict 由 :func:`guardrail_verdict_of`
        派生，并绑定其 Spec 版本与 seal）；ablation_result 为消融结论
        （证据引用一并落账）。
        """
        if not candidate or not isinstance(candidate, str):
            raise AdmissionError("candidate must be a non-empty reference string")
        verdict = guardrail_verdict_of(guardrail_run)
        if not isinstance(ablation_result, AblationResult):
            raise AdmissionError(
                f"ablation_result must be an AblationResult, got "
                f"{type(ablation_result).__name__}")

        spec_version = guardrail_run.spec.spec_version
        seal_ref = guardrail_run.seal_ref
        tenant_id = guardrail_run.spec.tenant_id or "t0"
        abl_verdict = ablation_result.verdict

        if verdict == VERDICT_UNKNOWN:
            # 门控协议完备性优先：UNKNOWN 一律 HELD，不终审（补齐后重新准入）
            decision = DECISION_HELD
            reason = (f"guardrail UNKNOWN (protocol incomplete) — held for "
                      f"re-admission, not final; ablation={abl_verdict}")
        elif verdict == VERDICT_BLOCKED:
            decision = DECISION_REJECTED
            reason = f"guardrail BLOCKED — admission denied; ablation={abl_verdict}"
        elif verdict == VERDICT_PASS:
            if abl_verdict in self._accept:
                decision = DECISION_ALLOWED
                reason = (f"ablation {abl_verdict} (accepted set {sorted(self._accept)}) "
                          f"+ guardrail PASS — allowed; evidence "
                          f"{ablation_result.evidence_ref}")
            else:
                decision = DECISION_REJECTED
                reason = (f"ablation {abl_verdict} not in accepted set "
                          f"{sorted(self._accept)} — admission denied "
                          "(INCONCLUSIVE is a legal rejection conclusion, WO-0007)")
        else:
            raise AdmissionError(f"unexpected guardrail verdict {verdict!r}")

        rec = AdmissionRecord(
            admission_id=uuid.uuid4().hex, ts=self._now(), decision=decision,
            reason=reason, candidate_ref=candidate,
            guardrail_run_ref=guardrail_run.run_id, guardrail_verdict=verdict,
            guardrail_spec_version=spec_version, guardrail_seal_ref=seal_ref,
            ablation_experiment_id=ablation_result.experiment_id,
            ablation_verdict=abl_verdict,
            ablation_evidence_ref=ablation_result.evidence_ref,
            tenant_id=tenant_id)
        self._records[rec.admission_id] = rec
        self._order.append(rec.admission_id)
        return rec

    # ── 查询（append-only 视图）──────────────────────────────────────────

    def get(self, admission_id: str) -> AdmissionRecord:
        try:
            return self._records[admission_id]
        except KeyError:
            raise AdmissionError(f"unknown admission record: {admission_id}") from None

    def all(self) -> List[AdmissionRecord]:
        return [self._records[i] for i in self._order]

    def by_decision(self, decision: str) -> List[AdmissionRecord]:
        if decision not in ADMISSION_DECISIONS:
            raise AdmissionError(
                f"decision must be one of {ADMISSION_DECISIONS}, got {decision!r}")
        return [r for r in self.all() if r.decision == decision]


# ── 外发：skill-pack 仓 OKF review 段形状（不 import 对方仓）────────────────

def export_to_skillpack(record: AdmissionRecord) -> Dict[str, Any]:
    """把 ALLOWED 的准入记录外发为 skill-pack 仓 OKF manifest ``review`` 段形状.

    形状契约（对齐 skill-pack docs/okf-spec.md §3 review 字段 / §6"生长必过门禁"；
    本仓不 import 对方仓，以对方 spec 文档为准）::

        { "gate_ref":    "guardrail://admission/<admission_id>",  # 必须 guardrail:// 前缀
          "evidence_ref": record.ablation_evidence_ref,            # 消融证据 URI，非空
          "approved_at":  "<UTC 日期 YYYY-MM-DD>" }                # 90 天复审参考

    非 ALLOWED 的记录不可外发——把拒绝/搁置装扮成 pack review 是违规
    （fail-closed，抛 AdmissionError）。
    """
    if not isinstance(record, AdmissionRecord):
        raise AdmissionError(
            f"record must be an AdmissionRecord, got {type(record).__name__}")
    if record.decision != DECISION_ALLOWED:
        raise AdmissionError(
            f"only ALLOWED admissions can be exported to skill-pack, "
            f"record {record.admission_id} is {record.decision}")
    return {
        "gate_ref": f"guardrail://admission/{record.admission_id}",
        "evidence_ref": record.ablation_evidence_ref,
        "approved_at": time.strftime("%Y-%m-%d", time.gmtime(record.ts)),
    }
