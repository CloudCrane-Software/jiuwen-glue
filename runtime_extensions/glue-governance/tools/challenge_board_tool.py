# -*- coding: utf-8 -*-
"""薄封装 jiuwen_glue.challenge（授权三态第三态台账）。

安全边界：不签发、不持有、不返回任何授权码或 token；批准结果如何变成
可执行凭证，由可信 Runtime 与 Credential Broker 兑换。台账只暴露高层状态
（pending/approved/denied/expired）与 ask 载荷。resolve 裁决只能由
who_confirms 对应的人在独立界面完成，Agent 不得代为裁决；过期或终态
Challenge 由 glue 拒绝裁决（fail-closed）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, AsyncIterator

_CORE_NAME = "glue_governance_core"
_CORE_PATH = Path(__file__).resolve().parent / "_governance_core.py"
if _CORE_NAME in sys.modules:
    _core = sys.modules[_CORE_NAME]
else:
    _spec = importlib.util.spec_from_file_location(_CORE_NAME, _CORE_PATH)
    _core = importlib.util.module_from_spec(_spec)
    sys.modules[_CORE_NAME] = _core
    _spec.loader.exec_module(_core)

try:
    from openjiuwen.core.foundation.tool import Tool, ToolCard
except ImportError:
    # openjiuwen 运行时缺失时降级，仅为结构校验与单测可导入；
    # GPU 机随 openjiuwen 加载时走真实 Tool。
    Tool = object

    class ToolCard:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)


_REQUIRED = {
    "open": ("who_confirms", "resource", "action", "method", "ttl_seconds"),
    "resolve": ("challenge_id", "approved", "by"),
    "pending_for": ("who_confirms",),
    "state_of": ("challenge_id",),
    "ask_payload": ("challenge_id",),
}


def _missing(inputs: dict[str, Any], required: tuple[str, ...]) -> list[str]:
    """缺键、None、空串、空列表算缺；False / 0 算已给出（approved=False 合法）。"""
    missing: list[str] = []
    for key in required:
        if key not in inputs or inputs[key] is None:
            missing.append(key)
            continue
        value = inputs[key]
        if value == "" or value == []:
            missing.append(key)
    return missing


class ChallengeBoardTool(Tool):
    """glue 授权审批：ChallengeBoard 的薄转发。"""

    def __init__(self) -> None:
        card = ToolCard(
            id="glue_challenge_board",
            name="glue_challenge_board",
            description=(
                "glue 授权审批工具：结构化 Challenge 的发起/裁决/待办队列/状态查询。"
                "台账只暴露高层状态（pending/approved/denied/expired）与 ask 载荷，"
                "不含任何授权码或 token 字段；resolve 裁决只能由 who_confirms 对应的人"
                "在独立界面完成，Agent 不得代为裁决（fail-closed：过期或终态 Challenge"
                "由 glue 拒绝裁决）。"
            ),
            input_params={
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": [
                            "open",
                            "resolve",
                            "pending_for",
                            "state_of",
                            "ask_payload",
                        ],
                        "description": "子操作",
                    },
                    "challenge_id": {"type": "string"},
                    "who_confirms": {
                        "type": "string",
                        "enum": ["user", "resource_owner", "duty_officer"],
                    },
                    "resource": {"type": "string"},
                    "action": {"type": "string"},
                    "method": {"type": "string"},
                    "ttl_seconds": {"type": "number"},
                    "agent_identity_ref": {"type": "string"},
                    "guardrail_run_ref": {"type": "string"},
                    "tenant_id": {"type": "string"},
                    "meta": {"type": "object"},
                    "approved": {"type": "boolean"},
                    "by": {"type": "string"},
                },
                "required": ["op"],
            },
            parallel_safe=False,
            stateless=False,
            idempotent=False,
        )
        if Tool is not object:
            super().__init__(card)
        else:
            self.card = card

    async def invoke(self, inputs: dict[str, Any], **kwargs: object) -> dict[str, Any]:
        try:
            if not isinstance(inputs, dict) or "op" not in inputs:
                return {"success": False, "error": "inputs must be a dict with op"}
            op = inputs["op"]
            required = _REQUIRED.get(op)
            if required is None:
                return {"success": False, "error": f"unknown op: {op}"}
            missing = _missing(inputs, required)
            if missing:
                return {
                    "success": False,
                    "error": "missing required: " + ", ".join(missing),
                }
            if op == "open":
                ch = _core.CHALLENGE_BOARD.open(
                    who_confirms=inputs["who_confirms"],
                    resource=inputs["resource"],
                    action=inputs["action"],
                    method=inputs["method"],
                    ttl_seconds=float(inputs["ttl_seconds"]),
                    agent_identity_ref=inputs.get("agent_identity_ref", ""),
                    guardrail_run_ref=inputs.get("guardrail_run_ref"),
                    tenant_id=inputs.get("tenant_id") or "t0",
                    meta=inputs.get("meta") or None,
                )
                return {
                    "success": True,
                    "challenge_id": ch.challenge_id,
                    "state": ch.state,
                    "who_confirms": ch.who_confirms,
                    "expires_at": ch.expires_at,
                    "ask_payload": ch.to_ask_payload(),
                }
            if op == "resolve":
                ch = _core.CHALLENGE_BOARD.resolve(
                    inputs["challenge_id"],
                    approved=bool(inputs["approved"]),
                    by=inputs["by"],
                )
                return {
                    "success": True,
                    "challenge_id": ch.challenge_id,
                    "state": ch.state,
                    "resolved_by": ch.resolved_by,
                }
            if op == "pending_for":
                pending = [
                    c.to_ask_payload()
                    for c in _core.CHALLENGE_BOARD.pending_for(inputs["who_confirms"])
                ]
                return {
                    "success": True,
                    "who_confirms": inputs["who_confirms"],
                    "pending": pending,
                }
            if op == "state_of":
                challenge_id = inputs["challenge_id"]
                return {
                    "success": True,
                    "challenge_id": challenge_id,
                    "state": _core.CHALLENGE_BOARD.state_of(challenge_id),
                }
            ch = _core.CHALLENGE_BOARD.get(inputs["challenge_id"])
            return {"success": True, "ask_payload": ch.to_ask_payload()}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_type": type(e).__name__,
            }

    async def stream(
        self, inputs: dict[str, Any], **kwargs: object
    ) -> AsyncIterator[dict[str, Any]]:
        if False:
            yield inputs
