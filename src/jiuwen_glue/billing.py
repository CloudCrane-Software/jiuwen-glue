# coding: utf-8
"""billing — 计费 P0：internal 按 project 归集 + 影子成本周报 + 确定性结算骨架
（v2.1 §4.4 计量字段 / §7 资源控制面：影子价格、上游计量）— 计费 P0.

四模式（settlement_class，与 DDL 006 CHECK 同源，见 :mod:`jiuwen_glue.usage`）：
``internal``（自身消耗，本模块主模式）/ ``customer``（外部客户，费率卡结算，
首个客户 [待客户]，P1）/ ``trial``（试验车道，v2.1 §7）/ ``free``（免费窗）。

三件能力（全部纯函数，零网络、零时钟、零 I/O——同输入恒同输出）:

1. :func:`aggregate_by_project` —— internal 模式按 租户×project 归集
   （对应 SQL：``SELECT tenant_id, project_id, kind, meter_type, count(*),
   sum(quantity), sum(quantity*shadow_price) FROM glue.usage_event
   GROUP BY tenant_id, ...``；
   落库视图 ``glue.v_usage_by_project`` 同构（按 租户×模式×项目×kind 分组），
   DDL 见 CNB company-ops ``ops/sql/006_billing_p0.sql``）。
2. :func:`weekly_report` —— 影子成本周报（markdown）：输入=计量事件×
   :class:`ShadowPriceTable`（resources/ 档案 ``shadow_pricing`` 段投影），
   输出=每 租户×项目 用量×影子成本表。**影子期红线：周报只记录，不驱动任何
   调度策略调整**（v2.1 施工红线）；报告内无生成时刻——时间只来自入参
   窗口，同输入两次渲染逐字节一致。
3. :func:`deterministic_settle` —— 结算引擎骨架：``deterministic_settle(
   usage_window, rate_card)`` 纯函数，用量 × 当时生效费率卡 → 账单行
   （Decimal 定点运算，逐字节可重算）。"当时生效"由调用方以
   :func:`select_rate_card` 选取并传入；事件落在卡生效窗外一律
   fail-closed 拒绝（结算不允许含糊）。

费率卡（:class:`RateCard`）纪律（注册表规范见 CNB company-ops ``billing/``）：

- 版本化 + 生效窗口 + **签名快照 hash**（:func:`rate_card_snapshot_hash`，
  sha256(canonical JSON of lines)）；:func:`parse_rate_card` 载入即验 hash，
  改一行价格即验签失败。
- **从不原地改**：卡是 frozen dataclass，无任何变更路径；改价 = 新版本
  新文件（v2），历史账单永远可用历史卡重算（可重算性）。

边界（写死）：

- 内存版是权威实现；Postgres 侧第二道闸是 DDL 006（枚举 CHECK +
  append-only 触发器不放松）。本模块不做金额汇出/开票，不碰任何网关。
- 凭证纪律：customer_id 是客户**名称引用**，密钥明文永不进计费对象。
- 影子价格是稀缺度归集口径（v2.0 §2 术语），**不是货币承诺**；快照未估价
  的事件在周报中如实标"未估价"，本模块不编造单价。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import (Iterable, Mapping, Optional, Sequence, Tuple)

from .errors import BillingSchemaError, BillingSettleError
from .usage import (SETTLEMENT_CLASSES, SETTLEMENT_INTERNAL, USAGE_KINDS,
                    UsageSchemaError)


def _finite(v: object) -> bool:
    """有限性闸（与 escalation.StormGuard / challenge 同口径）：NaN/±inf 一律
    非有限；巨型 int 经 math.isfinite 抛 OverflowError——按非有限同拒（D1-R6
    PR#15 纪律：schema 错误不得变形为未归类崩溃）。"""
    try:
        return math.isfinite(v)  # type: ignore[arg-type]
    except (OverflowError, TypeError):   # 巨型 int / 非数值类型 → 非有限
        return False

__all__ = [
    "SETTLEMENT_CLASSES", "SETTLEMENT_INTERNAL",
    "RATE_CARD_SCHEMA", "SETTLE_ENGINE", "UNATTRIBUTED_LABEL",
    "MONEY_QUANTUM", "REPORT_SHADOW_BANNER",
    "UsageEventRow", "ShadowPrice", "ShadowPriceTable",
    "UsageAggRow", "ProjectUsage",
    "aggregate_by_project", "weekly_report",
    "RateCardLine", "RateCard",
    "rate_card_snapshot_hash", "parse_rate_card", "verify_rate_card",
    "select_rate_card", "BillLine", "SettlementResult",
    "deterministic_settle", "settlement_to_json", "bill_markdown",
]

# 结算引擎标识（写进结算结果的固定常量——非版本号查询、非时钟）
SETTLE_ENGINE = "jiuwen_glue.billing/deterministic-v1"
RATE_CARD_SCHEMA = "company-ops/rate-card/v1"
UNATTRIBUTED_LABEL = "（未归集）"          # project_id IS NULL 的如实呈现
MONEY_QUANTUM = Decimal("0.0001")          # 金额 4 位小数（半进位，写死）
REPORT_SHADOW_BANNER = (
    "影子期红线：本报告只记录不生效——任何调度策略调整在影子价格与利用率"
    "周报正式上线前一律挂起（v2.1 施工红线）")


# ── 确定性基元 ────────────────────────────────────────────────────────────────

