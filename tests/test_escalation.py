# coding: utf-8
"""升级阶梯状态机测试（v2.1 §4.7 / 工单 W-06）：六级阶梯 + 风暴防护三参数 +
签名 3 次 → 权限扩展提案 + 人类就绪包四件套（字段不齐 = BLOCKED）+ task_ref 挂单."""
from __future__ import annotations

import pytest

from jiuwen_glue import MissingTaskReferenceError
from jiuwen_glue.escalation import (
    HARD_IRREVERSIBLE,
    HARD_LEGAL_TOS,
    HARD_LIST,
    LEVELS,
    LEVEL_L0,
    LEVEL_L4,
    LEVEL_L5,
    PROOF_LEVELS,
    EscalationLedger,
    EscalationRequiredError,
    EscalationSchemaError,
    EscalationStateError,
    EscalationStormError,
    FrozenFact,
    ReadinessTask,
    ReversibilityAssessment,
    StormGuard,
    UnknownEscalationCaseError,
    signature_for,
)

BLOCKER = {"error": "PermissionDenied", "resource": "prod/release/17", "action": "deploy"}


def _climb(led: EscalationLedger, task_ref: str, blocker=BLOCKER, upto: str = LEVEL_L4,
           clock=None):
    """从 L0（自动开 case）逐级爬到 upto：每级打满级内限次再升级（同一 blocker 签名贯穿）。

    clock 给出时在每次升级前推过最小步进间隔（生产里每一级都有真实处理时间；
    红队 R1-F3 补充防线要求相邻两次升级不得发生在同一瞬间）。
    """
    led.attempt(task_ref, blocker)                       # 自动开 case（计入当前级）
    while led.level_of(task_ref) != upto:
        case = led.case_of(task_ref)
        key = (case.level, signature_for(blocker))
        while case.attempts.get(key, 0) < led.guard.max_attempts_per_level:
            led.attempt(task_ref, blocker)
        if clock is not None:
            clock.advance(led.guard.min_step_interval_seconds + 1)
        led.escalate(task_ref, blocker, added_context=("logs:prod",),
                     added_tools=("kubectl",), time_budget_seconds=1800,
                     reason="per-level cap hit")


@pytest.fixture()
def ledger(clock):
    return EscalationLedger(now=clock)


# ── 阶梯与挂单 ────────────────────────────────────────────────────────────────

def test_ladder_is_six_levels_with_declared_roles():
    """六级阶梯 L0-L5，各级角色与能力上限声明齐全（v2.1 §4.7）。"""
    assert LEVELS == ("L0", "L1", "L2", "L3", "L4", "L5")
    assert PROOF_LEVELS == ("L0", "L1", "L2", "L3")   # 权限内层级（L4 守门者之下）
    from jiuwen_glue.escalation import LEVEL_CAPABILITIES, LEVEL_ROLE
    assert set(LEVEL_ROLE) == set(LEVELS)
    assert LEVEL_ROLE[LEVEL_L4] == "gatekeeper" and LEVEL_ROLE[LEVEL_L5] == "human"
    assert "panorama-readonly" in LEVEL_CAPABILITIES[LEVEL_L4]
    assert "human-hard-list" in LEVEL_CAPABILITIES[LEVEL_L5]


def test_escalation_is_a_single_step_and_attaches_task_ref(ledger, clock):
    """升级单步推进且升级对象挂工单（task_ref 引用 + 显式增量声明）。"""
    esc = ledger.escalate("wo-100", BLOCKER, added_context=("logs:prod",),
                          added_tools=("kubectl",), time_budget_seconds=900,
                          reason="stuck")
    assert (esc.from_level, esc.to_level) == (LEVEL_L0, "L1")
    assert esc.task_ref == "wo-100"                      # 升级对象挂工单
    assert esc.added_context == ("logs:prod",)
    assert esc.added_tools == ("kubectl",)
    assert esc.time_budget_seconds == 900.0
    assert ledger.level_of("wo-100") == "L1"
    clock.advance(ledger.guard.min_step_interval_seconds + 1)
    esc2 = ledger.escalate("wo-100", BLOCKER, time_budget_seconds=900)   # 单步：L1→L2
    assert (esc2.from_level, esc2.to_level) == ("L1", "L2")
    assert ledger.escalation_of(esc.escalation_id).task_ref == "wo-100"  # 台账可回查


