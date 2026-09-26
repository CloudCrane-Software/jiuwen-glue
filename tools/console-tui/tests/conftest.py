# coding: utf-8
"""console-tui 测试夹具：包路径 + 可控时钟的 mock 后端。"""
from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]          # tools/console-tui
REPO_ROOT = TOOL_DIR.parents[1]                          # jiuwen-glue 仓库根
for p in (str(TOOL_DIR / "src"), str(REPO_ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest

from console_tui.data import MockConsoleStore

# 固定纪元（任意确定值；与真实时间无关，保证过期场景可控）
T0 = 1_800_000_000.0
MINUTE = 60.0


class Clock:
    """可控时钟：`clock.advance(90)` 推进秒数。"""

    def __init__(self, start: float = T0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> float:
        self.value += seconds
        return self.value


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def store(clock: Clock) -> MockConsoleStore:
    return MockConsoleStore(now=clock)
