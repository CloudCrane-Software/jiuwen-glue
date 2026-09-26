# coding: utf-8
"""e2b_compat 异常族：依赖缺失 / 凭据缺失 / 一般配置错误。

设计约定（PROP-0008 + EXECUTION-PROTOCOL.md 红线 3）：
- 依赖与凭据错误以异常形式向调用方传播（它们是部署/配置问题，不是沙箱操作失败，
  不应被折叠成 success=false 的操作结果）；
- 沙箱侧操作失败（命令非零退出、文件不存在、E2B SDK 抛错）按 openJiuwen 惯例
  折叠成非零 code 的 Result 对象返回（见 provider.py 的 _build_*_error 帮助函数，
  对齐 aio.py:52-71 的错误结果构造方式）；
- 所有错误消息只允许出现环境变量名，绝不包含 key 值。
"""
from __future__ import annotations


class E2BCompatError(RuntimeError):
    """e2b_compat 配置/使用错误基类。"""


class E2BCompatDependencyError(E2BCompatError):
    """e2b SDK 未安装或不可用（可选 extra 缺失）。"""


class E2BCompatCredentialError(E2BCompatError):
    """E2B API key 未配置。消息只含环境变量名，不含 key 值。"""
