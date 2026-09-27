# coding: utf-8
"""计费 P0 测试（v2.1 §4.4/§7）— 计费 P0.

覆盖：结算模式四枚举门（Python 第一道闸）、internal 按 project 归集、
append-only 不可改断言（ledger 无写路径 + frozen + 输入不被改动）、
影子成本周报（空数据出表头 + 逐字节确定性）、影子价表（档案投影+冲突拒绝）、
费率卡（快照 hash 验签+防篡改+frozen）、deterministic_settle（金额精确、
fail-closed、同输入逐字节一致、**改费率不改历史账单**）。
"""
from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path

import pytest

from jiuwen_glue import (
    KIND_LLM_RELAY,
    RATE_CARD_SCHEMA,
    SETTLEMENT_CLASSES,
    SETTLEMENT_CUSTOMER,
    SETTLEMENT_FREE,
    SETTLEMENT_INTERNAL,
    SETTLEMENT_TRIAL,
    BillingSchemaError,
    BillingSettleError,
    RateCardLine,
    ShadowPriceTable,
    UsageEvent,
    UsageEventRow,
    UsageLedger,
    aggregate_by_project,
    bill_markdown,
    deterministic_settle,
    parse_rate_card,
    rate_card_snapshot_hash,
    select_rate_card,
    settlement_to_json,
    verify_rate_card,
    weekly_report,
)
from jiuwen_glue.usage import (
    KIND_COMPUTE_SECONDS,
    KIND_SANDBOX_SECONDS,
    replace_occurred_at,
)

SRC_BILLING = (Path(__file__).resolve().parents[1] / "src" / "jiuwen_glue" / "billing.py")


def _ev(event_id, quantity, *, occurred_at, project_id=None, customer_id=None,
        settlement_class=SETTLEMENT_INTERNAL, kind=KIND_COMPUTE_SECONDS,
        meter_type=None, shadow_price=0, consumer_key=None, **kw):
    """usage_events 行投影（004+005+006 全列）——归集/周报/结算的标准输入。"""
    return UsageEventRow(
        event_id=event_id, kind=kind, quantity=quantity, occurred_at=occurred_at,
        consumer_key=consumer_key or ("c" if kind == KIND_LLM_RELAY else None),
        project_id=project_id, customer_id=customer_id,
        settlement_class=settlement_class, meter_type=meter_type,
        shadow_price=shadow_price, **kw)


# ── 四模式枚举（Python 第一道闸；DDL 006 CHECK 是第二道）─────────────────────

def test_settlement_enum_python_gate():
    assert SETTLEMENT_CLASSES == ("internal", "customer", "trial", "free")
    # 枚举外拒绝
    with pytest.raises(BillingSchemaError, match="settlement_class"):
        _ev("e1", 1, occurred_at=10, settlement_class="postpaid")
    # customer 模式必须挂客户（无主账单不可存在）
    with pytest.raises(BillingSchemaError, match="customer_id"):
        _ev("e2", 1, occurred_at=10, settlement_class=SETTLEMENT_CUSTOMER)
    # 空白 project_id 拒绝
    with pytest.raises(BillingSchemaError, match="project_id"):
        _ev("e3", 1, occurred_at=10, project_id="   ")
    # 四模式各构造一条，全通过
    for i, sclass in enumerate(SETTLEMENT_CLASSES):
        ev = _ev(f"ok{i}", 1, occurred_at=10 + i, settlement_class=sclass,
                 customer_id="cust-a" if sclass == SETTLEMENT_CUSTOMER else None)
        assert ev.settlement_class == sclass


def test_ledger_carries_and_filters_tenant_fields():
    ledger = UsageLedger()
    ledger.record_compute_seconds(10, project_id="proj-a")
    ledger.record_compute_seconds(20, project_id="proj-b")
    ledger.record_llm_relay(5, "cons-x", customer_id="cust-a",
                            settlement_class=SETTLEMENT_CUSTOMER)
    assert len(ledger.events(project_id="proj-a")) == 1
    assert len(ledger.events(settlement_class=SETTLEMENT_CUSTOMER)) == 1
    assert ledger.events(customer_id="cust-a")[0].quantity == 5
    # replace_occurred_at 携带三字段（签发辅助不丢计费维度）
    ev = ledger.events(project_id="proj-a")[0]
    moved = replace_occurred_at(ev, ev.occurred_at + 1)
    assert (moved.project_id, moved.settlement_class) == ("proj-a", SETTLEMENT_INTERNAL)


