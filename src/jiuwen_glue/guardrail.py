# coding: utf-8
"""GuardrailRun — 门控协议聚合薄层（WO-0003 返工补齐，v1.7 §0 总则 3 / §4.1）.

规格来源（AI Native 研发手册 §3.3.2"Guardrail 工作原理"五步链路，逐字见
docs/native-handbook-boundary-check.md；openJiuwen 原生 core.security.guardrail =
检查执行本体，本模块**只做协议聚合**）:

1. 发布系统创建或复用运行并提交动作上下文   → ``GuardrailRunStore.create_run(spec)``
2. Guardrail 固化规则与上下文               → spec 在创建时冻结（deepcopy，不可再改）
3. Agent 查询事实并提交判断与 Evidence      → ``submit_check(...)``（逐 check 结论 + 证据引用）
4. Guardrail 验收协议并固化结果             → ``finalize(run_id, seal_ref=...)``
5. 发布系统执行前主动查询门控结果、终检并执行 → ``gate(run_id)``（fail-closed）

聚合语义（唯一门控输出）:
- 任一 check BLOCKED → **BLOCKED**；
- 否则任一 check UNKNOWN（含"必填 check 未提交结论"= 信息不足）→ **UNKNOWN**；
- 否则 **PASS**。
- **fail-closed（写死）**：门控查询方对 UNKNOWN 必须拒绝执行——信息不足或现场已变化
  时系统拒绝自动执行（手册 §3.3.2）。``GuardrailResult.executable`` 只在 PASS 为 True；
  任何调用方不得把 UNKNOWN 当作放行。

决策点唯一（PROP-0001 v1.6 §4.9，写死）:
**本模块不新增第二个决策点。** 聚合 verdict（PASS/BLOCKED/UNKNOWN）是唯一门控输出；
每个 check 的"允不允许"判断由其声明的执行点完成——native_guardrail=原生
core.security.guardrail、permission_rail=原生 TeamPermissionRail（allow/ask/deny）、
eval_gate=公司级门禁 eval-gate、scan=安全扫描三档（v1.7 §12.9）。本模块只存声明、
聚合结果，不执行任何检查。

任何关键状态变化使原有结论失效：``void()`` 将 run 置 VOID，此后 gate 恒 UNKNOWN
（"任何关键状态发生变化，原有结论都应失效并重新检查"）。
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .challenge import ChallengeBoard, Challenge
from .errors import (
    GuardrailSchemaError,
    GuardrailStateError,
    UnknownGuardrailRunError,
)

# check 后端档位（只存声明；执行在原生与各执行点）
BACKEND_NATIVE_GUARDRAIL = "native_guardrail"   # 原生 core.security.guardrail（检查执行）
BACKEND_PERMISSION_RAIL = "permission_rail"     # 原生 TeamPermissionRail（授权三态 allow/ask/deny）
BACKEND_EVAL_GATE = "eval_gate"                 # eval-gate 公司级门禁
BACKEND_SCAN = "scan"                           # 安全扫描三档：静态/轻扫/深扫（v1.7 §12.9）
BACKENDS = (BACKEND_NATIVE_GUARDRAIL, BACKEND_PERMISSION_RAIL,
            BACKEND_EVAL_GATE, BACKEND_SCAN)

# 聚合三态（唯一门控输出）
VERDICT_PASS = "PASS"
VERDICT_BLOCKED = "BLOCKED"
VERDICT_UNKNOWN = "UNKNOWN"

# 单个 check 可提交的结论（ASK=授权三态第三态，来自 permission_rail 的 ask 语义）
OUTCOME_PASS = "PASS"
OUTCOME_BLOCKED = "BLOCKED"
OUTCOME_UNKNOWN = "UNKNOWN"
OUTCOME_ASK = "ASK"
_OUTCOMES = (OUTCOME_PASS, OUTCOME_BLOCKED, OUTCOME_UNKNOWN, OUTCOME_ASK)

# run 状态机
RUN_OPEN = "OPEN"          # 已固化 spec，接受 check 结论提交
RUN_FINALIZED = "FINALIZED"  # 验收固化，只读；gate 按聚合语义给出结果
RUN_VOID = "VOID"          # 现场已变化，原有结论失效；gate 恒 UNKNOWN
_TERMINAL_STATES = (RUN_FINALIZED, RUN_VOID)


def aggregate(verdicts: Iterable[str]) -> str:
    """聚合语义（纯函数）：任一 BLOCKED → BLOCKED；否则任一 UNKNOWN → UNKNOWN；否则 PASS。"""
    vs = list(verdicts)
    if any(v == VERDICT_BLOCKED for v in vs):
        return VERDICT_BLOCKED
    if any(v == VERDICT_UNKNOWN for v in vs):
        return VERDICT_UNKNOWN
    return VERDICT_PASS


def _utcnow() -> float:
    import time

    return time.time()


@dataclass(frozen=True)
class CheckSpec:
    """单个 check 的声明（只声明后端与参数，不执行——执行属原生与各执行点）。"""

    check_id: str
    backend: str                       # BACKENDS 之一
    declaration: Mapping[str, Any] = field(default_factory=dict)  # 机读声明（如扫描档位/规则集）
    required: bool = True              # 必填 check 未提交结论 → 聚合 UNKNOWN（fail-closed）

    def __post_init__(self) -> None:
        if not self.check_id or not isinstance(self.check_id, str):
            raise GuardrailSchemaError("check_id must be a non-empty string")
        if self.backend not in BACKENDS:
            raise GuardrailSchemaError(
                f"backend must be one of {BACKENDS}, got {self.backend!r}")


@dataclass(frozen=True)
class GuardrailSpec:
    """动作上下文 + check 声明表（创建 run 时冻结 = 手册步骤 2"固化规则与上下文"）。"""

    action: str                        # 什么动作（如 release.resume / deploy.config）
    resource: str                      # 什么资源（发布批次 / 配置项 / 产物）
    agent_identity_ref: str            # 哪个 Agent（三层复合身份引用）
    env_ref: str = ""                  # 环境引用（12.2 环境定义版本）
    tenant_id: str = "t0"
    checks: Tuple[CheckSpec, ...] = ()
    spec_version: str = "1"            # 固化规则版本（准入记录绑定 Spec 版本，v1.6 WO-0007）

    def __post_init__(self) -> None:
        if not self.action or not isinstance(self.action, str):
            raise GuardrailSchemaError("action must be a non-empty string")
        if not self.resource or not isinstance(self.resource, str):
            raise GuardrailSchemaError("resource must be a non-empty string")
        if not self.agent_identity_ref or not isinstance(self.agent_identity_ref, str):
            raise GuardrailSchemaError(
                "agent_identity_ref must be a non-empty three-layer identity reference")
        ids = [c.check_id for c in self.checks]
        if len(ids) != len(set(ids)):
            raise GuardrailSchemaError(f"duplicate check_id in spec: {ids}")


@dataclass(frozen=True)
class CheckResult:
    """单个 check 的固化结论（步骤 3 提交 → 步骤 4 验收固化）。"""

    check_id: str
    verdict: str                       # PASS / BLOCKED / UNKNOWN（ASK 提交时落为 UNKNOWN + challenge_id）
    evidence_ref: str = ""             # Evidence 引用（向数据与观测系统查询返回的原始快照）
    challenge_id: Optional[str] = None # ASK 时生成的 Challenge（授权三态第三态）
    note: str = ""
    submitted_at: float = 0.0


@dataclass(frozen=True)
class GuardrailResult:
    """聚合门控结果（步骤 5 执行前查询的输出；verdict 是唯一门控输出）。"""

    run_id: str
    verdict: str                       # PASS / BLOCKED / UNKNOWN
    state: str                         # run 当前状态
    per_check: Mapping[str, str] = field(default_factory=dict)   # check_id → verdict
    missing_required: Tuple[str, ...] = ()                        # 未提交结论的必填 check
    checked_at: float = 0.0

    @property
    def executable(self) -> bool:
        """fail-closed：只有 PASS 可执行；UNKNOWN（信息不足/现场已变化）必须拒绝。"""
        return self.verdict == VERDICT_PASS


@dataclass
class GuardrailRun:
    """一次门控运行（对应 DDL glue.guardrail_run + glue.guardrail_check_result）。"""

    run_id: str
    spec: GuardrailSpec                # 创建时已冻结
    state: str = RUN_OPEN
    results: Dict[str, CheckResult] = field(default_factory=dict)
    created_at: float = 0.0
    finalized_at: Optional[float] = None
    seal_ref: Optional[str] = None
    void_reason: Optional[str] = None
    tenant_id: str = "t0"


class GuardrailRunStore:
    """进程内 GuardrailRun 台账。

    边界（写死）：本 store 只做**协议聚合**——固化上下文、登记结论、聚合 verdict；
    检查的**执行**一律在原生 core.security.guardrail 与各执行点
    （TeamPermissionRail / eval-gate / OPA / 扫描器）。本模块不新增第二个决策点。
    """

    def __init__(self, *, challenge_board: Optional[ChallengeBoard] = None,
                 now: Optional[Callable[[], float]] = None) -> None:
        self._now = now or _utcnow
        self._runs: Dict[str, GuardrailRun] = {}
        self.challenges = challenge_board  # ASK 语义的落点（授权三态第三态）

    # ── 查询 ─────────────────────────────────────────────────────────────

    def get(self, run_id: str) -> GuardrailRun:
        try:
            return self._runs[run_id]
        except KeyError:
            raise UnknownGuardrailRunError(f"unknown guardrail run: {run_id}") from None

    # ── 步骤 1+2：创建并固化 ─────────────────────────────────────────────

    def create_run(self, spec: GuardrailSpec) -> GuardrailRun:
        """提交动作上下文并固化规则与上下文（spec 深拷贝冻结，之后不可改）。"""
        if not isinstance(spec, GuardrailSpec):
            raise GuardrailSchemaError("spec must be a GuardrailSpec")
        run = GuardrailRun(run_id=uuid.uuid4().hex, spec=copy.deepcopy(spec),
                           created_at=self._now(), tenant_id=spec.tenant_id)
        self._runs[run.run_id] = run
        return run

    # ── 步骤 3：提交判断与 Evidence ──────────────────────────────────────

    def submit_check(self, run_id: str, check_id: str, outcome: str, *,
                     evidence_ref: str = "", note: str = "") -> GuardrailRun:
        """Agent 查询事实后提交单个 check 的判断与 Evidence 引用。

        - outcome=PASS/BLOCKED/UNKNOWN：直接登记；
        - outcome=ASK：**只有 permission_rail 型 check 可提交**（授权三态第三态）。
          经 ChallengeBoard 生成结构化 Challenge，check 结论落为 UNKNOWN——
          在 Challenge 被相应确认人批准前，门控保持 fail-closed；
          批准后调用方以 outcome=PASS + evidence_ref 注明 challenge 引用重新提交。
        - run 已 FINALIZED/VOID 后再提交 → GuardrailStateError（固化结果不可改）。
        """
        run = self.get(run_id)
        if run.state != RUN_OPEN:
            raise GuardrailStateError(
                f"guardrail run {run_id} is {run.state}; results are frozen "
                "(re-open via a new run — any state change invalidates old conclusions)")
        spec_check = next((c for c in run.spec.checks if c.check_id == check_id), None)
        if spec_check is None:
            raise GuardrailSchemaError(
                f"check {check_id!r} is not declared in run {run_id}'s spec")
        if outcome not in _OUTCOMES:
            raise GuardrailSchemaError(
                f"outcome must be one of {_OUTCOMES}, got {outcome!r}")

        challenge: Optional[Challenge] = None
        verdict = outcome
        if outcome == OUTCOME_ASK:
            if spec_check.backend != BACKEND_PERMISSION_RAIL:
                raise GuardrailSchemaError(
                    f"ASK outcome is only valid for backend "
                    f"{BACKEND_PERMISSION_RAIL}, check {check_id} is {spec_check.backend}")
            if self.challenges is None:
                raise GuardrailStateError(
                    "store was built without a ChallengeBoard; ASK cannot be routed")
            decl = spec_check.declaration or {}
            challenge = self.challenges.open(
                who_confirms=decl.get("who_confirms", "user"),
                resource=run.spec.resource, action=run.spec.action,
                method=decl.get("ask_method", "console.ask"),
                ttl_seconds=decl.get("ask_ttl_seconds", 3600.0),
                agent_identity_ref=run.spec.agent_identity_ref,
                guardrail_run_ref=run_id, tenant_id=run.spec.tenant_id,
                meta={"check_id": check_id})
            verdict = VERDICT_UNKNOWN  # 未决的 ask 在门控上就是 UNKNOWN（fail-closed）

        run.results[check_id] = CheckResult(
            check_id=check_id, verdict=verdict, evidence_ref=evidence_ref,
            challenge_id=challenge.challenge_id if challenge else None,
            note=note, submitted_at=self._now())
        return run

    # ── 步骤 4：验收固化 ─────────────────────────────────────────────────

    def finalize(self, run_id: str, *, seal_ref: str) -> GuardrailRun:
        """验收协议并固化结果（步骤 4）。固化后 check 结论只读；重开必须建新 run。"""
        run = self.get(run_id)
        if run.state != RUN_OPEN:
            raise GuardrailStateError(
                f"guardrail run {run_id} is {run.state}; finalize requires OPEN")
        if not seal_ref:
            raise GuardrailStateError("finalize requires a seal_ref")
        run.state = RUN_FINALIZED
        run.finalized_at = self._now()
        run.seal_ref = seal_ref
        return run

    # ── 现场变化：原有结论失效 ───────────────────────────────────────────

    def void(self, run_id: str, *, reason: str) -> GuardrailRun:
        """关键状态变化（生产现场已变化）→ 原有结论失效；此后 gate 恒 UNKNOWN。"""
        run = self.get(run_id)
        if run.state == RUN_VOID:
            return run
        run.state = RUN_VOID
        run.void_reason = reason or None
        return run

    # ── 步骤 5：执行前门控查询（唯一门控输出）────────────────────────────

    def gate(self, run_id: str) -> GuardrailResult:
        """执行前主动查询门控结果（终检）。

        - OPEN：未验收固化 = 信息不足 → UNKNOWN（fail-closed）；
        - VOID：现场已变化，原有结论失效 → UNKNOWN（fail-closed）；
        - FINALIZED：聚合语义——任一 BLOCKED→BLOCKED；否则任一 UNKNOWN 或
          必填 check 未提交→UNKNOWN；否则 PASS。
        **查询方对 UNKNOWN 必须拒绝执行。**
        """
        run = self.get(run_id)
        now = self._now()
        if run.state != RUN_FINALIZED:
            reason = "run not finalized (insufficient protocol)" if run.state == RUN_OPEN \
                else f"run void: {run.void_reason or 'state changed'}"
            return GuardrailResult(run_id=run_id, verdict=VERDICT_UNKNOWN, state=run.state,
                                   per_check={k: r.verdict for k, r in run.results.items()},
                                   missing_required=self._missing_required(run),
                                   checked_at=now)
        per_check: Dict[str, str] = {k: r.verdict for k, r in run.results.items()}
        missing = self._missing_required(run)
        verdict = aggregate(list(per_check.values())) if not missing else VERDICT_UNKNOWN
        return GuardrailResult(run_id=run_id, verdict=verdict, state=run.state,
                               per_check=per_check, missing_required=tuple(missing),
                               checked_at=now)

    @staticmethod
    def _missing_required(run: GuardrailRun) -> Tuple[str, ...]:
        return tuple(c.check_id for c in run.spec.checks
                     if c.required and c.check_id not in run.results)

    # ── 便捷 ─────────────────────────────────────────────────────────────

    def pending_challenges(self, run_id: str) -> List[Challenge]:
        """该 run 发出且仍 pending 的 Challenge（治理面 ask 审批队列联动）。"""
        run = self.get(run_id)
        if self.challenges is None:
            return []
        out: List[Challenge] = []
        for r in run.results.values():
            if r.challenge_id:
                ch = self.challenges.get(r.challenge_id)
                if self.challenges.state_of(ch.challenge_id) == "pending":
                    out.append(ch)
        return out
