# coding: utf-8
"""记忆晋升管线 — 只做"个人→组织"资产晋升（WO-0003 返工补齐，v1.6 §4.9 #4）.

规格来源（PROP-0001 v1.6 §4.9 #4；v1.7 §0 总则 4）:
- 个人/会话级记忆 = 原生全家桶（core.memory / JiuwenMemory / TTSE）；
- glue 晋升管线**只做"个人→组织"资产晋升**（质量门 + 脱敏门 + 版本化入库），
  **不回写执行面记忆**——本模块没有任何通往原生记忆库的写路径。

阶段机：working → shortlist → promoted（旁支 rejected / withdrawn）
每次转移必须带理由（reason）与证据引用（evidence_ref）。

三道门（shortlist → promoted，全部通过才可晋升；fail-closed）:
1. **质量门**：eval 指标达标——指标复用 capabilities 模块的 append-only 单向回流
   模式（record_eval 只增不改，无手改接口）；样本不足 = UNKNOWN = 拒绝。
2. **脱敏门**：可注入的 redaction 检查函数（返回 PASS/BLOCKED/UNKNOWN）；
   **未注入 checker → UNKNOWN → 拒绝晋升**（fail-closed，写死）。
3. **决策层打分（可选交叉点，方案 §4.7"第六植入点"，唯一交叉点）**：
   ``score_hook`` 由决策层注入，对晋升候选打分；默认 None 时走纯质量门 +
   脱敏门（不打分）。hook 返回 None = UNKNOWN = 拒绝（fail-closed）。

版本化入库：promoted 资产带 (asset_key, version) 单调递增版本号；
tombstone 撤回路径：promoted → withdrawn（墓碑标记，append-only 不物理删除）。
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from .errors import PromotionTransitionError, UnknownPromotionAssetError

STAGE_WORKING = "working"
STAGE_SHORTLIST = "shortlist"
STAGE_PROMOTED = "promoted"
STAGE_REJECTED = "rejected"
STAGE_WITHDRAWN = "withdrawn"      # tombstone（墓碑：撤回但不物理删除）
_LEGAL = {
    (STAGE_WORKING, STAGE_SHORTLIST),
    (STAGE_SHORTLIST, STAGE_PROMOTED),
    (STAGE_WORKING, STAGE_REJECTED),
    (STAGE_SHORTLIST, STAGE_REJECTED),
    (STAGE_PROMOTED, STAGE_WITHDRAWN),   # tombstone 撤回路径
    (STAGE_SHORTLIST, STAGE_WITHDRAWN),  # 候选人主动撤回
}
_TERMINAL = (STAGE_PROMOTED, STAGE_REJECTED, STAGE_WITHDRAWN)

# 门控结论三态（与 GuardrailRun 聚合语义同形）
GATE_PASS = "PASS"
GATE_BLOCKED = "BLOCKED"
GATE_UNKNOWN = "UNKNOWN"

# redaction checker 签名：content → PASS / BLOCKED / UNKNOWN
RedactionChecker = Callable[[Mapping[str, Any]], str]
# score_hook 签名：候选快照 → float 分数；None = UNKNOWN（决策层不置评 → fail-closed）
ScoreHook = Callable[[Mapping[str, Any]], Optional[float]]


def _utcnow() -> float:
    import time

    return time.time()


@dataclass(frozen=True)
class PromotionTransition:
    """一次阶段转移的留痕（含被拒绝的违规尝试）。"""

    promotion_id: str
    asset_key: str
    from_stage: Optional[str]
    to_stage: Optional[str]
    kind: str                        # CREATE / TRANSITION / GATE_REJECT / VIOLATION
    reason: str
    evidence_ref: str = ""
    occurred_at: float = 0.0
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PromotionCandidate:
    """一个晋升候选（个人级经验/技能/提示词包等资产；content 只在 working 期可改）。"""

    promotion_id: str
    asset_key: str                   # 组织资产键（如 skill:video-cut）
    origin_ref: str                  # 来源引用（个人记忆 / TTSE bank / Skill 演进轨的引用）
    content: Dict[str, Any]
    stage: str = STAGE_WORKING
    version: int = 0                 # promoted 时分配的单调版本号（从 1 起）
    tenant_id: str = "t0"
    redaction_verdict: Optional[str] = None
    score: Optional[float] = None    # score_hook 打分（决策层第六植入点）
    created_at: float = 0.0
    promoted_at: Optional[float] = None
    promoted_version_key: Optional[str] = None


class PromotionLedger:
    """进程内晋升台账（Postgres DDL 见 ops/sql/002_glue_v2.sql 的 glue.promotion_record）。

    边界（写死）：本台账只管**资产准入治理**；发现的候选项由原生自演进通道
    （Skill 自演进 / SwarmSkills / TTSE / AutoHarness）产生，本模块不自建经验库、
    不自建发现机制（4.9 #1/#4）、**不回写执行面记忆**。
    """

    def __init__(self, *, redaction_checker: Optional[RedactionChecker] = None,
                 score_hook: Optional[ScoreHook] = None,
                 min_success_rate: float = 0.8, min_eval_samples: int = 3,
                 min_score: float = 0.6,
                 now: Optional[Callable[[], float]] = None) -> None:
        self._now = now or _utcnow
        self._items: Dict[str, PromotionCandidate] = {}
        self.transitions: List[PromotionTransition] = []
        # 组织资产版本登记：asset_key → {version: promotion_id}；单调递增
        self._org_versions: Dict[str, Dict[int, str]] = {}
        self._tombstones: Dict[str, int] = {}   # asset_key → 已撤回的最高版本
        # eval 指标（append-only 单向回流，复用 capabilities 模式）
        self._eval_events: List[Dict[str, Any]] = []
        self.redaction_checker = redaction_checker
        self.score_hook = score_hook
        self.min_success_rate = min_success_rate
        self.min_eval_samples = min_eval_samples
        self.min_score = min_score

    # ── 查询 ─────────────────────────────────────────────────────────────

    def get(self, promotion_id: str) -> PromotionCandidate:
        try:
            return self._items[promotion_id]
        except KeyError:
            raise UnknownPromotionAssetError(
                f"unknown promotion candidate: {promotion_id}") from None

    def history(self, promotion_id: str) -> List[PromotionTransition]:
        self.get(promotion_id)
        return [t for t in self.transitions if t.promotion_id == promotion_id]

    def org_versions_of(self, asset_key: str) -> Dict[int, str]:
        """某资产键已入库的组织版本表（版本号 → promotion_id；不含 tombstone 过滤）。"""
        return dict(self._org_versions.get(asset_key, {}))

    def is_tombstoned(self, asset_key: str, version: int) -> bool:
        """版本是否已被墓碑撤回（组织记忆消费方必须跳过墓碑版本）。"""
        top = self._tombstones.get(asset_key)
        return top is not None and version <= top

    def latest_live_version(self, asset_key: str) -> Optional[int]:
        """最新未被墓碑撤回的组织版本（无则 None）。"""
        versions = self._org_versions.get(asset_key, {})
        top = self._tombstones.get(asset_key, 0)
        live = [v for v in versions if v > top]
        return max(live) if live else None

    # ── 内部留痕 ─────────────────────────────────────────────────────────

    def _record(self, cand: PromotionCandidate, kind: str, *, from_stage: Optional[str],
                to_stage: Optional[str], reason: str, evidence_ref: str = "",
                **detail: Any) -> None:
        self.transitions.append(PromotionTransition(
            promotion_id=cand.promotion_id, asset_key=cand.asset_key,
            from_stage=from_stage, to_stage=to_stage, kind=kind, reason=reason,
            evidence_ref=evidence_ref, occurred_at=self._now(), detail=dict(detail)))

    # ── 生命周期 ─────────────────────────────────────────────────────────

    def nominate(self, asset_key: str, content: Dict[str, Any], *,
                 origin_ref: str = "", tenant_id: str = "t0") -> PromotionCandidate:
        """提名一个晋升候选（working 阶段；内容仅 working 期可改）。"""
        if not asset_key or not isinstance(asset_key, str):
            raise PromotionTransitionError("asset_key must be a non-empty string")
        cand = PromotionCandidate(
            promotion_id=uuid.uuid4().hex, asset_key=asset_key,
            origin_ref=origin_ref, content=copy.deepcopy(content),
            tenant_id=tenant_id or "t0", created_at=self._now())
        self._items[cand.promotion_id] = cand
        self._record(cand, "CREATE", from_stage=None, to_stage=STAGE_WORKING,
                     reason="nominated", origin_ref=origin_ref)
        return cand

    def update_content(self, promotion_id: str, content: Dict[str, Any]) -> PromotionCandidate:
        """内容仅 working 期可编辑（进入 shortlist 后冻结——评审对象不可漂移）。"""
        cand = self.get(promotion_id)
        if cand.stage != STAGE_WORKING:
            self._record(cand, "VIOLATION", from_stage=cand.stage, to_stage=cand.stage,
                         reason="edit rejected: content frozen after working")
            raise PromotionTransitionError(
                f"candidate {promotion_id} is {cand.stage}; only working content is editable")
        cand.content = copy.deepcopy(content)
        return cand

    def transition(self, promotion_id: str, to_stage: str, *, reason: str,
                   evidence_ref: str = "") -> PromotionCandidate:
        """阶段转移唯一入口。非法迁移抛 PromotionTransitionError 并留痕。"""
        cand = self.get(promotion_id)
        if not reason or not str(reason).strip():
            raise PromotionTransitionError("every transition requires a reason")
        if (cand.stage, to_stage) not in _LEGAL:
            self._record(cand, "VIOLATION", from_stage=cand.stage, to_stage=to_stage,
                         reason=f"illegal transition {cand.stage}->{to_stage}")
            raise PromotionTransitionError(
                f"illegal promotion transition {cand.stage} -> {to_stage}")
        if to_stage == STAGE_PROMOTED:
            return self._promote(cand, reason=reason, evidence_ref=evidence_ref)
        frm = cand.stage
        cand.stage = to_stage
        self._record(cand, "TRANSITION", from_stage=frm, to_stage=to_stage,
                     reason=reason, evidence_ref=evidence_ref)
        return cand

    # ── 三道门（shortlist → promoted）────────────────────────────────────

    def _promote(self, cand: PromotionCandidate, *, reason: str,
                 evidence_ref: str) -> PromotionCandidate:
        """晋升 = 质量门 + 脱敏门（+ 可选决策层打分）全部通过；任一不过 → rejected。"""

        def _reject(gate: str, verdict: str, detail: Dict[str, Any]) -> None:
            cand.stage = STAGE_REJECTED
            self._record(cand, "GATE_REJECT", from_stage=STAGE_SHORTLIST,
                         to_stage=STAGE_REJECTED,
                         reason=f"{gate} gate {verdict}: promoted denied (fail-closed)",
                         evidence_ref=evidence_ref, **detail)

        # 门 1：质量门（eval 指标单向回流；样本不足 = UNKNOWN = 拒绝）
        metrics = self.eval_metrics_of(cand.asset_key)
        samples = int(metrics.get("samples", 0))
        rate = metrics.get("success_rate")
        if samples < self.min_eval_samples or rate is None:
            _reject("quality", GATE_UNKNOWN,
                    {"samples": samples, "min_eval_samples": self.min_eval_samples})
            return cand
        if float(rate) < self.min_success_rate:
            _reject("quality", GATE_BLOCKED,
                    {"success_rate": rate, "min_success_rate": self.min_success_rate})
            return cand

        # 门 2：脱敏门（未注入 checker = UNKNOWN = 拒绝；fail-closed 写死）
        if self.redaction_checker is None:
            _reject("redaction", GATE_UNKNOWN,
                    {"why": "no redaction checker injected — promotion denied by default"})
            return cand
        red = self.redaction_checker(cand.content)
        if red not in (GATE_PASS, GATE_BLOCKED, GATE_UNKNOWN):
            _reject("redaction", GATE_UNKNOWN,
                    {"why": f"checker returned non-verdict {red!r}"})
            return cand
        if red != GATE_PASS:
            _reject("redaction", red, {})
            return cand
        cand.redaction_verdict = red

        # 门 3（可选交叉点）：决策层打分（§4.7 第六植入点，唯一交叉点）
        if self.score_hook is not None:
            try:
                score = self.score_hook(self._snapshot(cand))
            except Exception as exc:                      # noqa: BLE001 — hook 故障必须 fail-closed
                _reject("score", GATE_UNKNOWN, {"why": f"score_hook raised: {exc!r}"})
                return cand
            if score is None:
                _reject("score", GATE_UNKNOWN,
                        {"why": "score_hook returned None (decision layer abstains)"})
                return cand
            score = float(score)
            if score < self.min_score:
                _reject("score", GATE_BLOCKED,
                        {"score": score, "min_score": self.min_score})
                return cand
            cand.score = score

        # 版本化入库：单调版本号
        frm = cand.stage
        versions = self._org_versions.setdefault(cand.asset_key, {})
        cand.version = (max(versions) + 1) if versions else 1
        cand.stage = STAGE_PROMOTED
        cand.promoted_at = self._now()
        cand.promoted_version_key = f"{cand.asset_key}@v{cand.version}"
        versions[cand.version] = cand.promotion_id
        self._record(cand, "TRANSITION", from_stage=frm, to_stage=STAGE_PROMOTED,
                     reason=reason, evidence_ref=evidence_ref,
                     version=cand.version, success_rate=rate,
                     redaction=cand.redaction_verdict, score=cand.score)
        return cand

    def _snapshot(self, cand: PromotionCandidate) -> Dict[str, Any]:
        """给 score_hook 的候选快照（不含原始 content 引用，防 hook 改写评审对象）。"""
        return {"promotion_id": cand.promotion_id, "asset_key": cand.asset_key,
                "origin_ref": cand.origin_ref,
                "content": copy.deepcopy(cand.content),
                "tenant_id": cand.tenant_id}

    # ── tombstone 撤回路径 ───────────────────────────────────────────────

    def tombstone(self, promotion_id: str, *, reason: str) -> PromotionCandidate:
        """撤回已晋升版本（墓碑标记；append-only，不物理删除、版本号不复用）。"""
        cand = self.get(promotion_id)
        if cand.stage != STAGE_PROMOTED:
            self._record(cand, "VIOLATION", from_stage=cand.stage, to_stage=STAGE_WITHDRAWN,
                         reason=f"tombstone rejected: candidate is {cand.stage}")
            raise PromotionTransitionError(
                f"only PROMOTED assets can be tombstoned, candidate {promotion_id} "
                f"is {cand.stage}")
        cand.stage = STAGE_WITHDRAWN
        self._tombstones[cand.asset_key] = max(
            self._tombstones.get(cand.asset_key, 0), cand.version)
        self._record(cand, "TRANSITION", from_stage=STAGE_PROMOTED,
                     to_stage=STAGE_WITHDRAWN, reason=reason,
                     version=cand.version)
        return cand

    # ── eval 指标：单向回流，只增不改（复用 capabilities 模式）────────────

    def record_eval(self, asset_key: str, outcome: str, *, run_ref: str = "") -> int:
        """记录一次资产 eval 结果（success/failure）。指标唯一入口——无任何手改接口。"""
        if outcome not in ("success", "failure"):
            raise ValueError("outcome must be success|failure")
        self._eval_events.append({"asset_key": asset_key, "outcome": outcome,
                                  "run_ref": run_ref, "at": self._now()})
        return sum(1 for e in self._eval_events if e["asset_key"] == asset_key)

    def eval_metrics_of(self, asset_key: str) -> Dict[str, float]:
        """成功率（success/(success+failure)）与样本数；无数据返回 samples=0。"""
        events = [e for e in self._eval_events if e["asset_key"] == asset_key]
        ok = sum(1 for e in events if e["outcome"] == "success")
        fail = sum(1 for e in events if e["outcome"] == "failure")
        out: Dict[str, float] = {"samples": float(ok + fail)}
        if ok + fail:
            out["success_rate"] = ok / (ok + fail)
        return out
