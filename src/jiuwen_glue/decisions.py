# coding: utf-8
"""决策记录（append-only）— WO-0003 返工补齐（v1.7 §0 总则 4 / M0 审计发现 1）.

规格来源（M0 独立审计报告发现 1：原交付缺决策记录模块；PROP-0001 §4.4 资产模型 /
§12.4 存储策略——决策记录永久保存）:

- 每次"决策层/治理裁决"落一条 append-only 记录：id、ts、agent_ref（三层复合身份
  引用）、context_hash（上下文摘要哈希）、options、chosen、rationale_ref（理由的证据
  引用）、guardrail_run_ref（绑定的门控运行）、tenant_id。
- **只增不改**：提供 append 与查询；任何 update/delete 接口不存在（Python 层无 API，
  DDL 层有 BEFORE UPDATE/DELETE 拒绝触发器作第二道闸——见 ops/sql/002_glue_v2.sql）。
- 上下文不落原文只落哈希：context 经 canonical JSON + SHA-256 摘要，原文归观测系统
  （12.4 保留策略）；记录里不带密钥/明文敏感值。
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .errors import DecisionSchemaError, UnknownDecisionError


def _utcnow() -> float:
    import time

    return time.time()


def context_hash(context: Mapping[str, Any]) -> str:
    """canonical JSON（排序键、无空白）+ SHA-256 hex。同一上下文 → 同一哈希，可对账。"""
    canonical = json.dumps(context, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DecisionRecord:
    """一条决策记录（对应 DDL glue.decision_record；frozen——落账后不可变）。"""

    decision_id: str
    ts: float
    agent_ref: str                    # 三层复合身份引用（identity.composite_ref）
    context_hash: str                 # 上下文摘要哈希（原文不落本账）
    options: Tuple[str, ...]          # 候选选项空间
    chosen: str                       # 最终选择（必须 ∈ options）
    rationale_ref: str                # 理由的证据引用（evidence/工单/轨迹）
    guardrail_run_ref: Optional[str] = None   # 绑定的 GuardrailRun（门控在先，决策在后）
    tenant_id: str = "t0"
    meta: Mapping[str, Any] = field(default_factory=dict)


class DecisionLog:
    """进程内 append-only 决策账本。

    边界（写死）：本账本只**记录**决策，不做决策——"允不允许"仍由各执行点的
    确定性系统判断（决策点唯一，4.9）；决策层（JevProvider）打分结果经
    promotion.score_hook / 本账本落档，本模块不实现 Provider 本体。
    """

    def __init__(self, *, now: Optional[Callable[[], float]] = None) -> None:
        self._now = now or _utcnow
        self._records: Dict[str, DecisionRecord] = {}
        self._order: List[str] = []

    # ── 追加（唯一写入口）────────────────────────────────────────────────

    def append(self, *, agent_ref: str, context: Mapping[str, Any],
               options: Iterable[str], chosen: str, rationale_ref: str,
               guardrail_run_ref: Optional[str] = None,
               tenant_id: str = "t0",
               meta: Optional[Mapping[str, Any]] = None) -> DecisionRecord:
        """落一条决策记录。chosen 必须在 options 内；agent_ref / rationale_ref 必填。

        任何"修改既有记录"的调用都无法表达——本类没有 update/delete 方法，
        存储用 frozen dataclass + 只追加的 dict。
        """
        if not agent_ref or not isinstance(agent_ref, str):
            raise DecisionSchemaError("agent_ref must be a non-empty identity reference")
        if not rationale_ref or not isinstance(rationale_ref, str):
            raise DecisionSchemaError("rationale_ref must be a non-empty evidence reference")
        opts = tuple(options)
        if not opts:
            raise DecisionSchemaError("options must be a non-empty sequence")
        if chosen not in opts:
            raise DecisionSchemaError(
                f"chosen {chosen!r} must be one of options {list(opts)}")
        rec = DecisionRecord(
            decision_id=uuid.uuid4().hex, ts=self._now(), agent_ref=agent_ref,
            context_hash=context_hash(context), options=opts, chosen=chosen,
            rationale_ref=rationale_ref, guardrail_run_ref=guardrail_run_ref,
            tenant_id=tenant_id or "t0",
            meta=copy.deepcopy(dict(meta or {})))
        self._records[rec.decision_id] = rec
        self._order.append(rec.decision_id)
        return rec

    # ── 查询 ─────────────────────────────────────────────────────────────

    def get(self, decision_id: str) -> DecisionRecord:
        try:
            return self._records[decision_id]
        except KeyError:
            raise UnknownDecisionError(f"unknown decision: {decision_id}") from None

    def all(self) -> List[DecisionRecord]:
        """全部记录（落账顺序；append-only 视图）。"""
        return [self._records[i] for i in self._order]

    def by_agent(self, agent_ref: str) -> List[DecisionRecord]:
        return [r for r in self.all() if r.agent_ref == agent_ref]

    def by_context(self, context: Mapping[str, Any]) -> List[DecisionRecord]:
        """按上下文原文查（经同一哈希函数对账）。"""
        h = context_hash(context)
        return [r for r in self.all() if r.context_hash == h]
