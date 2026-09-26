# coding: utf-8
"""快照边界声明测试（PROP-0001 v1.7 §12.8 接缝 1：ws-ckpt vs 容器卷快照边界）。"""
from __future__ import annotations

import pytest

from e2b_compat.provider import SnapshotScope


def test_default_scope_declares_bind_mount_boundary(fallback_stack, monkeypatch):
    monkeypatch.delenv("E2B_COMPAT_PROTECTED_PATHS", raising=False)
    monkeypatch.delenv("E2B_COMPAT_EPHEMERAL_PATHS", raising=False)
    facade = fallback_stack.provider.E2BCompatProvider(
        fallback_stack.make_endpoint(fallback_stack, sandbox_id="sbx-1")
    )
    scope = facade.snapshot_scope()
    assert scope.protected_paths == ("/workspace",)  # 受保护工作区 bind mount 范围
    assert scope.ephemeral_paths == ("/tmp", "/var/tmp", "/dev/shm")  # 容器临时态
    assert scope.mount_kind == "bind"
    assert scope.container_temp_state_promised is False  # 临时态永远不进承诺


def test_scope_note_states_agenticfs_contract(fallback_stack):
    facade = fallback_stack.provider.E2BCompatProvider(
        fallback_stack.make_endpoint(fallback_stack, sandbox_id="sbx-1")
    )
    note = facade.snapshot_scope().note
    assert "AgenticFS" in note
    assert "不进承诺" in note  # 契约：容器临时态不进快照承诺


def test_scope_env_override_parsed_from_colon_list(fallback_stack, monkeypatch):
    monkeypatch.setenv("E2B_COMPAT_PROTECTED_PATHS", "/srv/ws:/mnt/shared")
    monkeypatch.setenv("E2B_COMPAT_EPHEMERAL_PATHS", "/tmp:/var/tmp")
    facade = fallback_stack.provider.E2BCompatProvider(
        fallback_stack.make_endpoint(fallback_stack, sandbox_id="sbx-1")
    )
    scope = facade.snapshot_scope()
    assert scope.protected_paths == ("/srv/ws", "/mnt/shared")
    assert scope.ephemeral_paths == ("/tmp", "/var/tmp")


def test_scope_rejects_protected_ephemeral_overlap():
    with pytest.raises(ValueError, match="overlap"):
        SnapshotScope(protected_paths=("/ws",), ephemeral_paths=("/ws", "/tmp"))


def test_all_providers_and_facade_declare_same_scope(fallback_stack):
    endpoint = fallback_stack.make_endpoint(fallback_stack, sandbox_id="sbx-1")
    fs_scope = fallback_stack.provider.E2BCompatFSProvider(endpoint).snapshot_scope()
    shell_scope = fallback_stack.provider.E2BCompatShellProvider(endpoint).snapshot_scope()
    code_scope = fallback_stack.provider.E2BCompatCodeProvider(endpoint).snapshot_scope()
    facade_scope = fallback_stack.provider.E2BCompatProvider(endpoint).snapshot_scope()
    assert fs_scope == shell_scope == code_scope == facade_scope
