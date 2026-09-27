# coding: utf-8
"""platform_tokens 铸造器测试（v2.1 §4.5；W-09）。

全部 mock 签名流程（本机无真实 GitHub App——[待 owner]；真实 App 创建后同一
测试面只需把 mock signer/transport 换成平台适配器实现）。重点红绿：
- 禁缓存：同参数重复铸造必然新 token、签名器必然重新调用；
- 禁代签：无签名器拒绝铸造；私钥材料/裸路径冒充 bao 引用构造即拒；
- TTL≤1h：超帽拒、边界 3600 过、非正拒；
- 每次铸造留痕 decided_by=decision_ref；撤销/过期同样留痕；
- 扩缩权不对称：硬清单四类 → L4 守门者升级对象；scope 超五交集快照 → L3。
"""
from __future__ import annotations

import pytest

from jiuwen_glue.platform_tokens import (
    ESCALATION_L3,
    ESCALATION_L4,
    GITHUB_JWT_TTL_CAP_SECONDS,
    HARD_LIST_CATEGORIES,
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_REVOKED,
    TOKEN_TTL_CAP_SECONDS,
    GitHubAppInstallationFlow,
    HardListEscalationError,
    MintedToken,
    PlatformTokenMinter,
    TokenMintError,
    TokenRequest,
    escalation_signal,
    hard_list_readiness,
)
from jiuwen_glue.escalation import (
    HARD_IRREVERSIBLE,
    HARD_LEGAL_TOS,
    HARD_LIST,
    HARD_MONEY,
    HARD_THEORY_APPROVAL,
    ReversibilityAssessment,
    FrozenFact,
)


# ── mock 平台适配器 ──────────────────────────────────────────────────────────

class MockSigner:
    """假签名器：记录每次调用（禁缓存的可观察面），返回确定性假签名。"""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, claims: dict) -> str:
        self.calls.append(dict(claims))
        return "mocksig:" + "|".join(f"{k}={claims[k]}" for k in sorted(claims))[:48]


class MockTransport:
    """假 HTTP：记录 call，返回 201 + 假 installation token（明文只在响应里）。"""

    def __init__(self, token: str = "ghu_mock-token-plaintext", expires_at: float = None,
                 status: int = 201) -> None:
        self.calls: list = []
        self.token = token
        self.expires_at = expires_at
        self.status = status

    def __call__(self, call: dict) -> dict:
        self.calls.append(dict(call))
        return {"status": self.status,
                "body": {"token": self.token, "expires_at": self.expires_at}}


def make_request(clock, *, task_class: str = "ci_fix", platform: str = "github",
                 scopes: tuple = ("github:repo:read", "github:repo:write"),
                 ttl: int = 1800, decision_ref: str = "opa:dec-0001",
                 task_ref: str = "", snapshot: tuple = None,
                 hard_list: str = None) -> TokenRequest:
    # snapshot 缺省 = scopes 本身（mint 现要求快照必须携带——grok H2 修复后的默认正例）
    if snapshot is None:
        snapshot = tuple(scopes)
    return TokenRequest(
        task_class=task_class, platform=platform, scopes=scopes, ttl_seconds=ttl,
        decision_ref=decision_ref, agent_ref="ag:ci-bot@t0/run:r1/task:t9",
        task_ref=task_ref, five_way_snapshot=snapshot, hard_list_category=hard_list)


def make_minter(clock, signer=None) -> PlatformTokenMinter:
    m = PlatformTokenMinter(now=clock)
    m.bind_signer(signer or MockSigner(), signer_bao_ref="openbao:secret/credentials/github-apps/ci-bot")
    return m


# ── 1. 正常铸造 + 留痕 ───────────────────────────────────────────────────────

