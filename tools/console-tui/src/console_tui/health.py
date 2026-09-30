# coding: utf-8
"""跨机服务健康探针（线E，2026-09-29）— 治理面"全貌"的执行面行.

规格来源（线E动作2）: TUI 面板加跨机服务健康行——对 GPU 机（anolis-gpu-01）
的 systemd 只读探针（``systemctl is-active``），经 ssh BatchMode 非交互执行。
语义纪律:

- **只读**：仅 ``hostname`` + ``systemctl is-active <units>``，无任何写操作、
  不 sudo、不重启（线E协议红线：不动既有容器/服务）；
- **状态归一**（借 lazyagent 的归一化词表思路，docs/tui-research.md）：
  active(绿) / starting(黄) / stopped(黄) / failed(红) / unreachable(红) / unknown(黄)；
- **fail-open 展示**：探针失败（网络/超时/权限）→ unreachable 行可见，不炸面板、
  不做任何治理决策（决策点唯一；本模块只产出展示行）；
- **env 门控**：探针只在 ``CONSOLE_TUI_PROBE_TARGETS`` 显式设置时启用
  （格式 ``user@host:unit1,unit2[;user@host:...]``），默认关闭——测试与 mock
  模式绝不产生真实 ssh 子进程；srv-1 的 crane-tui 包装脚本负责注入。

分层（写死）：本模块不 import textual、不碰数据库；子进程仅在 probe() 调用时
产生，超时硬上限，输出解析为纯函数（可单测）。
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .state import utcnow

#: 状态归一词表（展示色语义见 app._HEALTH_STYLES；绿=active 黄=过渡/未知 红=坏）
STATE_ACTIVE = "active"
STATE_STARTING = "starting"
STATE_STOPPED = "stopped"
STATE_FAILED = "failed"
STATE_UNREACHABLE = "unreachable"
STATE_UNKNOWN = "unknown"

#: systemd is-active 的原始读数 → 归一状态（其余读数一律 unknown）
_SYSTEMD_MAP: Dict[str, str] = {
    "active": STATE_ACTIVE,
    "activating": STATE_STARTING,
    "inactive": STATE_STOPPED,
    "failed": STATE_FAILED,
}

PROBE_ENV = "CONSOLE_TUI_PROBE_TARGETS"
DEFAULT_TARGET = "anuser@100.64.0.7:jiuwenswarm-app,vllm-local"
DEFAULT_TIMEOUT = 8.0


@dataclass(frozen=True)
class ServiceHealthRow:
    """服务健康行（服务健康面板一行 = 一台主机上的一个 systemd unit）。"""

    host: str
    service: str
    state: str                     # 归一词表之一
    detail: str                    # 人类可读补充（失败类别/主机名回读等）
    checked_at: float


def parse_probe_output(stdout: str, host: str,
                       services: Sequence[str], *, at: float) -> List[ServiceHealthRow]:
    """探针 stdout → 行列表（纯函数）。约定输出首行=hostname，其后按序每 unit 一行。"""
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    hostname = lines[0] if lines else host
    readings = lines[1:]
    rows: List[ServiceHealthRow] = []
    for i, svc in enumerate(services):
        raw = readings[i].lower() if i < len(readings) else ""
        rows.append(ServiceHealthRow(
            host=hostname or host, service=svc,
            state=_SYSTEMD_MAP.get(raw, STATE_UNKNOWN),
            detail=raw or "no reading", checked_at=at))
    return rows


def unreachable_rows(host: str, services: Sequence[str], reason: str, *,
                     at: float) -> List[ServiceHealthRow]:
    """探针整体失败（连接/超时）→ 全部 unreachable（fail-open 展示，不给假绿）。"""
    return [ServiceHealthRow(host=host, service=svc, state=STATE_UNREACHABLE,
                             detail=reason, checked_at=at) for svc in services]


@dataclass(frozen=True)
class ProbeTarget:
    host: str                      # ssh 目标（user@host 或 alias）
    services: "tuple[str, ...]"    # systemd unit 名


def parse_targets(spec: str) -> List[ProbeTarget]:
    """``user@host:u1,u2;user@host2:u3`` → ProbeTarget 列表（格式错段跳过）。"""
    targets: List[ProbeTarget] = []
    for chunk in (spec or "").split(";"):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        host, _, svc_part = chunk.partition(":")
        services = tuple(s.strip() for s in svc_part.split(",") if s.strip())
        if host.strip() and services:
            targets.append(ProbeTarget(host.strip(), services))
    return targets


class SshServiceProbe:
    """ssh BatchMode 只读探针（TTL 缓存；线程安全靠不可变缓存行——竞态只多探一次）。"""

    def __init__(self, targets: Sequence[ProbeTarget], *,
                 timeout: float = DEFAULT_TIMEOUT, ttl: float = 60.0) -> None:
        self._targets = list(targets)
        self._timeout = float(timeout)
        self._ttl = float(ttl)
        self._cache: Optional[List[ServiceHealthRow]] = None
        self._cache_at = 0.0

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> Optional["SshServiceProbe"]:
        """env 门控工厂：``CONSOLE_TUI_PROBE_TARGETS`` 未设置/为空 → None（探针关闭）。"""
        import os
        source = env if env is not None else os.environ
        spec = (source.get(PROBE_ENV) or "").strip()
        if not spec:
            return None
        targets = parse_targets(spec)
        return cls(targets) if targets else None

    @property
    def targets(self) -> "tuple[ProbeTarget, ...]":
        return tuple(self._targets)

    def _probe_one(self, target: ProbeTarget) -> List[ServiceHealthRow]:
        """单目标探针：ssh 非交互只读；任何失败 → unreachable（fail-open）。

        韧性：unreachable 自动重试 1 次（2s 退避）——跨机 ssh 偶发抖动（SRV-1→GPU
        公网实测时好时坏）不应直接打成红行；两次都失败才 unreachable。
        """
        rows = self._probe_once(target)
        if any(r.state == STATE_UNREACHABLE for r in rows):
            time.sleep(2.0)
            rows = self._probe_once(target)
        return rows

    def _probe_once(self, target: ProbeTarget) -> List[ServiceHealthRow]:
        cmd = ["ssh", "-o", "BatchMode=yes",
               "-o", f"ConnectTimeout={max(2, int(self._timeout))}",
               "-o", "StrictHostKeyChecking=accept-new",
               target.host,
               "hostname; systemctl is-active " + " ".join(target.services)]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=self._timeout + 4.0)
        except subprocess.TimeoutExpired:
            return unreachable_rows(target.host, target.services,
                                    "probe timeout", at=utcnow())
        except OSError as exc:
            return unreachable_rows(target.host, target.services,
                                    f"os error: {type(exc).__name__}", at=utcnow())
        if proc.returncode != 0 and not proc.stdout.strip():
            # ssh 层失败（连接拒绝/无密钥等）：systemctl is-active 对 failed unit
            # 也会给非零返回码，但那是有 stdout 读数的——只在零读数时判 unreachable
            reason = (proc.stderr or "").strip().splitlines()
            return unreachable_rows(target.host, target.services,
                                    reason[-1][:80] if reason
                                    else f"exit={proc.returncode}", at=utcnow())
        return parse_probe_output(proc.stdout, target.host, target.services, at=utcnow())

    def probe(self, *, force: bool = False,
              now: Optional[float] = None) -> List[ServiceHealthRow]:
        """带 TTL 缓存的全量探针（展示专用；force=True 跳过缓存）。"""
        at = now if now is not None else utcnow()
        if not force and self._cache is not None and at - self._cache_at < self._ttl:
            return list(self._cache)
        rows: List[ServiceHealthRow] = []
        for target in self._targets:
            rows.extend(self._probe_one(target))
        self._cache = rows
        self._cache_at = at
        return list(rows)


def demo_rows(now: float) -> List[ServiceHealthRow]:
    """mock/演示模式的服务健康确定性种子（不产生任何子进程）。"""
    m = 60.0
    return [
        ServiceHealthRow("anolis-gpu-01", "jiuwenswarm-app", STATE_ACTIVE,
                         "active", now - 2 * m),
        ServiceHealthRow("anolis-gpu-01", "vllm-local", STATE_ACTIVE,
                         "active", now - 2 * m),
        ServiceHealthRow("anolis-gpu-01", "example-down", STATE_FAILED,
                         "failed", now - 5 * m),
    ]
