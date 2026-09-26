# coding: utf-8
"""e2b_compat → openJiuwen SandboxRegistry 的注册路径（"写完即成为第三 backend"）。

openJiuwen 的扩展点：``SandboxRegistry.register_provider(sandbox_type, operation_type,
provider_cls)``（repos/openjiuwen/agent-core/openjiuwen/core/sys_operation/sandbox/
sandbox_registry.py:45-48），网关按 ``provider_cls(endpoint=..., config=...)`` 实例化
（sandbox_registry.py:62-72）。本模块把 E2BCompat 三件套注册为 sandbox_type
``"e2b_compat"``，与内置 aio / jiuwenbox / yuanrong 并列（aio.py:199/814/960 等）。

对 openjiuwen 的 import 全部包在 try/except：未安装 openjiuwen 时
``register_e2b_compat_provider`` 抛带指引的 ImportError，``registration_status``
如实报告（不假装注册成功）。Provider 类本身不依赖 openjiwen（见 _shims.py），
因此"pip install jiuwen-e2b-compat[e2b]"后即使没有 openjiwen 也可独立使用门面。
"""
from __future__ import annotations

import importlib.util
import sys
from types import ModuleType
from typing import Any, Dict, Type

from .provider import (
    E2B_SANDBOX_TYPE,
    E2BCompatCodeProvider,
    E2BCompatFSProvider,
    E2BCompatShellProvider,
)

OPERATIONS = ("fs", "shell", "code")

_REGISTER_GUIDANCE = (
    "openjiuwen is not importable in this environment; the e2b_compat provider registers "
    "into openJiuwen agent-core's SandboxRegistry (openjiuwen/core/sys_operation/sandbox/"
    "sandbox_registry.py). Install openJiuwen agent-core (or make it importable on "
    "sys.path) and call register_e2b_compat_provider() again. Note: the provider classes "
    "themselves import fine without openjiwen (graceful fallback in e2b_compat._shims), "
    "and the E2BCompatProvider facade is usable standalone."
)


def openjiuwen_available() -> bool:
    """openjiuwen 是否可导入（先查 sys.modules 以兼容测试注入的假模块）。"""
    if "openjiuwen" in sys.modules:
        return True
    try:
        return importlib.util.find_spec("openjiuwen") is not None
    except (ImportError, ValueError):
        return False


def _load_registry_module() -> ModuleType:
    from openjiuwen.core.sys_operation.sandbox import sandbox_registry  # noqa: PLC0415

    return sandbox_registry


def provider_classes() -> Dict[str, Type]:
    """返回注册单元：operation → Provider 类（与 aio.py 三件套同构）。"""
    return {
        "fs": E2BCompatFSProvider,
        "shell": E2BCompatShellProvider,
        "code": E2BCompatCodeProvider,
    }


def register_e2b_compat_provider(sandbox_type: str = E2B_SANDBOX_TYPE) -> Dict[str, Any]:
    """把 e2b_compat 三件套注册进 openJiuwen SandboxRegistry。

    返回 {"sandbox_type": ..., "operations": {op: provider_cls}}；幂等（重复注册
    只是字典覆盖，与 SandboxRegistry.register_provider 语义一致）。

    Raises:
        ImportError: openjiuwen 不可导入时（附安装指引，不静默降级）。
    """
    if not openjiuwen_available():
        raise ImportError(_REGISTER_GUIDANCE)
    registry_module = _load_registry_module()
    classes = provider_classes()
    for operation, provider_cls in classes.items():
        registry_module.SandboxRegistry.register_provider(sandbox_type, operation, provider_cls)
    return {"sandbox_type": sandbox_type, "operations": dict(classes)}


def unregister_e2b_compat_provider(sandbox_type: str = E2B_SANDBOX_TYPE) -> Dict[str, bool]:
    """注销 e2b_compat 三件套（测试与卸载用）。返回各 operation 注销前的注册状态。"""
    if not openjiuwen_available():
        return {}
    registry_module = _load_registry_module()
    before: Dict[str, bool] = {}
    for operation in OPERATIONS:
        before[operation] = (
            registry_module.SandboxRegistry.get_provider_cls(sandbox_type, operation) is not None
        )
        registry_module.SandboxRegistry.unregister_provider(sandbox_type, operation)
    return before


def registration_status(sandbox_type: str = E2B_SANDBOX_TYPE) -> Dict[str, Any]:
    """如实报告注册状态（运维/自检用；openjiuwen 缺失时报告原因而非假装成功）。"""
    if not openjiuwen_available():
        return {
            "openjiuwen": False,
            "sandbox_type": sandbox_type,
            "registered": {},
            "reason": _REGISTER_GUIDANCE,
        }
    registry_module = _load_registry_module()
    return {
        "openjiuwen": True,
        "sandbox_type": sandbox_type,
        "registered": {
            operation: registry_module.SandboxRegistry.get_provider_cls(sandbox_type, operation)
            for operation in OPERATIONS
        },
    }
