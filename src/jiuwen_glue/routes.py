# coding: utf-8
"""产物路由表 + 节点容量模型 — glue 便宜三件之 Python 侧（WO-0003 返工，v1.7 §13/§12.6）.

规格来源（PROP-0001 v1.7 §13 多域流水线 / §12.6 节点池纳管；风险登记册"多域流水线
产物误入 git（或代码产物误入 DAM）"）:

- **ArtifactRoute**：pipeline → sink 声明表。流水线差异收敛为三种声明之一
  （环境定义 YAML / 资产路由策略 / 验收定义）；sink 换客户 DAM = 改路由表。
  基线：code→git / video→minio / eval→eval-assets。``resolve`` 对未声明的
  (pipeline, artifact_kind) 抛 ArtifactRouteUndeclaredError——**显式错误，
  防产物误入 git**；无"默认进 git"之类的兜底。
- **NodeCapacity**：节点能力声明（CPU/GPU 份额、工具、信任等级、max_parallel、
  在线窗口）——字段定义与校验，供 DDL（glue.node）与 Wave2 fleet 调度器消费。
  ``GPU_FRAC`` 概念吸收为 gpu_frac 字段（0.0–1.0 份额）；不可信节点只派沙箱
  任务类（sandbox_only）。

本模块是**纯声明与校验**：不做调度（Wave2 WO-0011）、不做传输、不新增决策点。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .errors import ArtifactRouteUndeclaredError, CapacitySchemaError, RouteSchemaError

# 基线 sink（声明表可扩展：sink 换客户 DAM = 改路由表，代码不动）
SINK_GIT = "git"
SINK_MINIO = "minio"
SINK_EVAL_ASSETS = "eval-assets"

# 产物种类基线
KIND_CODE = "code"
KIND_VIDEO = "video"
KIND_EVAL = "eval"

PIPELINE_DEFAULT = "*"      # 基线路由的 pipeline 通配（精确声明优先于通配）

TRUST_TRUSTED = "trusted"
TRUST_UNTRUSTED = "untrusted"
_TRUST_LEVELS = (TRUST_TRUSTED, TRUST_UNTRUSTED)


# ── 产物路由表 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ArtifactRoute:
    """一条路由声明：pipeline 的某类产物发往哪个 sink（对应 DDL glue.artifact_route）。"""

    pipeline: str
    artifact_kind: str
    sink: str
    tenant_id: str = "t0"

    def __post_init__(self) -> None:
        for name in ("pipeline", "artifact_kind", "sink"):
            v = getattr(self, name)
            if not v or not isinstance(v, str):
                raise RouteSchemaError(f"{name} must be a non-empty string, got {v!r}")


class ArtifactRouteTable:
    """pipeline → sink 声明表。resolve 是唯一出口；未声明路由 = 显式错误（fail-closed）。

    用法：``default_table()`` 给出 code→git / video→minio / eval→eval-assets 基线；
    ``declare()`` 覆盖或新增（精确 pipeline 优先于 "*" 基线）——
    视频产物sink换客户 DAM 只改声明，不改代码（v1.7 §13 原则）。
    """

    def __init__(self, routes: Iterable[ArtifactRoute] = ()) -> None:
        self._routes: Dict[Tuple[str, str, str], ArtifactRoute] = {}
        for r in routes:
            self.declare(r)

    def declare(self, route: ArtifactRoute) -> ArtifactRoute:
        if not isinstance(route, ArtifactRoute):
            raise RouteSchemaError("route must be an ArtifactRoute")
        self._routes[(route.tenant_id, route.pipeline, route.artifact_kind)] = route
        return route

    def resolve(self, pipeline: str, artifact_kind: str, *,
                tenant_id: str = "t0") -> ArtifactRoute:
        """解析 (pipeline, artifact_kind) → 声明路由。

        命中顺序：精确 (pipeline, kind) → 基线 ("*", kind)；都未声明 →
        ArtifactRouteUndeclaredError（**不做任何默认落盘兜底——防产物误入 git**）。
        """
        if not pipeline or not artifact_kind:
            raise RouteSchemaError("pipeline and artifact_kind are required")
        route = (self._routes.get((tenant_id, pipeline, artifact_kind))
                 or self._routes.get((tenant_id, PIPELINE_DEFAULT, artifact_kind)))
        if route is None:
            raise ArtifactRouteUndeclaredError(
                f"no artifact route declared for pipeline={pipeline!r} "
                f"kind={artifact_kind!r} tenant={tenant_id!r} — refusing to guess a sink "
                "(products must never silently land in git or DAM)")
        return route

    def all(self) -> List[ArtifactRoute]:
        return list(self._routes.values())

    @classmethod
    def default_table(cls) -> "ArtifactRouteTable":
        """v1.7 §13 基线：code→git / video→minio / eval→eval-assets。"""
        return cls(routes=[
            ArtifactRoute(PIPELINE_DEFAULT, KIND_CODE, SINK_GIT),
            ArtifactRoute(PIPELINE_DEFAULT, KIND_VIDEO, SINK_MINIO),
            ArtifactRoute(PIPELINE_DEFAULT, KIND_EVAL, SINK_EVAL_ASSETS),
        ])


# ── 节点容量模型 ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NodeCapacity:
    """节点能力声明（对应 DDL glue.node；消费方：Wave2 fleet 调度器 / 控制台 TUI）。

    两种取活模式（12.6）不影响本声明：调度制（控制台派单）与自取制（节点轮询
    glue 工单队列认领 PENDING）都按 capacity 匹配。
    """

    node_id: str
    cpu_frac: float = 1.0            # CPU 份额 0.0–1.0
    gpu_frac: float = 0.0            # GPU 份额 0.0–1.0（GPU_FRAC 吸收为字段；V100 独占=1.0）
    tools: Tuple[str, ...] = ()      # 工具/能力标签（如 shell / browser / vllm）
    trust_level: str = TRUST_TRUSTED  # trusted | untrusted（不可信节点只派沙箱任务类）
    max_parallel: int = 1            # 最大并行任务数
    online_window: str = "always"    # 在线窗口声明（如 "always" / "09:00-18:00+08"）
    tenant_id: str = "t0"

    def __post_init__(self) -> None:
        if not self.node_id or not isinstance(self.node_id, str):
            raise CapacitySchemaError("node_id must be a non-empty string")
        for name in ("cpu_frac", "gpu_frac"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or not (0.0 <= float(v) <= 1.0):
                raise CapacitySchemaError(
                    f"{name} must be a float in [0.0, 1.0], got {v!r}")
        if self.trust_level not in _TRUST_LEVELS:
            raise CapacitySchemaError(
                f"trust_level must be one of {_TRUST_LEVELS}, got {self.trust_level!r}")
        if not isinstance(self.max_parallel, int) or self.max_parallel < 1:
            raise CapacitySchemaError(
                f"max_parallel must be an int >= 1, got {self.max_parallel!r}")
        if not self.online_window or not isinstance(self.online_window, str):
            raise CapacitySchemaError("online_window must be a non-empty string")

    @property
    def sandbox_only(self) -> bool:
        """不可信节点只派沙箱任务类：产物隔离 + 人工复核（12.6 边界，写死）。"""
        return self.trust_level == TRUST_UNTRUSTED

    def can_take(self, *, needs_gpu: bool = False, sandbox_class: bool = False,
                 parallel_slots: int = 1) -> bool:
        """调度器消费的最小匹配谓词（贪心匹配用；完整调度在 Wave2，本模块不调度）。"""
        if needs_gpu and self.gpu_frac <= 0.0:
            return False
        if (not sandbox_class) and self.sandbox_only:
            return False   # 非沙箱任务类不可派给不可信节点
        return parallel_slots <= self.max_parallel
