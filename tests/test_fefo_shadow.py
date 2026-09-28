# coding: utf-8
"""fleet 调度器 FEFO 资源压力感知测试（影子期）— W-04, v2.1 §7 消耗策略.

覆盖：ResourcePressure 输入接口校验（fail-closed）、pressure_score 主信号
（weekly_remaining_ratio）与辅信号升权（window_5h<15%）、缺读数中性处理、
pressures_from_meter_rows 五键计量行投影（W-03 契约）、rank_key_fefo 确定性、
**影子期红线**：assign(shadow_fefo=...) 只写 choice_trace（policy_active=False），
实际指派与账本与不传影子时逐字节一致；FEFO 分歧可观测（trace 记录本会怎么选）。
"""
from __future__ import annotations

import pytest

from jiuwen_glue import TRUST_TRUSTED
from jiuwen_glue.fleet import (
    FEFO_5H_LOW,
    FEFO_5H_UPLIFT,
    Assignment,
    FleetRegistry,
    GreedyScheduler,
    Rejected,
    ResourcePressure,
    SCHED_POLICY_BEST_FIT,
    SCHED_POLICY_GREEDY,
    SchedulingError,
    TaskOffering,
    assign,
    pressure_score,
    pressures_from_meter_rows,
    rank_key_fefo,
)

from fleet_utils import make_registration


def _sched(clock, *nodes, policy=SCHED_POLICY_GREEDY):
    registry = FleetRegistry(now=clock)
    for node in nodes:
        registry.register(node)
    return GreedyScheduler(registry, now=clock, policy=policy)


def _trusted(node_id="node-1", **kw):
    return make_registration(node_id, trust_level=TRUST_TRUSTED, **kw)


# ── ResourcePressure 输入接口（计量数据对象，fail-closed）─────────────────────

def test_resource_pressure_rejects_out_of_range_ratios():
    """剩余比必须 None 或 [0,1] 浮点——越界/bool 拒绝（计量投影坏了就别进调度）。"""
    with pytest.raises(SchedulingError):
        ResourcePressure(node_id="n1", weekly_remaining_ratio=1.5)
    with pytest.raises(SchedulingError):
        ResourcePressure(node_id="n1", window_5h_remaining=-0.01)
    with pytest.raises(SchedulingError):
        ResourcePressure(node_id="n1", weekly_remaining_ratio=True)   # bool 也拒
    with pytest.raises(SchedulingError):
        ResourcePressure(node_id="")                                  # 空节点标识拒
    p = ResourcePressure(node_id="n1", weekly_remaining_ratio=None,
                         window_5h_remaining=0.0)
    assert p.weekly_remaining_ratio is None and p.window_5h_remaining == 0.0


# ── pressure_score：主信号 + 辅信号升权 + 缺读数中性 ──────────────────────────

def test_pressure_score_weekly_primary_signal():
    """主信号：周限剩余越少压力越大（FEFO：将过期额度先用）。"""
    assert pressure_score(ResourcePressure(node_id="n", weekly_remaining_ratio=0.9)) \
        == pytest.approx(0.1)
    assert pressure_score(ResourcePressure(node_id="n", weekly_remaining_ratio=0.1)) \
        == pytest.approx(0.9)
    # 剩余 0 → 压力满格 1.0；剩余满格 → 压力 0
    assert pressure_score(ResourcePressure(node_id="n", weekly_remaining_ratio=0.0)) == 1.0
    assert pressure_score(ResourcePressure(node_id="n", weekly_remaining_ratio=1.0)) == 0.0


def test_pressure_score_five_hours_uplift_only_below_threshold():
    """辅信号：5h 窗剩余 <15% 升权（+0.25 封顶 1.0）；恰好 15% 不升权。"""
    base = ResourcePressure(node_id="n", weekly_remaining_ratio=0.5,
                            window_5h_remaining=FEFO_5H_LOW)   # 恰在阈值 → 不升权
    assert pressure_score(base) == pytest.approx(0.5)
    lifted = ResourcePressure(node_id="n", weekly_remaining_ratio=0.5,
                              window_5h_remaining=FEFO_5H_LOW - 0.01)
    assert pressure_score(lifted) == pytest.approx(0.5 + FEFO_5H_UPLIFT)
    capped = ResourcePressure(node_id="n", weekly_remaining_ratio=0.0,
                              window_5h_remaining=0.0)         # 1.0 + 0.25 → 封顶 1.0
    assert pressure_score(capped) == 1.0


