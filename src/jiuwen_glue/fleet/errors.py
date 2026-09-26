# coding: utf-8
"""fleet 子包异常 — 全部继承既有 ``jiuwen_glue.errors.GlueError``（不改既有模块）.

所有违规异常在触发时都会被对应组件记入 audit 日志（检测 = 拒绝 + 留痕），
与包内既有模块的纪律一致。
"""
from __future__ import annotations

from ..errors import GlueError


class FleetError(GlueError):
    """fleet（节点池 + 调度器）异常基类。"""


# ── 节点注册（PROP-0003）─────────────────────────────────────────────────────

class RegistrationError(FleetError):
    """节点注册协议违规基类。"""


class AttestationError(RegistrationError):
    """OpenBao JWT 注册声明不合法：声明缺失 / 已过期 / 主体或信任等级与声明不符。

    注意：本层只做**协议级校验**（claims 齐全性 + 有效期 + 一致性）；
    JWT 签名验证归真实 OpenBao（jwt auth role）接入工单，见 fleet-design.md。
    """


class OnlineWindowError(RegistrationError):
    """在线窗口声明不可解析（fail-closed：解析不了就拒绝注册，不猜）。"""


class UnknownNodeError(FleetError):
    """节点未注册。"""


class NodeStateError(FleetError):
    """节点状态操作非法（对 STALE 节点心跳 / 对已注销节点操作等）。"""


# ── 调度（PROP-0004 P1）──────────────────────────────────────────────────────

class SchedulingError(FleetError):
    """调度器使用错误（重复记账、非法参数等）。"""


class UnknownAssignmentError(SchedulingError):
    """派工单（Assignment）不存在。"""


class WorkOrderStateError(SchedulingError):
    """工单状态迁移非法（重复完成 / 对非 CLAIMED 工单回收等）。"""


class SecretScopeError(FleetError):
    """密钥范围声明违规：``lease_scoped_secrets`` 只允许 scheme 限定的**引用**
    （如 ``bao://kv/data/company/gpu/token``），不允许裸令牌值——
    执行面零长期密钥红线在协议层的体现。
    """
