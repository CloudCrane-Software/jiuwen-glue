# coding: utf-8
"""statebuilder — 决策域状态装配器（sandbox.placement 首域）— W-04（v2.1 §7 决策域）.

职责（v2.1 §7"决策域注册表"三条款：状态白名单 / 选项目录投影 / 选项 3-7）：
把"一次放置决策所需的上下文"装配成**有界、可溯、可哈希**的 :class:`DecisionState`：

- **字段白名单**：只有 decisions/<domain>.yaml ``state_schema.whitelist`` 列内的
  字段名可进入状态；白名单外字段一律 :class:`StateBuilderError`（fail-closed）
  ——决策上下文注入面被白名单封死，提示词/检索结果不能私带字段进来。
- **来源可溯**：每个字段必须带 ``source`` ∈ {probed, declared, doc, computed}
  （对齐 company-ops/resources 档案的来源标注纪律：probed=实机实测，
  declared=声明未验证，doc=官方文档，computed=本域内从输入投影的派生值）；
  事实性来源（probed/declared/doc）还必须带**非空 evidence**——无出处的数值
  进不了决策状态（"严禁无来源数值"的决策域对应物）。
- **上限**：字段数 ≤ ``state_schema.max_fields``、单值序列化 ≤
  ``max_value_chars``、选项数 ∈ ``option_count`` [min, max]（默认 3-7）。
  超限 raise，**不静默截断**（截断=静默改变决策依据，属造假）。

两侧无知（v2.1 §7）：决策域只说"去哪"，本装配器只装配"用于选去哪的状态"，
不懂"怎么调动"（那是 company-ops ``adapters/`` 的诀窍居所）；沙箱 agent 零外部
认知。:meth:`DecisionState.context_hash` 产出可直接作
``glue.decisions.DecisionLog.append`` 的上下文摘要（决策记录原文不落账，12.4）。

边界（写死）：本模块**不读文件系统**——decisions/*.yaml 由调用方加载为 dict
传入（glue 零依赖、跨仓不互指）；不实现打分与终选（两级漏斗的 Score 过滤与
Choice 终选归决策域执行器 [待] 意图层接线）；选项的 score 是上游漏斗第一级
的输出，本模块只登记不重算。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Tuple

from .decisions import context_hash

__all__ = [
    "STATEBUILDER_SCHEMA", "DOMAIN_SANDBOX_PLACEMENT",
    "SOURCE_PROBED", "SOURCE_DECLARED", "SOURCE_DOC", "SOURCE_COMPUTED",
    "FIELD_SOURCES", "EVIDENCE_REQUIRED_SOURCES",
    "DEFAULT_OPTION_COUNT_MIN", "DEFAULT_OPTION_COUNT_MAX",
    "DEFAULT_MAX_FIELDS", "DEFAULT_MAX_VALUE_CHARS",
    "StateBuilderError", "StateField", "PlacementOption", "DecisionState",
    "SandboxPlacementStateBuilder",
]

STATEBUILDER_SCHEMA = "jiuwen-glue/statebuilder/v1"
DOMAIN_SANDBOX_PLACEMENT = "sandbox.placement"

SOURCE_PROBED = "probed"        # 实机实测（对齐 resources 档案来源纪律）
SOURCE_DECLARED = "declared"    # 声明未验证
SOURCE_DOC = "doc"              # 官方文档
SOURCE_COMPUTED = "computed"    # 本域内从输入投影计算的派生值（可由输入溯源）
FIELD_SOURCES = (SOURCE_PROBED, SOURCE_DECLARED, SOURCE_DOC, SOURCE_COMPUTED)
EVIDENCE_REQUIRED_SOURCES = (SOURCE_PROBED, SOURCE_DECLARED, SOURCE_DOC)

DEFAULT_OPTION_COUNT_MIN = 3    # v2.1 §7：选项 3-7（超限走两级漏斗，见域注册表）
DEFAULT_OPTION_COUNT_MAX = 7
DEFAULT_MAX_FIELDS = 24
DEFAULT_MAX_VALUE_CHARS = 2000


class StateBuilderError(Exception):
    """状态装配失败（白名单/来源/上限违规，全部 fail-closed）。"""


def _jsonable(value: Any) -> str:
    """严格 JSON 化校验 + 返回序列化文本（同时用作长度上限的度量）。

    **不设 default 兜底**——未知对象静默 str() 强转 = 静默改变决策依据，
    与"不静默截断"同一条红线；状态值只允许 plain JSON 类型。
    """
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise StateBuilderError(f"field/option value must be JSON-able: {exc}") from None


@dataclass(frozen=True)
class StateField:
    """决策状态的一个字段：名 + 值 + 来源标注 + 凭据（来源可溯四元组）。"""

    name: str
    value: Any
    source: str
    evidence: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StateField":
        try:
            return cls(name=raw["name"], value=raw.get("value"),
                       source=raw.get("source", ""), evidence=raw.get("evidence", ""))
        except KeyError:
            raise StateBuilderError(
                f"state field requires 'name' (and 'source'), got keys "
                f"{sorted(raw.keys())}") from None

    def to_dict(self) -> dict:
        return {"name": self.name, "value": self.value,
                "source": self.source, "evidence": self.evidence}


@dataclass(frozen=True)
class PlacementOption:
    """决策状态的一个候选选项：选项 = 资源档案投影（不硬编码，v2.1 二.3）。

    ``score`` 是两级漏斗第一级（Score 过滤）的输出分，本模块只登记不重算；
    ``adapter_ref`` 指向诀窍居所（company-ops ``adapters/<id>``）——决策域
    只引用适配器，不理解适配器（两侧无知）。
    """

    option_id: str                     # 资源档案 resource_id（投影主键）
    adapter_ref: str = ""              # 如 "adapters/cnb-sandbox"
    score: float = 0.0                 # Score 过滤（漏斗第一级）输出分
    attrs: Mapping[str, Any] = field(default_factory=dict)   # 投影属性（档位/剩余比等）

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PlacementOption":
        try:
            return cls(option_id=raw["option_id"],
                       adapter_ref=raw.get("adapter_ref", ""),
                       score=float(raw.get("score", 0.0)),
                       attrs=dict(raw.get("attrs") or {}))
        except KeyError:
            raise StateBuilderError(
                f"placement option requires 'option_id', got keys "
                f"{sorted(raw.keys())}") from None
        except (TypeError, ValueError, OverflowError):
            # OverflowError（D1-R8 二批）：巨型 int score 越出 float 域与垃圾
            # 类型/值同款归类 StateBuilderError，不未归类泄漏
            raise StateBuilderError(
                f"placement option score must be a number, got "
                f"{raw.get('score')!r}") from None

    def to_dict(self) -> dict:
        return {"option_id": self.option_id, "adapter_ref": self.adapter_ref,
                "score": self.score, "attrs": dict(self.attrs)}


@dataclass(frozen=True)
class DecisionState:
    """一次决策的装配产物：有界（白名单+上限）、可溯（每字段带来源）、
    可哈希（context_hash → 决策记录摘要，原文不落账）。"""

    domain: str
    fields: Tuple[StateField, ...]
    options: Tuple[PlacementOption, ...]
    built_at: float
    schema: str = STATEBUILDER_SCHEMA

    def context_hash(self) -> str:
        """canonical JSON + SHA-256（复用 decisions.context_hash，同一上下文同哈希）。"""
        return context_hash(self.to_dict())

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "domain": self.domain,
            "built_at": self.built_at,
            "fields": [f.to_dict() for f in self.fields],
            "options": [o.to_dict() for o in self.options],
        }

    def field(self, name: str) -> Optional[StateField]:
        for f in self.fields:
            if f.name == name:
                return f
        return None


class SandboxPlacementStateBuilder:
    """sandbox.placement 域状态装配器（域规格 = decisions/sandbox.placement.yaml
    加载后的 dict；缺键 fail-closed）。

    域规格消费的键（与注册表一致，跨仓对齐点）：
    - ``domain``（必填，须 == ``sandbox.placement``）；
    - ``state_schema.whitelist``（必填非空 list——字段白名单）；
    - ``state_schema.max_fields`` / ``state_schema.max_value_chars``（可缺省默认）；
    - ``option_count: {min, max}``（可缺省默认 3-7）。
    """

    def __init__(self, domain_spec: Mapping[str, Any]) -> None:
        if not isinstance(domain_spec, Mapping):
            raise StateBuilderError("domain_spec must be a mapping (loaded YAML dict)")
        domain = domain_spec.get("domain")
        if domain != DOMAIN_SANDBOX_PLACEMENT:
            raise StateBuilderError(
                f"domain_spec.domain must be {DOMAIN_SANDBOX_PLACEMENT!r}, got {domain!r}")
        schema = domain_spec.get("state_schema")
        if not isinstance(schema, Mapping):
            raise StateBuilderError("domain_spec.state_schema must be a mapping")
        whitelist = schema.get("whitelist")
        if not isinstance(whitelist, (list, tuple)) or not whitelist or \
                not all(isinstance(w, str) and w for w in whitelist):
            raise StateBuilderError(
                "state_schema.whitelist must be a non-empty list of field names")
        self.domain = domain
        self._whitelist = frozenset(whitelist)
        self._max_fields = int(schema.get("max_fields", DEFAULT_MAX_FIELDS))
        self._max_value_chars = int(schema.get("max_value_chars", DEFAULT_MAX_VALUE_CHARS))
        if self._max_fields < 1 or self._max_value_chars < 1:
            raise StateBuilderError("max_fields / max_value_chars must be >= 1")
        option_count = domain_spec.get("option_count") or {}
        try:
            self._option_min = int(option_count.get("min", DEFAULT_OPTION_COUNT_MIN))
            self._option_max = int(option_count.get("max", DEFAULT_OPTION_COUNT_MAX))
        except (TypeError, ValueError):
            raise StateBuilderError("option_count.min/max must be integers") from None
        if not (1 <= self._option_min <= self._option_max):
            raise StateBuilderError(
                f"option_count must satisfy 1 <= min <= max, got "
                f"[{self._option_min}, {self._option_max}]")

    # ── 装配（唯一入口）──────────────────────────────────────────────────

    def build(self, *, fields: Iterable[Any], options: Iterable[Any],
              built_at: Optional[float] = None) -> DecisionState:
        """装配决策状态：白名单 → 来源可溯 → 上限 → 选项 3-7，全过才产出。"""
        import time

        built_fields: Tuple[StateField, ...] = tuple(
            f if isinstance(f, StateField) else StateField.from_mapping(f)
            for f in fields)
        built_options: Tuple[PlacementOption, ...] = tuple(
            o if isinstance(o, PlacementOption) else PlacementOption.from_mapping(o)
            for o in options)
        self._enforce_whitelist(built_fields)
        self._enforce_provenance(built_fields)
        self._enforce_field_caps(built_fields)
        self._enforce_options(built_options)
        return DecisionState(
            domain=self.domain,
            fields=built_fields,
            options=built_options,
            built_at=time.time() if built_at is None else float(built_at),
        )

    # ── 三道执法（全部 fail-closed）──────────────────────────────────────

    def _enforce_whitelist(self, fields: Tuple[StateField, ...]) -> None:
        for f in fields:
            if f.name not in self._whitelist:
                raise StateBuilderError(
                    f"field {f.name!r} is not in the domain whitelist "
                    f"(decision context injection surface is closed; extend "
                    f"decisions/{self.domain}.yaml state_schema.whitelist via PR)")

    def _enforce_provenance(self, fields: Tuple[StateField, ...]) -> None:
        for f in fields:
            if f.source not in FIELD_SOURCES:
                raise StateBuilderError(
                    f"field {f.name!r} source must be one of {FIELD_SOURCES}, "
                    f"got {f.source!r}")
            if f.source in EVIDENCE_REQUIRED_SOURCES and not str(f.evidence).strip():
                raise StateBuilderError(
                    f"field {f.name!r} with source={f.source!r} requires non-empty "
                    f"evidence (no provenance, no entry into decision state)")

    def _enforce_field_caps(self, fields: Tuple[StateField, ...]) -> None:
        if len(fields) > self._max_fields:
            raise StateBuilderError(
                f"too many fields: {len(fields)} > max_fields {self._max_fields} "
                "(state is bounded; trim inputs upstream, never silently truncate)")
        for f in fields:
            text = _jsonable(f.value)
            if len(text) > self._max_value_chars:
                raise StateBuilderError(
                    f"field {f.name!r} value exceeds max_value_chars "
                    f"{self._max_value_chars} (serialized len {len(text)})")

    def _enforce_options(self, options: Tuple[PlacementOption, ...]) -> None:
        count = len(options)
        if not (self._option_min <= count <= self._option_max):
            raise StateBuilderError(
                f"option count {count} outside [{self._option_min}, "
                f"{self._option_max}] — apply the Score filter (funnel stage 1) "
                "upstream; over-limit means the projection/filter is misconfigured, "
                "not something to truncate silently")
        seen = set()
        for o in options:
            if not o.option_id or not isinstance(o.option_id, str):
                raise StateBuilderError("option_id must be a non-empty str")
            if o.option_id in seen:
                raise StateBuilderError(f"duplicate option_id {o.option_id!r}")
            seen.add(o.option_id)
            _jsonable(dict(o.attrs))      # JSON-able 校验（attrs 进状态哈希）