def test_missing_task_ref_is_rejected_everywhere(ledger):
    """task_ref 是挂单键：attempt/escalate/downgrade 全部拒绝空引用。"""
    for call in (
        lambda: ledger.attempt("", BLOCKER),
        lambda: ledger.escalate("", BLOCKER, time_budget_seconds=1),
        lambda: ledger.downgrade(""),
        lambda: ledger.case_of(""),
    ):
        with pytest.raises((MissingTaskReferenceError, UnknownEscalationCaseError)):
            call()


# ── 风暴防护一：级内限次 ──────────────────────────────────────────────────────

def test_per_level_attempt_cap_forces_escalation(ledger):
    """风暴防护①：每级最大尝试——到顶后原地重试被拒（EscalationRequiredError），
    升级后新层级重新计数。"""
    for i in range(ledger.guard.max_attempts_per_level):
        assert ledger.attempt("wo-200", BLOCKER) == i + 1
    with pytest.raises(EscalationRequiredError):
        ledger.attempt("wo-200", BLOCKER)
    ledger.escalate("wo-200", BLOCKER, time_budget_seconds=60)
    assert ledger.attempt("wo-200", BLOCKER) == 1        # L1 层重新计数
    events = [e.event for e in ledger.audit]
    assert "ATTEMPT_REJECTED" in events and events[-1] == "ATTEMPT"


def test_custom_guard_validates_parameters():
    """风暴参数可配置但非法值拒绝（fail-closed 配置纪律）。"""
    assert StormGuard().max_downgrades == 1              # 回退 ≤1（v2.1 §4.7）
    for kwargs in ({"max_attempts_per_level": 0}, {"max_downgrades": -1},
                   {"dedup_window_seconds": -1}, {"signature_threshold": 0}):
        with pytest.raises(EscalationSchemaError):
            StormGuard(**kwargs)
    # R6/D1：NaN 经 ``nan < 0`` 恒 False 静默通过，进入比较后 ``(now-x) < nan``
    # 恒 False ——签名去重与最小步进两道风暴闸被静默关闭（fail-open）；±inf 同拒。
    nan = float("nan")
    for kwargs in ({"dedup_window_seconds": nan}, {"min_step_interval_seconds": nan},
                   {"dedup_window_seconds": float("inf")},
                   {"min_step_interval_seconds": float("-inf")},
                   {"dedup_window_seconds": 10 ** 309},   # 巨型 int：isfinite 抛 OverflowError
                   {"min_step_interval_seconds": 10 ** 400}):
        with pytest.raises(EscalationSchemaError):
            StormGuard(**kwargs)


# ── 风暴防护二：向下回退 ≤1 ──────────────────────────────────────────────────

def test_downgrade_allowed_once_then_storm_rejected(clock):
    """风暴防护②：向下回退 ≤1——第一次放行，从高层再回退时拒绝（cap 先于 L0 检查）；
    L0 本身不可降；未知工单显式报错。"""
    led = EscalationLedger(now=clock)
    led.escalate("wo-300", BLOCKER, time_budget_seconds=60)     # L0→L1
    led.downgrade("wo-300", reason="peer solved partially")     # L1→L0（第 1 次，放行）
    assert led.level_of("wo-300") == LEVEL_L0
    with pytest.raises(EscalationStateError):                   # 已在 L0，无级可降
        led.downgrade("wo-300")
    clock.advance(led.guard.dedup_window_seconds + 1)
    led.escalate("wo-300", BLOCKER, time_budget_seconds=60)     # L0→L1（窗口外）
    clock.advance(led.guard.min_step_interval_seconds + 1)
    led.escalate("wo-300", BLOCKER, time_budget_seconds=60)     # L1→L2
    with pytest.raises(EscalationStormError):                   # 回退 ≤1 已用尽
        led.downgrade("wo-300")                                 # case 一生只回退一次
    led2 = EscalationLedger(now=clock)
    with pytest.raises(UnknownEscalationCaseError):
        led2.downgrade("wo-fresh")                              # 未知 case 显式报错
    led2.attempt("wo-l0", {"e": "only"})                        # 开 case（停在 L0）
    with pytest.raises(EscalationStateError):
        led2.downgrade("wo-l0")                                 # L0 不可再降


