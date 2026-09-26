# coding: utf-8
"""自取制 worker 客户端协议（模拟侧）— WO-0011 / PROP-0003 §12.6.

规格来源（v1.7 §12.6 自取制 + 执行面零长期密钥红线）:

- **WorkerLoop.tick()**：轮询（claim）→ 本地执行回调 → 回报（完成/失败）。
  真实网络不在此实现——接口 + 内存队列演示（WorkQueue 在 scheduler.py）。
- **执行面零长期密钥在协议层的体现**：worker 可见的密钥范围声明字段
  ``lease_scoped_secrets`` **只允许 scheme 限定的引用**
  （``bao://kv/data/company/gpu/<name>`` 形状；复用 scheduler.secret_ref_ok），
  裸令牌值 → SecretScopeError（构造即拒绝）。真实密钥由执行机按引用经
  OpenBao 短时租约自取（M0 审计 B4 execution-worker policy：只读
  ``kv/data/company/gpu/*``、≤1h 短时令牌——policy 本身在 bao 侧，本层只锁形状）。
- 产物只回**引用**（artifact_ref，4.9 #10）；执行回调抛异常 → 工单退回
  PENDING（自取制重试语义），异常类型名进 TickResult.error（不带消息，防泄漏）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from ..rules import PENDING
from .errors import SecretScopeError
from .scheduler import (
    Assignment,
    Rejected,
    SelfPickMode,
    TaskOffering,
    WorkQueue,
    secret_ref_ok,
)

__all__ = ["WorkerContext", "TickResult", "WorkerLoop"]

Executor = Callable[["WorkerContext", TaskOffering], str]


@dataclass(frozen=True)
class WorkerContext:
    """单次执行的可见范围：任务/租约/密钥全部是**引用**，不是值。

    ``lease_scoped_secrets`` 是本次派生租约范围内可自取的密钥引用集合
    （引用 ≠ 密钥值）；worker 不持长期凭证，范围随租约生灭。
    """

    node_id: str
    order_id: str
    task_ref: str
    pipeline_label: str
    assignment_id: str
    lease_ref: Optional[str]
    lease_expires_at: Optional[float]
    lease_scoped_secrets: Tuple[str, ...] = ()
    effective_perms: tuple = ()          # 派生租约固化的权限交集快照
    sandbox_only: bool = False
    review_required: bool = False

    def __post_init__(self) -> None:
        for ref in self.lease_scoped_secrets:
            if not secret_ref_ok(ref):
                raise SecretScopeError(
                    f"lease_scoped_secrets entries must be scheme-qualified "
                    f"references (like 'bao://kv/data/company/gpu/token'), "
                    f"got {ref!r} — workers never hold raw credentials")


@dataclass(frozen=True)
class TickResult:
    """一次 tick 的结果：idle（无可认领）/ executed（完成回报）/ failed（退回）。"""

    action: str                          # idle | executed | failed
    order_id: Optional[str] = None
    assignment_id: Optional[str] = None
    artifact_ref: Optional[str] = None
    error: Optional[str] = None          # 异常类型名（不带消息，防泄漏）


class WorkerLoop:
    """自取制客户端主循环（模拟侧）：tick = 轮询 → 认领 → 本地执行 → 回报。

    ``executor`` 是节点本地的执行回调（真实部署 = jiuwenswarm 实例执行；
    本层只定义协议，不碰实例内部——4.9 #13）。返回值必须是产物**引用**
    （非空字符串），worker 不搬运产物本体。
    """

    def __init__(self, node_id: str, mode: SelfPickMode, queue: WorkQueue,
                 executor: Executor) -> None:
        self.node_id = node_id
        self._mode = mode
        self._queue = queue
        self._executor = executor

    def tick(self, *, now: Optional[float] = None) -> TickResult:
        result = self._mode.claim(self.node_id, self._queue, now=now)
        if isinstance(result, Rejected):
            return TickResult(action="idle", error=result.code)
        order = self._queue.get(result.order_id)
        context = WorkerContext(
            node_id=self.node_id,
            order_id=result.order_id,
            task_ref=result.offering.task_ref,
            pipeline_label=result.offering.pipeline_label,
            assignment_id=result.assignment_id,
            lease_ref=result.lease_ref,
            lease_expires_at=result.lease_expires_at,
            lease_scoped_secrets=order.secret_refs,
            effective_perms=result.effective_perms,
            sandbox_only=result.sandbox_only,
            review_required=result.review_required,
        )
        try:
            artifact_ref = self._executor(context, result.offering)
        except Exception as exc:                      # noqa: BLE001 — 协议层只透类型名
            self._mode.fail(result.assignment_id, self._queue,
                            reason=f"executor-error:{type(exc).__name__}")
            return TickResult(action="failed", order_id=result.order_id,
                              assignment_id=result.assignment_id,
                              error=type(exc).__name__)
        if not isinstance(artifact_ref, str) or not artifact_ref:
            self._mode.fail(result.assignment_id, self._queue,
                            reason="executor-returned-no-artifact-ref")
            return TickResult(action="failed", order_id=result.order_id,
                              assignment_id=result.assignment_id,
                              error="ExecutorContractError")
        assignment = self._mode.complete(result.assignment_id, self._queue,
                                         artifact_ref=artifact_ref)
        return TickResult(action="executed", order_id=order.order_id,
                          assignment_id=assignment.assignment_id,
                          artifact_ref=artifact_ref)

    def run(self, *, ticks: int = 1, now: Optional[float] = None) -> List[TickResult]:
        """连续 tick 的便利方法（自取制轮询节奏的模拟）。"""
        return [self.tick(now=now) for _ in range(max(0, ticks))]
