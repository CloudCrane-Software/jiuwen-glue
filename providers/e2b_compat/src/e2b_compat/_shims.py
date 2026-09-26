# coding: utf-8
"""openJiuwen 可选绑定层（零硬 import）。

本模块把"对 openjiuwen 的全部 import"收敛到一处，全部包在 try/except 里：

- openjiuwen 可导入时 → 直接绑定真实的基类 / 结果模型 / StatusCode / 注册器，
  Provider 与 aio.py/jiuwenbox.py 同构（接口形状来源：
  repos/openjiuwen/agent-core/openjiuwen/core/sys_operation/sandbox/providers/base_provider.py:27-223、
  .../sandbox/sandbox_registry.py:44-80、.../result/ 各模型）。
- openjiuwen 不可导入时 → 装入零依赖的"形状等价替身"（同名同字段同签名，
  仅 kwargs 构造 + 属性访问），让本包仍可导入、Provider 仍可实例化与单测，
  但不注册进任何真实注册表（registry_patch 在该情况下会给出安装指引）。

替身只服务于"未安装 openjiuwen 时的降级与测试"，不模拟 openjiwen 的任何行为
（诚实边界：缺基类就是缺，不假装）。
"""
from __future__ import annotations

from typing import Any, ClassVar, Dict, Optional, Tuple, Type

# ---------------------------------------------------------------------------
# 真实绑定（openjiuwen 已安装）
# ---------------------------------------------------------------------------
OPENJIUWEN_AVAILABLE = False

try:  # pragma: no cover - 本机未安装 openjiuwen 时走 except 分支
    from openjiuwen.core.sys_operation.sandbox.providers.base_provider import (  # noqa: F401
        BaseFSProvider,
        BaseShellProvider,
        BaseCodeProvider,
    )
    from openjiuwen.core.sys_operation.sandbox.gateway.gateway import SandboxEndpoint  # noqa: F401
    from openjiuwen.core.sys_operation.config import SandboxGatewayConfig  # noqa: F401
    from openjiuwen.core.sys_operation.result import (  # noqa: F401
        ReadFileResult,
        ReadFileData,
        WriteFileResult,
        WriteFileData,
        ListFilesResult,
        ListDirsResult,
        FileSystemData,
        FileSystemItem,
        ExecuteCmdResult,
        ExecuteCmdData,
        ExecuteCmdStreamResult,
        ExecuteCmdChunkData,
        ExecuteCodeResult,
        ExecuteCodeData,
        ExecuteCodeStreamResult,
        ExecuteCodeChunkData,
    )
    from openjiuwen.core.sys_operation.result.base_result import build_operation_error_result  # noqa: F401
    from openjiuwen.core.common.exception.codes import StatusCode  # noqa: F401
    from openjiuwen.core.sys_operation.sandbox.sandbox_registry import SandboxRegistry  # noqa: F401

    OPENJIUWEN_AVAILABLE = True
except Exception:  # ImportError 或 openjiwen 自身依赖缺失（如 pydantic）—— 一律降级
    OPENJIUWEN_AVAILABLE = False