def test_mint_happy_path_registers_with_decided_by(clock):
    signer = MockSigner()
    m = make_minter(clock, signer)
    minted = m.mint(make_request(clock))
    assert isinstance(minted, MintedToken)
    assert minted.token_ref.startswith("tok:github:")
    assert minted.ttl_seconds == 1800
    assert minted.expires_at == minted.issued_at + 1800
    rec = m.record_of(minted.token_ref)
    assert rec["status"] == STATUS_ACTIVE
    assert rec["decided_by"] == "opa:dec-0001"          # ← 每次铸造留痕 decided_by
    assert rec["signer_bao_ref"] == "openbao:secret/credentials/github-apps/ci-bot"
    mint_events = [e for e in m.mint_log if e["event"] == "MINT"]
    assert len(mint_events) == 1 and mint_events[0]["decided_by"] == "opa:dec-0001"
    assert "token" not in rec                            # 登记簿永不存令牌明文
    assert signer.calls and signer.calls[0]["task_class"] == "ci_fix"


# ── 2. 每次铸造必须带 OPA 决策引用 ───────────────────────────────────────────

def test_mint_requires_decision_ref(clock):
    with pytest.raises(TokenMintError, match="decision_ref"):
        make_request(clock, decision_ref="")
    m = make_minter(clock)
    naked = make_request(clock)
    object.__setattr__(naked, "decision_ref", "")        # 绕过构造直改（防御路径）
    with pytest.raises(TokenMintError, match="decision_ref"):
        m.mint(naked)


# ── 2b. 红队回归（grok H2/H4）────────────────────────────────────────────────

def test_mint_requires_five_way_snapshot(clock):
    """快照缺省 → 拒绝（H2：空快照曾静默跳过扩权检查，repo_archive 被无证铸造）。"""
    m = make_minter(clock)
    naked = make_request(clock, task_class="repo_archive",
                         scopes=("github:repo:read",), snapshot=())
    with pytest.raises(TokenMintError, match="five_way_snapshot is required"):
        m.mint(naked)
    assert m.registered_count == 0


def test_mint_refuses_bool_ttl(clock):
    """H4：bool 是 int 子类——isinstance 校验曾把 True 放行成 1 秒令牌。"""
    m = make_minter(clock)
    with pytest.raises(TokenMintError, match="positive int"):
        m.mint(make_request(clock, ttl=True))
    with pytest.raises(TokenMintError, match="positive int"):
        m.mint(make_request(clock, ttl=1800.5))          # float 同样拒
    assert m.registered_count == 0


# ── 3. TTL 帽 ≤1h（超帽拒 / 边界 3600 过 / 非正拒）──────────────────────────

def test_mint_ttl_cap(clock):
    m = make_minter(clock)
    with pytest.raises(TokenMintError, match="exceeds cap 3600"):
        m.mint(make_request(clock, ttl=TOKEN_TTL_CAP_SECONDS + 1))
    minted = m.mint(make_request(clock, ttl=TOKEN_TTL_CAP_SECONDS))   # 边界：恰 1h
    assert minted.ttl_seconds == 3600
    with pytest.raises(TokenMintError, match="positive int"):
        m.mint(make_request(clock, ttl=0))
    with pytest.raises(TokenMintError, match="positive int"):
        m.mint(make_request(clock, ttl=-5))


# ── 4. 禁缓存：同参数重复铸造必然全新 token ──────────────────────────────────

def test_no_cache_identical_requests_mint_fresh(clock):
    signer = MockSigner()
    m = make_minter(clock, signer)
    r = make_request(clock)
    t1 = m.mint(r)
    clock.advance(60)
    t2 = m.mint(r)                                        # 完全相同的请求再来一次
    assert t1.token_ref != t2.token_ref                   # 不可能拿到旧 token
    assert t1.expires_at != t2.expires_at
    assert len(signer.calls) == 2                         # 签名器每次都重新走（禁缓存）
    assert len([e for e in m.mint_log if e["event"] == "MINT"]) == 2
    assert m.cache_mode == "disabled"                     # 结构性质：无缓存路径可开


# ── 5. 禁代签：无签名器拒绝；私钥材料/裸路径冒充 bao 引用构造即拒 ─────────────

def test_no_proxy_signing_without_signer(clock):
    m = PlatformTokenMinter(now=clock)                    # 未 bind_signer
    with pytest.raises(TokenMintError, match="never signs by itself"):
        m.mint(make_request(clock))
    assert m.mint_log == ()                               # 拒绝路径零留痕污染（未登记任何 token）


