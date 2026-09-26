# coding: utf-8
"""注册路径测试：e2b_compat 三件套注册进 openJiuwen SandboxRegistry（假 openjiuwen）。"""
from __future__ import annotations

import pytest


def test_register_without_openjiuwen_raises_with_guidance(fallback_stack):
    rp = fallback_stack.registry_patch
    assert rp.openjiuwen_available() is False
    with pytest.raises(ImportError, match="openjiuwen is not importable"):
        rp.register_e2b_compat_provider()


def test_registration_status_reports_missing_openjiuwen_honestly(fallback_stack):
    status = fallback_stack.registry_patch.registration_status()
    assert status["openjiuwen"] is False
    assert status["registered"] == {}
    assert "Install openJiuwen agent-core" in status["reason"]  # 指引，不假装注册成功


def test_register_binds_three_operations_alongside_builtin_shape(fake_openjiuwen):
    rp = fake_openjiuwen.registry_patch
    assert rp.openjiuwen_available() is True
    registered = rp.register_e2b_compat_provider()
    assert registered["sandbox_type"] == "e2b_compat"
    assert set(registered["operations"]) == {"fs", "shell", "code"}
    registry = fake_openjiuwen.fake.registry
    # 与 aio/jiuwenbox/yuanrong 相同的注册位（sandbox_type, operation_type）→ provider_cls
    assert registry.get_provider_cls("e2b_compat", "fs") is fake_openjiuwen.provider.E2BCompatFSProvider
    assert registry.get_provider_cls("e2b_compat", "shell") is fake_openjiuwen.provider.E2BCompatShellProvider
    assert registry.get_provider_cls("e2b_compat", "code") is fake_openjiuwen.provider.E2BCompatCodeProvider


def test_gateway_create_provider_contract_instantiates(fake_openjiuwen):
    """网关路径：SandboxRegistry.create_provider(endpoint=..., config=...) 可实例化
    （对齐真实 sandbox_registry.py:62-72 的调用约定）。"""
    fake_openjiuwen.registry_patch.register_e2b_compat_provider()
    registry = fake_openjiuwen.fake.registry
    endpoint = fake_openjiuwen.fake.endpoint(base_url="https://e2b.example.invalid", sandbox_id="sbx-1")
    for operation, expected_cls in (
        ("fs", fake_openjiuwen.provider.E2BCompatFSProvider),
        ("shell", fake_openjiuwen.provider.E2BCompatShellProvider),
        ("code", fake_openjiuwen.provider.E2BCompatCodeProvider),
    ):
        instance = registry.create_provider("e2b_compat", operation, endpoint=endpoint, config=None)
        assert isinstance(instance, expected_cls)
        assert instance.endpoint is endpoint


def test_registration_status_after_register_and_unregister(fake_openjiuwen):
    rp = fake_openjiuwen.registry_patch
    rp.register_e2b_compat_provider()
    status = rp.registration_status()
    assert all(status["registered"][op] is not None for op in ("fs", "shell", "code"))
    before = rp.unregister_e2b_compat_provider()
    assert before == {"fs": True, "shell": True, "code": True}
    status = rp.registration_status()
    assert all(status["registered"][op] is None for op in ("fs", "shell", "code"))


def test_unregister_without_openjiuwen_is_noop(fallback_stack):
    assert fallback_stack.registry_patch.unregister_e2b_compat_provider() == {}