# ── append-only 不可改断言 ────────────────────────────────────────────────────

def test_append_only_no_mutation_paths():
    # 1) ledger 公开方法只有 record*/events/summarize/__len__——无 update/delete
    public = {n for n in dir(UsageLedger) if not n.startswith("_")}
    assert not any(n.startswith(("update", "delete", "remove", "amend", "patch"))
                   for n in public), f"ledger 出现写改路径: {sorted(public)}"
    # 2) 计费事件 frozen：任何原地改一律 FrozenInstanceError
    ev = _ev("e1", 1, occurred_at=10, project_id="p")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.quantity = 99
    # 3) 归集与周报不改动输入（幂等只读——重放基线的前提）
    events = [_ev("a", 3, occurred_at=10, project_id="p1", shadow_price=2),
              _ev("b", 4, occurred_at=20, project_id=None, shadow_price=1)]
    before = [(e.event_id, e.quantity, e.project_id, e.shadow_price) for e in events]
    aggregate_by_project(events)
    weekly_report(events, start=0, end=100)
    after = [(e.event_id, e.quantity, e.project_id, e.shadow_price) for e in events]
    assert before == after
    # 4) 模块源码自证：无 now()/time()/random 等不确定性调用（逐字节重算的静态前提）
    code = SRC_BILLING.read_text(encoding="utf-8")
    body = "\n".join(l for l in code.splitlines() if not l.lstrip().startswith(("#", '"')))
    for banned in ("time.time", "datetime.now", "date.today", "random", "uuid"):
        assert banned not in body, f"billing.py 出现不确定调用 {banned!r}"


# ── internal 归集 ─────────────────────────────────────────────────────────────

def test_aggregate_by_project_two_projects_and_unattributed():
    events = [
        _ev("1", 100, occurred_at=10, project_id="proj-a", meter_type="cnb_core_hours",
            shadow_price="0.5", shadow_unit="cny"),
        _ev("2", 50, occurred_at=20, project_id="proj-a", meter_type="cnb_core_hours",
            shadow_price="0.5", shadow_unit="cny"),
        _ev("3", 7, occurred_at=30, project_id="proj-b", kind=KIND_SANDBOX_SECONDS,
            shadow_price=0),
        _ev("4", 9, occurred_at=40, project_id=None),          # 未归集桶
    ]
    result = aggregate_by_project(events)
    assert [u.project_id for u in result] == ["proj-a", "proj-b", None]
    a = result[0]
    assert a.settlement_class == SETTLEMENT_INTERNAL
    assert a.events == 2 and a.quantity == 150
    assert a.rows[0].meter_type == "cnb_core_hours"
    assert a.rows[0].snapshot_cost == Decimal("75.0000")   # 150 × 0.5
    assert result[2].project_id is None and result[2].events == 1
    # 半开窗：start 含、end 不含
    windowed = aggregate_by_project(events, start=10, end=30)
    assert sum(u.events for u in windowed) == 2            # 事件 1、2


# ── 影子价表（resources/ 档案投影）────────────────────────────────────────────

def test_shadow_price_table_from_profiles_conflict_and_lookup():
    profiles = [
        {"resource_id": "cnb-sandbox", "shadow_pricing": [
            {"meter_type": "cnb_core_hours", "unit_price": 0.24, "unit": "cny",
             "source": "doc", "evidence": "2C4G≈0.24 元/时"},
            {"meter_type": "zcode_quota_units", "unit_price": 0, "unit": "quota_unit",
             "source": "declared", "evidence": "套餐内无边际价"}]},
        {"resource_id": "bailian-maas", "shadow_pricing": [
            {"meter_type": "token_upstream_qwen3_max", "unit_price": 10,
             "unit": "cny_per_m_token", "source": "doc", "evidence": "价目页"}]},
    ]
    table = ShadowPriceTable.from_profiles(profiles)
    assert len(table) == 3
    hit = table.lookup(meter_type="cnb_core_hours", kind=KIND_COMPUTE_SECONDS)
    assert hit.unit_price == Decimal("0.24") and "cnb-sandbox" in hit.provenance
    # meter_type 未声明 → kind 兜底 → 仍未声明 = None（如实未估价）
    assert table.lookup(meter_type=None, kind=KIND_SANDBOX_SECONDS) is None
    # 同键冲突价 → 拒绝（结算口径不许含糊）；同键同价 → 幂等接受
    conflict = [profiles[0], {"resource_id": "cnb-sandbox-2", "shadow_pricing": [
        {"meter_type": "cnb_core_hours", "unit_price": 0.99, "unit": "cny",
         "source": "doc", "evidence": "另一价"}]}]
    with pytest.raises(BillingSchemaError, match="conflicting shadow price"):
        ShadowPriceTable.from_profiles(conflict)
    # 无来源单价 → 构造期拒绝（禁止编造）
    with pytest.raises(BillingSchemaError, match="provenance"):
        ShadowPriceTable.build({"x": (1, "cny", "")})