def test_raw_key_material_rejected_everywhere(clock):
    m = PlatformTokenMinter(now=clock)
    with pytest.raises(TokenMintError, match="bao reference"):
        m.bind_signer(MockSigner(), signer_bao_ref="-----BEGIN RSA PRIVATE KEY-----")
    with pytest.raises(TokenMintError, match="bao reference"):
        m.bind_signer(MockSigner(), signer_bao_ref="D:/keys/app.pem")   # 裸文件路径也不是 bao 引用
    with pytest.raises(TokenMintError, match="bao reference"):
        GitHubAppInstallationFlow(
            app_id="123", installation_id="456",
            private_key_bao_ref="pem-blob:MIIE...",        # 非 openbao: 前缀
            signer=MockSigner(), transport=MockTransport(expires_at=clock() + 600), now=clock)


# ── 6. GitHub App installation 流程（mock 两跳）──────────────────────────────

def test_installation_flow_mock_end_to_end(clock):
    signer = MockSigner()
    transport = MockTransport(expires_at=clock() + 1800)
    flow = GitHubAppInstallationFlow(
        app_id="123456", installation_id="7891011",
        private_key_bao_ref="openbao:secret/credentials/github-apps/ci-bot",
        signer=signer, transport=transport, now=clock)
    req = make_request(clock, ttl=1800)
    minted, reference = flow.mint_installation_token(req)
    # 第一跳：JWT claims 组装在本模块、签名在 signer
    assert flow.last_jwt_claims["iss"] == "123456"
    assert flow.last_jwt_claims["exp"] - flow.last_jwt_claims["iat"] == 600
    assert signer.calls[0]["iss"] == "123456"
    # 第二跳：call 打到 installation 端点；明文只随返回值交给调用方
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert "/app/installations/7891011/access_tokens" in call["url"]
    assert reference["token_plaintext_held_by"] == "caller_only"
    assert reference["installation_token_ref"] == "gh-install:7891011"
    assert "ghu_mock-token-plaintext" not in repr(minted)          # 铸造对象无明文
    assert minted.decision_ref == "opa:dec-0001" and minted.signer_bao_ref.startswith("openbao:")
    # H3b：flow 内 minter 常驻——留痕挂在 flow 上，不随单次铸造丢弃
    assert len([e for e in flow.mint_log if e["event"] == "MINT"]) == 1
    assert flow.registered_count == 1
    minted2, _ = flow.mint_installation_token(make_request(clock, decision_ref="opa:dec-0002"))
    assert len([e for e in flow.mint_log if e["event"] == "MINT"]) == 2
    assert flow.registered_count == 2
    assert minted2.token_ref != minted.token_ref


def test_installation_jwt_claims_ttl_cap(clock):
    flow = GitHubAppInstallationFlow(
        app_id="1", installation_id="2",
        private_key_bao_ref="openbao:secret/credentials/github-apps/reader",
        signer=MockSigner(), transport=MockTransport(expires_at=clock() + 600), now=clock)
    claims = flow.installation_jwt_claims()
    assert claims["exp"] - claims["iat"] == GITHUB_JWT_TTL_CAP_SECONDS
    with pytest.raises(TokenMintError, match="600"):
        flow.installation_jwt_claims(ttl_seconds=601)


def test_installation_flow_refuses_platform_ttl_over_cap(clock):
    transport = MockTransport(expires_at=clock() + 7200)   # GitHub 侧给了 2h
    flow = GitHubAppInstallationFlow(
        app_id="1", installation_id="2",
        private_key_bao_ref="openbao:secret/credentials/github-apps/release-bot",
        signer=MockSigner(), transport=transport, now=clock)
    with pytest.raises(TokenMintError, match="exceeds 3600s cap"):
        flow.mint_installation_token(make_request(clock, ttl=3600))
    assert transport.calls and flow.last_jwt_claims        # 两跳都走过（拒绝发生在登记前）


def test_installation_flow_refuses_failed_exchange(clock):
    transport = MockTransport(status=403)
    flow = GitHubAppInstallationFlow(
        app_id="1", installation_id="2",
        private_key_bao_ref="openbao:secret/credentials/github-apps/ci-bot",
        signer=MockSigner(), transport=transport, now=clock)
    with pytest.raises(TokenMintError, match="failed"):
        flow.mint_installation_token(make_request(clock))


