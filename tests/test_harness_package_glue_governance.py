# coding: utf-8
"""glue-governance Harness Package 工具的集成测试。

加载方式与 jiuwenswarm 激活路径一致：包内 file 型工具由
importlib.util.spec_from_file_location + exec_module 从文件路径真实加载
（agent-core meta_elements.py:101-126 的加载链路），此处用同一机制加载，
验证薄封装转发与 glue 治理语义（fail-closed / 授权三态 / append-only）。

运行前提：conftest.py 已把本仓 src/ 置于 sys.path 最前（jiuwen_glue 可导入）。
openjiuwen 运行时缺失时工具类走降级占位基类——本文件只测转发逻辑与
glue 语义，真实 Tool 基类注入在 GPU 机 [待GPU实测]。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "runtime_extensions" / "glue-governance" / "tools"
CORE_NAME = "glue_governance_core"


def _load(name: str):
    """按文件路径加载包内工具模块（与激活时 exec_module 同链路）。"""
    path = TOOLS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"glue_governance_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guardrail_tool():
    return _load("guardrail_gate_tool").GuardrailGateTool()


@pytest.fixture(scope="module")
def challenge_tool():
    return _load("challenge_board_tool").ChallengeBoardTool()


@pytest.fixture(scope="module")
def decision_tool():
    return _load("decision_log_tool").DecisionLogTool()


def _run(tool, **inputs):
    return asyncio.run(tool.invoke(inputs))


# ---------------------------------------------------------------------------
# 共享内核：三个工具必须共享同一组进程内台账单例
# ---------------------------------------------------------------------------

def test_shared_core_singleton():
    gg = _load("guardrail_gate_tool")
    cb = _load("challenge_board_tool")
    core = sys.modules.get(CORE_NAME)
    assert core is not None, "tool modules must register the shared core"
    assert gg._core is cb._core is core


# ---------------------------------------------------------------------------
# GuardrailGateTool：五步链路 + fail-closed
# ---------------------------------------------------------------------------

def _create_run(tool, *, backends=("native_guardrail", "scan"), action="deploy.config"):
    return _run(
        tool,
        op="create_run",
        action=action,
        resource="release/batch-42",
        agent_identity_ref="org:cloudcrane/agent:deployer/instance:i1",
        checks=[
            {"check_id": f"c{i}", "backend": b}
            for i, b in enumerate(backends)
        ],
    )


def test_gate_unknown_before_finalize(guardrail_tool):
    """未验收固化（OPEN）→ gate=UNKNOWN 且不可执行（fail-closed）。"""
    made = _create_run(guardrail_tool)
    assert made["success"] is True
    gate = _run(guardrail_tool, op="gate", run_id=made["run_id"])
    assert gate["verdict"] == "UNKNOWN"
    assert gate["executable"] is False
    assert gate["state"] == "OPEN"


def test_full_pass_flow_is_executable(guardrail_tool):
    made = _create_run(guardrail_tool)
    run_id = made["run_id"]
    for cid in made["checks"]:
        sub = _run(guardrail_tool, op="submit_check", run_id=run_id,
                   check_id=cid, outcome="PASS", evidence_ref="ev://scan/001")
        assert sub["success"] is True and sub["verdict"] == "PASS"
    fin = _run(guardrail_tool, op="finalize", run_id=run_id, seal_ref="seal://t6/001")
    assert fin["state"] == "FINALIZED"
    gate = _run(guardrail_tool, op="gate", run_id=run_id)
    assert gate["verdict"] == "PASS"
    assert gate["executable"] is True


def test_blocked_check_blocks_gate(guardrail_tool):
    made = _create_run(guardrail_tool)
    run_id = made["run_id"]
    _run(guardrail_tool, op="submit_check", run_id=run_id,
         check_id="c0", outcome="PASS")
    _run(guardrail_tool, op="submit_check", run_id=run_id,
         check_id="c1", outcome="BLOCKED", evidence_ref="ev://sast/hit")
    _run(guardrail_tool, op="finalize", run_id=run_id, seal_ref="seal://t6/002")
    gate = _run(guardrail_tool, op="gate", run_id=run_id)
    assert gate["verdict"] == "BLOCKED" and gate["executable"] is False


def test_missing_required_check_is_unknown(guardrail_tool):
    """必填 check 未提交结论 → UNKNOWN（fail-closed），即使 finalize 也不放行。"""
    made = _create_run(guardrail_tool)
    run_id = made["run_id"]
    _run(guardrail_tool, op="submit_check", run_id=run_id,
         check_id="c0", outcome="PASS")
    _run(guardrail_tool, op="finalize", run_id=run_id, seal_ref="seal://t6/003")
    gate = _run(guardrail_tool, op="gate", run_id=run_id)
    assert gate["verdict"] == "UNKNOWN"
    assert gate["executable"] is False
    assert "c1" in gate["missing_required"]


def test_void_invalidates_conclusions(guardrail_tool):
    """现场已变化 → void 后 gate 恒 UNKNOWN，原有 PASS 结论失效。"""
    made = _create_run(guardrail_tool)
    run_id = made["run_id"]
    for cid in made["checks"]:
        _run(guardrail_tool, op="submit_check", run_id=run_id,
             check_id=cid, outcome="PASS")
    _run(guardrail_tool, op="finalize", run_id=run_id, seal_ref="seal://t6/004")
    assert _run(guardrail_tool, op="gate", run_id=run_id)["verdict"] == "PASS"
    void = _run(guardrail_tool, op="void", run_id=run_id, reason="infra changed")
    assert void["state"] == "VOID"
    gate = _run(guardrail_tool, op="gate", run_id=run_id)
    assert gate["verdict"] == "UNKNOWN" and gate["executable"] is False


def test_submit_check_after_finalize_is_rejected(guardrail_tool):
    made = _create_run(guardrail_tool)
    run_id = made["run_id"]
    _run(guardrail_tool, op="submit_check", run_id=run_id,
         check_id="c0", outcome="PASS")
    _run(guardrail_tool, op="finalize", run_id=run_id, seal_ref="seal://t6/005")
    res = _run(guardrail_tool, op="submit_check", run_id=run_id,
               check_id="c1", outcome="PASS")
    assert res["success"] is False
    assert res["error_type"] == "GuardrailStateError"


# ---------------------------------------------------------------------------
# ASK → Challenge 审批闭环（授权三态第三态）
# ---------------------------------------------------------------------------

def _ask_run(guardrail_tool):
    made = _run(
        guardrail_tool,
        op="create_run",
        action="secret.grant",
        resource="vault/prod/db-1",
        agent_identity_ref="org:cloudcrane/agent:operator/instance:i2",
        checks=[
            {"check_id": "perm", "backend": "permission_rail",
             "declaration": {"who_confirms": "resource_owner",
                             "ask_ttl_seconds": 3600.0}},
        ],
    )
    return made


def test_ask_creates_challenge_and_stays_closed(guardrail_tool):
    """ASK → Challenge 生成，check 落 UNKNOWN，gate 恒关（fail-closed）。"""
    made = _ask_run(guardrail_tool)
    run_id = made["run_id"]
    sub = _run(guardrail_tool, op="submit_check", run_id=run_id,
               check_id="perm", outcome="ASK")
    assert sub["success"] is True
    assert sub["verdict"] == "UNKNOWN"          # 未决 ask = UNKNOWN
    assert sub["challenge_id"]
    fin = _run(guardrail_tool, op="finalize", run_id=run_id, seal_ref="seal://t6/006")
    assert fin["state"] == "FINALIZED"
    gate = _run(guardrail_tool, op="gate", run_id=run_id)
    assert gate["verdict"] == "UNKNOWN" and gate["executable"] is False
    pend = _run(guardrail_tool, op="pending_challenges", run_id=run_id)
    assert len(pend["challenges"]) == 1
    payload = pend["challenges"][0]
    assert payload["who_confirms"] == "resource_owner"
    # ask 载荷只含展示/路由字段，不含任何授权码/token 字段
    assert not any("token" in k or "secret" in k or "credential" in k
                   for k in payload)


def test_ask_flow_closes_after_human_approval(guardrail_tool, challenge_tool):
    """人在独立界面批准 → 以 PASS 重新提交 → gate PASS。Agent 不代裁决。"""
    made = _ask_run(guardrail_tool)
    run_id = made["run_id"]
    sub = _run(guardrail_tool, op="submit_check", run_id=run_id,
               check_id="perm", outcome="ASK")
    challenge_id = sub["challenge_id"]

    queue = _run(challenge_tool, op="pending_for", who_confirms="resource_owner")
    assert any(c["challenge_id"] == challenge_id for c in queue["pending"])

    resolved = _run(challenge_tool, op="resolve", challenge_id=challenge_id,
                    approved=True, by="owner-alice")
    assert resolved["state"] == "approved"

    resub = _run(guardrail_tool, op="submit_check", run_id=run_id,
                 check_id="perm", outcome="PASS",
                 evidence_ref=f"challenge:{challenge_id}")
    assert resub["success"] is True and resub["verdict"] == "PASS"
    _run(guardrail_tool, op="finalize", run_id=run_id, seal_ref="seal://t6/007")
    gate = _run(guardrail_tool, op="gate", run_id=run_id)
    assert gate["verdict"] == "PASS" and gate["executable"] is True


def test_denied_challenge_keeps_gate_closed(guardrail_tool, challenge_tool):
    made = _ask_run(guardrail_tool)
    run_id = made["run_id"]
    sub = _run(guardrail_tool, op="submit_check", run_id=run_id,
               check_id="perm", outcome="ASK")
    res = _run(challenge_tool, op="resolve", challenge_id=sub["challenge_id"],
               approved=False, by="owner-bob")
    assert res["state"] == "denied"
    # 拒绝后不重新提交 → 必填 check 无 PASS 结论 → gate 不放行
    _run(guardrail_tool, op="finalize", run_id=run_id, seal_ref="seal://t6/008")
    gate = _run(guardrail_tool, op="gate", run_id=run_id)
    assert gate["executable"] is False


def test_challenge_state_and_ask_payload(challenge_tool):
    opened = _run(challenge_tool, op="open", who_confirms="user",
                  resource="repo/x", action="force_push", method="console.ask",
                  ttl_seconds=60)
    assert opened["success"] is True and opened["state"] == "pending"
    st = _run(challenge_tool, op="state_of", challenge_id=opened["challenge_id"])
    assert st["state"] == "pending"
    payload = _run(challenge_tool, op="ask_payload",
                   challenge_id=opened["challenge_id"])["ask_payload"]
    assert payload["agent"] == "" and payload["action"] == "force_push"


def test_bad_inputs_surface_as_errors(guardrail_tool, challenge_tool):
    assert _run(guardrail_tool, op="nope")["success"] is False
    assert asyncio.run(guardrail_gate_tool_invoke_bad(guardrail_tool))["success"] is False
    assert _run(guardrail_tool, op="create_run", action="a",
                resource="r")["success"] is False  # 缺 agent_identity_ref
    # ASK 只允许 permission_rail check 提交（glue 拒绝，工具透传错误）
    made = _create_run(guardrail_tool)
    res = _run(guardrail_tool, op="submit_check", run_id=made["run_id"],
               check_id="c0", outcome="ASK")
    assert res["success"] is False
    assert res["error_type"] == "GuardrailSchemaError"
    # 过期 Challenge 拒绝裁决（fail-closed）
    opened = _run(challenge_tool, op="open", who_confirms="user",
                  resource="r", action="a", method="console.ask", ttl_seconds=-1)
    assert opened["success"] is False  # glue 拒绝无有效期的 Challenge


async def guardrail_gate_tool_invoke_bad(tool):
    return await tool.invoke("not-a-dict")


# ---------------------------------------------------------------------------
# DecisionLogTool：append-only 决策账本
# ---------------------------------------------------------------------------

def test_decision_append_get_list(decision_tool):
    ctx = {"action": "deploy.config", "resource": "release/batch-42", "seq": 1}
    rec = _run(decision_tool, op="append",
               agent_ref="org:cloudcrane/agent:deployer/instance:i1",
               context=ctx,
               options=["proceed", "hold", "rollback"],
               chosen="proceed",
               rationale_ref="evidence://eval/9901")
    assert rec["success"] is True and rec["chosen"] == "proceed"
    assert len(rec["context_hash"]) == 64  # sha256 hex；原文不落账

    got = _run(decision_tool, op="get", decision_id=rec["decision_id"])
    assert got["success"] is True
    assert got["context_hash"] == rec["context_hash"]
    assert "context" not in got  # 原文确实不在账面

    by_agent = _run(decision_tool, op="list",
                    agent_ref="org:cloudcrane/agent:deployer/instance:i1")
    assert any(r["decision_id"] == rec["decision_id"] for r in by_agent["records"])
    by_ctx = _run(decision_tool, op="list", context=ctx)
    assert any(r["decision_id"] == rec["decision_id"] for r in by_ctx["records"])
    everything = _run(decision_tool, op="list")
    assert everything["count"] >= len(by_agent["records"])


def test_decision_chosen_must_be_in_options(decision_tool):
    res = _run(decision_tool, op="append",
               agent_ref="org:cloudcrane/agent:x/instance:i",
               context={"k": "v"}, options=["a", "b"], chosen="c",
               rationale_ref="evidence://x")
    assert res["success"] is False
    assert res["error_type"] == "DecisionSchemaError"


def test_decision_requires_rationale(decision_tool):
    res = _run(decision_tool, op="append",
               agent_ref="org:cloudcrane/agent:x/instance:i",
               context={"k": "v"}, options=["a"], chosen="a",
               rationale_ref="")
    assert res["success"] is False


# ---------------------------------------------------------------------------
# 包结构自检（与官方校验器同向的轻量断言；完整 9 项见 validate_sample_package.py）
# ---------------------------------------------------------------------------

def test_harness_config_declares_the_three_tools():
    # pyyaml 仅本测试解析用（运行时零依赖不变）；CI 裸 pytest 环境无 pyyaml 时
    # 跳过而非失败（W-05 修复：anolis-23 环节5 自 glue main 6f4674c 起红）。
    yaml = pytest.importorskip("yaml")

    cfg = yaml.safe_load(
        (REPO_ROOT / "runtime_extensions" / "glue-governance"
         / "harness_config.yaml").read_text(encoding="utf-8"))
    assert cfg["schema_version"] == "expert_harness.v1"
    assert "mcps" not in cfg
    tools = {item["class"]: item["file"] for item in cfg["tools"]}
    assert tools == {
        "GuardrailGateTool": "tools/guardrail_gate_tool.py",
        "ChallengeBoardTool": "tools/challenge_board_tool.py",
        "DecisionLogTool": "tools/decision_log_tool.py",
    }
    for path in tools.values():
        assert (REPO_ROOT / "runtime_extensions" / "glue-governance" / path).is_file()


def test_package_does_not_copy_glue_source():
    """包内只允许 import 引用 jiuwen_glue——不许出现其源码正文（薄封装红线）。"""
    import re

    pkg = REPO_ROOT / "runtime_extensions" / "glue-governance"
    for py in pkg.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert not re.search(r"^class GuardrailRunStore\b", text, re.M)
        assert not re.search(r"^class ChallengeBoard\b", text, re.M)
        assert not re.search(r"^class DecisionLog\b", text, re.M)
        assert not re.search(r"^def aggregate\(", text, re.M)
