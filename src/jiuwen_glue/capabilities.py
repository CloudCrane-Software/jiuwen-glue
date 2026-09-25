# coding: utf-8
"""能力元数据注册（能力治理台账）— jiuwen-glue 自研对象之一.

规格来源（PROP-0001 v1.6 第 4.1/4.9#1 节；Handbook ch30:199 "副作用/幂等性/风险等级的
可机读声明——当前最欠缺、且收益最直接的一类元数据"）:

- 机读声明五类：外部副作用 / 幂等 / 失败后可安全重试 / 风险等级 / 前置条件；
- 台账只管：五类机读声明、命中率/成功率指标、版本准入；
- **发现与编排一律用原生 Symphony，本台账禁止自建发现机制**（4.9 #1）；
- 指标数据从 TrajectoryRail 执行结果**单向回流**——本层只提供 append-only 的
  执行结果记录，不提供任何"手改指标"的接口。

不变量（违规必被检测）:
1. 声明必须通过 schema 校验：side_effect 枚举、risk_level 0..4、
   retry_safe=True 蕴含 idempotent=True（不可重试安全却非幂等）。
2. 版本准入（ADMITTED）必须引用一条 VERIFIED 或 FINALIZED 的 Evidence；
   缺证据抛 AdmissionDeniedError 并留痕。
3. 指标只增不改：record_* 只做计数追加；不存在 set_metrics 类接口。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .errors import (
    AdmissionDeniedError,
    DeclarationSchemaError,
    UnknownCapabilityError,
)
from .evidence import EvidenceStore

SIDE_EFFECT_NONE = "none"
SIDE_EFFECT_READ = "read"
SIDE_EFFECT_EXTERNAL_WRITE = "external_write"
SIDE_EFFECT_EXTERNAL_IRREVERSIBLE = "external_irreversible"
SIDE_EFFECTS = (SIDE_EFFECT_NONE, SIDE_EFFECT_READ,
                SIDE_EFFECT_EXTERNAL_WRITE, SIDE_EFFECT_EXTERNAL_IRREVERSIBLE)

CANDIDATE = "CANDIDATE"
ADMITTED = "ADMITTED"
REJECTED = "REJECTED"


@dataclass(frozen=True)
class CapabilityDeclaration:
    """五类机读声明（对齐选型报告 §7 的 schema 建议）。"""

    capability_id: str
    version: str
    name: str
    side_effect: str                 # none | read | external_write | external_irreversible
    idempotent: bool
    retry_safe: bool
    risk_level: int                  # 0..4
    preconditions: tuple = ()        # 前置条件（机读字符串/表达式引用）

    def validate(self) -> None:
        problems: List[str] = []
        if not self.capability_id or not isinstance(self.capability_id, str):
            problems.append("capability_id must be a non-empty string")
        if not self.version or not isinstance(self.version, str):
            problems.append("version must be a non-empty string")
        if self.side_effect not in SIDE_EFFECTS:
            problems.append(f"side_effect must be one of {SIDE_EFFECTS}, got {self.side_effect!r}")
        if not isinstance(self.idempotent, bool):
            problems.append("idempotent must be bool")
        if not isinstance(self.retry_safe, bool):
            problems.append("retry_safe must be bool")
        if not isinstance(self.risk_level, int) or not (0 <= self.risk_level <= 4):
            problems.append(f"risk_level must be int 0..4, got {self.risk_level!r}")
        if self.retry_safe and not self.idempotent:
            problems.append("retry_safe=True requires idempotent=True "
                            "(a non-idempotent operation is never retry-safe)")
        if problems:
            raise DeclarationSchemaError("; ".join(problems))


@dataclass
class CapabilityVersion:
    declaration: CapabilityDeclaration
    status: str = CANDIDATE
    admitted_evidence_id: Optional[str] = None


class CapabilityRegistry:
    """进程内能力治理台账（Postgres DDL 见 sql/001_glue_objects.sql 的 glue.capability_version）。"""

    def __init__(self, evidence: Optional[EvidenceStore] = None) -> None:
        self._versions: Dict[tuple, CapabilityVersion] = {}
        self.evidence = evidence or EvidenceStore()

    # ── 注册（schema 校验即检测） ─────────────────────────────────────────

    def register(self, declaration: CapabilityDeclaration) -> CapabilityVersion:
        declaration.validate()
        key = (declaration.capability_id, declaration.version)
        if key in self._versions:
            raise DeclarationSchemaError(
                f"capability {declaration.capability_id}@{declaration.version} already registered")
        cap = CapabilityVersion(declaration=declaration)
        self._versions[key] = cap
        return cap

    def get(self, capability_id: str, version: str) -> CapabilityVersion:
        try:
            return self._versions[(capability_id, version)]
        except KeyError:
            raise UnknownCapabilityError(
                f"unknown capability {capability_id}@{version}") from None

    # ── 版本准入 ──────────────────────────────────────────────────────────

    def admit(self, capability_id: str, version: str, evidence_id: str) -> CapabilityVersion:
        """准入必须挂一条 VERIFIED/FINALIZED 证据；否则拒绝并留痕（status=REJECTED）。"""
        cap = self.get(capability_id, version)
        if cap.status == ADMITTED:
            return cap
        if not self.evidence.admit_ready(evidence_id):
            cap.status = REJECTED
            raise AdmissionDeniedError(
                f"admission of {capability_id}@{version} denied: evidence {evidence_id} "
                "is not VERIFIED/FINALIZED")
        cap.status = ADMITTED
        cap.admitted_evidence_id = evidence_id
        return cap

    def reject(self, capability_id: str, version: str) -> CapabilityVersion:
        cap = self.get(capability_id, version)
        cap.status = REJECTED
        return cap

    def is_admitted(self, capability_id: str, version: str) -> bool:
        return self.get(capability_id, version).status == ADMITTED

    # ── 指标：单向回流，只增不改 ──────────────────────────────────────────

    def record_execution(self, capability_id: str, version: str, outcome: str) -> int:
        """记录一次执行结果（success/failure/hit/miss）。返回该 (capability, outcome) 计数。

        这是指标唯一入口——没有设置/覆盖指标的 API（单向回流）。"""
        if outcome not in ("success", "failure", "hit", "miss"):
            raise ValueError("outcome must be success|failure|hit|miss")
        self.get(capability_id, version)  # UnknownCapabilityError if absent
        key = (capability_id, version, outcome)
        self._metrics[key] = self._metrics.get(key, 0) + 1
        return self._metrics[key]

    @property
    def _metrics(self) -> Dict[tuple, int]:
        if not hasattr(self, "_metric_counts"):
            self._metric_counts: Dict[tuple, int] = {}
        return self._metric_counts

    def metrics_of(self, capability_id: str, version: str) -> Dict[str, float]:
        """命中率（hit/(hit+miss)）与成功率（success/(success+failure)），无数据返回空。"""
        self.get(capability_id, version)
        m = self._metrics
        out: Dict[str, float] = {}
        hit, miss = m.get((capability_id, version, "hit"), 0), m.get((capability_id, version, "miss"), 0)
        ok, fail = m.get((capability_id, version, "success"), 0), m.get((capability_id, version, "failure"), 0)
        if hit + miss:
            out["hit_rate"] = hit / (hit + miss)
        if ok + fail:
            out["success_rate"] = ok / (ok + fail)
        return out