def test_signature_dedup_blocks_same_source_level_repeat_not_ladder_climb(clock):
    """风暴防护③：签名去重限流——同（源层级,签名）窗口内重复升级被拒；
    逐级爬升（源层级不同）不受限；窗口过后放行。"""
    led = EscalationLedger(now=clock)
    led.attempt("wo-400", BLOCKER)
    led.escalate("wo-400", BLOCKER, time_budget_seconds=60)     # L0→L1（签名 X）
    led.downgrade("wo-400")                                     # L1→L0
    with pytest.raises(EscalationStormError):                   # 同源层级重升 → 风暴
        led.escalate("wo-400", BLOCKER, time_budget_seconds=60)
    clock.advance(led.guard.dedup_window_seconds + 1)
    led.escalate("wo-400", BLOCKER, time_budget_seconds=60)     # 窗口外放行
    # 逐级爬升同签名不受限：L1→L2 与 L0→L1 源层级不同，不在去重键内
    # （但受最小步进间隔约束——真实爬升必须有时间流逝，R1-F3）
    clock.advance(led.guard.min_step_interval_seconds + 1)
    led.escalate("wo-400", BLOCKER, time_budget_seconds=60)
    assert led.level_of("wo-400") == "L2"


# ── 组织学习：同类签名 3 次 → 权限扩展提案 ────────────────────────────────────

def test_three_same_signatures_open_permission_expansion_proposal(clock):
    """同类签名 3 次（跨工单累计）→ 自动开权限扩展提案对象（OPEN）；
    提案只登记不授权；第 4 次起 bump 同一提案不重复开。"""
    led = EscalationLedger(now=clock)
    for task in ("wo-a", "wo-b", "wo-c"):
        led.escalate(task, BLOCKER, added_context=("logs:prod",),
                     added_tools=("kubectl",), time_budget_seconds=60)
        clock.advance(led.guard.min_step_interval_seconds + 1)   # 每次升级时间推进（R1-F3）
    assert len(led.proposals) == 1
    p = led.proposals[signature_for(BLOCKER)]
    assert p.occurrences == 3 and p.state == "OPEN"
    assert p.task_refs == ("wo-a", "wo-b", "wo-c")
    assert p.scope_requested == ("logs:prod",) and p.tools_requested == ("kubectl",)
    # 第 4 次：bump 同一提案（不重复开），仍然只是登记
    clock.advance(led.guard.min_step_interval_seconds + 1)
    led.escalate("wo-d", BLOCKER, time_budget_seconds=60)
    assert len(led.proposals) == 1
    assert led.proposals[signature_for(BLOCKER)].occurrences == 4
    kinds = [e.event for e in led.audit]
    assert kinds.count("PROPOSAL_OPENED") == 1 and kinds.count("PROPOSAL_BUMPED") == 1
    # 提案不改任何 case 状态（只登记不授权——扩权属人类硬清单）
    assert led.level_of("wo-a") == "L1"


# ── 升级的是权限/工具/上下文而非模型 ─────────────────────────────────────────

def test_model_delta_is_never_escalatable(ledger):
    """"升级的是权限/工具/上下文而非模型"：extras 携带 model 键即拒绝（大小写不敏感）。"""
    for extras in ({"model": "glm-x"}, {"Model": "glm-x"}):
        with pytest.raises(EscalationSchemaError):
            ledger.escalate("wo-500", BLOCKER, time_budget_seconds=60, extras=extras)
    assert ledger.level_of("wo-500") == LEVEL_L0          # 拒绝路径零副作用


def test_time_budget_must_be_declared(ledger):
    """增量显式声明纪律：时间预算缺失/非正 → 拒绝（added_context/tools 允许显式空）。"""
    with pytest.raises(EscalationSchemaError):
        ledger.escalate("wo-600", BLOCKER)                          # 未声明
    with pytest.raises(EscalationSchemaError):
        ledger.escalate("wo-600", BLOCKER, time_budget_seconds=0)   # 非正
    esc = ledger.escalate("wo-600", BLOCKER, time_budget_seconds=1)  # 显式空增量合法
    assert esc.added_context == () and esc.added_tools == ()


def test_time_budget_must_be_finite(ledger):
    """R7/D1：NaN/±inf 经 ``nan <= 0`` 恒 False 静默入账——『升级必须显式声明
    正时间预算』闸 fail-open；与 D1-R6 已加固的 StormGuard 两浮点参数同口径
    拒绝（巨型 int 不变形为 OverflowError 未归类崩溃）。"""
    for bad in (float("nan"), float("inf"), 10**309):
        with pytest.raises(EscalationSchemaError):
            ledger.escalate("wo-610", BLOCKER, time_budget_seconds=bad)
    assert ledger.case_of("wo-610").history == []        # 拒绝不落账


