# coding: utf-8
"""产物路由表 + 节点容量模型测试（便宜三件 Python 侧）."""
from __future__ import annotations

import pytest

from jiuwen_glue import (
    KIND_CODE,
    KIND_EVAL,
    KIND_VIDEO,
    PIPELINE_DEFAULT,
    SINK_EVAL_ASSETS,
    SINK_GIT,
    SINK_MINIO,
    TRUST_TRUSTED,
    TRUST_UNTRUSTED,
    ArtifactRoute,
    ArtifactRouteTable,
    ArtifactRouteUndeclaredError,
    CapacitySchemaError,
    NodeCapacity,
    RouteSchemaError,
)


# ── 产物路由表 ────────────────────────────────────────────────────────────────

def test_default_baseline_routes():
    table = ArtifactRouteTable.default_table()
    assert table.resolve("any-pipeline", KIND_CODE).sink == SINK_GIT
    assert table.resolve("video-ads", KIND_VIDEO).sink == SINK_MINIO
    assert table.resolve("dev", KIND_EVAL).sink == SINK_EVAL_ASSETS


def test_undeclared_route_is_explicit_error(clock=None):
    """未声明路由 → 显式错误——防产物误入 git；没有"默认进 git"兜底。"""
    table = ArtifactRouteTable.default_table()
    with pytest.raises(ArtifactRouteUndeclaredError):
        table.resolve("video-ads", "report")            # 声明表没有 report 类产物
    with pytest.raises(ArtifactRouteUndeclaredError):
        table.resolve("video-ads", "video", tenant_id="t1")   # 租户隔离：t1 无基线


def test_video_never_lands_in_git_by_default():
    """风险登记册"产物误入 git"的对照：video→minio 基线存在且唯一。"""
    table = ArtifactRouteTable.default_table()
    route = table.resolve("video-ads", KIND_VIDEO)
    assert route.sink == SINK_MINIO and route.sink != SINK_GIT


def test_exact_declaration_overrides_baseline():
    """sink 换客户 DAM = 改路由表（精确声明优先于 "*" 基线），代码不动。"""
    table = ArtifactRouteTable.default_table()
    table.declare(ArtifactRoute("video-ads", KIND_VIDEO, "customer-dam",
                                tenant_id="t1"))
    assert table.resolve("video-ads", KIND_VIDEO, tenant_id="t1").sink == "customer-dam"
    assert table.resolve("video-ads", KIND_VIDEO, tenant_id="t0").sink == SINK_MINIO


def test_route_validation():
    with pytest.raises(RouteSchemaError):
        ArtifactRoute("", KIND_CODE, SINK_GIT)
    with pytest.raises(RouteSchemaError):
        table = ArtifactRouteTable()
        table.declare("not-a-route")


# ── 节点容量模型 ─────────────────────────────────────────────────────────────

def test_node_capacity_defaults_and_validation():
    node = NodeCapacity(node_id="gpu-0", gpu_frac=1.0, tools=("vllm", "shell"),
                        max_parallel=4)
    assert node.cpu_frac == 1.0 and node.trust_level == TRUST_TRUSTED
    assert node.online_window == "always"
    with pytest.raises(CapacitySchemaError):
        NodeCapacity(node_id="x", gpu_frac=1.5)            # 份额越界
    with pytest.raises(CapacitySchemaError):
        NodeCapacity(node_id="x", cpu_frac=-0.1)
    with pytest.raises(CapacitySchemaError):
        NodeCapacity(node_id="x", trust_level="semi")
    with pytest.raises(CapacitySchemaError):
        NodeCapacity(node_id="x", max_parallel=0)
    with pytest.raises(CapacitySchemaError):
        NodeCapacity(node_id="", online_window="")


def test_untrusted_nodes_take_sandbox_class_only():
    """12.6 边界写死：不可信节点只派沙箱任务类（产物隔离+人工复核）。"""
    untrusted = NodeCapacity(node_id="offline-pc", trust_level=TRUST_UNTRUSTED,
                             gpu_frac=0.0, max_parallel=1)
    assert untrusted.sandbox_only is True
    assert untrusted.can_take(sandbox_class=True)
    assert not untrusted.can_take(sandbox_class=False)     # 非沙箱任务类不可派
    trusted = NodeCapacity(node_id="gpu-0", gpu_frac=1.0)
    assert trusted.sandbox_only is False
    assert trusted.can_take(sandbox_class=False, needs_gpu=True)
    assert not trusted.can_take(needs_gpu=True, parallel_slots=2)  # 并行位不足


def test_gpu_frac_scheduling_predicate():
    """GPU 份额（GPU_FRAC）为容量字段：视频任务带 GPU 标签只派有闲份额节点。"""
    cpu_only = NodeCapacity(node_id="srv-1", gpu_frac=0.0)
    gpu_half = NodeCapacity(node_id="gpu-1", gpu_frac=0.5)
    assert not cpu_only.can_take(needs_gpu=True)
    assert gpu_half.can_take(needs_gpu=True)
    assert gpu_half.can_take(needs_gpu=False)              # CPU 任务也可跑
