# coding: utf-8
"""jiuwen-e2b-compat：openJiuwen SandboxRegistry 的 E2B 兼容 Provider（PROP-0008）。

- 零强制第三方依赖：e2b 为可选 extra（``pip install jiuwen-e2b-compat[e2b]``）；
- openjiuwen 亦为可选绑定（``e2b_compat.OPENJIUWEN_AVAILABLE`` 如实反映）；
- 入口：
  - ``E2BCompatProvider``：使用方门面（fs/shell/code + connect/snapshot_scope/kill）；
  - ``E2BCompatFSProvider / E2BCompatShellProvider / E2BCompatCodeProvider``：
    注册进 SandboxRegistry 的三件套（与 aio/jiuwenbox/yuanrong 并列）；
  - ``registry_patch.register_e2b_compat_provider()``：注册路径（未装 openjiwen 时
    抛带指引的 ImportError）。
"""
from ._shims import OPENJIUWEN_AVAILABLE, SandboxEndpoint

from .config import E2BCompatConfig
from .errors import (
    E2BCompatCredentialError,
    E2BCompatDependencyError,
    E2BCompatError,
)
from .provider import (
    E2B_SANDBOX_TYPE,
    E2BCompatCodeProvider,
    E2BCompatFSProvider,
    E2BCompatProvider,
    E2BCompatShellProvider,
    SnapshotScope,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "OPENJIUWEN_AVAILABLE",
    "E2B_SANDBOX_TYPE",
    "E2BCompatProvider",
    "E2BCompatFSProvider",
    "E2BCompatShellProvider",
    "E2BCompatCodeProvider",
    "SnapshotScope",
    "E2BCompatConfig",
    "SandboxEndpoint",
    "E2BCompatError",
    "E2BCompatDependencyError",
    "E2BCompatCredentialError",
]