# ── 人类就绪包（四件套）───────────────────────────────────────────────────────

def _ready_task(task_ref="wo-700", category=HARD_IRREVERSIBLE, facts=True, rev=True):
    return ReadinessTask(
        task_ref=task_ref, title="发布批次 17",
        hard_list_category=category,
        facts=(FrozenFact("预算剩余 200 元", "evidence://ev-1"),) if facts else (),
        reversibility=(ReversibilityAssessment(False, "生产发布不可逆", assessed_by="gk")
                       if rev else None))


def test_readiness_full_package_passes_all_four_pieces(ledger, clock):
    """四件套齐 → PASS：事实固定/范畴清晰/权限内无解/L4 可逆性评估全过，
    且包挂工单 + 决策记录引用。"""
    _climb(ledger, "wo-700", clock=clock)
    pkg = ledger.readiness(_ready_task())
    assert [p.key for p in pkg.pieces] == [
        "facts_fixed", "category_clear", "in_permission_no_solution", "reversibility"]
    assert all(p.verdict == "PASS" for p in pkg.pieces)
    assert pkg.ready and pkg.missing == ()
    assert pkg.task_ref == "wo-700" and pkg.category == HARD_IRREVERSIBLE


def test_readiness_blocked_when_any_field_missing(ledger, clock):
    """字段不齐 = BLOCKED 不放行（fail-closed 聚合：任一件 BLOCKED → 全包 BLOCKED）。"""
    _climb(ledger, "wo-710", clock=clock)
    pkg = ledger.readiness(_ready_task("wo-710", facts=False))
    assert not pkg.ready and "facts_fixed" in pkg.missing
    pkg = ledger.readiness(_ready_task("wo-710", rev=False))
    assert not pkg.ready and "reversibility" in pkg.missing
    # 无台账 case：无解证明不可证 → BLOCKED
    pkg = ledger.readiness(ReadinessTask(task_ref="wo-ghost", title="x",
                                         hard_list_category=HARD_LEGAL_TOS,
                                         facts=(FrozenFact("f", "e://1"),),
                                         reversibility=ReversibilityAssessment(
                                             True, "可回滚", rollback_ref="git revert")))
    assert not pkg.ready and "in_permission_no_solution" in pkg.missing


def test_readiness_in_permission_proof_requires_every_level_exhausted(clock):
    """权限内无解证明是机器可判的：L0..L3 未全部打到级内限次 → BLOCKED；
    打满后同一签名 → PASS。"""
    led = EscalationLedger(now=clock)
    led.attempt("wo-720", BLOCKER)                       # L0 只试 1 次（< 3）
    led.escalate("wo-720", BLOCKER, time_budget_seconds=60)
    _climb(led, "wo-720", clock=clock)                                # 直达 L4（L1 起打满）
    pkg = led.readiness(_ready_task("wo-720"))
    assert not pkg.ready
    piece = next(p for p in pkg.pieces if p.key == "in_permission_no_solution")
    assert piece.verdict == "BLOCKED" and "L0" in piece.detail


def test_readiness_category_outside_hard_list_knocks_back_to_l3(ledger, clock):
    """四类硬清单之外 → 范畴件 BLOCKED，knock_back_to_l3=True（不在表内打回 L3）。"""
    _climb(ledger, "wo-730", clock=clock)
    pkg = ledger.readiness(_ready_task("wo-730", category="style_preference"))
    piece = next(p for p in pkg.pieces if p.key == "category_clear")
    assert piece.verdict == "BLOCKED" and "L3" in piece.detail
    assert not pkg.ready and pkg.knock_back_to_l3
    assert set(HARD_LIST) == {"money", "legal_tos", "irreversible", "theory_approval"}


def test_readiness_reversibility_requires_rollback_ref_when_reversible(ledger, clock):
    """可逆性评估：声称可逆就必须给回滚引用；不可逆只需评估本身在场。"""
    _climb(ledger, "wo-740", clock=clock)
    pkg = ledger.readiness(_ready_task("wo-740", rev=False))
    assert not pkg.ready and "reversibility" in pkg.missing      # 缺评估
    bad = ReadinessTask(task_ref="wo-740", title="x",
                        hard_list_category=HARD_IRREVERSIBLE,
                        facts=(FrozenFact("f", "e://1"),),
                        reversibility=ReversibilityAssessment(True, "可回滚",
                                                             rollback_ref=""))
    pkg = ledger.readiness(bad)
    assert "reversibility" in pkg.missing                        # 声称可逆却无回滚引用
    ok = replace_readiness_task(bad, rollback_ref="git revert <sha>")
    pkg = ledger.readiness(ok)
    assert next(p for p in pkg.pieces
                if p.key == "reversibility").verdict == "PASS"


