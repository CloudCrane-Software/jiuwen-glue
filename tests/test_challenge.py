# coding: utf-8
"""Challenge（授权三态第三态）生命周期测试：结构化对象 / 过期 fail-closed / 模型只见高层状态."""
from __future__ import annotations

import pytest

from jiuwen_glue import (
    APPROVED,
    CHALLENGE_EXPIRED,
    CHALLENGE_PENDING,
    CONFIRM_DUTY_OFFICER,
    CONFIRM_RESOURCE_OWNER,
    CONFIRM_USER,
    DENIED,
    ChallengeBoard,
    ChallengeStateError,
    UnknownChallengeError,
)


def _open(board, **over):
    kw = dict(who_confirms=CONFIRM_RESOURCE_OWNER, resource="prod:svc-a/instance-7",
              action="restart", method="console.ask", ttl_seconds=600,
              agent_identity_ref="ag:dev-core@t0/run:i-1/task:t-1")
    kw.update(over)
    return board.open(**kw)


def test_structured_object_fields(clock):
    board = ChallengeBoard(now=clock)
    ch = _open(board)
    assert ch.state == CHALLENGE_PENDING
    assert ch.who_confirms == CONFIRM_RESOURCE_OWNER
    assert ch.resource == "prod:svc-a/instance-7"
    assert ch.action == "restart"
    assert ch.expires_at == ch.created_at + 600


def test_to_ask_payload_shows_who_wants_what(clock):
    """载荷展示"哪个 Agent 想对哪个资源做什么"（TeamPermissionRail ask 路由）。"""
    board = ChallengeBoard(now=clock)
    ch = _open(board)
    p = ch.to_ask_payload()
    assert p["agent"] == "ag:dev-core@t0/run:i-1/task:t-1"
    assert p["resource"] == "prod:svc-a/instance-7"
    assert p["action"] == "restart"
    assert p["who_confirms"] == CONFIRM_RESOURCE_OWNER
    assert p["state"] == CHALLENGE_PENDING


def test_model_only_sees_high_level_states_never_auth_code(clock):
    """模型只见高层状态（pending/approved/denied/expired）；
    对象与载荷都拿不到授权码/token 字段（API 层写死）。"""
    board = ChallengeBoard(now=clock)
    ch = _open(board)
    assert set(vars(ch)) >= {"state", "who_confirms", "resource", "action",
                             "method", "expires_at"}
    forbidden = {"auth_code", "token", "secret", "credential", "authorization"}
    assert not forbidden & set(vars(ch))                    # 对象字段
    assert not forbidden & set(ch.to_ask_payload())         # ask 载荷
    board.resolve(ch.challenge_id, approved=True, by="resource_owner:owner-1")
    assert board.state_of(ch.challenge_id) == APPROVED      # 高层状态
    assert not forbidden & set(board.get(ch.challenge_id).to_ask_payload())


def test_approve_and_deny_lifecycle(clock):
    board = ChallengeBoard(now=clock)
    a = _open(board)
    board.resolve(a.challenge_id, approved=True, by="resource_owner:owner-1")
    assert board.state_of(a.challenge_id) == APPROVED
    assert board.get(a.challenge_id).resolved_by == "resource_owner:owner-1"

    d = _open(board)
    board.resolve(d.challenge_id, approved=False, by="resource_owner:owner-1")
    assert board.state_of(d.challenge_id) == DENIED
    # 终态不可再裁决
    with pytest.raises(ChallengeStateError):
        board.resolve(a.challenge_id, approved=False, by="resource_owner:owner-2")


def test_expiry_is_fail_closed(clock):
    """过期即 expired：过期后裁决被拒绝——缺口必须重新发起 Challenge。"""
    board = ChallengeBoard(now=clock)
    ch = _open(board, ttl_seconds=60)
    clock.advance(61)
    assert board.state_of(ch.challenge_id) == CHALLENGE_EXPIRED      # 惰性过期
    with pytest.raises(ChallengeStateError):
        board.resolve(ch.challenge_id, approved=True, by="resource_owner:owner-1")
    assert board.state_of(ch.challenge_id) == CHALLENGE_EXPIRED      # 批准不产生效力
    # 重新发起一条才能继续
    ch2 = _open(board, ttl_seconds=600)
    board.resolve(ch2.challenge_id, approved=True, by="resource_owner:owner-1")
    assert board.state_of(ch2.challenge_id) == APPROVED


