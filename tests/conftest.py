# coding: utf-8
"""共享 fixture：固定时钟，保证时间相关用例确定性。"""
from __future__ import annotations

import pytest


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