# ── 影子成本周报 ──────────────────────────────────────────────────────────────

def test_weekly_report_empty_input_still_renders_headers():
    report = weekly_report([], start=1_700_000_000, end=1_700_060_480)
    assert "# 影子成本周报" in report
    for header in ("报告窗口", "结算模式：internal", "计量事件：窗口内 0 条",
                   "影子价表：0 条", "| kind | meter_type | 事件数 | 用量合计 | 单位 |",
                   "## 总计", "口径与 [待]", "[待]"):
        assert header in report, f"空数据周报缺表头/口径行: {header!r}"
    assert "影子期红线" in report and "只记录不生效" in report


def test_weekly_report_renders_projects_and_is_byte_identical():
    def mk_events():
        return [_ev("1", 100, occurred_at=10, project_id="proj-a",
                    meter_type="cnb_core_hours", shadow_price="0.5", shadow_unit="cny"),
                _ev("2", 50, occurred_at=20, project_id="proj-a",
                    meter_type="cnb_core_hours", shadow_price="0.5", shadow_unit="cny"),
                _ev("3", 9, occurred_at=30, project_id="proj-b",
                    kind=KIND_SANDBOX_SECONDS, shadow_price=0),
                _ev("4", 3, occurred_at=40, project_id=None, shadow_price=0)]

    table = ShadowPriceTable.build({
        "cnb_core_hours": (0.24, "cny", "cnb-sandbox#shadow_pricing[0] source=doc")})
    kw = dict(start=0, end=100, price_table=table)
    r1 = weekly_report(mk_events(), **kw)
    r2 = weekly_report(mk_events(), **kw)
    assert r1 == r2 and r1.encode("utf-8") == r2.encode("utf-8")   # 逐字节一致
    # 输入顺序打乱 → 输出不变（渲染前全排序）
    shuffled = list(reversed(mk_events()))
    assert weekly_report(shuffled, **kw) == r1
    # 内容断言：两项目 + 未归集桶都在；快照成本 75；价表回填 36（150×0.24）
    assert "### 项目：proj-a（internal）" in r1
    assert "### 项目：proj-b（internal）" in r1
    assert "### 项目：（未归集）" in r1
    assert "| 75.0000 |" in r1 and "| 36.0000 |" in r1
    assert "cnb-sandbox#shadow_pricing[0] source=doc" in r1
    assert "未估价" in r1                                            # 无价行如实标注


# ── 费率卡：解析、验签、防篡改 ────────────────────────────────────────────────

def _card_mapping(unit_price="0.002", snapshot_hash=None, customer="cust-acme",
                  version=1, card_id="rc-cust-acme-20261001"):
    lines = [
        {"line_id": "L1", "meter": "token_upstream_qwen3_max", "unit": "k_token",
         "unit_price": unit_price},
        {"line_id": "L2", "meter": "cnb_core_hours", "unit": "core_hour",
         "unit_price": "0.30", "min_charge": "1.00"},
    ]
    if snapshot_hash is None:
        snapshot_hash = rate_card_snapshot_hash([
            RateCardLine(line_id=l["line_id"], meter=l["meter"], unit=l["unit"],
                         unit_price=Decimal(l["unit_price"]),
                         min_charge=Decimal(l["min_charge"]) if "min_charge" in l else None)
            for l in lines])
    return {
        "schema": RATE_CARD_SCHEMA, "rate_card_id": card_id,
        "customer_id": customer, "currency": "cny", "version": version,
        "effective_from": "2026-10-01T00:00:00+08:00",
        "effective_until": None, "lines": lines,
        "signature": {"algorithm": "sha256", "snapshot_hash": snapshot_hash},
    }


