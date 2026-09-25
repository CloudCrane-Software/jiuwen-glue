# coding: utf-8
"""协同三铁律的执行与检测 — jiuwen-glue 自研（Task 台账 + 消息回执 + 拆分校验）.

规格来源（PROP-0001 v1.6 第 4.3 节；规格母本为 Handbook 第 11 章 + openJiuwen
agent_teams specs，见选型报告 §7.1）:

1. **消息可以触发任务或补充信息，但发送成功不代表任务已被承接**；
2. **不能把对话历史当作 Team State**——协作事实由 Task State 与 Artifact 维护；
3. **只有存在独立交付、不同责任或明确依赖时才拆子任务**，
   同一成员连续完成的内部步骤不建任务。

实现方式（违规必被检测）:
- 任务状态只存在于 TaskLedger；唯一合法的承接路径是 claim(executor)。
  post_message() 返回的 MessageReceipt 是独立对象，**不携带任何任务状态变更能力**；
  试图以 message / chat_history 作为状态来源调用 transition() 会被拒绝并记入
  violation_log（铁律 1 / 铁律 2）。
- 完成任务必须携带 TaskRun 引用与 Artifact 引用（铁律 2）。
- split_task() 校验每个子任务：必须声明独立交付物，且必须满足
  （owner 不同于父任务 = 不同责任）或（声明 depends_on = 明确依赖）；
  同一成员的连续内部步骤被拒绝（铁律 3）。
- 每条违规 = 抛出 IronRuleViolation 子类 + 追加 violation_log（检测 = 拒绝 + 留痕）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .errors import (
    IronRuleViolation,
    MissingTaskReferenceError,
    Rule1MessageIsNotClaim,
    Rule2ConversationIsNotState,
    Rule3InternalStepIsNotTask,
    UnknownTaskError,
)

PENDING = "PENDING"
CLAIMED = "CLAIMED"
COMPLETED = "COMPLETED"
BLOCKED = "BLOCKED"
CANCELLED = "CANCELLED"

# openjiuwen agent_teams 任务状态机的最小化子集（PENDING→CLAIMED→COMPLETED，旁支 BLOCKED→PENDING）
_LEGAL_TRANSITIONS = {
    (PENDING, CLAIMED),
    (CLAIMED, COMPLETED),
    (CLAIMED, BLOCKED),
    (BLOCKED, PENDING),
    (PENDING, CANCELLED),
    (CLAIMED, CANCELLED),
    (BLOCKED, CANCELLED),
}

# 状态变更的合法来源：claim=显式承接，run=TaskRun 结果，admin=治理操作。
# 任何"消息/对话历史"来源都不是任务事实（铁律 1/2 在此把关）。
_LEGAL_SOURCES = ("claim", "run", "admin")
_FORBIDDEN_SOURCES = {
    "message": Rule1MessageIsNotClaim,
    "chat": Rule2ConversationIsNotState,
    "chat_history": Rule2ConversationIsNotState,
    "conversation": Rule2ConversationIsNotState,
}


@dataclass(frozen=True)
class ViolationRecord:
    rule_no: int
    code: str
    occurred_at: float
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MessageReceipt:
    """消息回执：只证明"消息已送达"，不承载任何任务状态语义（铁律 1）。"""

    receipt_id: str
    task_ref: str
    sender: str
    delivered_at: float
    text: str


@dataclass
class Task:
    task_id: str
    title: str
    owner: Optional[str] = None
    deliverable: Optional[str] = None
    state: str = PENDING
    parent_task_id: Optional[str] = None
    run_ref: Optional[str] = None        # 完成时的 TaskRun 引用（协作事实承载）
    artifact_ref: Optional[str] = None   # 完成时的 Artifact 引用


class TaskLedger:
    """任务台账 = Team State 的唯一承载（Postgres DDL: glue.team_task / glue.task_transition）。"""

    def __init__(self, now: Optional[Any] = None) -> None:
        if now is None:
            import time

            now = time.time
        self._now = now
        self._tasks: Dict[str, Task] = {}
        self.transitions: List[Dict[str, Any]] = []
        self.violation_log: List[ViolationRecord] = []

    # ── 查询 ─────────────────────────────────────────────────────────────

    def get(self, task_id: str) -> Task:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise UnknownTaskError(f"unknown task: {task_id}") from None

    def state_of(self, task_id: str) -> str:
        return self.get(task_id).state

    def _record_violation(self, exc: IronRuleViolation, **detail: Any) -> IronRuleViolation:
        self.violation_log.append(ViolationRecord(
            rule_no=exc.rule_no, code=exc.code, occurred_at=self._now(),
            detail={**detail, "error": str(exc)}))
        return exc

    # ── 创建 / 消息 ──────────────────────────────────────────────────────

    def create(self, title: str, *, owner: Optional[str] = None,
               deliverable: Optional[str] = None,
               parent_task_id: Optional[str] = None) -> Task:
        task = Task(task_id=uuid.uuid4().hex, title=title, owner=owner,
                    deliverable=deliverable, parent_task_id=parent_task_id)
        self._tasks[task.task_id] = task
        self.transitions.append({"task_id": task.task_id, "from": None, "to": PENDING,
                                 "source": "admin", "at": self._now(), "by": owner})
        return task

    def post_message(self, task_ref: str, sender: str, text: str) -> MessageReceipt:
        """投递一条与任务相关的消息。返回回执——发送成功 ≠ 任务被承接（铁律 1）。

        本方法不触碰任务状态：没有任何从这里通往 claim/complete 的代码路径。"""
        return MessageReceipt(receipt_id=uuid.uuid4().hex, task_ref=task_ref,
                              sender=sender, delivered_at=self._now(), text=text)

    # ── 状态变更（唯一入口，来源受检） ────────────────────────────────────

    def transition(self, task_id: str, to_state: str, *, source: str,
                   executor: Optional[str] = None,
                   run_ref: Optional[str] = None,
                   artifact_ref: Optional[str] = None,
                   note: Optional[str] = None) -> Task:
        """状态变更唯一入口。

        - source 必须是 claim/run/admin；message/chat_history 等来源一律拒绝（铁律 1/2）；
        - PENDING→CLAIMED 必须显式给出 executor（承接是有主行为）；
        - →COMPLETED 必须携带 run_ref 与 artifact_ref（协作事实由 Task State 与 Artifact
          维护，不由对话历史维护——铁律 2）。
        """
        task = self.get(task_id)
        if source in _FORBIDDEN_SOURCES:
            exc_cls = _FORBIDDEN_SOURCES[source]
            exc = exc_cls(
                f"task {task_id} state change {task.state}->{to_state} rejected: "
                f"source={source!r} is not a task fact (rule {exc_cls.rule_no})")
            raise self._record_violation(exc, task_id=task_id, to_state=to_state, source=source)
        if source not in _LEGAL_SOURCES:
            exc = MissingTaskReferenceError(f"unknown transition source: {source!r}")
            self.violation_log.append(ViolationRecord(
                rule_no=0, code="UNKNOWN_SOURCE", occurred_at=self._now(),
                detail={"task_id": task_id, "source": source}))
            raise exc
        if (task.state, to_state) not in _LEGAL_TRANSITIONS:
            exc = IronRuleViolation(
                f"illegal task transition {task.state}->{to_state} (not in state machine)")
            self.violation_log.append(ViolationRecord(
                rule_no=0, code="ILLEGAL_TRANSITION", occurred_at=self._now(),
                detail={"task_id": task_id, "from": task.state, "to": to_state}))
            raise exc
        if to_state == CLAIMED and not executor:
            exc = IronRuleViolation("claim requires an explicit executor "
                                    "(message delivery is not acceptance)")
            self.violation_log.append(ViolationRecord(
                rule_no=1, code="CLAIM_WITHOUT_EXECUTOR", occurred_at=self._now(),
                detail={"task_id": task_id}))
            raise exc
        if to_state == COMPLETED and not (run_ref and artifact_ref):
            exc = MissingTaskReferenceError(
                f"completing task {task_id} requires run_ref and artifact_ref "
                "(conversation history is not Team State)")
            self.violation_log.append(ViolationRecord(
                rule_no=2, code="COMPLETE_WITHOUT_REFERENCES", occurred_at=self._now(),
                detail={"task_id": task_id, "run_ref": run_ref, "artifact_ref": artifact_ref}))
            raise exc
        frm = task.state
        task.state = to_state
        if to_state == CLAIMED:
            task.owner = executor
        if to_state == COMPLETED:
            task.run_ref = run_ref
            task.artifact_ref = artifact_ref
        self.transitions.append({"task_id": task_id, "from": frm, "to": to_state,
                                 "source": source, "at": self._now(),
                                 "by": executor, "note": note})
        return task

    # ── 铁律 3：拆分校验 ──────────────────────────────────────────────────

    def split_task(self, parent_task_id: str,
                   subtasks: Sequence["SubtaskSpec"]) -> List[Task]:
        """拆分父任务。违规（内部步骤拆分 / 缺独立交付物）被拒绝并留痕（铁律 3）。

        判定：每个子任务必须声明独立交付物；且整个拆分必须表现出
        （不同责任——至少一个子任务 owner 不同于父任务 owner）
        或（明确依赖——至少一个子任务声明 depends_on）；
        否则即"同一成员连续完成的内部步骤"，不得成为任务。
        """
        parent = self.get(parent_task_id)
        if len(subtasks) < 2:
            exc = Rule3InternalStepIsNotTask("a split must produce >= 2 subtasks")
            raise self._record_violation(exc, parent_task_id=parent_task_id,
                                         n=len(subtasks))
        for st in subtasks:
            if not st.deliverable or not str(st.deliverable).strip():
                exc = Rule3InternalStepIsNotTask(
                    f"subtask {st.title!r} has no independent deliverable")
                raise self._record_violation(
                    exc, parent_task_id=parent_task_id, title=st.title)
        same_owner_all = all(st.owner == parent.owner for st in subtasks)
        no_dependency_all = all(not st.depends_on for st in subtasks)
        if same_owner_all and no_dependency_all:
            exc = Rule3InternalStepIsNotTask(
                "all subtasks belong to the parent owner with no explicit dependency "
                "— consecutive internal steps of one member do not become tasks")
            raise self._record_violation(
                exc, parent_task_id=parent_task_id,
                titles=[st.title for st in subtasks], owner=parent.owner)
        created = [
            self.create(st.title, owner=st.owner, deliverable=st.deliverable,
                        parent_task_id=parent_task_id)
            for st in subtasks
        ]
        return created


@dataclass(frozen=True)
class SubtaskSpec:
    title: str
    deliverable: str
    owner: str
    depends_on: tuple = ()   # 兄弟子任务标题的显式依赖
