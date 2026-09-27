# coding: utf-8
"""就绪包裁决卡（W-06，v2.1 §4.7 升级体系）— 渲染 + 两键裁决面.

规格来源（v2.1 §4.7 守门者人类就绪包四件套）:

- 裁决卡 = L4 守门者递呈的人类就绪包的治理面展示形态：四件套逐件展示
  （事实固定 / 范畴清晰 / 权限内无解 / 可逆性评估）+ 两键裁决；
- **两键**：``y`` approve（批准递呈人类 L5——包必须 READY，fail-closed）/
  ``e`` escalate（打回 L3——范畴不在四类硬清单或包不齐时的安全方向）；
- 留痕：两键都走数据层 ``resolve_adjudication``（状态机 + append-only 决策记录），
  与 s/a/p 同一审计纪律；本模块不新增全局干预键（三级干预 s/a/p 写死不变），
  卡片经 ``ConsoleApp.open_adjudication(card_id)`` 打开（裁决队列接线 [待 W-11/W-12]）。

分层（写死）：``render_readiness_card`` 是纯函数（不 import textual，data 层同款
纪律）；``AdjudicationCardScreen`` 只做展示与按键路由，裁决语义在 state/data 层。
"""
from __future__ import annotations

from typing import List

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from .data import HARD_LIST_CATEGORIES, ReadinessCardRow
from .state import CARD_APPROVED, CARD_RETURNED

PIECE_LABELS = {
    "facts_fixed": "① 事实固定",
    "category_clear": "② 范畴清晰",
    "in_permission_no_solution": "③ 权限内无解",
    "reversibility": "④ 可逆性评估",
}

_MARK = {"PASS": "[green]✔ PASS[/green]", "BLOCKED": "[red]✘ BLOCKED[/red]"}


def render_readiness_card(card: ReadinessCardRow) -> List[str]:
    """裁决卡纯文本渲染（mock/pg 两模式同一条显示路径；无 textual 依赖）。

    返回行列表：头（工单/签名/范畴/层）→ 四件套逐件 → 缺料与打回提示 → 两键提示。
    """
    lines = [
        f"[b]人类就绪包裁决卡[/b]  {card.card_id}  工单 {card.task_ref}",
        f"任务：{card.task_title}    阶梯层：{card.level}    生成："
        f"{card.generated_at:.0f}",
        f"阻塞签名：{card.signature[:16]}…",
        f"硬清单范畴：{card.category if card.category else '（未归类）'}"
        f"    四类 = {', '.join(HARD_LIST_CATEGORIES)}",
        "",
    ]
    for key, verdict, detail in card.pieces:
        mark = _MARK.get(verdict, verdict)
        label = PIECE_LABELS.get(key, key)
        lines.append(f"{label}  {mark}")
        if detail:
            lines.append(f"    {detail}")
    lines.append("")
    if card.ready:
        lines.append("四件套齐全：可批准递呈人类（L5）。")
    else:
        lines.append(f"缺料（BLOCKED 不放行）：{', '.join(card.missing) or '—'}")
    if card.knock_back_to_l3:
        lines.append("范畴不在四类硬清单 → 按规程打回 L3（不在表内打回 L3）。")
    lines.append("y=approve 批准递呈人类 / e=escalate 打回 L3 / Esc=取消")
    return lines


class AdjudicationCardScreen(ModalScreen[None]):
    """裁决卡弹层：四件套展示 + y/e 两键。裁决走数据层状态机（不绕过）。"""

    BINDINGS = [("y", "approve", "approve 递呈人类"),
                ("e", "escalate_back", "escalate 打回 L3"),
                ("escape", "cancel", "取消")]

    def __init__(self, card: ReadinessCardRow) -> None:
        super().__init__()
        self._card = card

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="adjudication-card"):
            for line in render_readiness_card(self._card):
                yield Static(line, classes="adjudication-line")

    @property
    def card_id(self) -> str:
        return self._card.card_id

    def action_approve(self) -> None:
        self.app.action_adjudication_submit(self._card.card_id, approved=True)
        self.dismiss(None)

    def action_escalate_back(self) -> None:
        self.app.action_adjudication_submit(self._card.card_id, approved=False)
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# 裁决结果 → 卡片状态（展示层提示用；权威语义在 state.resolve_adjudication_state）
_CARD_STATE_LABEL = {CARD_APPROVED: "已批准递呈人类（L5）",
                     CARD_RETURNED: "已打回 L3"}
