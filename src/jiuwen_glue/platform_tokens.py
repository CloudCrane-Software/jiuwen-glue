# coding: utf-8
"""platform_tokens — 平台权限中介的 installation token 铸造器（v2.1 §4.5；W-09）.

平台（GitHub App / CNB）短时授权 token 的**铸造、登记、过期**。蓝图原文条款
逐条落点：

- **短时授权租约 TTL≤1h**：``TOKEN_TTL_CAP_SECONDS = 3600``，铸造时硬校验
  （与 company-ops ``ops/permissions/catalog.yaml`` 的 token_caps、OPA 中间件
  ``policy/permission-middleware.rego`` 的 max_ttl_seconds 三处同值，不一致以严者为准）；
- **installation token 每次铸造、禁缓存禁代签**：
  - 禁缓存——同参数重复请求**必然重新铸造**（新 token_ref、重新走签名器），
    本模块没有任何 memoize/缓存路径，``cache_mode`` 恒为 ``"disabled"``；
  - 禁代签——本模块**没有产出令牌的自身代码路径**：签名只发生在构造时注入的
    ``signer`` 回调（由平台适配器实现，私钥材料经 bao 引用现取现用）。``signer``
    缺失 → mint 直接拒绝；把私钥**材料**当 bao 引用传入 → 构造即拒绝（密钥
    永不入代码/日志，AGENTS.md 红线）。
- **每次铸造留痕 decided_by=decision_ref**：铸造必须携带 OPA 决策引用
  （决策点是 OPA 中间件——PROP-0001 不新增第二决策点，本模块**不做 allow/deny
  判定**，只认引用）；每次铸造/撤销/过期/升级都写 append-only ``mint_log``。
- **GitHub App installation token 流程封装**：``GitHubAppInstallationFlow``——
  App 私钥路径 = **bao 引用**（``openbao:secret/...`` 形态）；JWT claims 组装
  （iss=App ID、exp-iat≤600s）在本模块，RS256 签名在注入的 signer；installation
  token 换取在注入的 transport（HTTP 由平台适配器承担，本模块零网络依赖）。
  真实 GitHub App **[待 owner]**——本机无 App，签名流程全走 mock（见 tests）。
- **扩缩权不对称（§4.5/决议 6）**：缩权不经过本模块（目录收紧 → OPA 下一轮
  求值自动拒绝）；**扩权被拒**——请求 scope 超出五交集快照 → 产出 L3 升级对象；
  **人类专属四类硬清单**（money/legal_tos/irreversible/theory_approval，清单
  本体引自 :mod:`jiuwen_glue.escalation` 的 ``HARD_LIST``——W-06 升级体系
  对象引用打通，本模块不另立清单）→ 产出 L4 守门者升级对象，**永不自动铸造**；
  升级对象内嵌 W-06 ``render_readiness`` 的人类就绪包机读结论（四件套逐件
  verdict/missing，字段不齐 = BLOCKED，fail-closed）。形态骨架（kind/level/
  escalation_level/candidate_causes/required_action）与 company-ops
  ``ops/meters/reconcile.py`` 的 reconcile_alarm（L2）同约，gatekeeper（srv-1）
  消费 [待接线]。

**import 边界**：只 import glue 内模块（errors、escalation——升级阶梯 L0-L5、
硬清单、就绪包）+ 标准库；不 import 平台 SDK、不 import jwt/requests、
不 import company-ops。

**[待接线]** 真实 GitHub App 创建（owner）；OPA 中间件生产加载（PR+实机同步）；
gatekeeper 对升级对象的消费。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .errors import GlueError
from .escalation import (
    HARD_IRREVERSIBLE,
    HARD_LEGAL_TOS,
    HARD_LIST,
    HARD_MONEY,
    HARD_THEORY_APPROVAL,
    LEVEL_L3,
    LEVEL_L4,
    LEVEL_L5,
    FrozenFact,
    ReadinessTask,
    ReversibilityAssessment,
    render_readiness,
)

# ── 常量 ─────────────────────────────────────────────────────────────────────

TOKEN_TTL_CAP_SECONDS = 3600          # v2.1 §4.5：TTL≤1h
GITHUB_JWT_TTL_CAP_SECONDS = 600      # GitHub App JWT 有效期上限（10 分钟）
PLATFORMS = ("github", "cnb")
BAO_REF_PREFIX = "openbao:"           # 私钥只允许 bao 引用形态

# 人类专属四类硬清单（v2.1 §4.7）——**引用 W-06 escalation.HARD_LIST 本体**
# （单一真相源；本模块不复制清单内容，只取引用——理论文档同样不复制）。
# 别名保留工单用语：扩权/App 调整/这四类，永不自动铸造。
HARD_LIST_CATEGORIES: Tuple[str, ...] = HARD_LIST

# 升级阶梯（§4.7 L0-L5，引用 W-06 常量）：本模块产出 L3（值班工程师·提权租约）
# 与 L4（守门者）；L5 人类是终点但永不由本模块代达——只递就绪包。
ESCALATION_L3 = LEVEL_L3
ESCALATION_L4 = LEVEL_L4
ESCALATION_L5 = LEVEL_L5

# 升级对象形态对齐 company-ops ops/meters/reconcile.py 的 reconcile_alarm 契约
ESCALATION_KIND = "permission_escalation_required"


# ── 错误 ─────────────────────────────────────────────────────────────────────

class TokenMintError(GlueError):
    """铸造请求不合规（缺决策引用/TTL 超帽/平台未知/无签名器/私钥材料直传）。"""


class HardListEscalationError(GlueError):
    """硬清单类任务/扩权尝试——铸造被拒，升级对象已留痕（exception 即升级载体）。"""

    def __init__(self, signal: dict) -> None:
        super().__init__(f"{signal['escalation_level']} escalation required: {signal['reason']}")
        self.signal = signal


# ── 对象 ─────────────────────────────────────────────────────────────────────

def _utcnow() -> float:
    import time

    return time.time()


def _require_bao_ref(value: str, name: str) -> str:
    """密钥位置只允许 bao 引用形态；裸材料/普通路径一律拒绝（禁代签纪律）。"""
    if not isinstance(value, str) or not value.startswith(BAO_REF_PREFIX):
        raise TokenMintError(
            f"{name} must be a bao reference like '{BAO_REF_PREFIX}secret/...' "
            f"(got {type(value).__name__}; raw key material is never accepted)")
    return value


@dataclass(frozen=True)
class TokenRequest:
    """一次铸造请求。决策判定在 OPA——这里只带结论引用（decision_ref）。"""

    task_class: str                    # 任务类（ops/permissions/catalog.yaml 的 id）
    platform: str                      # "github" | "cnb"
    scopes: Sequence[str]              # 申请的平台限定 scope（github:repo:read 等）
    ttl_seconds: int                   # 申请 TTL（>0 且 ≤3600）
    decision_ref: str                  # OPA 决策引用（每次铸造留痕 decided_by=它）
    agent_ref: str = ""                # 三层复合身份引用（identity.composite_ref）
    task_ref: str = ""                 # 工单引用（硬清单就绪包挂靠；空则回退 agent_ref）
    five_way_snapshot: Tuple[str, ...] = ()   # 五交集快照（给出则强制 scopes ⊆ 快照）
    hard_list_category: Optional[str] = None  # 命中硬清单的类目（None=未命中；取值=escalation.HARD_LIST）

    def __post_init__(self) -> None:
        if not isinstance(self.task_class, str) or not self.task_class:
            raise TokenMintError("task_class must be a non-empty string")
        if self.platform not in PLATFORMS:
            raise TokenMintError(f"unknown platform {self.platform!r} (known: {PLATFORMS})")
        if not isinstance(self.decision_ref, str) or not self.decision_ref:
            raise TokenMintError("decision_ref is required for every mint (decided_by audit)")
        if self.hard_list_category is not None and self.hard_list_category not in HARD_LIST_CATEGORIES:
            raise TokenMintError(
                f"unknown hard_list_category {self.hard_list_category!r} (known: {HARD_LIST_CATEGORIES})")


@dataclass(frozen=True)
class MintedToken:
    """铸造结果——**只有引用与元数据，永不存 token 明文**（明文随调用方即取即用）。"""

    token_ref: str                     # 内部引用（登记簿主键）
    platform: str
    task_class: str
    scopes: Tuple[str, ...]
    issued_at: float
    expires_at: float
    ttl_seconds: int
    decision_ref: str                  # decided_by
    installation_ref: str              # App/installation 引用（如 bao 内 App 档案引用）
    signer_bao_ref: str                # 签名私钥的 bao 引用（留痕，不含材料）


# 状态机
STATUS_ACTIVE = "ACTIVE"
STATUS_EXPIRED = "EXPIRED"
STATUS_REVOKED = "REVOKED"


# ── 升级对象（与 company-ops reconcile_alarm 同形契约）───────────────────────

def _readiness_view(package) -> dict:
    """W-06 ReadinessPackage → 机读 dict（verdict/missing/逐件结论，fail-closed 面）。"""
    return {
        "verdict": package.verdict,
        "ready": package.ready,
        "missing": list(package.missing),
        "knock_back_to_l3": package.knock_back_to_l3,
        "pieces": [{"key": p.key, "verdict": p.verdict, "detail": p.detail}
                   for p in package.pieces],
        "source": "jiuwen_glue.escalation.render_readiness (W-06)",
    }


def hard_list_readiness(request: TokenRequest, *, facts: Sequence[FrozenFact] = (),
                        reversibility: Optional[ReversibilityAssessment] = None,
                        title: str = ""):
    """为硬清单拒绝装配 W-06 就绪包（字段不齐 = BLOCKED，绝不放行铸造）。

    本函数只装配调用方已给的事实/可逆性评估；「权限内无解」件需要升级台账 case
    （L0..L3 尝试记录），由守门者接线时经 EscalationLedger 补齐后重渲染——
    这里渲染出的包 verdict 恒为 BLOCKED 及早（fail-closed 起点态）。
    """
    task = ReadinessTask(
        task_ref=request.task_ref or request.agent_ref or request.task_class,
        title=title or f"hard-list token request: {request.task_class}",
        hard_list_category=request.hard_list_category,
        facts=tuple(facts),
        reversibility=reversibility,
    )
    return render_readiness(task)                      # case 缺省：件3 = BLOCKED


def escalation_signal(*, level: str, reason: str, request: TokenRequest,
                      candidate_causes: Sequence[str],
                      required_action: str,
                      readiness_package=None) -> dict:
    """构造升级对象：kind/level/escalation_level 形态对齐 ops/meters/reconcile.py。

    L4（硬清单/守门者）附 W-06 ``render_readiness`` 就绪包机读结论（verdict/
    missing/逐件，BLOCKED 即守门者不得放行 L5）；L3（扩权尝试/值班工程师）
    指向提权租约。对象由调用方投递给信号收件箱（gatekeeper [待接线]）。
    """
    signal = {
        "kind": ESCALATION_KIND,
        "level": "alarm",
        "escalation_level": level,
        "task_class": request.task_class,
        "platform": request.platform,
        "requested_scopes": sorted(request.scopes),
        "agent_ref": request.agent_ref,
        "decided_by": request.decision_ref,
        "reason": reason,
        "candidate_causes": list(candidate_causes),
        "required_action": required_action,
    }
    if level == ESCALATION_L4:
        if readiness_package is None:
            readiness_package = hard_list_readiness(request)
        signal["readiness"] = _readiness_view(readiness_package)
        signal["note"] = ("L0-L3 无权批准；守门者补齐四件套（就绪包 READY）后递 L5 "
                          "人类，仅人类可批准（v2.1 §4.7 硬清单）")
    return signal


# ── 铸造器 ───────────────────────────────────────────────────────────────────

class PlatformTokenMinter:
    """铸造/登记/过期。禁缓存禁代签是结构性质，不是约定：没有 signer 就没有 token。

    ``signer``：``Callable[[dict], str]``——输入 JWT/令牌 claims（dict），返回签名
    结果（串）。由平台适配器实现（内部经 bao 现取私钥材料），本模块不理解其内容。
    ``now``：可注入时钟（测试/审计要求可重放时间线）。
    """

    cache_mode = "disabled"            # 类级常量：禁缓存是结构性质（无缓存路径可开）

    def __init__(self, *, signer: Optional[Callable[[dict], str]] = None,
                 now: Optional[Callable[[], float]] = None) -> None:
        self._signer = signer
        self._now = now or _utcnow
        self._registry: Dict[str, dict] = {}     # token_ref → 记录（引用与元数据，无明文）
        self._mint_log: List[dict] = []          # append-only 留痕
        self._signer_bao_ref: Optional[str] = None

    # ── 登记簿与留痕（只读视图）──
    @property
    def mint_log(self) -> Tuple[dict, ...]:
        return tuple(self._mint_log)

    @property
    def registered_count(self) -> int:
        """登记簿中的 token 记录数（升级路径必须保持 0——绝不铸造）。"""
        return len(self._registry)

    def record_of(self, token_ref: str) -> Optional[dict]:
        rec = self._registry.get(token_ref)
        return dict(rec) if rec else None

    # ── 留痕 ──
    def _log(self, event: str, **kv) -> None:
        entry = {"event": event, "at": self._now()}
        entry.update(kv)
        self._mint_log.append(entry)

    # ── 铸造 ──
    def mint(self, request: TokenRequest) -> MintedToken:
        """每次铸造：校验 → 硬清单/扩权拒绝（升级对象留痕）→ 签名 → 登记。

        同参数重复调用**必然产出新 token**（禁缓存）：token_ref 取 uuid4，签名器
        每次都重新调用，无任何返回旧结果的路径。
        """
        # 1) 决策引用：每次铸造留痕 decided_by=decision_ref（缺失即拒）
        if not request.decision_ref:
            raise TokenMintError("decision_ref is required (decided_by audit)")  # 防御：构造已挡

        # 2) TTL 帽（TTL≤1h）
        if not isinstance(request.ttl_seconds, int) or request.ttl_seconds <= 0:
            raise TokenMintError(f"ttl_seconds must be a positive int, got {request.ttl_seconds!r}")
        if request.ttl_seconds > TOKEN_TTL_CAP_SECONDS:
            raise TokenMintError(
                f"ttl_seconds {request.ttl_seconds} exceeds cap {TOKEN_TTL_CAP_SECONDS} (v2.1 §4.5)")

        # 3) scope 非空（目录里显式空 scope 的硬清单类在这里被第二次挡住）
        scopes = tuple(request.scopes)
        if not scopes:
            raise TokenMintError(
                f"task_class {request.task_class!r} declares no scopes "
                "(empty-scope catalog entries are escalation-only, never minted)")

        # 4) 禁代签：没有注入签名器就没有 token（不存在本模块自签路径）
        if self._signer is None:
            raise TokenMintError(
                "no signer bound — the minter never signs by itself (proxy-signing forbidden); "
                "bind a platform adapter signer backed by a bao-held key")

        # 5) 扩缩权不对称：缩权不进来（OPA 拒），扩权与硬清单从这里出（升级对象）
        self._guard_escalation(request)

        # 6) 签名（每次铸造都走签名器——禁缓存）+ 登记留痕
        now = self._now()
        claims = {
            "task_class": request.task_class,
            "platform": request.platform,
            "scopes": sorted(scopes),
            "iat": now,
            "exp": now + request.ttl_seconds,
            "agent_ref": request.agent_ref,
            "tenant_id": "t0",
            "jti": uuid.uuid4().hex,
        }
        signed = self._signer(dict(claims))          # 签名在平台适配器（bao 私钥侧）
        token_ref = f"tok:{request.platform}:{uuid.uuid4().hex}"
        record = {
            "token_ref": token_ref,
            "status": STATUS_ACTIVE,
            "platform": request.platform,
            "task_class": request.task_class,
            "scopes": sorted(scopes),
            "issued_at": now,
            "expires_at": claims["exp"],
            "ttl_seconds": request.ttl_seconds,
            "decided_by": request.decision_ref,      # ← 每次铸造留痕
            "agent_ref": request.agent_ref,
            "installation_ref": self._installation_ref(request),
            "signer_bao_ref": self._signer_bao_ref,
            "signed_len": len(signed),               # 只留签名产物长度作核对，不留内容
        }
        self._registry[token_ref] = record
        self._log("MINT", token_ref=token_ref, decided_by=request.decision_ref,
                  task_class=request.task_class, platform=request.platform,
                  scopes=sorted(scopes), expires_at=claims["exp"], jti=claims["jti"])
        return MintedToken(
            token_ref=token_ref, platform=request.platform, task_class=request.task_class,
            scopes=tuple(sorted(scopes)), issued_at=now, expires_at=claims["exp"],
            ttl_seconds=request.ttl_seconds, decision_ref=request.decision_ref,
            installation_ref=record["installation_ref"], signer_bao_ref=self._signer_bao_ref or "")

    def _installation_ref(self, request: TokenRequest) -> str:
        return f"app-installation:{request.platform}/{request.task_class}"

    # ── 扩缩权不对称守卫 ──
    def _guard_escalation(self, request: TokenRequest) -> None:
        """硬清单 → L4 守门者；扩权（scope ⊄ 五交集快照）→ L3 提权租约。

        两类都：产出升级对象 → append-only 留痕（event=ESCALATE）→ 抛
        HardListEscalationError（**绝不铸造**）。
        """
        if request.hard_list_category is not None:
            signal = escalation_signal(
                level=ESCALATION_L4,
                reason=f"hard-list category {request.hard_list_category!r} "
                       "(money/legal_tos/irreversible/theory_approval never auto-mints)",
                request=request,
                candidate_causes=["hard_list_category_set"],
                required_action="守门者补齐四件套（就绪包 READY）后递 L5 人类批准；"
                                "批准后由人类在平台侧手工授权，本模块不补铸",
            )
            self._log("ESCALATE", escalation_level=ESCALATION_L4,
                      decided_by=request.decision_ref, task_class=request.task_class,
                      platform=request.platform, reason=signal["reason"])
            raise HardListEscalationError(signal)

        if request.five_way_snapshot:
            beyond = sorted(set(request.scopes) - set(request.five_way_snapshot))
            if beyond:
                signal = escalation_signal(
                    level=ESCALATION_L3,
                    reason=f"scope expansion beyond five-way snapshot: {beyond} "
                           "(permissions only converge, never expand)",
                    request=request,
                    candidate_causes=["out_of_snapshot_scopes", "stale_snapshot", "scope_typo"],
                    required_action="值班工程师评估 L3 提权租约；未经人类批准不得铸造",
                )
                self._log("ESCALATE", escalation_level=ESCALATION_L3,
                          decided_by=request.decision_ref, task_class=request.task_class,
                          platform=request.platform, beyond=beyond)
                raise HardListEscalationError(signal)

    # ── 撤销（缩权/提前失效——每次状态变更同样留痕）──
    def revoke(self, token_ref: str, *, decision_ref: str) -> dict:
        if not decision_ref:
            raise TokenMintError("revoke also requires decision_ref (decided_by audit)")
        rec = self._registry.get(token_ref)
        if rec is None:
            raise TokenMintError(f"unknown token_ref {token_ref!r}")
        rec["status"] = STATUS_REVOKED
        rec["revoked_by"] = decision_ref
        self._log("REVOKE", token_ref=token_ref, decided_by=decision_ref)
        return dict(rec)

    # ── 过期（扫描 + 查询；过期是状态迁移，同样留痕）──
    def expire_due(self) -> List[str]:
        """把已过 TTL 的 ACTIVE 记录翻成 EXPIRED（返回本次翻转的 token_ref）。"""
        now = self._now()
        flipped: List[str] = []
        for ref, rec in self._registry.items():
            if rec["status"] == STATUS_ACTIVE and now >= rec["expires_at"]:
                rec["status"] = STATUS_EXPIRED
                flipped.append(ref)
                self._log("EXPIRE", token_ref=ref, decided_by=rec["decided_by"],
                          at=now, expires_at=rec["expires_at"])
        return flipped

    def status_of(self, token_ref: str) -> str:
        rec = self._registry.get(token_ref)
        if rec is None:
            raise TokenMintError(f"unknown token_ref {token_ref!r}")
        if rec["status"] == STATUS_ACTIVE and self._now() >= rec["expires_at"]:
            return STATUS_EXPIRED
        return rec["status"]

    def active_at(self, ts: float) -> List[str]:
        return sorted(ref for ref, rec in self._registry.items()
                      if rec["status"] == STATUS_ACTIVE
                      and rec["issued_at"] <= ts < rec["expires_at"])

    def bind_signer(self, signer: Callable[[dict], str], *, signer_bao_ref: str) -> None:
        """绑定签名器；私钥位置必须是 bao 引用（材料直传在此被拒——禁代签纪律）。"""
        _require_bao_ref(signer_bao_ref, "signer_bao_ref")
        self._signer = signer
        self._signer_bao_ref = signer_bao_ref


# ── GitHub App installation token 流程封装 ───────────────────────────────────

class GitHubAppInstallationFlow:
    """GitHub App → installation token 两跳流程（claims 组装在此，签名/网络注入）。

    - 私钥路径 = bao 引用（``private_key_bao_ref`` 必须形如 ``openbao:secret/...``）；
    - 第一跳：App JWT（RS256）——claims 本模块组装（iss/exp/iat），签名走注入的
      ``signer``（适配器内经 bao 现取私钥材料；**本模块零密钥零签名算法**）；
    - 第二跳：installation token——HTTP 交互走注入的 ``transport(call)``，call 是
      ``{"method","url","headers","body"}`` 的 dict，返回
      ``{"status", "body": {"token": <明文>, "expires_at": <epoch 秒>}}``；
      明文**只随返回值交给调用方**，本模块登记的只有引用与元数据。

    真实 App **[待 owner]**——当前 mock 流程验证（tests/test_platform_tokens.py）。
    """

    def __init__(self, *, app_id: str, installation_id: str,
                 private_key_bao_ref: str,
                 signer: Callable[[dict], str],
                 transport: Callable[[dict], dict],
                 api_base: str = "https://api.github.com",
                 now: Optional[Callable[[], float]] = None) -> None:
        if not app_id or not installation_id:
            raise TokenMintError("app_id/installation_id are required")
        self.app_id = app_id
        self.installation_id = installation_id
        self.private_key_bao_ref = _require_bao_ref(private_key_bao_ref, "private_key_bao_ref")
        if signer is None or transport is None:
            raise TokenMintError("signer and transport are required "
                                 "(the flow never signs or performs HTTP by itself)")
        self._signer = signer
        self._transport = transport
        self.api_base = api_base
        self._now = now or _utcnow
        self.last_jwt_claims: Optional[dict] = None   # 最近一跳的 claims（审计/测试观察点）

    def installation_jwt_claims(self, *, ttl_seconds: int = GITHUB_JWT_TTL_CAP_SECONDS) -> dict:
        """第一跳 claims：iss=App ID，iat=now，exp=now+min(ttl, 600)。"""
        if ttl_seconds <= 0 or ttl_seconds > GITHUB_JWT_TTL_CAP_SECONDS:
            raise TokenMintError(
                f"jwt ttl must be in (0, {GITHUB_JWT_TTL_CAP_SECONDS}]")
        now = self._now()
        claims = {"iss": self.app_id, "iat": now, "exp": now + ttl_seconds, "alg": "RS256"}
        self.last_jwt_claims = dict(claims)
        return dict(claims)

    def mint_installation_token(self, request: TokenRequest) -> Tuple[MintedToken, dict]:
        """两跳全流程，返回 (铸造结果, 原始响应引用)。TTL 超帽/硬清单/扩权照拒。"""
        if not isinstance(request.ttl_seconds, int) or request.ttl_seconds <= 0 \
                or request.ttl_seconds > TOKEN_TTL_CAP_SECONDS:
            raise TokenMintError(
                f"ttl_seconds must be in (0, {TOKEN_TTL_CAP_SECONDS}]")
        jwt_claims = self.installation_jwt_claims()
        jwt_signed = self._signer(dict(jwt_claims))            # 第一跳：bao 私钥侧签名

        call = {
            "method": "POST",
            "url": f"{self.api_base}/app/installations/{self.installation_id}/access_tokens",
            "headers": {
                "Authorization": f"Bearer <app-jwt:{self.app_id}>",  # 占位：明文不进本模块
                "Accept": "application/vnd.github+json",
                "X-GitHub-Jwt-Signed-Len": str(len(jwt_signed)),     # 审计用长度核对
            },
            "body": {"permissions_hint": sorted(request.scopes)},
        }
        resp = self._transport(dict(call))                     # 第二跳：网络在适配器
        if not isinstance(resp, dict) or resp.get("status") != 201:
            raise TokenMintError(f"installation token exchange failed: {resp!r}")
        body = resp.get("body") or {}
        token = body.get("token")
        expires_at = body.get("expires_at")
        if not token or not isinstance(expires_at, (int, float)):
            raise TokenMintError("installation response must carry token and expires_at")
        # GitHub 侧 TTL 也必须 ≤1h 帽（对端给的比帽宽 → 拒，不截断不代签）
        if expires_at - self._now() > TOKEN_TTL_CAP_SECONDS:
            raise TokenMintError(
                "installation token TTL from platform exceeds 3600s cap — refusing "
                "(mint a shorter-lived one, never widen)")

        minter = PlatformTokenMinter(signer=self._signer, now=self._now)
        minter.bind_signer(self._signer, signer_bao_ref=self.private_key_bao_ref)
        minted = minter.mint(request)                          # 留痕/硬清单/扩权守卫复用
        reference = {"installation_token_ref": f"gh-install:{self.installation_id}",
                     "expires_at": expires_at,
                     "token_plaintext_held_by": "caller_only"}  # 明文永远只在调用方手里
        return minted, reference
