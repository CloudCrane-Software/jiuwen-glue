# coding: utf-8
"""共享 fixture：固定时钟，保证时间相关用例确定性。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 本仓是独立克隆（decision/wave2 分支）：全局 site-packages 里的 editable 安装
# 可能指向另一个克隆（gh-jiuwen-glue）。测试必须测本仓 src/ 的代码——
# 把本仓 src 置于 sys.path 最前，保证 `python -m pytest` 在任何环境自洽。
_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC in sys.path:
    sys.path.remove(_SRC)
sys.path.insert(0, _SRC)


class FakeClock:
    """手动拨动的时钟（epoch 秒）。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now_value = start

    def __call__(self) -> float:
        return self.now_value

    def advance(self, seconds: float) -> None:
        self.now_value += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()