# ── 7. 扩缩权不对称：硬清单 → L4 守门者；扩权 → L3 提权租约 ───────────────────

def test_hard_list_categories_are_w06_escalation_objects(clock):
    """引用打通：四类硬清单**就是** W-06 escalation.HARD_LIST 本体（单一真相源）。"""
    assert HARD_LIST_CATEGORIES is HARD_LIST
    assert HARD_LIST == (HARD_MONEY, HARD_LEGAL_TOS, HARD_IRREVERSIBLE, HARD_THEORY_APPROVAL)
    # 清单外的类目在构造期即拒（打回 L3 的语义由 W-06 render_readiness 承担）
    with pytest.raises(TokenMintError, match="hard_list_category"):
        make_request(clock, hard_list="something_else")


def test_hard_list_category_escalates_L4_never_mints(clock):
    m = make_minter(clock)
    for category in HARD_LIST_CATEGORIES:                  # 四类硬清单逐类验证
        with pytest.raises(HardListEscalationError) as ei:
            m.mint(make_request(clock, task_class="repo_archive", task_ref="wo-77",
                                scopes=("github:repo:read",), hard_list=category))
        signal = ei.value.signal
        assert signal["kind"] == "permission_escalation_required"
        assert signal["escalation_level"] == ESCALATION_L4
        # 内嵌 W-06 就绪包机读结论：起步态 BLOCKED（四件套未补齐），fail-closed
        readiness = signal["readiness"]
        assert readiness["verdict"] == "BLOCKED"
        assert readiness["ready"] is False
        assert "in_permission_no_solution" in readiness["missing"]   # 无台账 case → 无法证明
        assert len(readiness["pieces"]) == 4
        assert readiness["pieces"][1]["key"] == "category_clear"     # 范畴件 PASS
        assert readiness["pieces"][1]["verdict"] == "PASS"
    assert m.registered_count == 0                         # 登记簿空：升级路径绝不铸 token
    escalates = [e for e in m.mint_log if e["event"] == "ESCALATE"]
    assert len(escalates) == len(HARD_LIST_CATEGORIES)


def test_hard_list_with_empty_scopes_still_escalates(clock):
    """H2b：硬清单 + 空 scope 必须先出升级对象（原先被空 scope 的
    TokenMintError 抢先，ESCALATE 零留痕）。"""
    m = make_minter(clock)
    with pytest.raises(HardListEscalationError) as ei:
        m.mint(make_request(clock, task_class="repo_archive", task_ref="wo-99",
                            scopes=(), hard_list=HARD_IRREVERSIBLE))
    assert ei.value.signal["escalation_level"] == ESCALATION_L4
    assert m.registered_count == 0
    assert len([e for e in m.mint_log if e["event"] == "ESCALATE"]) == 1


def test_hard_list_readiness_ready_when_guardian_fills_pieces(clock):
    """守门者补齐事实+可逆性+台账 case 后，就绪包可转 READY——但铸造路径仍封闭。"""
    from jiuwen_glue.escalation import (
        EscalationCase,
        LEVEL_L3,
        ReadinessTask,
        render_readiness,
    )

    req = make_request(clock, task_class="repo_archive", task_ref="wo-88",
                       scopes=("github:repo:read",), hard_list=HARD_IRREVERSIBLE)
    # 造一个 L0..L3 打满尝试的台账 case（权限内无解证明的机判输入；
    # case 无升级历史 → _case_last_signature 返回 ""，attempt 键用空签名）
    case = EscalationCase(task_ref="wo-88", level=LEVEL_L3)
    for lv in ("L0", "L1", "L2", "L3"):
        case.attempts[(lv, "")] = 3
    task = ReadinessTask(
        task_ref="wo-88", title="归档仓库", hard_list_category=HARD_IRREVERSIBLE,
        facts=(FrozenFact(statement="repo_archive 是不可逆动作", evidence_ref="wo-88/fact-1"),),
        reversibility=ReversibilityAssessment(
            reversible=False, impact="仓库及其历史不可恢复", rollback_ref="",
            assessed_by="gatekeeper"))
    pkg = render_readiness(task, case=case)
    assert pkg.verdict == "PASS" and pkg.ready
    assert pkg.knock_back_to_l3 is False
    # 起步态（无 facts/reversibility/case）对照：hard_list_readiness 恒 BLOCKED
    assert hard_list_readiness(req).verdict == "BLOCKED"
    # 即便 READY：铸造器对硬清单仍然永不铸造（门在铸造器，不在包）
    m = make_minter(clock)
    with pytest.raises(HardListEscalationError):
        m.mint(req)
    assert m.registered_count == 0