def replace_readiness_task(task: ReadinessTask, *, rollback_ref: str) -> ReadinessTask:
    from dataclasses import replace
    return replace(task, reversibility=ReversibilityAssessment(
        True, task.reversibility.impact, rollback_ref=rollback_ref))


# ── L4→L5 闸（就绪包是唯一放行形态）──────────────────────────────────────────

def test_escalate_to_l5_requires_ready_readiness_package(clock):
    """fail-closed 主闸：无包 / 包不齐 / 范畴表外 → L4→L5 一律拒绝；
    READY 后放行；L5 是阶梯终点。"""
    led = EscalationLedger(now=clock)
    _climb(led, "wo-800", clock=clock)
    with pytest.raises(EscalationStateError):            # 未 render 包
        clock.advance(led.guard.min_step_interval_seconds + 1)   # 每次尝试都在间隔外（R1-F3）
        led.escalate("wo-800", BLOCKER, time_budget_seconds=60)
    led.readiness(_ready_task("wo-800", facts=False))    # 包不齐 → BLOCKED 登记
    with pytest.raises(EscalationStateError):
        clock.advance(led.guard.min_step_interval_seconds + 1)
        led.escalate("wo-800", BLOCKER, time_budget_seconds=60)
    led.readiness(_ready_task("wo-800", category="tone_of_voice"))  # 表外范畴
    with pytest.raises(EscalationStateError):
        clock.advance(led.guard.min_step_interval_seconds + 1)
        led.escalate("wo-800", BLOCKER, time_budget_seconds=60)
    led.readiness(_ready_task("wo-800"))                 # 四件套齐
    clock.advance(led.guard.min_step_interval_seconds + 1)   # 步进间隔（R1-F3）
    esc = led.escalate("wo-800", BLOCKER, time_budget_seconds=60)
    assert (esc.from_level, esc.to_level) == (LEVEL_L4, LEVEL_L5)
    with pytest.raises(EscalationStateError):            # L5 = 终点
        led.escalate("wo-800", BLOCKER, time_budget_seconds=60)


def test_downgrade_invalidates_readiness_package(clock):
    """回退后现场已变：就绪包作废，重升 L5 必须重 render（fail-closed）。
    （重升在去重窗口外进行——窗口是另一道独立的闸，见签名去重用例。）"""
    led = EscalationLedger(now=clock)
    _climb(led, "wo-810", clock=clock)
    led.readiness(_ready_task("wo-810"))
    clock.advance(led.guard.min_step_interval_seconds + 1)   # 步进间隔（R1-F3）
    led.escalate("wo-810", BLOCKER, time_budget_seconds=60)   # → L5
    led.downgrade("wo-810")                                   # L5→L4
    assert led.case_of("wo-810").ready_package is None
    clock.advance(led.guard.dedup_window_seconds + 1)         # 窗口外：只考察就绪包闸
    with pytest.raises(EscalationStateError):                 # 旧包已作废
        clock.advance(led.guard.dedup_window_seconds + 1)     # 窗口外（去重闸不触发）
        led.escalate("wo-810", BLOCKER, time_budget_seconds=60)
    led.readiness(_ready_task("wo-810"))
    clock.advance(led.guard.min_step_interval_seconds + 1)   # 步进间隔（R1-F3）
    led.escalate("wo-810", BLOCKER, time_budget_seconds=60)   # 重 render 后放行
    assert led.level_of("wo-810") == LEVEL_L5


def test_unknown_case_query_is_an_error(ledger):
    """未知工单查询显式报错（不是静默 L0——台账查询必须诚实）。"""
    with pytest.raises(UnknownEscalationCaseError):
        ledger.case_of("wo-unknown")
    with pytest.raises(UnknownEscalationCaseError):
        ledger.level_of("wo-unknown")


