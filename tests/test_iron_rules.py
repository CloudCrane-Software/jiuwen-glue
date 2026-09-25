# coding: utf-8
"""协同三铁律失败用例（WO-0003 动作 2）.

每个铁律一条失败用例：喂入违规输入，断言违规被检测——
抛出对应 IronRuleViolation 子类 + violation_log 留痕 + 任务事实未被篡改。
"""
from __future__ import annotations

import pytest

from jiuwen_glue import (
    CLAIMED,
    COMPLETED,
    MessageReceipt,
    PENDING,
    Rule1MessageIsNotClaim,
    Rule2ConversationIsNotState,
    Rule3InternalStepIsNotTask,
    SubtaskSpec,
    TaskLedger,
)


# ── 铁律 1：消息可以触发任务或补充信息，但发送成功不代表任务已被承接 ──────────

def test_rule1_failure_case_message_send_is_not_claim(clock):
    """失败用例：消息发送成功后，任务必须仍未被承接；
    把"消息送达"当承接来源会抛 Rule1MessageIsNotClaim 并留痕。"""
    led = TaskLedger(now=clock)
    task = led.create("修复构建", owner="dev-core")

    receipt: MessageReceipt = led.post_message(task.task_id, "orchestrator",
                                               "请认领此任务并尽快开始")
    # 回执只证明"送达"，不携带任何状态变更能力
    assert isinstance(receipt, MessageReceipt)
    assert receipt.task_ref == task.task_id
    assert not any(hasattr(receipt, a) for a in ("claim", "complete", "transition"))

    # 违规路径：试图以"消息"为来源承接任务 → 被检测
    with pytest.raises(Rule1MessageIsNotClaim):
        led.transition(task.task_id, CLAIMED, source="message",
                       executor="dev-core")
    # 违规被记录
    assert any(v.rule_no == 1 for v in led.violation_log)
    # 任务事实未被篡改：发送成功 ≠ 任务已被承接
    assert led.state_of(task.task_id) == PENDING

    # 正路径：显式 claim 才是承接
    led.transition(task.task_id, CLAIMED, source="claim", executor="dev-core")
    assert led.state_of(task.task_id) == CLAIMED


# ── 铁律 2：不能把对话历史当作 Team State ────────────────────────────────────

def test_rule2_failure_case_conversation_is_not_state(clock):
    """失败用例：对话历史里说"任务完成了"不构成完成事实；
    以对话历史为来源变更状态 → Rule2ConversationIsNotState；
    无 TaskRun/Artifact 引用的完成 → MissingTaskReferenceError。"""
    led = TaskLedger(now=clock)
    task = led.create("交付评测报告", owner="eval-runner")
    led.transition(task.task_id, CLAIMED, source="claim", executor="eval-runner")

    # "对话历史"：成员在群里说完成了
    chat = [
        {"sender": "eval-runner", "text": "报告写完了，任务搞定！"},
        {"sender": "leader", "text": "收到，棒。"},
    ]
    for msg in chat:
        led.post_message(task.task_id, msg["sender"], msg["text"])

    # 违规路径 A：以对话历史为来源宣布完成 → 被检测
    with pytest.raises(Rule2ConversationIsNotState):
        led.transition(task.task_id, COMPLETED, source="chat_history")
    assert any(v.rule_no == 2 for v in led.violation_log)

    # 违规路径 B：即使走"合法来源"，没有 TaskRun/Artifact 引用也不能完成
    with pytest.raises(Exception) as ei:
        led.transition(task.task_id, COMPLETED, source="run")
    assert "run_ref" in str(ei.value)

    # 任务事实仍由台账承载，不受对话影响
    assert led.state_of(task.task_id) == CLAIMED

    # 正路径：run_ref + artifact_ref 齐备才可完成
    led.transition(task.task_id, COMPLETED, source="run",
                   run_ref="run:taskrun-42", artifact_ref="artifact:report-v3.pdf")
    assert led.state_of(task.task_id) == COMPLETED
    assert led.get(task.task_id).artifact_ref == "artifact:report-v3.pdf"


# ── 铁律 3：同一成员连续完成的内部步骤不建任务 ────────────────────────────────

def test_rule3_failure_case_internal_steps_do_not_become_tasks(clock):
    """失败用例：把同一成员的连续内部步骤拆成子任务 → Rule3InternalStepIsNotTask；
    合法拆分（不同责任 / 明确依赖）可通过。"""
    led = TaskLedger(now=clock)
    parent = led.create("发布新版本", owner="dev-core")

    # 违规拆分：同一成员、顺序步骤、无明确依赖、其中一步无独立交付物
    bogus = [
        SubtaskSpec(title="step1 改配置", deliverable="配置已改", owner="dev-core"),
        SubtaskSpec(title="step2 顺手看一下", deliverable="", owner="dev-core"),
    ]
    with pytest.raises(Rule3InternalStepIsNotTask):
        led.split_task(parent.task_id, bogus)
    assert any(v.rule_no == 3 for v in led.violation_log)
    # 没有产生任何子任务
    assert not [t for t in led._tasks.values() if t.parent_task_id == parent.task_id]

    # 违规拆分 2：同一成员 + 无依赖（即便都有"交付物"字样）
    with pytest.raises(Rule3InternalStepIsNotTask):
        led.split_task(parent.task_id, [
            SubtaskSpec(title="写代码", deliverable="代码", owner="dev-core"),
            SubtaskSpec(title="自测", deliverable="自测通过", owner="dev-core"),
        ])

    # 合法拆分：不同责任（dev-core 写、eval-runner 验证）
    ok = led.split_task(parent.task_id, [
        SubtaskSpec(title="实现功能", deliverable="功能代码", owner="dev-core"),
        SubtaskSpec(title="独立验证", deliverable="验证报告", owner="eval-runner"),
    ])
    assert len(ok) == 2
    assert all(t.parent_task_id == parent.task_id for t in ok)

    # 合法拆分 2：同一成员但声明了明确依赖（非"连续内部步骤"，有交接边界）
    led2 = TaskLedger(now=clock)
    p2 = led2.create("迁移数据", owner="dba")
    ok2 = led2.split_task(p2.task_id, [
        SubtaskSpec(title="导出", deliverable="dump 文件", owner="dba"),
        SubtaskSpec(title="导入", deliverable="导入完成报告", owner="dba",
                    depends_on=("导出",)),
    ])
    assert len(ok2) == 2
