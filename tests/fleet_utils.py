# coding: utf-8
"""fleet 测试共享工具：假 JWT 生成 + 节点/注册构造器（仅新增，不改既有测试）。"""
from __future__ import annotations

import base64
import json

from jiuwen_glue import TRUST_TRUSTED, NodeCapacity
from jiuwen_glue.fleet import NodeAttestation, NodeRegistration

DEFAULT_T = 1_700_000_000.0   # 与 conftest.FakeClock 起点一致


def _b64url(obj) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_jwt(claims: dict, *, segments: int = 3,
             payload_text: str = None) -> str:
    """构造假 JWT（无签名语义——协议层不验签，签名验证归真实 OpenBao）。

    segments=2 模拟格式残缺；payload_text 直接替换 payload 段模拟不可解码。
    """
    payload = payload_text if payload_text is not None else _b64url(claims)
    return ".".join(["hdr", payload, "sig"][:segments])


def make_attestation(claims: dict, *, jwt_ref: str,
                     now: float = DEFAULT_T) -> NodeAttestation:
    """claims 原样进 JWT——测试对缺失/过期/不一致字段有完全控制。"""
    return NodeAttestation.from_jwt(make_jwt(claims), jwt_ref=jwt_ref, now=now)


def make_registration(node_id: str = "node-1", *, now: float = DEFAULT_T,
                      trust_level: str = TRUST_TRUSTED, gpu_frac: float = 0.5,
                      cpu_frac: float = 1.0, tools: tuple = ("shell",),
                      max_parallel: int = 2, online_window: str = "always",
                      heartbeat_ttl_seconds: float = 300.0,
                      claims: dict = None, agent_ref: str = None,
                      tenant_id: str = "t0") -> NodeRegistration:
    """一条完整注册声明：NodeCapacity 复用 + attestation（协议级校验入口）。"""
    att_claims = {"sub": node_id, "exp": int(now + 3600), "iat": int(now)}
    att_claims.update(claims or {})
    att = NodeAttestation.from_jwt(make_jwt(att_claims),
                                   jwt_ref=f"bao://audit/jwt/{node_id}",
                                   now=now)
    return NodeRegistration(
        node_id=node_id,
        capacity=NodeCapacity(node_id=node_id, cpu_frac=cpu_frac,
                              gpu_frac=gpu_frac, tools=tuple(tools),
                              trust_level=trust_level,
                              max_parallel=max_parallel,
                              online_window=online_window, tenant_id=tenant_id),
        attestation=att,
        heartbeat_ttl_seconds=heartbeat_ttl_seconds,
        agent_ref=agent_ref,
    )
