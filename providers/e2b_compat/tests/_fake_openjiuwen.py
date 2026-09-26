# coding: utf-8
"""测试用假 openjiuwen 模块：按真实接口形状搭出最小可导入面，供"openjiuwen 在"的
注册路径测试使用（本机未安装 openjiuwen，真实形状以 repos/openjiuwen 源码为参照，
这里只复刻语义：注册表字典行为 + kwargs 构造的结果对象，不复制源码）。

覆盖 e2b_compat._shims 需要的全部绑定：
- sandbox.sandbox_registry.SandboxRegistry（register/get/unregister/create_provider/provider）
- sandbox.providers.base_provider.{BaseFSProvider, BaseShellProvider, BaseCodeProvider}
- sandbox.gateway.gateway.SandboxEndpoint
- config.SandboxGatewayConfig
- result 包（BaseResult + 各 Data/Result）
- common.exception.codes.StatusCode + result.base_result.build_operation_error_result
"""
from __future__ import annotations

import sys
import types
from typing import Any, ClassVar, Dict, Optional, Tuple, Type


class _Model:
    """pydantic BaseModel 的最小替身（同 _shims 的降级策略：kwargs + 属性访问）。"""

    _fields: ClassVar[Tuple[Tuple[str, Any], ...]] = ()

    def __init__(self, **data: Any) -> None:
        unknown = set(data) - {name for name, _d in self._fields}
        if unknown:
            raise TypeError(f"{type(self).__name__} got unexpected field(s): {sorted(unknown)}")
        for name, default in self._fields:
            setattr(self, name, data.pop(name, default))

    def __repr__(self) -> str:  # pragma: no cover
        inner = ", ".join(f"{n}={getattr(self, n)!r}" for n, _d in self._fields)
        return f"{type(self).__name__}({inner})"


class _Code:
    def __init__(self, code: int, errmsg: str) -> None:
        self.code = code
        self.errmsg = errmsg


class _StatusCode:
    SUCCESS = _Code(0, "success")
    SYS_OPERATION_FS_EXECUTION_ERROR = _Code(
        199003, "file system operation execution error, execution: {execution}, reason: {error_msg}"
    )
    SYS_OPERATION_SHELL_EXECUTION_ERROR = _Code(
        199004, "shell operation execution error, execution: {execution}, reason: {error_msg}"
    )
    SYS_OPERATION_CODE_EXECUTION_ERROR = _Code(
        199005, "code operation execution error, execution: {execution}, reason: {error_msg}"
    )


def _build_operation_error_result(
    *, error_type: Any, msg_format_kwargs: Dict[str, Any], result_cls: Type, data: Any = None, **kwargs: Any
) -> Any:
    message = error_type.errmsg.format(**msg_format_kwargs)
    payload = {"code": error_type.code, "message": message, "data": data}
    payload.update(kwargs)
    return result_cls(**payload)


class _BaseResult(_Model):
    _fields = (("code", 0), ("message", ""), ("data", None))


def _make_data_class(name: str, fields: Tuple[Tuple[str, Any], ...]) -> Type:
    return type(name, (_Model,), {"_fields": fields})


_DATA_SPECS = {
    "ReadFileData": (("path", ""), ("content", ""), ("mode", "text")),
    "WriteFileData": (("path", ""), ("size", 0), ("mode", "text")),
    "FileSystemItem": (
        ("name", ""), ("path", ""), ("size", 0),
        ("modified_time", ""), ("is_directory", False), ("type", None),
    ),
    "FileSystemData": (
        ("total_count", 0), ("list_items", ()), ("root_path", ""),
        ("recursive", False), ("max_depth", None),
    ),
    "ExecuteCmdData": (
        ("command", ""), ("cwd", "."), ("exit_code", None), ("stdout", ""), ("stderr", ""),
    ),
    "ExecuteCmdChunkData": (
        ("text", ""), ("type", None), ("chunk_index", 0), ("exit_code", None), ("metadata", None),
    ),
    "ExecuteCodeData": (
        ("code_content", ""), ("language", ""), ("exit_code", None), ("stdout", ""), ("stderr", ""),
    ),
    "ExecuteCodeChunkData": (
        ("text", ""), ("type", None), ("chunk_index", 0), ("exit_code", None), ("metadata", None),
    ),
}
_RESULT_NAMES = (
    "ReadFileResult", "WriteFileResult", "ListFilesResult", "ListDirsResult",
    "ExecuteCmdResult", "ExecuteCmdStreamResult", "ExecuteCodeResult", "ExecuteCodeStreamResult",
)


