# -*- coding: utf-8 -*-
"""薄封装 jiuwen_glue.decisions（append-only 决策账本，chosen∈options 由 glue 校验，frozen 不可变）。

本账本只记录决策、不做决策（决策点唯一）。账面只存 context_hash，上下文原文不落账。
禁止把密钥/凭据写进 context 或 meta。
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
    "append": ("agent_ref", "context", "options", "chosen", "rationale_ref"),
    "get": ("decision_id",),
}


def _missing(inputs: dict[str, Any], required: tuple[str, ...]) -> list[str]:
    """缺键、None、空串、空列表算缺；空 dict 算已给出（context={} 可哈希）。"""
    missing: list[str] = []
    for key in required:
        if key not in inputs or inputs[key] is None:
            missing.append(key)
            continue
        value = inputs[key]
        if value == "" or value == []:
            missing.append(key)
    return missing


def _list_row(rec: Any) -> dict[str, Any]:
    return {
        "decision_id": rec.decision_id,
        "ts": rec.ts,
        "agent_ref": rec.agent_ref,
        "context_hash": rec.context_hash,  # 原文不落账
        "options": list(rec.options),
        "chosen": rec.chosen,
        "rationale_ref": rec.rationale_ref,
        "guardrail_run_ref": rec.guardrail_run_ref,
    }


class DecisionLogTool(Tool):
    """glue 决策记录：DecisionLog 的薄转发。"""

    def __init__(self) -> None:
        card = ToolCard(
            id="glue_decision_log",
            name="glue_decision_log",
            description=(
                "glue 决策记录工具：append-only 决策账本的落账与查询（append/get/list）。"
                "本账本只记录决策不做决策（决策点唯一）；账面只存 context_hash"
                "（上下文原文不落账）；禁止把密钥/凭据写进 context 或 meta。"
            ),
            input_params={
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": ["append", "get", "list"],
                        "description": "子操作",
                    },
                    "agent_ref": {"type": "string"},
                    "context": {"type": "object"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "chosen": {"type": "string"},
                    "rationale_ref": {"type": "string"},
                    "guardrail_run_ref": {"type": "string"},
                    "tenant_id": {"type": "string"},
                    "meta": {"type": "object"},
                    "decision_id": {"type": "string"},
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
            if op == "list":
                required: tuple[str, ...] = ()
            else:
                required = _REQUIRED.get(op)
                if required is None:
                    return {"success": False, "error": f"unknown op: {op}"}
            missing = _missing(inputs, required)
            if missing:
                return {
                    "success": False,
                    "error": "missing required: " + ", ".join(missing),
                }
            if op == "append":
                rec = _core.DECISION_LOG.append(
                    agent_ref=inputs["agent_ref"],
                    context=inputs["context"],
                    options=[str(o) for o in inputs["options"]],
                    chosen=inputs["chosen"],
                    rationale_ref=inputs["rationale_ref"],
                    guardrail_run_ref=inputs.get("guardrail_run_ref"),
                    tenant_id=inputs.get("tenant_id") or "t0",
                    meta=inputs.get("meta"),
                )
                return {
                    "success": True,
                    "decision_id": rec.decision_id,
                    "ts": rec.ts,
                    "context_hash": rec.context_hash,  # 原文不落账
                    "chosen": rec.chosen,
                }
            if op == "get":
                rec = _core.DECISION_LOG.get(inputs["decision_id"])
                return {
                    "success": True,
                    "decision_id": rec.decision_id,
                    "ts": rec.ts,
                    "agent_ref": rec.agent_ref,
                    "context_hash": rec.context_hash,  # 原文不落账
                    "options": list(rec.options),
                    "chosen": rec.chosen,
                    "rationale_ref": rec.rationale_ref,
                    "guardrail_run_ref": rec.guardrail_run_ref,
                    "meta": dict(rec.meta),
                }
            if inputs.get("agent_ref"):
                records = _core.DECISION_LOG.by_agent(inputs["agent_ref"])
            elif "context" in inputs and inputs["context"] is not None:
                records = _core.DECISION_LOG.by_context(inputs["context"])
            else:
                records = _core.DECISION_LOG.all()
            rows = [_list_row(rec) for rec in records]
            return {"success": True, "count": len(rows), "records": rows}
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