def test_pressure_score_missing_readings_are_neutral():
    """缺读数（None）= 中性 0——缺计量不制造优先级；无压力对象同。"""
    assert pressure_score(None) == 0.0
    assert pressure_score(ResourcePressure(node_id="n")) == 0.0
    # 有 5h 读数但无周限读数：只有升权项可生效（主信号不臆造）
    only5 = ResourcePressure(node_id="n", window_5h_remaining=0.05)
    assert pressure_score(only5) == pytest.approx(FEFO_5H_UPLIFT)


# ── pressures_from_meter_rows：W-03 五键计量行 → 投影 ─────────────────────────

def test_pressures_from_meter_rows_projection():
    """计量行（value=已用量）× 档案 window.limit 投影 → 剩余比；窗口前缀识别
    weekly-* / five_hours*；monthly 窗限额可作周限主信号限额（CNB 口径）。"""
    rows = [
        {"resource": "cnb-sandbox", "window_id": "monthly-202609", "value": 400.0,
         "ts": "2026-09-28T10:00:00+08:00", "source": "cnb_api",
         "meter_type": "cnb_core_hours"},
        {"resource": "zcode-coding-plan", "window_id": "weekly-2026W39", "value": 30.0,
         "ts": "2026-09-28T10:00:00+08:00", "source": "bigmodel_quota_api",
         "meter_type": "zcode_quota_units"},
        {"resource": "zcode-coding-plan", "window_id": "five_hours", "value": 9.0,
         "ts": "2026-09-28T10:00:00+08:00", "source": "bigmodel_quota_api",
         "meter_type": "zcode_quota_units"},
        {"resource": "cnb-sandbox", "window_id": "daily-20260928", "value": 12.0,
         "ts": "2026-09-28T10:00:00+08:00", "source": "cnb_api",
         "meter_type": "cnb_core_hours"},   # 非 weekly/five_hours 窗 → 不入压力
    ]
    limits = {
        "cnb-sandbox": {"monthly": 1600.0},          # 档案 window.limit 投影
        "zcode-coding-plan": {"weekly": 100.0, "five_hours": 10.0},
    }
    pressures = pressures_from_meter_rows(rows, limits=limits, now=123.0)
    cnb = pressures["cnb-sandbox"]
    assert cnb.weekly_remaining_ratio == pytest.approx(1 - 400.0 / 1600.0)  # 0.75
    assert cnb.window_5h_remaining is None            # 无 5h 计量 → 中性，不臆造
    assert cnb.as_of == 123.0 and cnb.resource_id == "cnb-sandbox"
    zcode = pressures["zcode-coding-plan"]
    assert zcode.weekly_remaining_ratio == pytest.approx(0.7)
    assert zcode.window_5h_remaining == pytest.approx(0.1)   # 已逼近窗口底
    assert "cnb-build" not in pressures               # 无计量行的资源不投影


def test_pressures_from_meter_rows_gap_rows_and_explicit_ratio():
    """缺口行（value=null，source=*_gap）如实忽略；行内显式 remaining_ratio 优先直读
    （同资源 value 换算会给出不同值，直读 0.33 胜出证明优先级）。"""
    rows = [
        {"resource": "cnb-sandbox", "window_id": "weekly-2026W39", "value": None,
         "ts": "2026-09-28T10:00:00+08:00", "source": "bigmodel_quota_api_gap",
         "meter_type": "zcode_quota_units"},
        {"resource": "zcode-coding-plan", "window_id": "weekly-2026W39",
         "remaining_ratio": 0.33, "value": 80.0,     # 换算只会得 0.2；直读 0.33 必须胜出
         "ts": "2026-09-28T10:00:00+08:00", "source": "bigmodel_quota_api",
         "meter_type": "zcode_quota_units"},
    ]
    pressures = pressures_from_meter_rows(
        rows, limits={"zcode-coding-plan": {"weekly": 100.0}})
    assert "cnb-sandbox" not in pressures             # 全缺口 → 不投影（中性缺席）
    zcode = pressures["zcode-coding-plan"]
    assert zcode.weekly_remaining_ratio == pytest.approx(0.33)   # 直读优先于换算
    # node_map：档案 resource_id → 调度节点投影
    mapped = pressures_from_meter_rows(
        [{"resource": "zcode-coding-plan", "window_id": "weekly", "value": 50.0}],
        limits={"zcode-coding-plan": {"weekly": 100.0}},
        node_map={"zcode-coding-plan": "fleet-node-7"})
    assert set(mapped) == {"fleet-node-7"}
    assert mapped["fleet-node-7"].weekly_remaining_ratio == pytest.approx(0.5)