def _dec(value: object, what: str) -> Decimal:
    """转定点：float 经 str 转写（ Decimal(str(0.1))==0.1 精确入账），bool 拒绝。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise BillingSchemaError(f"{what} must be a number, got {value!r}")
    try:
        d = Decimal(str(value))
    except Exception as exc:                      # noqa: BLE001 — 原样带出
        raise BillingSchemaError(f"{what} is not parseable as Decimal: {value!r}") from exc
    if not d.is_finite():
        raise BillingSchemaError(f"{what} must be finite, got {value!r}")
    return d


def _q4(value: Decimal) -> Decimal:
    """金额口径：4 位小数 ROUND_HALF_UP（写死——改口径=改引擎版本，不原地换）。"""
    return Decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _fmt(value: Decimal) -> str:
    """定点格式化（固定 'f' 记法，无指数；-0 归 0）。"""
    if value == 0:
        value = Decimal(0)
    return format(value, "f")


def _canonical_json(obj: object) -> str:
    """canonical JSON（键排序 + 紧凑分隔 + ASCII 转义）——hash 与序列化的唯一底座。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _meter_key_of(event: object) -> str:
    """计费项键：meter_type 优先（计量器身份），缺省回退 kind（四维度）。"""
    meter = getattr(event, "meter_type", None)
    if isinstance(meter, str) and meter.strip():
        return meter
    kind = getattr(event, "kind", None)
    if isinstance(kind, str) and kind.strip():
        return kind
    raise BillingSchemaError(f"event {event!r} carries neither meter_type nor kind")


def _iso_utc(epoch: float) -> str:
    """epoch → ISO8601 UTC（只依赖入参，不读时钟）。"""
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


