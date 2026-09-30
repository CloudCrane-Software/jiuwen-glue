# coding: utf-8
"""工单下单接口（线E，2026-09-29）— 自然语言指令 → RULES 分解 → team_task 草稿.

规格来源（OWNER 需求 ①②，2026-09-29 过夜协议线E动作1）:

- TUI ``n`` 键 = 新建工单（**下单，不是干预**）：owner 输入自然语言指令 →
  本模块做确定性任务分解（RULES 优先）→ 工单落 glue.team_task
  （state='PENDING', owner=NULL）→ worker（windev-worker 等）自取执行。
- **RULES 优先，不硬造 LLM 依赖**：内置模板匹配 [周报/复审/测试/部署] 四类；
  匹配不上 → 单张 ``adhoc`` 草稿落 PENDING **等人工拆**（不猜、不编造多任务切分）。
- 1 条指令 → 恰 1 张工单（v1 边界）：分解的对象是"任务形态"（handler/spec 该是什么），
  不是多任务切分——多任务拆分留给人工或将来的独立分解器，这里不假装聪明。
- **可执行性诚实**：``executable=True`` 的草稿 deliverable 是 worker handler 注册表
  认识的 JSON（windev-worker parse_spec 契约：{"handler": ..., "spec": {...}}）；
  ``executable=False``（如测试类未指明目录 / 自由单）deliverable 用
  ``{"handler": "manual", ...}``——**故意**用任何 worker 注册表都没有的 handler 名，
  worker 解析即拒绝 → 重试后 BLOCKED（阻塞红色可见、等人工），而不是拿 report
  handler 把指令文本写成文件冒充"完成"。
- ``[标签]`` 前缀保留：指令开头的方括号标签（如 ``[worker-smoke]``）原样保留在
  标题最前——冒烟红线（windev-worker 只认领 ``[worker-smoke]%``）下也能经本接口
  走通全链，不必为验证破坏隔离纪律。
- 留痕语义：n 的留痕 = team_task 行本身 + task_transition（NULL→PENDING，
  source='admin'）；**不写 decision_record**——决策点唯一纪律：下单没有方案权衡，
  不是决策（s/a/p 才是干预/裁决），工单台账就是它的事实源。

分层（写死）：本模块纯函数、不 import textual、不碰数据库（data 层同款纪律）；
模板词表是产品语义，与 worker 的 handler 注册表（echo/run_tests/report）对齐处
有 tests 交叉校验防漂移。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence

# ── 常量 ─────────────────────────────────────────────────────────────────────

#: 等人工拆的草稿模板名 / deliverable handler 名（worker 注册表永不收录此名：
#: windev-worker parse_spec 未知 handler → 拒绝执行 → BLOCKED，不冒充完成）
MANUAL_HANDLER = "manual"
MANUAL_TEMPLATE = "adhoc"

#: 标题里指令正文的最大长度（超出截断；完整指令永远保留在 deliverable.spec.instruction）
_TITLE_INSTRUCTION_MAX = 48

#: 指令开头的 [标签]（可连续多个）；保留到工单标题最前（冒烟红线兼容）
_BRACKET_TAG_RE = re.compile(r"^(\[[^\[\]]{1,64}\])\s*")

#: 测试目录提取：显式 "目录/dir/path/在 X" 或任意含路径分隔符的 token（尾斜杠合法）
_DIR_EXPLICIT_RE = re.compile(r"(?:目录|路径|dir|path|在)\s*[:：]?\s*([^\s，,。；;]+)", re.I)
_DIR_PATHLIKE_RE = re.compile(r"([^\s，,。；;]*[/\\][^\s，,。；;]*)")


@dataclass(frozen=True)
class TaskDraft:
    """一张待落库工单（intake → store.create_task 的中间形态）。"""

    title: str
    deliverable: str            # worker 契约 JSON（或 manual JSON）
    template: str               # weekly_report | review | tests | deploy | adhoc
    kind_label: str             # 周报 | 复审 | 测试 | 部署 | 自由单
    executable: bool            # False = 等人工拆（worker 会拒绝执行，BLOCKED 可见）
    note: str = ""              # 给操作员的分解说明（TUI notify/预览用）


@dataclass(frozen=True)
class _TemplateSpec:
    key: str
    kind_label: str
    title_tag: str
    pattern: "re.Pattern[str]"   # 命中即采用（固定顺序，首中即止——确定性）


# 固定顺序 = 匹配优先级（确定性，无第二个决策点）。部署类在测试类之前：
# "部署并测试" 这类复合指令按部署处理（保守方向——部署产物是清单文档+人工闸，
# 不会替 owner 真的自动执行部署）。
_TEMPLATES: Sequence[_TemplateSpec] = (
    _TemplateSpec("weekly_report", "周报", "[周报]",
                  re.compile(r"周报|周報|weekly\s*report", re.I)),
    _TemplateSpec("review", "复审", "[复审]",
                  re.compile(r"复审|复核|评审|review", re.I)),
    _TemplateSpec("deploy", "部署", "[部署]",
                  re.compile(r"部署|发布|deploy|release", re.I)),
    _TemplateSpec("tests", "测试", "[测试]",
                  re.compile(r"测试|单测|跑测|回归|pytest|unit\s*test", re.I)),
)


def extract_leading_tags(instruction: str) -> "tuple[str, str]":
    """指令开头连续 [标签] 前缀 → (标签串, 去除标签后的正文)。标签原样保留不清洗。"""
    text = instruction.strip()
    tags: List[str] = []
    while True:
        m = _BRACKET_TAG_RE.match(text)
        if not m:
            break
        tags.append(m.group(1))
        text = text[m.end():]
    return (" ".join(tags), text.strip())


def extract_test_dir(instruction: str) -> str:
    """测试类指令中提取目标目录（空串 = 未指明，调用方降级为等人工拆）。"""
    m = _DIR_EXPLICIT_RE.search(instruction)
    if m:
        return m.group(1).strip("\"'`，。；")
    m = _DIR_PATHLIKE_RE.search(instruction)
    if m:
        return m.group(1).strip("\"'`，。；")
    return ""


def _manual_deliverable(instruction: str, reason: str) -> str:
    return json.dumps({"handler": MANUAL_HANDLER,
                       "spec": {"instruction": instruction, "reason": reason}},
                      ensure_ascii=False)


def _title_for(tag_prefix: str, title_tag: str, instruction: str) -> str:
    body = instruction.strip()
    short = body[:_TITLE_INSTRUCTION_MAX] + ("…" if len(body) > _TITLE_INSTRUCTION_MAX else "")
    parts = [p for p in (tag_prefix, title_tag, short) if p]
    return " ".join(parts)


def decompose(instruction: str, *, now: Optional[float] = None) -> List[TaskDraft]:
    """自然语言指令 → 工单草稿列表（RULES；恰 1 张；确定性）。

    匹配不上任何模板 → 单张 ``adhoc`` 草稿（executable=False，落 PENDING 等人工拆）。
    空指令 → GovernanceError（app 层 notify，不落库）。
    """
    from .state import GovernanceError

    if not instruction or not instruction.strip():
        raise GovernanceError("order instruction is empty; nothing to decompose")
    tag_prefix, body = extract_leading_tags(instruction)
    if not body:
        raise GovernanceError(
            "order instruction has no content beyond tags; nothing to decompose")

    tpl = next((t for t in _TEMPLATES if t.pattern.search(body)), None)
    if tpl is None:
        return [TaskDraft(
            title=_title_for(tag_prefix, "[待拆]", body),
            deliverable=_manual_deliverable(body, "no built-in template matched"),
            template=MANUAL_TEMPLATE, kind_label="自由单", executable=False,
            note="未匹配内置模板（周报/复审/测试/部署）→ PENDING 等人工拆")]

    if tpl.key == "weekly_report":
        day = datetime.fromtimestamp(now).date().isoformat() if now is not None else \
            datetime.now().date().isoformat()
        deliverable = json.dumps(
            {"handler": "report",
             "spec": {"title": f"weekly-report-{day}",
                      "content": f"周报草稿素材（操作员指令原文）：\n{body}"}},
            ensure_ascii=False)
        return [TaskDraft(title=_title_for(tag_prefix, tpl.title_tag, body),
                          deliverable=deliverable, template=tpl.key,
                          kind_label=tpl.kind_label, executable=True,
                          note="周报模板 → worker report handler（产物=周报草稿文档）")]

    if tpl.key == "review":
        deliverable = json.dumps(
            {"handler": "report",
             "spec": {"title": "review-checklist",
                      "content": ("复审清单（对照 glue 三铁律与验收口径逐项过）：\n"
                                  f"复审对象/指令：{body}\n"
                                  "① 事实核对（引用/行号逐条在案）\n"
                                  "② 测试真实通过（命令+输出）\n"
                                  "③ 边界与失败路径\n"
                                  "④ 结论与遗留项")}},
            ensure_ascii=False)
        return [TaskDraft(title=_title_for(tag_prefix, tpl.title_tag, body),
                          deliverable=deliverable, template=tpl.key,
                          kind_label=tpl.kind_label, executable=True,
                          note="复审模板 → worker report handler（产物=复审清单文档）")]

    if tpl.key == "tests":
        directory = extract_test_dir(body)
        if directory:
            deliverable = json.dumps(
                {"handler": "run_tests", "spec": {"dir": directory}},
                ensure_ascii=False)
            return [TaskDraft(title=_title_for(tag_prefix, tpl.title_tag, body),
                              deliverable=deliverable, template=tpl.key,
                              kind_label=tpl.kind_label, executable=True,
                              note=f"测试模板 → run_tests（dir={directory}）")]
        return [TaskDraft(
            title=_title_for(tag_prefix, tpl.title_tag, body),
            deliverable=_manual_deliverable(body, "tests template matched but no "
                                                    "directory specified"),
            template=tpl.key, kind_label=tpl.kind_label, executable=False,
            note="测试模板命中但未指明目录 → 等人工拆（不猜目录，防毒丸）")]

    # deploy：产物 = 部署清单文档（真实交付物），实际部署动作仍须人工走 PR+实机拉取
    deliverable = json.dumps(
        {"handler": "report",
         "spec": {"title": "deploy-runbook",
                  "content": (f"部署清单（指令：{body}）\n"
                              "① 变更清单与影响面\n"
                              "② 回滚方案\n"
                              "③ 红线核对（不动 company-bao-1/temporal；DSN 不落盘）\n"
                              "④ 实际部署须人工执行：走 PR + 实机拉取（本单产物仅为清单文档）")}},
        ensure_ascii=False)
    return [TaskDraft(title=_title_for(tag_prefix, tpl.title_tag, body),
                      deliverable=deliverable, template=tpl.key,
                      kind_label=tpl.kind_label, executable=True,
                      note="部署模板 → 产物=部署清单文档（实际部署仍走 PR+实机，人工）")]