def _not_implemented(method: str):
    def _method(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(f"{type(self).__name__}.{method} is not implemented")

    return _method


def _make_base_provider(name: str, methods: Tuple[str, ...]) -> Type:
    ns: Dict[str, Any] = {
        "__init__": lambda self, endpoint=None, config=None: (
            setattr(self, "endpoint", endpoint), setattr(self, "config", config)
        )[0],
        "_fields": (("endpoint", None), ("config", None)),
    }
    for method in methods:
        ns[method] = _not_implemented(method)
    return type(name, (_Model,), ns)


class _FakeSandboxRegistry:
    """SandboxRegistry 的语义复刻（类级字典 + 装饰器），供注册路径测试断言。"""

    _operations: Dict[str, Dict[str, Type]] = {}
    _launchers: Dict[str, Type] = {}

    @classmethod
    def register_provider(cls, sandbox_type: str, operation_type: str, provider_cls: Type) -> None:
        cls._operations.setdefault(sandbox_type, {})[operation_type] = provider_cls

    @classmethod
    def get_provider_cls(cls, sandbox_type: str, operation_type: str) -> Optional[Type]:
        return cls._operations.get(sandbox_type, {}).get(operation_type)

    @classmethod
    def unregister_provider(cls, sandbox_type: str, operation_type: str) -> None:
        if sandbox_type in cls._operations:
            cls._operations[sandbox_type].pop(operation_type, None)
            if not cls._operations[sandbox_type]:
                cls._operations.pop(sandbox_type, None)

    @classmethod
    def create_provider(
        cls, sandbox_type: str, operation_type: str, endpoint: Any, config: Any = None
    ) -> Any:
        provider_cls = cls.get_provider_cls(sandbox_type, operation_type)
        if not provider_cls:
            raise NotImplementedError(
                f"Sandbox type '{sandbox_type}' does not support operation '{operation_type}'"
            )
        return provider_cls(endpoint=endpoint, config=config)

    @classmethod
    def provider(cls, sandbox_type: str, operation_type: str):
        def decorator(provider_cls: Type) -> Type:
            cls.register_provider(sandbox_type, operation_type, provider_cls)
            return provider_cls

        return decorator


class _FakeSandboxEndpoint(_Model):
    _fields = (("base_url", ""), ("sandbox_id", None), ("isolation_key", None))


class _FakeSandboxGatewayConfig(_Model):
    _fields = ()


def install_fake_openjiuwen() -> types.SimpleNamespace:
    """构建假 openjiuwen 模块树并注入 sys.modules；返回可断言的关键对象。"""
    # 类级注册表状态跨测试复位（模块缓存使本文件只执行一次）
    _FakeSandboxRegistry._operations = {}
    _FakeSandboxRegistry._launchers = {}

    def module(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    module("openjiuwen")
    module("openjiuwen.core")
    module("openjiuwen.core.common")
    module("openjiuwen.core.common.exception")
    codes_mod = module("openjiuwen.core.common.exception.codes")
    codes_mod.StatusCode = _StatusCode

    sys_op = module("openjiuwen.core.sys_operation")
    module("openjiuwen.core.sys_operation.result")
    base_result_mod = module("openjiuwen.core.sys_operation.result.base_result")
    base_result_mod.BaseResult = _BaseResult
    base_result_mod.build_operation_error_result = _build_operation_error_result

    result_pkg = sys.modules["openjiuwen.core.sys_operation.result"]
    data_classes: Dict[str, Type] = {}
    for name, fields in _DATA_SPECS.items():
        cls = _make_data_class(name, fields)
        setattr(result_pkg, name, cls)
        data_classes[name] = cls
    for result_name in _RESULT_NAMES:
        setattr(result_pkg, result_name, type(result_name, (_BaseResult,), {}))

    config_mod = module("openjiuwen.core.sys_operation.config")
    config_mod.SandboxGatewayConfig = _FakeSandboxGatewayConfig

    sandbox_mod = module("openjiuwen.core.sys_operation.sandbox")
    registry_mod = module("openjiuwen.core.sys_operation.sandbox.sandbox_registry")
    registry_mod.SandboxRegistry = _FakeSandboxRegistry
    gateway_mod = module("openjiuwen.core.sys_operation.sandbox.gateway")
    gateway_file = module("openjiuwen.core.sys_operation.sandbox.gateway.gateway")
    gateway_file.SandboxEndpoint = _FakeSandboxEndpoint
    providers_mod = module("openjiuwen.core.sys_operation.sandbox.providers")
    base_provider_mod = module("openjiuwen.core.sys_operation.sandbox.providers.base_provider")

    fs_methods = (
        "read_file", "read_file_stream", "write_file", "write_file_stream",
        "upload_file", "upload_file_stream", "download_file", "download_file_stream",
        "list_files", "list_directories", "search_files",
    )
    base_provider_mod.BaseFSProvider = _make_base_provider("BaseFSProvider", fs_methods)
    base_provider_mod.BaseShellProvider = _make_base_provider("BaseShellProvider", ("execute_cmd", "execute_cmd_stream"))
    base_provider_mod.BaseCodeProvider = _make_base_provider("BaseCodeProvider", ("execute_code", "execute_code_stream"))

    # 父包属性挂链（import 语句会用到）
    sys.modules["openjiuwen"].core = sys.modules["openjiuwen.core"]
    sys.modules["openjiuwen.core"].common = sys.modules["openjiuwen.core.common"]
    sys.modules["openjiuwen.core.common"].exception = sys.modules["openjiuwen.core.common.exception"]
    sys.modules["openjiuwen.core.common.exception"].codes = codes_mod
    sys.modules["openjiuwen.core"].sys_operation = sys_op
    sys_op.result = result_pkg
    sys_op.config = config_mod
    sys_op.sandbox = sandbox_mod
    sandbox_mod.sandbox_registry = registry_mod
    sandbox_mod.gateway = gateway_mod
    sandbox_mod.providers = providers_mod
    gateway_mod.gateway = gateway_file
    providers_mod.base_provider = base_provider_mod

    return types.SimpleNamespace(
        registry=_FakeSandboxRegistry,
        endpoint=_FakeSandboxEndpoint,
        gateway_config=_FakeSandboxGatewayConfig,
        status_code=_StatusCode,
    )


def uninstall_fake_openjiuwen() -> None:
    for name in list(sys.modules):
        if name == "openjiuwen" or name.startswith("openjiuwen."):
            del sys.modules[name]
