# coding: utf-8
"""降级与凭据测试：e2b 缺失优雅降级 / 凭据只走环境变量或注入引用 / 零默认 key。"""
from __future__ import annotations

import asyncio

import pytest


def test_import_and_instantiation_without_e2b_and_openjiuwen(fallback_stack):
    """e2b 与 openjiwen 都缺失：可导入、可实例化（可注册），快照声明也可用。"""
    assert fallback_stack.shims.OPENJIUWEN_AVAILABLE is False
    endpoint = fallback_stack.make_endpoint(fallback_stack, sandbox_id="sbx-1")
    facade = fallback_stack.provider.E2BCompatProvider(endpoint)
    assert facade.sandbox_id == "sbx-1"
    assert facade.snapshot_scope().protected_paths == ("/workspace",)


def test_operation_without_e2b_gives_install_hint(fallback_stack, env_api_key):
    """e2b 缺失时操作报清晰错误（带安装指引），而不是 ImportError 裸栈。"""
    provider = fallback_stack.provider.E2BCompatFSProvider(
        endpoint=fallback_stack.make_endpoint(fallback_stack, sandbox_id="sbx-1"), config=None
    )
    with pytest.raises(
        fallback_stack.errors.E2BCompatDependencyError,
        match=r"pip install 'jiuwen-e2b-compat\[e2b\]'",
    ):
        asyncio.run(provider.read_file("/ws/a.txt"))


def test_missing_api_key_names_env_var_not_value(fallback_stack, fake_e2b, monkeypatch):
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    provider = fallback_stack.provider.E2BCompatShellProvider(
        endpoint=fallback_stack.make_endpoint(fallback_stack, sandbox_id="sbx-1"), config=None
    )
    with pytest.raises(fallback_stack.errors.E2BCompatCredentialError) as exc_info:
        asyncio.run(provider.execute_cmd("ls"))
    message = str(exc_info.value)
    assert "E2B_API_KEY" in message  # 指明环境变量名
    assert "test-only-key" not in message  # 也不会冒出任何 key 值


def test_injected_api_key_reference_is_used_and_never_leaks(fallback_stack, fake_e2b):
    """注入引用路径：api_key 只在内存里传给 SDK；repr/错误信息零泄漏。"""
    secret_ref = "injected-reference-value-should-not-leak"
    config = fallback_stack.config.E2BCompatConfig(api_key=secret_ref)
    assert secret_ref not in repr(config)  # dataclass repr 屏蔽凭据字段
    provider = fallback_stack.provider.E2BCompatFSProvider(
        endpoint=fallback_stack.make_endpoint(fallback_stack, sandbox_id="sbx-inject"),
        config=_ConfigHolder(config),
    )
    assert secret_ref not in repr(provider)
    client = asyncio.run(asyncio.to_thread(provider.get_client))
    connect_call = fake_e2b.connect_calls[-1]
    assert connect_call["sandbox_id"] == "sbx-inject"
    assert connect_call["api_key"] == secret_ref  # 注入引用直达 SDK
    assert client is provider.get_client()  # 客户端缓存


class _ConfigHolder:
    """模拟网关 config 携带 e2b_compat_config 注入引用（见 provider._resolve_e2b_config）。"""

    def __init__(self, e2b_config) -> None:
        self.e2b_compat_config = e2b_config


def test_no_sandbox_id_requires_explicit_opt_in(fallback_stack, fake_e2b, env_api_key):
    """默认只连接"已启动"沙箱（PreDeploymentLauncher 语义）；创建必须显式 opt-in。"""
    provider = fallback_stack.provider.E2BCompatFSProvider(
        endpoint=fallback_stack.make_endpoint(fallback_stack, sandbox_id=None), config=None
    )
    with pytest.raises(fallback_stack.errors.E2BCompatError, match="sandbox_id is required"):
        asyncio.run(asyncio.to_thread(provider.get_client))
    assert fake_e2b.create_calls == []  # 未偷跑创建


def test_opt_in_create_passes_template_and_timeout(fallback_stack, fake_e2b):
    config = fallback_stack.config.E2BCompatConfig(
        api_key="test-only-key", allow_create=True, template_id="tpl-e2b-compat-01", sandbox_timeout=77
    )
    provider = fallback_stack.provider.E2BCompatFSProvider(
        endpoint=fallback_stack.make_endpoint(fallback_stack, sandbox_id=None),
        config=_ConfigHolder(config),
    )
    client = asyncio.run(asyncio.to_thread(provider.get_client))
    create_call = fake_e2b.create_calls[-1]
    assert create_call["template"] == "tpl-e2b-compat-01"
    assert create_call["timeout"] == 77
    assert create_call["api_key"] == "test-only-key"
    assert client is not None


def test_connect_by_id_passes_domain_from_env(fallback_stack, fake_e2b, monkeypatch):
    """connect/reconnect 按 E2B sandbox_id；domain 自环境变量读取。"""
    monkeypatch.setenv("E2B_DOMAIN", "e2b.corp.example.invalid")
    monkeypatch.setenv("E2B_API_KEY", "test-only-key")
    provider = fallback_stack.provider.E2BCompatFSProvider(
        endpoint=fallback_stack.make_endpoint(fallback_stack, sandbox_id="sbx-42"), config=None
    )
    asyncio.run(asyncio.to_thread(provider.get_client))
    connect_call = fake_e2b.connect_calls[-1]
    assert connect_call == {
        "sandbox_id": "sbx-42",
        "api_key": "test-only-key",
        "domain": "e2b.corp.example.invalid",
    }
