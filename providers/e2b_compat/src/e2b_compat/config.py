# coding: utf-8
"""e2b_compat 配置：凭据只从环境变量 / 注入引用读取，域名 / 超时 / 模板 id 可配置。

凭据红线（PROP-0001 v1.7 §12.8 "执行面零长期密钥" + EXECUTION-PROTOCOL.md 红线 3）：
- 代码里绝不出现默认 API key；
- api_key 仅接受两种来源：显式注入引用（内存值，例如经 OpenBao/JIT 下发的引用）或
  环境变量 ``E2B_API_KEY``；
- 错误消息只含环境变量 *名*，不含 key 值；repr 也不含（dataclass field(repr=False)）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

ENV_API_KEY = "E2B_API_KEY"
ENV_DOMAIN = "E2B_DOMAIN"
ENV_TEMPLATE_ID = "E2B_COMPAT_TEMPLATE_ID"
ENV_REQUEST_TIMEOUT = "E2B_COMPAT_REQUEST_TIMEOUT"
ENV_SANDBOX_TIMEOUT = "E2B_COMPAT_SANDBOX_TIMEOUT"
ENV_ALLOW_CREATE = "E2B_COMPAT_ALLOW_CREATE"
ENV_PROTECTED_PATHS = "E2B_COMPAT_PROTECTED_PATHS"
ENV_EPHEMERAL_PATHS = "E2B_COMPAT_EPHEMERAL_PATHS"

DEFAULT_REQUEST_TIMEOUT = 60
DEFAULT_SANDBOX_TIMEOUT = 300

_TRUTHY = {"1", "true", "yes", "on"}


def _split_paths(raw: str) -> Tuple[str, ...]:
    """冒号分隔的路径列表 → 元组（跳过空段）。"""
    return tuple(p for p in (part.strip() for part in raw.split(":")) if p)


def _int_value(raw: str, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class E2BCompatConfig:
    """e2b_compat 运行配置。

    属性说明：
    - ``api_key``：显式注入引用（内存值）。repr 不显示（凭据红线）。
    - ``api_key_env``：环境变量名（默认 ``E2B_API_KEY``），只存名字不存值。
    - ``domain``：E2B API 域名（自托管 / 私有部署场景；对应 e2b SDK 的 domain 参数）。
    - ``template_id``：沙箱模板 id（仅在显式 allow_create 时不传则用 E2B 默认模板）。
    - ``request_timeout``：本地等待 E2B 流式回调的逐块超时（秒）。
    - ``sandbox_timeout``：E2B 沙箱生命周期超时（秒），仅在 opt-in 创建时传给 SDK。
    - ``allow_create``：默认 False —— e2b_compat 只连接"已启动"的 E2B 沙箱
      （对齐 openJiuwen PreDeploymentLauncher 语义，repos/openjiuwen/
      agent-core/openjiuwen/core/sys_operation/sandbox/launchers/
      pre_deployment_launcher.py:7-14）；按 sandbox_id 连接见 provider.get_client。
      显式置真后才允许按模板即时创建（12.8 接缝 3"高频创建销毁"的实测入口，
      真实创建行为 [待云沙箱实测]）。
    - ``protected_paths`` / ``ephemeral_paths``：快照边界声明（12.8 接缝 1），
      见 provider.SnapshotScope 与 AgenticFS 契约注释。
    """

    api_key: Optional[str] = field(default=None, repr=False)
    api_key_env: str = ENV_API_KEY
    domain: Optional[str] = None
    template_id: Optional[str] = None
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT
    sandbox_timeout: int = DEFAULT_SANDBOX_TIMEOUT
    allow_create: bool = False
    protected_paths: Tuple[str, ...] = ("/workspace",)
    ephemeral_paths: Tuple[str, ...] = ("/tmp", "/var/tmp", "/dev/shm")

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None, **overrides) -> "E2BCompatConfig":
        """从环境变量构建配置；显式 overrides 优先。

        本方法在"第一次真正需要 e2b 客户端"时才被调用（惰性），因此测试可以在
        实例化 Provider 之后、调用操作之前用 monkeypatch.setenv 改环境。
        """
        env = os.environ if environ is None else environ
        kwargs = {}
        if env.get(ENV_DOMAIN):
            kwargs["domain"] = env[ENV_DOMAIN]
        if env.get(ENV_TEMPLATE_ID):
            kwargs["template_id"] = env[ENV_TEMPLATE_ID]
        if env.get(ENV_REQUEST_TIMEOUT):
            kwargs["request_timeout"] = _int_value(env[ENV_REQUEST_TIMEOUT], DEFAULT_REQUEST_TIMEOUT)
        if env.get(ENV_SANDBOX_TIMEOUT):
            kwargs["sandbox_timeout"] = _int_value(env[ENV_SANDBOX_TIMEOUT], DEFAULT_SANDBOX_TIMEOUT)
        if env.get(ENV_ALLOW_CREATE) is not None:
            kwargs["allow_create"] = env[ENV_ALLOW_CREATE].strip().lower() in _TRUTHY
        if env.get(ENV_PROTECTED_PATHS) is not None:
            kwargs["protected_paths"] = _split_paths(env[ENV_PROTECTED_PATHS])
        if env.get(ENV_EPHEMERAL_PATHS) is not None:
            kwargs["ephemeral_paths"] = _split_paths(env[ENV_EPHEMERAL_PATHS])
        kwargs.update(overrides)
        return cls(**kwargs)

    def resolve_api_key(self) -> Optional[str]:
        """解析 API key：显式注入引用优先，其次环境变量。

        返回 None 表示未配置——调用方必须报清晰错误（含环境变量名），绝不使用默认值。
        """
        if self.api_key is not None:
            return self.api_key
        return os.environ.get(self.api_key_env)