def test_scope_expansion_escalates_L3(clock):
    m = make_minter(clock)
    req = make_request(clock, scopes=("github:repo:read", "github:repo:write"),
                       snapshot=("github:repo:read",))    # 申请超出五交集快照
    with pytest.raises(HardListEscalationError) as ei:
        m.mint(req)
    signal = ei.value.signal
    assert signal["escalation_level"] == ESCALATION_L3
    assert "github:repo:write" in signal["candidate_causes"] or \
           signal["reason"].startswith("scope expansion")
    assert "guardian_package" not in signal               # 四件套是 L4 专属
    assert m.registered_count == 0                        # 扩权尝试绝不铸造


def test_escalation_signal_shape_matches_reconcile_contract(clock):
    """升级对象与 ops/meters/reconcile.py 的 reconcile_alarm 契约同形（W-08 先例）。"""
    req = make_request(clock)
    sig = escalation_signal(level=ESCALATION_L4, reason="r", request=req,
                            candidate_causes=["c"], required_action="a")
    for key in ("kind", "level", "escalation_level", "task_class", "platform",
                "requested_scopes", "agent_ref", "decided_by", "reason",
                "candidate_causes", "required_action"):
        assert key in sig                                  # 契约字段逐一对齐
    assert sig["level"] == "alarm"


# ── 8. 撤销/过期：状态机 + 每次状态变更留痕 ──────────────────────────────────

def test_revoke_and_expire_with_audit(clock):
    m = make_minter(clock)
    t = m.mint(make_request(clock, ttl=600))
    assert m.status_of(t.token_ref) == STATUS_ACTIVE
    assert m.active_at(clock() + 599) == [t.token_ref]
    assert m.active_at(clock() + 601) == []
    # H3a：仅轮询 status_of（不调 expire_due）也必须翻状态并留 EXPIRE 痕
    clock.advance(700)
    assert m.status_of(t.token_ref) == STATUS_EXPIRED
    expires = [e for e in m.mint_log if e["event"] == "EXPIRE"]
    assert len(expires) == 1 and expires[0]["decided_by"] == "opa:dec-0001"
    assert m.status_of(t.token_ref) == STATUS_EXPIRED       # 幂等：不重复留痕
    assert len([e for e in m.mint_log if e["event"] == "EXPIRE"]) == 1
    t1b = m.mint(make_request(clock, ttl=600))
    clock.advance(700)
    flipped = m.expire_due()
    assert flipped == [t1b.token_ref]
    assert m.status_of(t1b.token_ref) == STATUS_EXPIRED
    assert len([e for e in m.mint_log if e["event"] == "EXPIRE"]) == 2
    # 撤销需要决策引用（状态变更留痕 decided_by）
    with pytest.raises(TokenMintError, match="decision_ref"):
        m.revoke(t.token_ref, decision_ref="")
    t2 = m.mint(make_request(clock))
    rec = m.revoke(t2.token_ref, decision_ref="opa:dec-shrink-7")
    assert rec["status"] == STATUS_REVOKED and rec["revoked_by"] == "opa:dec-shrink-7"
    assert m.status_of(t2.token_ref) == STATUS_REVOKED
    assert [e for e in m.mint_log if e["event"] == "REVOKE"]


def test_unknown_token_and_unknown_platform_refused(clock):
    m = make_minter(clock)
    with pytest.raises(TokenMintError, match="unknown token_ref"):
        m.status_of("tok:github:nope")
    with pytest.raises(TokenMintError, match="unknown platform"):
        make_request(clock, platform="gitlab")
    with pytest.raises(TokenMintError, match="no scopes"):
        m.mint(make_request(clock, scopes=(), snapshot=("github:repo:read",)))
        # 空 scope 永不铸（escalation-only；快照显式给出以过 H2 检查）
