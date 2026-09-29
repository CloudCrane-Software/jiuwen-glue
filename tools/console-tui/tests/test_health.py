# coding: utf-8
"""服务健康探针（线E动作2）测试：状态归一/目标解析/env 门控/fail-open/真实 ssh 降级."""
from __future__ import annotations

import shutil

import pytest

from console_tui.health import (DEFAULT_TARGET, PROBE_ENV, SshServiceProbe,
                                STATE_UNREACHABLE, demo_rows, parse_probe_output,
                                parse_targets, unreachable_rows)

NOW = 1_800_000_000.0
SERVICES = ("jiuwenswarm-app", "vllm-local")


# ── 纯解析 ───────────────────────────────────────────────────────────────────

def test_parse_probe_output_maps_systemd_readings_in_order():
    stdout = "anolis-gpu-01\nactive\ninactive\n"
    rows = parse_probe_output(stdout, "t@h", SERVICES, at=NOW)
    assert [r.service for r in rows] == list(SERVICES)
    assert rows[0].state == "active" and rows[0].host == "anolis-gpu-01"
    assert rows[1].state == "stopped"


def test_parse_probe_output_normalizes_vocabulary():
    stdout = "h\nactivating\nfailed\nmaintenance\n"
    rows = parse_probe_output(stdout, "t@h", ("a", "b", "c"), at=NOW)
    assert [r.state for r in rows] == ["starting", "failed", "unknown"]


def test_parse_probe_output_missing_readings_become_unknown_not_active():
    rows = parse_probe_output("h\nactive\n", "t@h", SERVICES, at=NOW)
    assert rows[1].state == "unknown" and rows[1].detail == "no reading"


# ── 目标解析与 env 门控 ──────────────────────────────────────────────────────

def test_parse_targets_splits_hosts_and_units():
    targets = parse_targets("u@h1:a,b; u@h2:c")
    assert [(t.host, t.services) for t in targets] == \
        [("u@h1", ("a", "b")), ("u@h2", ("c",))]
    assert parse_targets("no-colon;;") == []


def test_probe_disabled_without_env(monkeypatch):
    monkeypatch.delenv(PROBE_ENV, raising=False)
    assert SshServiceProbe.from_env() is None            # 默认关闭：测试/演示零子进程


def test_probe_enabled_by_env_with_default_gpu_target(monkeypatch):
    monkeypatch.setenv(PROBE_ENV, DEFAULT_TARGET)
    probe = SshServiceProbe.from_env()
    assert probe is not None
    assert probe.targets[0].host == "anuser@100.64.0.7"
    assert probe.targets[0].services == ("jiuwenswarm-app", "vllm-local")
    monkeypatch.setenv(PROBE_ENV, "")
    assert SshServiceProbe.from_env() is None            # 空串 = 显式关闭


# ── fail-open 展示 ───────────────────────────────────────────────────────────

def test_unreachable_rows_carry_reason_and_never_fake_green():
    rows = unreachable_rows("h", SERVICES, "connect refused", at=NOW)
    assert all(r.state == STATE_UNREACHABLE for r in rows)
    assert all(r.detail == "connect refused" for r in rows)


def test_probe_cache_ttl_avoids_refetch():
    calls = []

    class FlakyProbe(SshServiceProbe):
        def _probe_one(self, target):
            calls.append(target.host)
            return unreachable_rows(target.host, target.services, "x", at=NOW)

    p = FlakyProbe(parse_targets(DEFAULT_TARGET))
    p.probe(now=NOW)
    p.probe(now=NOW + 30)                                # TTL 内 → 缓存
    assert len(calls) == 1
    p.probe(now=NOW + 61, force=True)                    # force → 重探
    assert len(calls) == 2


@pytest.mark.skipif(shutil.which("ssh") is None, reason="本机无 ssh 客户端")
def test_real_probe_to_closed_port_fails_open_unreachable():
    """真实子进程降级：指向本机不可能有 sshd 的高端口 → unreachable（不抛异常）。"""
    probe = SshServiceProbe(parse_targets("nobody@127.0.0.1:9,a-bogus-unit"), timeout=2)
    rows = probe.probe(force=True)
    assert rows and all(r.state == STATE_UNREACHABLE for r in rows)


# ── mock 演示种子 ────────────────────────────────────────────────────────────

def test_demo_rows_are_deterministic_shape():
    rows = demo_rows(NOW)
    assert {(r.host, r.service) for r in rows} == {
        ("anolis-gpu-01", "jiuwenswarm-app"), ("anolis-gpu-01", "vllm-local"),
        ("anolis-gpu-01", "example-down")}
    assert all(r.checked_at <= NOW for r in rows)        # 检查时刻=种子时刻附近（确定性）