def test_approval_right_before_expiry_ok(clock):
    board = ChallengeBoard(now=clock)
    ch = _open(board, ttl_seconds=60)
    clock.advance(59)
    board.resolve(ch.challenge_id, approved=True, by="resource_owner:owner-1")
    clock.advance(1000)
    assert board.state_of(ch.challenge_id) == APPROVED   # 已决状态不再过期


def test_pending_queue_by_confirmer(clock):
    board = ChallengeBoard(now=clock)
    _open(board, who_confirms=CONFIRM_RESOURCE_OWNER)
    _open(board, who_confirms=CONFIRM_RESOURCE_OWNER, resource="prod:svc-b/instance-1")
    clock.advance(3600)                                  # 前两条已过期
    duty = _open(board, who_confirms=CONFIRM_DUTY_OFFICER,
                 ttl_seconds=600)                        # duty 的窗口此刻才开始
    assert board.pending_for(CONFIRM_RESOURCE_OWNER) == []
    assert [c.challenge_id for c in board.pending_for(CONFIRM_DUTY_OFFICER)] == \
        [duty.challenge_id]


def test_open_validation(clock):
    board = ChallengeBoard(now=clock)
    with pytest.raises(ChallengeStateError):
        _open(board, who_confirms="agent-self")          # Agent 不得自确认
    with pytest.raises(ChallengeStateError):
        _open(board, ttl_seconds=0)                      # 无有效期的 Challenge 不允许存在
    with pytest.raises(ChallengeStateError):
        _open(board, resource="")
    with pytest.raises(UnknownChallengeError):
        board.state_of("nope")


def test_wrong_confirmer_realm_rejected(clock):
    """who 维度强制：错域裁决人一律拒绝（by 必须落在 who_confirms 的前缀域内）；
    拒绝后 Challenge 保持 pending，且不留 RESOLVE 审计痕。"""
    board = ChallengeBoard(now=clock)
    u = _open(board, who_confirms=CONFIRM_USER)
    with pytest.raises(ChallengeStateError, match="not allowed"):
        board.resolve(u.challenge_id, approved=True, by="resource_owner:alice")
    assert board.state_of(u.challenge_id) == CHALLENGE_PENDING
    # 手册案例：资源级/值班级操作不能由控制台操作员代裁
    ro = _open(board, who_confirms=CONFIRM_RESOURCE_OWNER)
    with pytest.raises(ChallengeStateError, match="not allowed"):
        board.resolve(ro.challenge_id, approved=True, by="console:operator")
    duty = _open(board, who_confirms=CONFIRM_DUTY_OFFICER)
    with pytest.raises(ChallengeStateError, match="not allowed"):
        board.resolve(duty.challenge_id, approved=True, by="console:operator")
    # 无前缀的裸身份同样拒绝
    with pytest.raises(ChallengeStateError, match="not allowed"):
        board.resolve(u.challenge_id, approved=True, by="alice")
    assert [e for e in board.audit if e["event"] == "RESOLVE"] == []


def test_confirmer_realms_allow_matching_prefix_and_audit_who(clock):
    """各域裁决人按前缀域放行；RESOLVE 审计记录 who 维度（不再纯展示）。"""
    board = ChallengeBoard(now=clock)
    u = _open(board, who_confirms=CONFIRM_USER)
    board.resolve(u.challenge_id, approved=True, by="console:operator")
    ro = _open(board, who_confirms=CONFIRM_RESOURCE_OWNER)
    board.resolve(ro.challenge_id, approved=False, by="resource_owner:alice")
    duty = _open(board, who_confirms=CONFIRM_DUTY_OFFICER)
    board.resolve(duty.challenge_id, approved=True, by="duty_officer:bob")
    resolves = [e for e in board.audit if e["event"] == "RESOLVE"]
    assert [e["who"] for e in resolves] == \
        [CONFIRM_USER, CONFIRM_RESOURCE_OWNER, CONFIRM_DUTY_OFFICER]
    assert board.get(u.challenge_id).resolved_by == "console:operator"