def test_attempt_and_escalation_events_are_audited(clock):
    """全部拦截与放行都留痕（检测 = 拒绝 + 留痕，全仓同款纪律）。"""
    led = EscalationLedger(now=clock)
    for _ in range(led.guard.max_attempts_per_level):
        led.attempt("wo-900", BLOCKER)
    with pytest.raises(EscalationRequiredError):
        led.attempt("wo-900", BLOCKER)
    led.escalate("wo-900", BLOCKER, time_budget_seconds=60)
    kinds = {e.event for e in led.audit}
    assert {"CASE_OPEN", "ATTEMPT", "ATTEMPT_REJECTED", "ESCALATE"} <= kinds
    assert all(e.task_ref == "wo-900" for e in led.audit)     # 事件全部挂工单


# ── grok 红队 R1 复判回归（2026-09-28）────────────────────────────────────────

def test_l5_gate_binds_readiness_package_to_current_blocker(clock):
    """红队 R1-F1：L4→L5 闸必须绑定当前 blocker 签名——用 A 的就绪包给 B 开门被拒；
    换回 A（包重 render 后）放行。"""
    led = EscalationLedger(now=clock)
    blocker_a, blocker_b = {"error": "A", "res": "r"}, {"error": "B", "res": "r"}
    for _ in range(4):
        for _ in range(led.guard.max_attempts_per_level):
            led.attempt("wo-r1", blocker_a)
        clock.advance(led.guard.min_step_interval_seconds + 1)
        led.escalate("wo-r1", blocker_a, time_budget_seconds=60)
    led.readiness(_ready_task("wo-r1"))                    # 包绑定 A 的签名
    with pytest.raises(EscalationStateError):              # 拿 A 的包给 B 开门 → 拒
        clock.advance(led.guard.min_step_interval_seconds + 1)   # 步进间隔外（R1-F3）
        led.escalate("wo-r1", blocker_b, time_budget_seconds=60)
    assert led.level_of("wo-r1") == LEVEL_L4               # 拒绝路径零副作用
    for _ in range(led.guard.max_attempts_per_level):      # B 在各级没有无解证明
        led.attempt("wo-r1", blocker_b)
    clock.advance(led.guard.min_step_interval_seconds + 1)
    led.readiness(_ready_task("wo-r1"))                    # 仍绑定 A（case 最近签名）
    esc = led.escalate("wo-r1", blocker_a, time_budget_seconds=60)   # A 本人升级 → 放行
    assert esc.to_level == LEVEL_L5


def test_per_level_total_cap_bounds_signature_shopping(clock):
    """红队 R1-F2：换 blocker 字段刷新签名无法重置级内限次——总量上限（默认 3×3）
    到顶后，任何新签名的原地尝试同样被拒。"""
    led = EscalationLedger(now=clock)
    for i in range(led.guard.max_total_attempts_per_level):   # 9 次尝试、9 个不同签名
        led.attempt("wo-r2", {"error": "varies", "nonce": i})
    with pytest.raises(EscalationRequiredError):              # 第 10 个签名也被总量上限拦下
        led.attempt("wo-r2", {"error": "brand-new"})
    assert led.case_of("wo-r2").level_totals["L0"] == led.guard.max_total_attempts_per_level


def test_min_step_interval_blocks_rapid_climb(clock):
    """红队 R1-F3：去重窗口内秒级连跳（L0→L4 各源层级不同、去重键拦不住）被
    最小步进间隔拦下；间隔外恢复逐级爬升。"""
    led = EscalationLedger(now=clock)
    for _ in range(led.guard.max_attempts_per_level):
        led.attempt("wo-r3", BLOCKER)
    led.escalate("wo-r3", BLOCKER, time_budget_seconds=60)    # L0→L1
    for _ in range(led.guard.max_attempts_per_level):
        led.attempt("wo-r3", BLOCKER)
    with pytest.raises(EscalationStormError):                 # 0s 后连跳 → 拒
        led.escalate("wo-r3", BLOCKER, time_budget_seconds=60)
    assert led.level_of("wo-r3") == "L1"
    clock.advance(led.guard.min_step_interval_seconds + 1)
    led.escalate("wo-r3", BLOCKER, time_budget_seconds=60)    # 间隔外 → 放行
    assert led.level_of("wo-r3") == "L2"


def test_storm_guard_additional_parameters_validated():
    """红队补充参数的配置纪律：总量上限 ≥ 级内限次；步进间隔非负。"""
    with pytest.raises(EscalationSchemaError):
        StormGuard(max_total_attempts_per_level=2)            # < max_attempts_per_level=3
    g = StormGuard(min_step_interval_seconds=0)               # 0 = 关闭步进间隔（合法）
    assert g.min_step_interval_seconds == 0