def _parse_iso(value: object, what: str) -> float:
    """ISO8601 → epoch（容 'Z' 后缀）；无时区按 UTC（卡内一律写全时区，README 约定）。"""
    if not isinstance(value, str) or not value.strip():
        raise BillingSchemaError(f"{what} must be an ISO8601 string, got {value!r}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BillingSchemaError(f"{what} is not ISO8601: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ── usage_events 行投影（004+005+006 全列的内存形态）─────────────────────────

@dataclass(frozen=True)
class UsageEventRow:
    """pg ``glue.usage_event`` 一行的内存投影（归集/周报/结算的标准输入）。

    内存权威 :class:`jiuwen_glue.usage.UsageEvent` 只有 004 四维度形状；
    本投影补齐 005 计量列（meter_type/window_id/expires_at/影子成本快照）与
    006 结算三列，供装载器（NDJSON/psql）直接灌入。与 UsageEvent 一样
    frozen、只读、无更新路径。
    """

    event_id: str
    kind: str
    quantity: float
    occurred_at: float
    tenant_id: str = "t0"
    consumer_key: Optional[str] = None
    lease_ref: Optional[str] = None
    node_ref: Optional[str] = None
    task_ref: Optional[str] = None
    meter_type: Optional[str] = None
    window_id: Optional[str] = None
    expires_at: Optional[float] = None
    shadow_price: object = 0            # Decimal 可接受 str/int/float；_dec 统一转
    shadow_unit: Optional[str] = None
    customer_id: Optional[str] = None
    project_id: Optional[str] = None
    settlement_class: str = SETTLEMENT_INTERNAL
    meta: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_id or not isinstance(self.event_id, str):
            raise BillingSchemaError(f"event_id must be non-empty, got {self.event_id!r}")
        if self.kind not in USAGE_KINDS:
            raise UsageSchemaError(
                f"usage kind must be one of {USAGE_KINDS}, got {self.kind!r}")
        if isinstance(self.quantity, bool) or \
                not isinstance(self.quantity, (int, float)) or \
                not _finite(self.quantity) or \
                not self.quantity >= 0:
            raise BillingSchemaError(
                f"quantity must be a finite number >= 0, got {self.quantity!r}")
        # R7/D1：occurred_at 非有限（NaN/±inf/巨型 int）使半开窗判定
        # ``nan < start``/``nan >= end`` 双双 False——同一事件落进每一个窗口，
        # 不相交窗重复计费，「可重算」不变式破缺；构造期即拒（fail-closed）。
        if not _finite(self.occurred_at) or self.occurred_at < 0:
            raise BillingSchemaError(
                "occurred_at must be a finite non-negative epoch")
        if self.settlement_class not in SETTLEMENT_CLASSES:
            raise BillingSchemaError(
                f"settlement_class must be one of {SETTLEMENT_CLASSES}, "
                f"got {self.settlement_class!r}")
        if self.settlement_class == "customer" and \
                (not self.customer_id or not isinstance(self.customer_id, str)):
            raise BillingSchemaError(
                "settlement_class='customer' requires customer_id "
                "(a bill without a customer must not exist)")
        for name in ("customer_id", "project_id"):
            v = getattr(self, name)
            if v is not None and (not isinstance(v, str) or not v.strip()):
                raise BillingSchemaError(
                    f"{name} must be a non-empty string when given, got {v!r}")


# ── 影子价格表（resources/ 档案投影）─────────────────────────────────────────

@dataclass(frozen=True)
class ShadowPrice:
    """一条影子单价（稀缺度归集口径，非货币承诺）。

    ``key`` 是计费项键（meter_type 或 kind）；``provenance`` 记来源
    （档案 resource_id#段 source=...），周报逐行披露，禁止无来源单价。
    """

    key: str
    unit_price: Decimal
    unit: str
    provenance: str

    def __post_init__(self) -> None:
        if not self.key or not isinstance(self.key, str):
            raise BillingSchemaError(f"shadow price key invalid: {self.key!r}")
        if self.unit_price < 0:
            raise BillingSchemaError(
                f"shadow price for {self.key!r} must be >= 0, got {self.unit_price}")
        if not self.unit or not isinstance(self.unit, str):
            raise BillingSchemaError(f"shadow price unit invalid for {self.key!r}")
        if not self.provenance or not isinstance(self.provenance, str):
            raise BillingSchemaError(
                f"shadow price for {self.key!r} lacks provenance (无来源单价禁止)")


class ShadowPriceTable:
    """只读影子价格表：key → ShadowPrice；键查找 meter_type 优先、kind 兜底。

    构造入口都是纯函数：:meth:`build`（显式映射）与 :meth:`from_profiles`
    （资源档案映射列表——YAML 由调用方解析成本 dict，本包零依赖）。
    """

    def __init__(self, entries: Mapping[str, ShadowPrice]) -> None:
        self._entries: Tuple[Tuple[str, ShadowPrice], ...] = tuple(
            sorted(entries.items()))                # 排序冻结 → 迭代顺序确定

    @classmethod
    def build(cls, table: Mapping[str, Sequence[object]]) -> "ShadowPriceTable":
        """从显式映射构建：{key: (unit_price, unit, provenance)}。"""
        entries = {}
        for key, spec in table.items():
            if not isinstance(spec, Sequence) or isinstance(spec, str) or len(spec) != 3:
                raise BillingSchemaError(
                    f"shadow price entry for {key!r} must be (price, unit, provenance)")
            entries[key] = ShadowPrice(
                key=key, unit_price=_dec(spec[0], f"shadow price for {key!r}"),
                unit=str(spec[1]), provenance=str(spec[2]))
        return cls(entries)

    @classmethod
    def from_profiles(cls, profiles: Iterable[Mapping]) -> "ShadowPriceTable":
        """从 resources/ 资源档案（已解析为 dict）投影 ``shadow_pricing`` 段。

        段内每条：``{meter_type, unit_price, unit, source, evidence}``（source
        取值同档案规范 probed/declared/doc）；冲突键且单价不同 → 拒绝（两份
        档案对同一计量器各报一个价，结算口径不许含糊）。
        """
        entries: dict = {}
        for profile in profiles:
            if not isinstance(profile, Mapping):
                raise BillingSchemaError("profile must be a mapping, got "
                                         f"{type(profile).__name__}")
            rid = profile.get("resource_id")
            if not rid or not isinstance(rid, str):
                raise BillingSchemaError("profile lacks resource_id")
            section = profile.get("shadow_pricing") or []
            if not isinstance(section, Sequence) or isinstance(section, str):
                raise BillingSchemaError(
                    f"profile {rid!r} shadow_pricing must be a list")
            for i, item in enumerate(section):
                if not isinstance(item, Mapping):
                    raise BillingSchemaError(
                        f"profile {rid!r} shadow_pricing[{i}] is not a mapping")
                key = item.get("meter_type")
                if not key or not isinstance(key, str):
                    raise BillingSchemaError(
                        f"profile {rid!r} shadow_pricing[{i}] lacks meter_type")
                provenance = (f"{rid}#shadow_pricing[{i}] "
                              f"source={item.get('source', 'undeclared')}")
                price = ShadowPrice(
                    key=key, unit_price=_dec(item.get("unit_price"),
                                             f"shadow_pricing[{i}].unit_price of {rid}"),
                    unit=str(item.get("unit", "")), provenance=provenance)
                prev = entries.get(key)
                if prev is not None and prev.unit_price != price.unit_price:
                    raise BillingSchemaError(
                        f"conflicting shadow price for {key!r}: "
                        f"{prev.unit_price} ({prev.provenance}) vs "
                        f"{price.unit_price} ({provenance})")
                entries[key] = price
        return cls(entries)

    def get(self, key: str) -> Optional[ShadowPrice]:
        for k, price in self._entries:
            if k == key:
                return price
        return None

    def lookup(self, *, meter_type: Optional[str], kind: Optional[str]) -> Optional[ShadowPrice]:
        """meter_type 精确优先，kind 兜底（四维度粗口径）。"""
        if meter_type:
            hit = self.get(meter_type)
            if hit is not None:
                return hit
        if kind:
            return self.get(kind)
        return None

    def __len__(self) -> int:
        return len(self._entries)

    def items(self) -> Tuple[Tuple[str, ShadowPrice], ...]:
        return self._entries


# ── internal 归集 ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UsageAggRow:
    """租户×项目×kind×meter_type 聚合行（v_usage_by_project 同构的内存投影）。

    权威视图按 租户×模式×项目×kind 分组（006_billing_p0.sql），本行携带
    所属租户——tenant_id 列全对象贯通（v2.0 §4.2 行5），聚合边界不断链。
    """

    tenant_id: str
    kind: str
    meter_type: Optional[str]
    events: int
    quantity: float
    snapshot_cost: Decimal            # Σ quantity × event.shadow_price（写时快照）
    shadow_unit: Optional[str]        # 事件影子单位样本（与视图 MAX 口径一致）
    first_at: float
    last_at: float


@dataclass(frozen=True)
class ProjectUsage:
    """单个项目的归集结果（project_id=None 即"未归集"桶，如实保留）。

    tenant_id 参与分组：跨租户同名 project_id 各自成桶，成本归集不串户
    （与 v_usage_by_project 的 租户×模式×项目 分组同口径）。
    """

    tenant_id: str
    project_id: Optional[str]
    settlement_class: str
    rows: Tuple[UsageAggRow, ...]
    events: int
    quantity: float
    snapshot_cost: Decimal


def _in_window(event: object, start: Optional[float], end: Optional[float]) -> bool:
    # R7/D1：非有限时间戳无法落入任何确定的半开窗（nan 与一切比较均为 False
    # → 原实现使其落入**每一个**窗口，不相交窗重复计费）——duck-typed 装载
    # 路径（aggregate_by_project 的 SimpleNamespace 契约）与行构造同口径
    # fail-closed：拒算而非多计/漏计（billing 不允许含糊）。
    # R7/D1 二轮（grok）：float() 转换本身可抛 OverflowError（巨型 int）/
    # TypeError（datetime 等非数值）/ValueError（不可解析 str）——包转为同一条
    # schema 错误，不再变形为未归类崩溃（承 PR#15 纪律）。
    raw = getattr(event, "occurred_at", 0.0) or 0.0
    try:
        at = float(raw)
    except (OverflowError, TypeError, ValueError) as exc:
        raise BillingSchemaError(
            f"event occurred_at must be a finite epoch to be windowed, "
            f"got {raw!r}") from exc
    if not math.isfinite(at):
        raise BillingSchemaError(
            f"event occurred_at must be a finite epoch to be windowed, got {at!r}")
    if start is not None and at < start:
        return False
    if end is not None and at >= end:
        return False
    return True


def aggregate_by_project(events: Iterable[object], *,
                         start: Optional[float] = None,
                         end: Optional[float] = None) -> Tuple[ProjectUsage, ...]:
    """按 project 归集（半开窗 [start, end)，均只读，不改输入顺序外任何状态）。

    未声明 project_id 的事件落入 ``project_id=None`` 桶（未归集如实可见，
    不悄悄丢弃）；settlement_class 参与分组（internal/trial/free 各自成桶）；
    tenant_id 同样参与分组（缺省/空白归 ``t0``，与 DDL 004 ``DEFAULT 't0'``
    同口径）——跨租户同名 project_id 各自成桶，成本归集不串户
    （与 v_usage_by_project 的 租户×模式×项目×kind 分组同口径）。
    """
    materialized = tuple(events)
    groups: dict = {}
    for e in materialized:
        if not _in_window(e, start, end):
            continue
        project = getattr(e, "project_id", None)
        project = project if isinstance(project, str) and project.strip() else None
        tenant = getattr(e, "tenant_id", None)
        tenant = tenant if isinstance(tenant, str) and tenant.strip() else "t0"
        sclass = getattr(e, "settlement_class", SETTLEMENT_INTERNAL)
        if sclass not in SETTLEMENT_CLASSES:
            raise BillingSchemaError(
                f"event carries settlement_class outside the four-mode enum: {sclass!r}")
        kind = getattr(e, "kind", None)
        meter = getattr(e, "meter_type", None)
        meter = meter if isinstance(meter, str) and meter.strip() else None
        quantity = float(getattr(e, "quantity", 0.0))
        shadow_price = _dec(getattr(e, "shadow_price", 0) or 0, "event.shadow_price")
        shadow_unit = getattr(e, "shadow_unit", None)
        shadow_unit = shadow_unit if isinstance(shadow_unit, str) and shadow_unit else None
        at = float(getattr(e, "occurred_at", 0.0) or 0.0)
        gkey = (tenant, project, sclass)
        rkey = (kind, meter)
        g = groups.setdefault(gkey, {"rows": {}, "events": 0, "quantity": 0.0})
        row = g["rows"].setdefault(rkey, {
            "events": 0, "quantity": 0.0, "snapshot_cost": Decimal(0),
            "shadow_unit": None, "first_at": at, "last_at": at})
        row["events"] += 1
        row["quantity"] += quantity
        row["snapshot_cost"] += _dec(quantity, "event.quantity") * shadow_price
        row["last_at"] = max(row["last_at"], at)
        row["first_at"] = min(row["first_at"], at)
        if shadow_unit is not None:
            row["shadow_unit"] = shadow_unit       # MAX 语义样本（同视图）
        g["events"] += 1
        g["quantity"] += quantity

    out = []
    for (tenant, project, sclass) in sorted(groups, key=lambda k: (
            k[0], k[1] is None, k[1] or "", k[2])):
        g = groups[(tenant, project, sclass)]
        rows = []
        for (kind, meter) in sorted(g["rows"], key=lambda k: (
                USAGE_KINDS.index(k[0]) if k[0] in USAGE_KINDS else len(USAGE_KINDS),
                k[0] or "", k[1] or "")):
            r = g["rows"][(kind, meter)]
            rows.append(UsageAggRow(
                tenant_id=tenant, kind=kind, meter_type=meter, events=r["events"],
                quantity=r["quantity"], snapshot_cost=_q4(r["snapshot_cost"]),
                shadow_unit=r["shadow_unit"], first_at=r["first_at"],
                last_at=r["last_at"]))
        out.append(ProjectUsage(
            tenant_id=tenant, project_id=project, settlement_class=sclass,
            rows=tuple(rows),
            events=g["events"], quantity=g["quantity"],
            snapshot_cost=_q4(sum((r.snapshot_cost for r in rows), Decimal(0)))))
    return tuple(out)


# ── 影子成本周报 ──────────────────────────────────────────────────────────────

def _fmt_num(value: float) -> str:
    """浮点数量的稳定呈现：Decimal(str(x)) 规范化，避免 0.30000000000004 噪声。"""
    if value == 0:
        return "0"
    d = Decimal(str(value)).normalize()
    return format(d, "f")


def _render_project_section(usage: ProjectUsage, table: Optional[ShadowPriceTable],
                            lines: list) -> Decimal:
    """渲染单项目小节；返回该项目价表口径成本（命中行全量重估对照，
    含快照已估价行；快照成本在 ProjectUsage 内）。"""
    label = usage.project_id if usage.project_id else UNATTRIBUTED_LABEL
    lines.append(
        f"### 项目：{label}（{usage.settlement_class}，租户 {usage.tenant_id}）\n")
    lines.append("| kind | meter_type | 事件数 | 用量合计 | 单位 | "
                 "影子成本(快照) | 影子成本(价表) | 计价来源 |")
    lines.append("|---|---|---:|---:|---|---:|---:|---|")
    table_cost_total = Decimal(0)
    for r in usage.rows:
        price = table.lookup(meter_type=r.meter_type, kind=r.kind) if table else None
        if price is not None:
            table_cost = _q4(price.unit_price * _dec(r.quantity, "row.quantity"))
            table_cost_total += table_cost
            cost_table_cell, source_cell = _fmt(table_cost), price.provenance
            unit_cell = price.unit
        else:
            cost_table_cell, source_cell = "未估价", "-"
            unit_cell = r.shadow_unit or "-"
        lines.append("| {k} | {m} | {e} | {q} | {u} | {sc} | {tc} | {src} |".format(
            k=r.kind, m=r.meter_type or "-", e=r.events,
            q=_fmt_num(r.quantity), u=unit_cell,
            sc=_fmt(r.snapshot_cost), tc=cost_table_cell, src=source_cell))
    lines.append("")
    lines.append(f"小计：事件 {usage.events} 条；快照成本 {_fmt(usage.snapshot_cost)}"
                 + (f"；价表成本 {_fmt(table_cost_total)}" if table else ""))
    lines.append("")
    return table_cost_total


def weekly_report(events: Iterable[object], *, start: float, end: float,
                  price_table: Optional[ShadowPriceTable] = None,
                  title: str = "影子成本周报") -> str:
    """影子成本周报（markdown；纯函数——无时钟无随机，同输入逐字节一致）。

    输入 = 计量事件（usage_events 行投影）× :class:`ShadowPriceTable`
    （resources/ 档案 ``shadow_pricing`` 段投影）。窗口为半开区间
    ``[start, end)``；空输入同样出全部表头（周报骨架恒完整）。
    """
    # R7/D1：窗口边界非有限时 ``end <= start`` 对 NaN 恒 False 静默放行，
    # 半开窗语义失效——与事件侧同口径 fail-closed（报告窗口必须确定可分）。
    if not _finite(start) or not _finite(end) or end <= start:
        raise BillingSchemaError(
            f"report window must be finite with end after start, got {start!r} ~ {end!r}")
    materialized = tuple(events)
    usage_all = aggregate_by_project(materialized, start=start, end=end)
    excluded = sum(1 for e in materialized if not _in_window(e, start, end))

    lines = [f"# {title}（internal 归集 · 影子期）", ""]
    lines.append(f"- 报告窗口：{_iso_utc(start)} ~ {_iso_utc(end)}（UTC，半开区间）")
    lines.append(f"- 结算模式：internal（自身消耗，影子成本口径）；"
                 f"customer 结算未启用（首个客户 [待客户]，启用 [待 owner 批]）")
    lines.append(f"- 计量事件：窗口内 {sum(u.events for u in usage_all)} 条"
                 + (f"；窗口外排除 {excluded} 条（不进本报告）" if excluded else ""))
    lines.append(f"- 影子价表：{len(price_table) if price_table else 0} 条"
                 "（resources/ 档案 shadow_pricing 段投影；未声明计量器如实标\"未估价\"）")
    lines.append(f"- {REPORT_SHADOW_BANNER}")
    lines.append("")
    lines.append("## 项目归集（用量 × 影子成本）")
    lines.append("")
    if not usage_all:
        lines.append("_（本窗口无计量事件——空表骨架如下，口径不变。）_")
        lines.append("")
        lines.append("| kind | meter_type | 事件数 | 用量合计 | 单位 | "
                     "影子成本(快照) | 影子成本(价表) | 计价来源 |")
        lines.append("|---|---|---:|---:|---|---:|---:|---|")
        lines.append("")
    table_cost_all = Decimal(0)
    for usage in usage_all:
        table_cost_all += _render_project_section(usage, price_table, lines)

    lines.append("## 总计")
    lines.append("")
    lines.append("| 口径 | 影子成本 |")
    lines.append("|---|---:|")
    lines.append(f"| 快照（event.shadow_price 写时快照求和） | "
                 f"{_fmt(sum((u.snapshot_cost for u in usage_all), Decimal(0)))} |")
    lines.append(f"| 价表（命中 ShadowPriceTable 事件按价表单价×用量全量重估——"
                 f"对照口径，含快照已估价行） | {_fmt(table_cost_all)} |")
    lines.append("")
    lines.append("## 口径与 [待]")
    lines.append("")
    lines.append("- 快照成本 = Σ quantity × event.shadow_price（005 起写时快照、读时计算；"
                 "影子价格是稀缺度归集口径，非货币承诺）。")
    lines.append("- 快照与价表两列**分列呈现不混算**：快照列=写时快照求和；"
                 "价表列=命中 ShadowPriceTable 的行一律按「价表单价×用量」全量重估"
                 "（对照口径，含快照已估价行），未命中价表的行如实标\"未估价\"，"
                 "命中来源逐行披露。")
    lines.append("- [待] 影子价格表来源档案 resources/ 各档案 shadow_pricing 段——"
                 "首批档案（W-03）尚未声明该段，本报告价表成本多为\"未估价\"属如实呈现。")
    lines.append("- [待] customer 模式结算与首个客户费率卡（company-ops billing/ 注册表，"
                 "格式已定稿，启用 [待客户+owner 批]）。")
    return "\n".join(lines) + "\n"


# ── 费率卡（注册表规范见 company-ops billing/README.md）───────────────────────

@dataclass(frozen=True)
class RateCardLine:
    """费率卡一行价：``{line_id, meter, unit, unit_price, min_charge?}``。"""

    line_id: str
    meter: str
    unit: str
    unit_price: Decimal
    min_charge: Optional[Decimal] = None

    def __post_init__(self) -> None:
        for name in ("line_id", "meter", "unit"):
            v = getattr(self, name)
            if not v or not isinstance(v, str):
                raise BillingSchemaError(f"rate card line {name} must be non-empty, got {v!r}")
        if self.unit_price < 0:
            raise BillingSchemaError(
                f"rate card line {self.line_id!r} unit_price must be >= 0")
        if self.min_charge is not None and self.min_charge < 0:
            raise BillingSchemaError(
                f"rate card line {self.line_id!r} min_charge must be >= 0")

    def to_canonical(self) -> dict:
        return {
            "line_id": self.line_id, "meter": self.meter, "unit": self.unit,
            "unit_price": _fmt(self.unit_price),
            "min_charge": _fmt(self.min_charge) if self.min_charge is not None else None,
        }


def rate_card_snapshot_hash(lines: Sequence[RateCardLine]) -> str:
    """签名快照 hash：sha256(canonical JSON of lines 按 line_id 排序)。

    这是"快照"的唯一算法（写死）：改任何一行价格/单位/最低消费 → hash 变；
    解析时验签（:func:`verify_rate_card`），从不原地改的锚点。
    """
    payload = [line.to_canonical() for line in
               sorted(lines, key=lambda l: l.line_id)]
    return "sha256:" + hashlib.sha256(
        _canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RateCard:
    """费率卡（frozen——**从不原地改**；改价 = 新版本新文件）。"""

    schema: str
    rate_card_id: str
    customer_id: str
    currency: str
    version: int
    effective_from: float                 # epoch 秒（含）
    effective_until: Optional[float]      # epoch 秒（不含）；None = 未设终止
    lines: Tuple[RateCardLine, ...]
    snapshot_hash: str

    def __post_init__(self) -> None:
        if self.schema != RATE_CARD_SCHEMA:
            raise BillingSchemaError(
                f"rate card schema must be {RATE_CARD_SCHEMA!r}, got {self.schema!r}")
        for name in ("rate_card_id", "customer_id", "currency"):
            v = getattr(self, name)
            if not v or not isinstance(v, str):
                raise BillingSchemaError(f"rate card {name} must be non-empty")
        if any(marker in self.customer_id for marker in ("<", "[待")):
            raise BillingSchemaError(
                f"rate card customer_id carries a placeholder marker: "
                f"{self.customer_id!r} (占位卡无效——billing/README.md：占位符出现"
                f"即为无效卡，永不入结算)")
        if not isinstance(self.version, int) or isinstance(self.version, bool) \
                or self.version < 1:
            raise BillingSchemaError(f"rate card version must be int >= 1")
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise BillingSchemaError(
                "rate card effective_until must be after effective_from")
        line_ids = [l.line_id for l in self.lines]
        if len(line_ids) != len(set(line_ids)):
            raise BillingSchemaError("rate card line_id duplicated")
        if not self.lines:
            raise BillingSchemaError("rate card must carry at least one line")
        verify_rate_card(self)             # 载入即验签（构造期定稿）

    def covers(self, at: float) -> bool:
        if at < self.effective_from:
            return False
        return self.effective_until is None or at < self.effective_until

    def line_for(self, meter: str) -> Optional[RateCardLine]:
        for line in self.lines:
            if line.meter == meter:
                return line
        return None


def verify_rate_card(card: RateCard) -> str:
    """重算快照 hash 并与卡上声明比对；不符即 BillingSchemaError（防篡改锚点）。"""
    computed = rate_card_snapshot_hash(card.lines)
    if computed != card.snapshot_hash:
        raise BillingSchemaError(
            f"rate card {card.rate_card_id} snapshot hash mismatch: "
            f"declared {card.snapshot_hash} != computed {computed} "
            "(lines were tampered after signing)")
    return computed


def parse_rate_card(mapping: Mapping) -> RateCard:
    """从注册表 YAML/JSON dict 解析费率卡（含生效窗口与快照 hash 验签）。

    时间字段 ISO8601（须带时区，缺省按 UTC）；``signature.snapshot_hash``
    必填——解析即验签，篡改一行价格在此处即失败。
    """
    schema = mapping.get("schema")
    if schema != RATE_CARD_SCHEMA:
        raise BillingSchemaError(
            f"rate card schema must be {RATE_CARD_SCHEMA!r}, got {schema!r}")
    card_id = mapping.get("rate_card_id")
    customer_id = mapping.get("customer_id")
    currency = mapping.get("currency")
    version = mapping.get("version")
    if not card_id or not customer_id or not currency:
        raise BillingSchemaError(
            "rate card requires rate_card_id / customer_id / currency")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise BillingSchemaError(f"rate card version invalid: {version!r}")
    effective_from = _parse_iso(mapping.get("effective_from"), "effective_from")
    until_raw = mapping.get("effective_until")
    effective_until = None if until_raw in (None, "") else \
        _parse_iso(until_raw, "effective_until")

    raw_lines = mapping.get("lines")
    if not isinstance(raw_lines, Sequence) or isinstance(raw_lines, str) or not raw_lines:
        raise BillingSchemaError("rate card lines must be a non-empty list")
    lines = []
    for i, item in enumerate(raw_lines):
        if not isinstance(item, Mapping):
            raise BillingSchemaError(f"lines[{i}] is not a mapping")
        min_charge = item.get("min_charge")
        lines.append(RateCardLine(
            line_id=str(item.get("line_id") or ""),
            meter=str(item.get("meter") or ""),
            unit=str(item.get("unit") or ""),
            unit_price=_dec(item.get("unit_price"), f"lines[{i}].unit_price"),
            min_charge=None if min_charge is None
            else _dec(min_charge, f"lines[{i}].min_charge")))
    lines = tuple(lines)

    signature = mapping.get("signature") or {}
    if not isinstance(signature, Mapping):
        raise BillingSchemaError("signature must be a mapping")
    algorithm = signature.get("algorithm")
    declared_hash = signature.get("snapshot_hash")
    if algorithm != "sha256" or not declared_hash or not isinstance(declared_hash, str):
        raise BillingSchemaError(
            "signature must carry algorithm=sha256 and a snapshot_hash "
            "(注册表纪律：hash 由工具计算，禁止手填占位)")
    card = RateCard(
        schema=schema, rate_card_id=str(card_id), customer_id=str(customer_id),
        currency=str(currency), version=version, effective_from=effective_from,
        effective_until=effective_until, lines=lines,
        snapshot_hash=declared_hash)
    verify_rate_card(card)
    return card


def select_rate_card(cards: Iterable[RateCard], at: float) -> RateCard:
    """选"当时生效"的卡：``effective_from <= at < effective_until``。

    0 张命中 → BillingSettleError（无卡不结算）；>1 张命中 → BillingSettleError
    （生效窗口重叠在注册表里就是非法态，见 billing/README.md——此处再兜底）。
    """
    hits = [c for c in cards if c.covers(at)]
    if not hits:
        raise BillingSettleError(f"no rate card effective at {_iso_utc(at)}")
    if len(hits) > 1:
        raise BillingSettleError(
            f"rate cards overlap at {_iso_utc(at)}: "
            f"{sorted(c.rate_card_id for c in hits)} — registry state illegal")
    return hits[0]


# ── 确定性结算（纯函数骨架）───────────────────────────────────────────────────

@dataclass(frozen=True)
class BillLine:
    """一条账单行：费率行 × 窗口内用量归集（Decimal 定点，逐字节可重算）。"""

    line_id: str
    meter: str
    unit: str
    unit_price: Decimal
    events: int
    quantity: Decimal
    amount: Decimal
    min_charge_applied: bool


@dataclass(frozen=True)
class SettlementResult:
    """结算结果（含重算凭据：卡 id+版本+快照 hash——历史可依此原样重放）。"""

    engine: str
    rate_card_id: str
    rate_card_version: int
    rate_card_snapshot_hash: str
    customer_id: str
    currency: str
    window_start: float
    window_end: float
    lines: Tuple[BillLine, ...]
    total: Decimal
    unpriced: Tuple[str, ...]          # strict=False 时未覆盖计费项如实留痕

    def __post_init__(self) -> None:
        if self.window_end < self.window_start:
            raise BillingSchemaError("settlement window_end before window_start")
        if not self.lines and not self.unpriced:
            raise BillingSchemaError("settlement of an empty usage window is refused "
                                     "(nothing to settle is a caller bug, not a bill)")


def deterministic_settle(usage_window: Iterable[object], rate_card: RateCard, *,
                         strict: bool = True) -> SettlementResult:
    """确定性结算：用量 × 当时生效费率卡 → 账单行（纯函数，零时钟零 I/O）。

    - 窗口内每条事件的 ``occurred_at`` 必须落在卡生效窗内——否则 fail-closed
      （调用方选错卡=结算作废，不允许"就近凑合"）；"当时生效"的选取用
      :func:`select_rate_card`。
    - 计费项键 = meter_type（缺省 kind）；卡上无该键行：strict（默认）→
      BillingSettleError（给客户出账不允许静默漏项）；strict=False → 计入
      ``unpriced`` 留痕，金额 0（对账用，不当账单）。
    - 金额 = unit_price × Σquantity（4 位小数 ROUND_HALF_UP）；min_charge
      生效时按 max(金额, min_charge) 取整行下限并留 ``min_charge_applied``。
    - 同输入两次调用：结果对象与 :func:`settlement_to_json` 序列化**逐字节
      一致**；账单行按 line_id 排序，与事件输入顺序无关。
    """
    materialized = tuple(usage_window)
    # 生效窗校验（fail-closed）：先全量检查，再聚合——错误信息一次给全
    offenders = [e for e in materialized if not rate_card.covers(
        float(getattr(e, "occurred_at", 0.0) or 0.0))]
    if offenders:
        sample = ", ".join(str(getattr(e, "event_id", e)) for e in offenders[:3])
        raise BillingSettleError(
            f"{len(offenders)} event(s) fall outside rate card "
            f"{rate_card.rate_card_id} v{rate_card.version} validity "
            f"[{_iso_utc(rate_card.effective_from)}, "
            f"{_iso_utc(rate_card.effective_until) if rate_card.effective_until else '∞'}); "
            f"pick the historically effective card, first offenders: {sample}")

    buckets: dict = {}
    unpriced: list = []
    for e in materialized:
        meter = _meter_key_of(e)
        line = rate_card.line_for(meter)
        if line is None:
            if strict:
                continue                      # 收集后统一 raise（报全不给挤牙膏）
            if meter not in unpriced:
                unpriced.append(meter)
            continue
        b = buckets.setdefault(line.line_id, {"line": line, "events": 0,
                                              "quantity": Decimal(0)})
        b["events"] += 1
        b["quantity"] += _dec(getattr(e, "quantity", 0), "event.quantity")

    if strict:
        missing = sorted({_meter_key_of(e) for e in materialized
                          if rate_card.line_for(_meter_key_of(e)) is None})
        if missing:
            raise BillingSettleError(
                f"rate card {rate_card.rate_card_id} v{rate_card.version} has no line "
                f"for meter(s) {missing}; refusing to settle with silent gaps "
                "(fail-closed — add the line in a NEW card version, never in place)")

    bill_lines = []
    for line_id in sorted(buckets):
        b = buckets[line_id]
        line: RateCardLine = b["line"]
        amount = _q4(line.unit_price * b["quantity"])
        min_applied = False
        if line.min_charge is not None and amount < line.min_charge:
            amount, min_applied = _q4(line.min_charge), True
        bill_lines.append(BillLine(
            line_id=line.line_id, meter=line.meter, unit=line.unit,
            unit_price=line.unit_price, events=b["events"],
            quantity=_q4(b["quantity"]), amount=amount,
            min_charge_applied=min_applied))

    total = _q4(sum((l.amount for l in bill_lines), Decimal(0)))
    times = [float(getattr(e, "occurred_at", 0.0) or 0.0) for e in materialized]
    return SettlementResult(
        engine=SETTLE_ENGINE, rate_card_id=rate_card.rate_card_id,
        rate_card_version=rate_card.version,
        rate_card_snapshot_hash=rate_card.snapshot_hash,
        customer_id=rate_card.customer_id, currency=rate_card.currency,
        window_start=min(times) if times else 0.0,
        window_end=max(times) if times else 0.0,
        lines=tuple(bill_lines), total=total,
        unpriced=tuple(sorted(unpriced)))


def settlement_to_json(result: SettlementResult) -> str:
    """结算结果 canonical JSON（可重算性断言的逐字节底座；金额一律字符串定点）。"""
    payload = {
        "engine": result.engine,
        "rate_card": {"id": result.rate_card_id, "version": result.rate_card_version,
                      "snapshot_hash": result.rate_card_snapshot_hash},
        "customer_id": result.customer_id,
        "currency": result.currency,
        "window": {"start": _iso_utc(result.window_start),
                   "end": _iso_utc(result.window_end)},
        "lines": [{"line_id": l.line_id, "meter": l.meter, "unit": l.unit,
                   "unit_price": _fmt(l.unit_price), "events": l.events,
                   "quantity": _fmt(l.quantity), "amount": _fmt(l.amount),
                   "min_charge_applied": l.min_charge_applied}
                  for l in result.lines],
        "total": _fmt(result.total),
        "unpriced": list(result.unpriced),
    }
    return _canonical_json(payload)


def bill_markdown(result: SettlementResult) -> str:
    """账单行 markdown（human 可读视图；机器对账以 settlement_to_json 为准）。"""
    lines = [
        f"# 账单（{result.rate_card_id} v{result.rate_card_version} · "
        f"{result.customer_id} · {result.currency}）", "",
        f"- 引擎：{result.engine}（确定性重算：同输入逐字节一致）",
        f"- 卡快照：`{result.rate_card_snapshot_hash}`",
        f"- 窗口：{_iso_utc(result.window_start)} ~ {_iso_utc(result.window_end)}（UTC）",
        "",
        "| line_id | meter | 单价 | 事件数 | 用量 | 金额 | 最低消费生效 |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for l in result.lines:
        lines.append("| {lid} | {m} | {p} | {e} | {q} | {a} | {mc} |".format(
            lid=l.line_id, m=l.meter, p=_fmt(l.unit_price), e=l.events,
            q=_fmt(l.quantity), a=_fmt(l.amount),
            mc="是" if l.min_charge_applied else "-"))
    lines += ["", f"**合计：{_fmt(result.total)} {result.currency}**"]
    if result.unpriced:
        lines.append("")
        lines.append(f"> 未计费项（strict=False 留痕，金额 0）：{', '.join(result.unpriced)}")
    return "\n".join(lines) + "\n"