# ---------------------------------------------------------------------------
# 零依赖替身（openjiuwen 缺失时的形状等价降级）
# ---------------------------------------------------------------------------
if not OPENJIUWEN_AVAILABLE:

    class _StandinModel:
        """pydantic BaseModel 的最小替身：仅 kwargs 构造 + 声明字段属性访问。

        不做校验、不做序列化——只保证 provider.py 在两种模式下用同一套
        构造/读取代码。未知字段直接 TypeError，与 pydantic 的严格性对齐，
        避免静默吞掉字段名拼写错误。
        """

        _fields_: ClassVar[Tuple[Tuple[str, Any], ...]] = ()

        def __init__(self, **data: Any) -> None:
            unknown = set(data) - {name for name, _default in self._fields_}
            if unknown:
                raise TypeError(
                    f"{type(self).__name__} got unexpected field(s): {sorted(unknown)}"
                )
            for name, default in self._fields_:
                setattr(self, name, data.pop(name, default))

        def __repr__(self) -> str:  # pragma: no cover - 仅调试用
            inner = ", ".join(f"{name}={getattr(self, name)!r}" for name, _d in self._fields_)
            return f"{type(self).__name__}({inner})"

    class _StandinResultBase(_StandinModel):
        _fields_: ClassVar[Tuple[Tuple[str, Any], ...]] = (
            ("code", 0),
            ("message", ""),
            ("data", None),
        )

    class _StandinProviderBase(_StandinModel):
        """替身基类公共部分：与真实实现一致地接受位置参数 (endpoint, config=None)。"""

        _fields_: ClassVar[Tuple[Tuple[str, Any], ...]] = (("endpoint", None), ("config", None))

        def __init__(self, endpoint: Any = None, config: Any = None, **data: Any) -> None:
            super().__init__(endpoint=endpoint, config=config, **data)

    class BaseFSProvider(_StandinProviderBase):  # noqa: N801 - 与真实类同名以保持导入兼容
        """替身：见真实 BaseFSProvider（base_provider.py:27-161）。"""

    class BaseShellProvider(_StandinProviderBase):  # noqa: N801
        """替身：见真实 BaseShellProvider（base_provider.py:164-191）。"""

    class BaseCodeProvider(_StandinProviderBase):  # noqa: N801
        """替身：见真实 BaseCodeProvider（base_provider.py:194-223）。"""

    class SandboxEndpoint(_StandinModel):  # noqa: N801
        """替身：见真实 SandboxEndpoint（gateway/gateway.py:44-48）。"""

        _fields_: ClassVar[Tuple[Tuple[str, Any], ...]] = (
            ("base_url", ""),
            ("sandbox_id", None),
            ("isolation_key", None),
        )

    class SandboxGatewayConfig(_StandinModel):  # noqa: N801
        """替身：网关配置占位（provider 仅透传，不读取其字段）。"""

        _fields_: ClassVar[Tuple[Tuple[str, Any], ...]] = ()

    class _StandinCode:
        __slots__ = ("code", "errmsg")

        def __init__(self, code: int, errmsg: str) -> None:
            self.code = code
            self.errmsg = errmsg

        def __repr__(self) -> str:  # pragma: no cover - 仅调试用
            return f"_StandinCode({self.code}, {self.errmsg!r})"

    class StatusCode:  # noqa: N801
        """替身：数值/模板与真实 StatusCode 对齐（codes.py:10, 988-994）。"""

        SUCCESS = _StandinCode(0, "success")
        SYS_OPERATION_FS_EXECUTION_ERROR = _StandinCode(
            199003,
            "file system operation execution error, execution: {execution}, reason: {error_msg}",
        )
        SYS_OPERATION_SHELL_EXECUTION_ERROR = _StandinCode(
            199004,
            "shell operation execution error, execution: {execution}, reason: {error_msg}",
        )
        SYS_OPERATION_CODE_EXECUTION_ERROR = _StandinCode(
            199005,
            "code operation execution error, execution: {execution}, reason: {error_msg}",
        )

    def build_operation_error_result(
        *,
        error_type: Any,
        msg_format_kwargs: Dict[str, Any],
        result_cls: Type,
        data: Optional[Any] = None,
        **kwargs: Any,
    ) -> Any:
        """替身：语义对齐真实实现（result/base_result.py:31-58）。"""
        message = error_type.errmsg.format(**msg_format_kwargs)
        payload: Dict[str, Any] = {"code": error_type.code, "message": message, "data": data}
        payload.update(kwargs)
        return result_cls(**payload)

    class _StandinData(_StandinModel):
        _fields_: ClassVar[Tuple[Tuple[str, Any], ...]] = ()

    class ReadFileData(_StandinData):  # noqa: N801
        _fields_: ClassVar[Tuple[Tuple[str, Any], ...]] = (
            ("path", ""),
            ("content", ""),
            ("mode", "text"),
        )

    class WriteFileData(_StandinData):  # noqa: N801
        _fields_: ClassVar[Tuple[Tuple[str, Any], ...]] = (
            ("path", ""),
            ("size", 0),
            ("mode", "text"),
        )

    class FileSystemItem(_StandinData):  # noqa: N801
        _fields_: ClassVar[Tuple[Tuple[str, Any], ...]] = (
            ("name", ""),
            ("path", ""),
            ("size", 0),
            ("modified_time", ""),
            ("is_directory", False),
            ("type", None),
        )

    class FileSystemData(_StandinData):  # noqa: N801
        _fields_: ClassVar[Tuple[Tuple[str, Any], ...]] = (
            ("total_count", 0),
            ("list_items", ()),
            ("root_path", ""),
            ("recursive", False),
            ("max_depth", None),
        )

    class ExecuteCmdData(_StandinData):  # noqa: N801
        _fields_: ClassVar[Tuple[Tuple[str, Any], ...]] = (
            ("command", ""),
            ("cwd", "."),
            ("exit_code", None),
            ("stdout", ""),
            ("stderr", ""),
        )

    class ExecuteCmdChunkData(_StandinData):  # noqa: N801
        _fields_: ClassVar[Tuple[Tuple[str, Any], ...]] = (
            ("text", ""),
            ("type", None),
            ("chunk_index", 0),
            ("exit_code", None),
            ("metadata", None),
        )

    class ExecuteCodeData(_StandinData):  # noqa: N801
        _fields_: ClassVar[Tuple[Tuple[str, Any], ...]] = (
            ("code_content", ""),
            ("language", ""),
            ("exit_code", None),
            ("stdout", ""),
            ("stderr", ""),
        )

    class ExecuteCodeChunkData(_StandinData):  # noqa: N801
        _fields_: ClassVar[Tuple[Tuple[str, Any], ...]] = (
            ("text", ""),
            ("type", None),
            ("chunk_index", 0),
            ("exit_code", None),
            ("metadata", None),
        )

    class ReadFileResult(_StandinResultBase):  # noqa: N801
        pass

    class WriteFileResult(_StandinResultBase):  # noqa: N801
        pass

    class ListFilesResult(_StandinResultBase):  # noqa: N801
        pass

    class ListDirsResult(_StandinResultBase):  # noqa: N801
        pass

    class ExecuteCmdResult(_StandinResultBase):  # noqa: N801
        pass

    class ExecuteCmdStreamResult(_StandinResultBase):  # noqa: N801
        pass

    class ExecuteCodeResult(_StandinResultBase):  # noqa: N801
        pass

    class ExecuteCodeStreamResult(_StandinResultBase):  # noqa: N801
        pass

    class SandboxRegistry:  # noqa: N801
        """替身：不做任何注册（真实注册见 registry_patch 的 openjiuwen 路径）。"""

        @classmethod
        def provider(cls, sandbox_type: str, operation_type: str):
            def decorator(provider_cls: Type) -> Type:
                return provider_cls

            return decorator
