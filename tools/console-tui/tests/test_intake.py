# coding: utf-8
"""下单分解（线E动作1）测试：RULES 模板/目录提取/标签保留/可执行性诚实/确定性."""
from __future__ import annotations

import json

import pytest

from console_tui.intake import (MANUAL_HANDLER, MANUAL_TEMPLATE, decompose,
                                extract_leading_tags, extract_test_dir)
from console_tui.state import GovernanceError

NOW = 1_800_000_000.0  # 固定纪元（与 conftest T0 同值；周报日期可控）


def deliverable_of(draft):
    return json.loads(draft.deliverable)


# ── 空指令与纯标签 ────────────────────────────────────────────────────────────

def test_empty_instruction_is_rejected_not_decomposed():
    with pytest.raises(GovernanceError):
        decompose("   ")
    with pytest.raises(GovernanceError):
        decompose("[worker-smoke]   ")          # 只有标签没有正文 → 拒绝


# ── 周报模板 ────────────────────────────────────────────────────────────────

def test_weekly_report_template_produces_executable_report_task():
    (draft,) = decompose("整理本周周报", now=NOW)
    assert draft.template == "weekly_report" and draft.kind_label == "周报"
    assert draft.executable is True
    assert draft.title.startswith("[周报]")
    spec = deliverable_of(draft)
    assert spec["handler"] == "report"
    assert "weekly-report-" in spec["spec"]["title"]
    assert "整理本周周报" in spec["spec"]["content"]


def test_weekly_report_english_keyword_matches():
    (draft,) = decompose("please write the weekly report", now=NOW)
    assert draft.template == "weekly_report"


# ── 复审模板 ────────────────────────────────────────────────────────────────

def test_review_template_produces_review_checklist_task():
    (draft,) = decompose("复审 leases 派生限额的验收口径", now=NOW)
    assert draft.template == "review" and draft.executable is True
    spec = deliverable_of(draft)
    assert spec["handler"] == "report"
    assert "复审清单" in spec["spec"]["content"]


# ── 测试模板：目录提取决定可执行性 ────────────────────────────────────────────

def test_tests_template_with_dir_is_run_tests():
    (draft,) = decompose("跑 tests/ 的测试", now=NOW)
    assert draft.template == "tests" and draft.executable is True
    spec = deliverable_of(draft)
    assert spec["handler"] == "run_tests"
    assert spec["spec"]["dir"] == "tests/"


def test_tests_template_explicit_dir_keyword_wins():
    assert extract_test_dir("对 src/jiuwen_glue 跑单测 目录: tools/console-tui") == \
        "tools/console-tui"


def test_tests_template_without_dir_degrades_to_manual_not_poison():
    """未指明目录 → 等人工拆（manual handler 任何 worker 注册表都不认——不冒充执行）。"""
    (draft,) = decompose("给新模块补测试", now=NOW)
    assert draft.template == "tests" and draft.executable is False
    spec = deliverable_of(draft)
    assert spec["handler"] == MANUAL_HANDLER
    assert spec["spec"]["reason"]


# ── 部署模板 ────────────────────────────────────────────────────────────────

def test_deploy_template_produces_runbook_with_human_gate():
    (draft,) = decompose("部署 eval-gate 新版到 srv-1", now=NOW)
    assert draft.template == "deploy" and draft.executable is True
    spec = deliverable_of(draft)
    assert spec["handler"] == "report"                 # 产物=清单文档（真实交付物）
    assert "PR + 实机拉取" in spec["spec"]["content"]  # 实际部署人工闸写进清单


# ── 自由单（未匹配）──────────────────────────────────────────────────────────

def test_unmatched_instruction_lands_adhoc_manual():
    (draft,) = decompose("给 eval-gate 加个 README 徽章", now=NOW)
    assert draft.template == MANUAL_TEMPLATE and draft.kind_label == "自由单"
    assert draft.executable is False
    assert draft.title.startswith("[待拆]")
    assert deliverable_of(draft)["handler"] == MANUAL_HANDLER


# ── 标签保留（冒烟红线兼容）──────────────────────────────────────────────────

def test_leading_bracket_tag_is_preserved_in_title():
    tag, body = extract_leading_tags("[worker-smoke] 出一版周报")
    assert tag == "[worker-smoke]" and body == "出一版周报"
    (draft,) = decompose("[worker-smoke] 出一版周报", now=NOW)
    assert draft.title.startswith("[worker-smoke] [周报]")


def test_worker_smoke_tag_with_test_dir_full_chain_shape():
    """真库闭环形态：标签+测试模板+目录 → worker smoke 过滤器可认领的可执行单。"""
    (draft,) = decompose("[worker-smoke] 跑 tools/console-tui/tests 的回归测试", now=NOW)
    assert draft.executable is True
    spec = deliverable_of(draft)
    assert spec == {"handler": "run_tests", "spec": {"dir": "tools/console-tui/tests"}}
    assert draft.title.startswith("[worker-smoke] [测试]")


# ── 确定性与标题截断 ─────────────────────────────────────────────────────────

def test_decompose_is_deterministic():
    a = decompose("复审 X", now=NOW)
    b = decompose("复审 X", now=NOW)
    assert [(d.title, d.deliverable) for d in a] == [(d.title, d.deliverable) for d in b]


def test_long_instruction_truncated_in_title_but_kept_in_spec():
    long = "复审" + "很长的指令" * 30
    (draft,) = decompose(long, now=NOW)
    assert len(draft.title) < len(long)
    assert long in deliverable_of(draft)["spec"]["content"]


def test_compound_deploy_test_instruction_prefers_deploy_conservatively():
    (draft,) = decompose("部署新版本并跑测试", now=NOW)
    assert draft.template == "deploy"        # 复合指令取保守方向（部署），确定性
