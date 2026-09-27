# -*- coding: utf-8 -*-
"""薄封装 jiuwen_glue.guardrail（GuardrailRun 五步链路，WO-0003 协议聚合）。

ASK=授权三态第三态，只有 permission_rail check 可提交，落 UNKNOWN fail-closed，
Challenge 经共享 ChallengeBoard。聚合 verdict 是唯一门控输出，工具不重新解释。
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


from jiuwen_glue.guardrail import CheckSpec, GuardrailSpec

_REQUIRED = {
    "create_run": ("action", "resource", "agent_identity_ref"),
    "submit_check": ("run_id", "check_id", "outcome"),
    "finalize": ("run_id", "seal_ref"),
    "void": ("run_id", "reason"),
    "gate": ("run_id",),
    "pending_challenges": ("run_id",),
}


class GuardrailGateTool(Tool):
    """glue 治理门禁：GuardrailRun 五步链路的薄转发。"""

    def __init__(self) -> None:
        card = ToolCard(
            id="glue_guardrail_gate",
            name="glue_guardrail_gate",
            description=(
                "glue 治理门禁工具：GuardrailRun 五步链路（创建固化→提交 check→验收固化"
                "→现场失效→执行前 gate 查询）+ run 级待审批 Challenge 查询。"
                "薄封装 jiuwen_glue.guardrail，聚合 verdict 是唯一门控输出，"
                "UNKNOWN 一律不得执行（fail-closed）。"
            ),
            input_params={
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": [
                            "create_run",
                            "submit_check",
                            "finalize",
                            "void",
                            "gate",
                            "pending_challenges",
                        ],
                        "description": "子操作",
                    },
                    "action": {
                        "type": "string",
                        "description": "create_run: 受控动作名（如 deploy.config）",
                    },
                    "resource": {"type": "string"},
                    "agent_identity_ref": {
                        "type": "string",
                        "description": "三层复合身份引用",
                    },
                    "env_ref": {"type": "string"},
                    "tenant_id": {"type": "string"},
                    "spec_version": {"type": "string"},
                    "checks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "check_id": {"type": "string"},
                                "backend": {
                                    "type": "string",
                                    "enum": [
                                        "native_guardrail",
                                        "permission_rail",
                                        "eval_gate",
                                        "scan",
                                    ],
                                },
                                "declaration": {"type": "object"},
                                "required": {"type": "boolean"},
                            },
                            "required": ["check_id", "backend"],
                        },
                    },
                    "check_id": {"type": "string"},
                    "outcome": {
                        "type": "string",
                        "enum": ["PASS", "BLOCKED", "UNKNOWN", "ASK"],
                    },
                    "evidence_ref": {"type": "string"},
                    "note": {"type": "string"},
                    "seal_ref": {"type": "string"},
                    "reason": {"type": "string"},
                    "run_id": {"type": "string"},
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
            missing = [k for k in required if not inputs.get(k)]
            if missing:
                return {
                    "success": False,
                    "error": "missing required: " + ", ".join(missing),
                }
            if op == "create_run":
                checks = [
                    CheckSpec(
                        check_id=c["check_id"],
                        backend=c["backend"],
                        declaration=c.get("declaration") or {},
                        required=bool(c.get("required", True)),
                    )
                    for c in inputs.get("checks") or []
                ]
                spec = GuardrailSpec(
                    action=inputs["action"],
                    resource=inputs["resource"],
                    agent_identity_ref=inputs["agent_identity_ref"],
                    env_ref=inputs.get("env_ref", ""),
                    tenant_id=inputs.get("tenant_id") or "t0",
                    spec_version=inputs.get("spec_version") or "1",
                    checks=checks,
                )
                run = _core.RUN_STORE.create_run(spec)
                return {
                    "success": True,
                    "run_id": run.run_id,
                    "state": run.state,
                    "spec_version": run.spec.spec_version,
                    "checks": [c.check_id for c in run.spec.checks],
                }
            if op == "submit_check":
                run_id = inputs["run_id"]
                check_id = inputs["check_id"]
                run = _core.RUN_STORE.submit_check(
                    run_id,
                    check_id,
                    inputs["outcome"],
                    evidence_ref=inputs.get("evidence_ref", ""),
                    note=inputs.get("note", ""),
                )
                result = run.results[check_id]
                return {
                    "success": True,
                    "run_id": run_id,
                    "check_id": check_id,
                    "verdict": result.verdict,
                    "challenge_id": result.challenge_id,
                }
            if op == "finalize":
                run_id = inputs["run_id"]
                run = _core.RUN_STORE.finalize(run_id, seal_ref=inputs["seal_ref"])
                return {
                    "success": True,
                    "run_id": run_id,
                    "state": run.state,
                    "seal_ref": run.seal_ref,
                }
            if op == "void":
                run_id = inputs["run_id"]
                run = _core.RUN_STORE.void(run_id, reason=inputs["reason"])
                return {"success": True, "run_id": run_id, "state": run.state}
            if op == "gate":
                res = _core.RUN_STORE.gate(inputs["run_id"])
                return {
                    "success": True,
                    "run_id": res.run_id,
                    "verdict": res.verdict,
                    "executable": res.executable,
                    "state": res.state,
                    "per_check": dict(res.per_check),
                    "missing_required": list(res.missing_required),
                    "checked_at": res.checked_at,
                }
            challenges = _core.RUN_STORE.pending_challenges(inputs["run_id"])
            return {
                "success": True,
                "run_id": inputs["run_id"],
                "challenges": [ch.to_ask_payload() for ch in challenges],
            }
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