# ── rank_key_fefo：压力降序 + node_id 确定性 ──────────────────────────────────

def test_rank_key_fefo_prefers_higher_pressure_then_node_id(clock):
    """FEFO 排序：压力大者先派（将过期额度先用）；平局 node_id 升序确定性。"""
    n_hot = _trusted("n-b")     # 周限快烧完（压力大）
    n_cold = _trusted("n-a")    # 周限充裕（压力小）
    hot = ResourcePressure(node_id="n-b", weekly_remaining_ratio=0.05)
    cold = ResourcePressure(node_id="n-a", weekly_remaining_ratio=0.9)
    assert rank_key_fefo(n_hot, hot) < rank_key_fefo(n_cold, cold)
    # 平局（不同信号混合、压力相同）→ node_id 升序
    p1 = ResourcePressure(node_id="n-a", weekly_remaining_ratio=0.5)             # 0.5
    p2 = ResourcePressure(node_id="n-b", weekly_remaining_ratio=0.75,
                          window_5h_remaining=0.0)   # 0.25 + 0.25 升权 = 0.5
    assert pressure_score(p1) == pressure_score(p2) == 0.5
    assert rank_key_fefo(_trusted("n-a"), p1) < rank_key_fefo(_trusted("n-b"), p2)
    # 无压力数据（缺计量）排在有压力数据之后，但仍可被指派（不弃派）
    assert rank_key_fefo(_trusted("n-b"), hot) < rank_key_fefo(_trusted("n-a"), None)


# ── 影子期红线：choice_trace 只记录，指派不变 ─────────────────────────────────

def test_shadow_assign_leaves_assignment_identical(clock):
    """红线主测试：传 shadow_fefo 与不传，实际指派（节点序列/账本/状态）完全一致；
    choice_trace 只在传影子时出现，且 policy_active=False。"""
    offerings = [TaskOffering(task_ref=f"t-{i}", gpu_demand=0.2) for i in range(3)]
    pressures = {
        "n-a": ResourcePressure(node_id="n-a", weekly_remaining_ratio=0.05,
                                window_5h_remaining=0.0),   # 压力 1.0：FEFO 最想派
        "n-b": ResourcePressure(node_id="n-b", weekly_remaining_ratio=0.95),
    }
    for use_shadow in (False, True):
        sched = _sched(clock, _trusted("n-a", gpu_frac=0.5), _trusted("n-b", gpu_frac=0.5))
        chosen = []
        for off in offerings:
            kw = {"shadow_fefo": pressures} if use_shadow else {}
            result = sched.assign(off, **kw)
            assert isinstance(result, Assignment)
            chosen.append(result.node_id)
            assert result.status == "ACTIVE"
            if use_shadow:
                assert result.choice_trace is not None
                assert result.choice_trace["policy_active"] is False
                assert result.choice_trace["effective_choice"] == result.node_id
                assert result.choice_trace["fefo_choice"] in ("n-a", "n-b")
            else:
                assert result.choice_trace is None
        # greedy 序列（P1 键锁定不变）：n-a → n-b（n-a 余量降为 0.3）→ n-a（平局 node_id）
        assert chosen == ["n-a", "n-b", "n-a"]   # FEFO 想先派 n-b 也**不许**改指派
        # 账本核对：n-a 承诺 0.4 / n-b 承诺 0.2（影子不偷扣份额）
        assert sched.free_gpu_frac("n-a") == pytest.approx(0.1)
        assert sched.free_gpu_frac("n-b") == pytest.approx(0.3)