def test_rate_card_parse_verify_and_tamper_detection():
    card = parse_rate_card(_card_mapping())
    assert card.customer_id == "cust-acme" and card.version == 1
    assert verify_rate_card(card) == card.snapshot_hash      # 载入即验签通过
    assert card.covers(card.effective_from) and not card.covers(
        card.effective_from - 1)
    # 篡改一行价格 → 验签失败（快照 hash 是防篡改锚点：行已改、签名还是旧的）
    signed_hash = card.snapshot_hash
    tampered = _card_mapping(unit_price="0.001", snapshot_hash=signed_hash)
    with pytest.raises(BillingSchemaError, match="snapshot hash mismatch"):
        parse_rate_card(tampered)
    # 直接构造 frozen 卡再改行 → FrozenInstanceError（从不原地改）
    line = card.lines[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        line.unit_price = Decimal("999")
    with pytest.raises(dataclasses.FrozenInstanceError):
        card.version = 2
    # hash 缺失/算法不符 → 解析拒绝（注册表纪律：hash 由工具算，禁止手填）
    bad = _card_mapping(snapshot_hash=None)
    bad["signature"] = {"algorithm": "md5", "snapshot_hash": "sha256:x"}
    with pytest.raises(BillingSchemaError, match="sha256"):
        parse_rate_card(bad)


# ── deterministic_settle ──────────────────────────────────────────────────────

CARD_V1_FROM = parse_rate_card(_card_mapping()).effective_from   # 2026-10-01T00:00+08:00


def _usage_window(base=None):
    b = base if base is not None else CARD_V1_FROM + 3600        # 生效窗内 1 小时
    return [
        _ev("u1", 1000, occurred_at=b, customer_id="cust-acme",
            settlement_class=SETTLEMENT_CUSTOMER, kind=KIND_LLM_RELAY,
            consumer_key="cons-acme", meter_type="token_upstream_qwen3_max"),
        _ev("u2", 500, occurred_at=b + 100, customer_id="cust-acme",
            settlement_class=SETTLEMENT_CUSTOMER, kind=KIND_LLM_RELAY,
            consumer_key="cons-acme", meter_type="token_upstream_qwen3_max"),
        _ev("u3", 2, occurred_at=b + 200, customer_id="cust-acme",
            settlement_class=SETTLEMENT_CUSTOMER, meter_type="cnb_core_hours"),
    ]


def test_deterministic_settle_amounts_min_charge_and_strict_unpriced():
    card = parse_rate_card(_card_mapping())
    result = deterministic_settle(_usage_window(), card)
    by_id = {l.line_id: l for l in result.lines}
    assert by_id["L1"].quantity == Decimal("1500.0000")
    assert by_id["L1"].amount == Decimal("3.0000")            # 1500 × 0.002
    assert by_id["L2"].quantity == Decimal("2.0000")
    assert by_id["L2"].amount == Decimal("1.0000")            # 0.60 < min 1.00 → 抬底
    assert by_id["L2"].min_charge_applied is True
    assert result.total == Decimal("4.0000") and result.currency == "cny"
    assert result.rate_card_snapshot_hash == card.snapshot_hash
    # 卡上无行 → strict（默认）fail-closed：报全部缺项，不静默漏
    extra = _usage_window() + [
        _ev("u4", 7, occurred_at=CARD_V1_FROM + 3900, customer_id="cust-acme",
            settlement_class=SETTLEMENT_CUSTOMER, meter_type="unknown_meter")]
    with pytest.raises(BillingSettleError, match="unknown_meter"):
        deterministic_settle(extra, card)
    # strict=False → unpriced 留痕、金额 0（对账用）
    lenient = deterministic_settle(extra, card, strict=False)
    assert lenient.unpriced == ("unknown_meter",)
    assert all("unknown_meter" not in l.meter for l in lenient.lines)
    # 空窗口拒绝结算（无账可出是调用方 bug）
    with pytest.raises(BillingSchemaError, match="empty usage window"):
        deterministic_settle([], card)
    # 事件在卡生效窗外 → fail-closed（调用方必须选"当时生效"的卡）
    stale = [_ev("u9", 1, occurred_at=1_600_000_000, customer_id="cust-acme",
                 settlement_class=SETTLEMENT_CUSTOMER, meter_type="cnb_core_hours")]
    with pytest.raises(BillingSettleError, match="outside rate card"):
        deterministic_settle(stale, card)


def test_settle_reproducible_byte_identical_regardless_of_input_order():
    card = parse_rate_card(_card_mapping())
    window = _usage_window()
    j1 = settlement_to_json(deterministic_settle(window, card))
    j2 = settlement_to_json(deterministic_settle(reversed(window), card))
    j3 = settlement_to_json(deterministic_settle(window, card))
    assert j1 == j2 == j3                                    # 逐字节一致
    assert bill_markdown(deterministic_settle(window, card)) == \
        bill_markdown(deterministic_settle(reversed(window), card))


def test_rate_change_does_not_alter_history():
    """改费率不改历史账单：历史窗用历史卡重算 → 与原始账单逐字节一致；
    新卡重算同窗要么被生效窗拒绝、要么（窗重叠态）产生可检出差异。"""
    card_v1 = parse_rate_card(_card_mapping())
    window = _usage_window()
    original = settlement_to_json(deterministic_settle(window, card_v1))
    # v2 卡：同 meter 提价（新版本新文件——v1 原样留档，一行未动）
    card_v2 = parse_rate_card(_card_mapping(
        unit_price="0.009", version=2, card_id="rc-cust-acme-20261101"))
    assert card_v2.snapshot_hash != card_v1.snapshot_hash
    # 历史窗仍由 v1（当时生效卡）重算 → 与原始账单逐字节一致
    replay = settlement_to_json(deterministic_settle(window, card_v1))
    assert replay == original
    # v1 卡对象自始至终不可变：卡上价格与 v2 互不影响
    assert card_v1.lines[0].unit_price == Decimal("0.002")
    # 用 v2 重算 v1 的历史窗 → 生效窗 fail-closed 拒绝（历史账单不可被新卡悄悄覆盖）
    v2_shifted = parse_rate_card({
        **_card_mapping(unit_price="0.009", version=2,
                        card_id="rc-cust-acme-overlap"),
        "effective_from": "2026-09-01T00:00:00+08:00",
        "effective_until": "2026-12-01T00:00:00+08:00"})
    assert settlement_to_json(deterministic_settle(window, v2_shifted)) != original
    # select_rate_card 在重叠态显式报错（重叠在注册表里就是非法态）
    with pytest.raises(BillingSettleError, match="overlap"):
        select_rate_card([card_v1, v2_shifted], at=CARD_V1_FROM + 3600)


def test_rate_card_rejects_placeholder_customer():
    """红队 finding B7（grok R2）修复：占位客户卡在构造期即拒——
    README 规则"占位符出现在任何实卡中即为无效卡"由代码强制。"""
    for bogus in ("[待客户]", "<customer_id>", "cust-[待定]"):
        with pytest.raises(BillingSchemaError, match="placeholder marker"):
            parse_rate_card(_card_mapping(customer=bogus))


def test_select_rate_card_effective_windows():
    v1 = parse_rate_card(_card_mapping(
        card_id="rc-seq-1", version=1))                       # from 2026-10-01, open end
    v1_closed = parse_rate_card({
        **_card_mapping(card_id="rc-seq-1", version=1),
        "effective_until": "2026-11-01T00:00:00+08:00"})
    v2 = parse_rate_card({
        **_card_mapping(unit_price="0.009", card_id="rc-seq-2", version=2),
        "effective_from": "2026-11-01T00:00:00+08:00"})
    at_v1 = v1_closed.effective_from + 60
    at_v2 = v2.effective_from + 60
    assert select_rate_card([v1_closed, v2], at=at_v1) is v1_closed
    assert select_rate_card([v1_closed, v2], at=at_v2) is v2
    with pytest.raises(BillingSettleError, match="no rate card effective"):
        select_rate_card([v2], at=at_v1)                       # 空洞不结算
    with pytest.raises(BillingSettleError, match="no rate card effective"):
        select_rate_card([], at=at_v1)


def test_trial_and_free_classes_flow_through_aggregation():
    """trial/free 两模式进归集（四模式全景在周报可见），internal 为主桶。"""
    events = [
        _ev("t1", 60, occurred_at=10, project_id="proj-trial",
            settlement_class=SETTLEMENT_TRIAL, meter_type="cnb_core_hours",
            shadow_price="0.24", shadow_unit="cny"),
        _ev("f1", 1000, occurred_at=10, project_id="proj-a",
            settlement_class=SETTLEMENT_FREE, kind=KIND_LLM_RELAY,
            consumer_key="cons-a", meter_type="qwen_free_window", shadow_price=0),
    ]
    usage = aggregate_by_project(events)
    assert [(u.project_id, u.settlement_class) for u in usage] == \
        [("proj-a", SETTLEMENT_FREE), ("proj-trial", SETTLEMENT_TRIAL)]
    assert usage[1].rows[0].snapshot_cost == Decimal("14.4000")   # 60 × 0.24
    report = weekly_report(events, start=0, end=100)
    assert "proj-trial（trial）" in report and "proj-a（free）" in report
