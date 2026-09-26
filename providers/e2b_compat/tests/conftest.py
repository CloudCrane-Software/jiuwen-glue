# coding: utf-8
"""e2b_compat 测试夹具：全新导入 + openjiuwen/e2b 双假体注入，全程不装真包、不联网。

三种模式（对应三个 fixture）：
- ``fallback_stack``：openjiuwen 与 e2b 都不可导入 → 走零依赖降级路径；
- ``fake_openjiuwen``：注入假 openjiuwen（接口形状复刻）→ 测注册路径；
- ``fake_e2b``：注入假 e2b SDK → 测语义映射（可与 fallback_stack 叠加）。

每个 fixture 都先清 sys.modules 再全新导入 e2b_compat，保证 _shims 的绑定
在"openjiuwen 在/不在"两种状态下正确生效；结束后再次清理，避免污染同会话其它测试。
"""
from __future__ import annotations

import sys
import types

import pytest

_PURGE_PREFIXES = ("openjiuwen", "e2b", "e2b_compat")


def _purge() -> None:
    for name in list(sys.modules):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in _PURGE_PREFIXES):
            del sys.modules[name]


def _fresh_import() -> types.SimpleNamespace:
    """全新导入 e2b_compat 全链路（在当前 sys.modules 状态下解析绑定）。"""
    import e2b_compat
    from e2b_compat import _shims
    from e2b_compat import config as config_mod
    from e2b_compat import errors as errors_mod
    from e2b_compat import provider as provider_mod
    from e2b_compat import registry_patch as registry_mod

    return types.SimpleNamespace(
        e2b_compat=e2b_compat,
        shims=_shims,
        config=config_mod,
        errors=errors_mod,
        provider=provider_mod,
        registry_patch=registry_mod,
    )


def _make_endpoint(ns: types.SimpleNamespace, sandbox_id=None):
    return ns.shims.SandboxEndpoint(
        base_url="https://e2b.example.invalid",
        sandbox_id=sandbox_id,
    )


@pytest.fixture
def fallback_stack():
    """openjiuwen / e2b 双缺失：零依赖降级路径。"""
    _purge()
    ns = _fresh_import()
    ns.make_endpoint = _make_endpoint
    yield ns
    _purge()


@pytest.fixture
def fake_openjiuwen():
    """假 openjiuwen 在位：注册路径测试（provider 类会经装饰器注册进假注册表）。"""
    from _fake_openjiuwen import install_fake_openjiuwen, uninstall_fake_openjiuwen

    _purge()
    fake = install_fake_openjiuwen()
    ns = _fresh_import()
    ns.fake = fake
    ns.make_endpoint = _make_endpoint
    yield ns
    uninstall_fake_openjiuwen()
    _purge()


@pytest.fixture
def fake_e2b():
    """假 e2b SDK 在位：语义映射测试（e2b 为惰性导入，无需重载 e2b_compat）。"""
    from _fake_e2b import install_fake_e2b, uninstall_fake_e2b

    controller = install_fake_e2b()
    yield controller
    uninstall_fake_e2b()


@pytest.fixture
def env_api_key(monkeypatch):
    """给执行面注入测试用 API key（假值，仅测管道，不碰真实凭据）。"""
    monkeypatch.setenv("E2B_API_KEY", "test-only-key-not-a-real-secret")
    return "test-only-key-not-a-real-secret"