def test_shadow_trace_records_fefo_divergence(clock):
    """可观测性：FEFO 本会选择另一节点时，trace 完整记录分歧（排序+双方选择）。"""
    sched = _sched(clock, _trusted("n-a", gpu_frac=0.5), _trusted("n-b", gpu_frac=0.5))
    pressures = {
        "n-a": ResourcePressure(node_id="n-a", weekly_remaining_ratio=1.0),  # 压力 0
        "n-b": ResourcePressure(node_id="n-b", weekly_remaining_ratio=0.0),  # 压力 1
    }
    result = sched.assign(TaskOffering(task_ref="t-1", gpu_demand=0.2),
                          shadow_fefo=pressures)
    trace = result.choice_trace
    assert trace["effective_choice"] == "n-a"       # greedy 实际选择（node_id tie-break）
    assert trace["fefo_choice"] == "n-b"            # FEFO 本会选压力最大的 n-b
    assert trace["shadow"] is True and trace["policy_active"] is False
    assert trace["effective_policy"] == SCHED_POLICY_GREEDY
    ranking = trace["fefo_ranking"]
    assert [e["node_id"] for e in ranking] == ["n-b", "n-a"]      # 压力降序
    assert ranking[0]["pressure"] == 1.0 and ranking[1]["pressure"] == 0.0
    assert ranking[0]["weekly_remaining_ratio"] == 0.0
    # 轨迹落审计：ASSIGNED 事件可追溯 FEFO 影子选择
    assigned_events = [e for e in sched.audit if e["event"] == "ASSIGNED"]
    assert assigned_events and assigned_events[-1]["fefo_shadow_choice"] == "n-b"


def test_shadow_works_under_best_fit_policy_and_rejects_untouched(clock):
    """影子机制与 P2 best-fit 正交；全部候选被过滤（Rejected）时不产生 trace。"""
    sched = _sched(clock, _trusted("n-a", gpu_frac=0.5), _trusted("n-b", gpu_frac=0.8),
                   policy=SCHED_POLICY_BEST_FIT)
    pressures = {"n-b": ResourcePressure(node_id="n-b", weekly_remaining_ratio=0.0)}
    result = sched.assign(TaskOffering(task_ref="t-1", gpu_demand=0.3),
                          shadow_fefo=pressures)
    assert result.node_id == "n-a"                  # best-fit：放置后剩余最小者优先
    assert result.choice_trace["effective_policy"] == SCHED_POLICY_BEST_FIT
    assert result.choice_trace["fefo_choice"] == "n-b"   # FEFO 本会选有压力数据的 n-b
    # 需求超所有节点 → Rejected（无 Assignment，即无 trace 可言）
    rejected = assign(TaskOffering(task_ref="t-2", gpu_demand=0.9), sched.registry,
                      ledger=None, policy=SCHED_POLICY_GREEDY,
                      shadow_fefo=pressures)
    assert isinstance(rejected, Rejected)


# ── D1-R8 二批（grok 红队草稿属实线索）：FEFO 输入面巨型 int 拒绝不变形 ──────

def test_resource_pressure_giant_int_typed_rejection():
    """ResourcePressure 份额声明传 10**400 → SchedulingError
    （此前 isinstance 通过后 float() OverflowError 未归类崩溃）。"""
    with pytest.raises(SchedulingError):
        ResourcePressure(node_id="n", weekly_remaining_ratio=10**400)
    with pytest.raises(SchedulingError):
        ResourcePressure(node_id="n", window_5h_remaining=10**400)


def test_pressures_giant_int_ratio_falls_through_not_crash():
    """W-03 外部计量行 remaining_ratio=10**400：越出 float 域 → 与越界有限值
    同款回落 value 换算路径（直读行弃用），不变形为未归类 OverflowError。"""
    rows = [{"resource": "cnb-sandbox", "window_id": "weekly-2026W39",
             "remaining_ratio": 10**400, "value": 400.0,
             "ts": "2026-09-28T10:00:00+08:00", "source": "cnb_api",
             "meter_type": "cnb_core_hours"}]
    limits = {"cnb-sandbox": {"weekly": 1600.0}}
    pressures = pressures_from_meter_rows(rows, limits=limits, now=123.0)
    assert pressures["cnb-sandbox"].weekly_remaining_ratio == pytest.approx(
        1 - 400.0 / 1600.0)
