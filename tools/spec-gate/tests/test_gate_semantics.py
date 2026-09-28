# coding: utf-8
"""spec-gate 门禁语义回归（验收 D4-R2 #10/#12）。

钉死条款：**空证据维度绝不默认放行**（v2.1 原则 2）——
- D6 零样本（outcome_cases.jsonl 缺失/空）→ BLOCKED，门禁非零退出；
- D3 真实扇出未接线 → BLOCKED（demo 分离只作参考信号，不构成通过）；
- 六维有证据但不达阈值 → FAIL，同非零；
- D1-R3 起本目录已入根 pyproject testpaths：随仓根裸 pytest 整仓门禁自动跑
  （floor=495，见 .github/workflows/ci.yml），防回归不再只靠人工按需运行；
  仍可单跑：``python -m pytest tools/spec-gate/tests/ -q``。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SPEC_GATE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SPEC_GATE_DIR))
sys.path.insert(0, str(SPEC_GATE_DIR.parents[2] / "src"))

import spec_gate  # noqa: E402


def _case(i, expect, label):
    return {"id": f"OC-{i:03d}", "input": [expect], "expect": expect,
            "outcome_label": label}


def test_load_outcome_cases_missing_file_is_empty(tmp_path):
    assert spec_gate.load_outcome_cases(tmp_path / "nope.jsonl") == []


def test_d6_zero_samples_blocked_never_pass(tmp_path):
    """零样本 = BLOCKED，绝不默认放行（验收 #12 回归：空证据曾被当作门禁通过）。"""
    d6 = spec_gate.dim_outcome_consistency([])
    assert d6["status"] == "BLOCKED" and d6["pass"] is False
    assert d6["samples"] == 0


def test_d6_under_threshold_pass():
    cases = [_case(i, "PASS", "PASS") for i in range(97)]
    cases += [_case(97 + i, "PASS", "DIVERGED") for i in range(3)]  # 3% 背离
    d6 = spec_gate.dim_outcome_consistency(cases)
    assert d6["samples"] == 100 and d6["status"] == "PASS" and d6["pass"] is True


def test_d6_at_or_over_threshold_fail():
    for n_bad in (5, 6):                       # 5% 不<5% 同 FAIL（阈值严格小于）
        cases = [_case(i, "PASS", "PASS") for i in range(100 - n_bad)]
        cases += [_case(100 - n_bad + i, "PASS", "DIVERGED") for i in range(n_bad)]
        d6 = spec_gate.dim_outcome_consistency(cases)
        assert d6["status"] == "FAIL" and d6["pass"] is False


def test_d3_real_fanout_missing_is_blocked():
    """真实扇出未接线 → BLOCKED；demo 分离仅参考（验收 #10 回归）。"""
    red_cases = spec_gate.load_red_cases(
        spec_gate.REPO / "evals" / "guardrail-aggregate" / "red_cases.jsonl")
    d3 = spec_gate.dim_discrimination(red_cases)
    assert d3["real_fanout"] is None
    assert d3["status"] == "BLOCKED" and d3["pass"] is False
    assert d3["demo_separated"] is True         # demo 仍应分离（参考信号）


def test_full_run_blocked_while_d3_d6_empty():
    """六维全跑：D3/D6 空证据 → 门禁非零退出（修复前 exit 0——红线回归）。"""
    rc = spec_gate.main([])
    assert rc == 1


def test_static_only_semantics_unchanged():
    """--static-only（CI 环节9 口径）不受影响：静态一致 → 0。"""
    assert spec_gate.main(["--static-only"]) == 0
