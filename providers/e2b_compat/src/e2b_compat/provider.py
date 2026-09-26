# coding: utf-8
"""e2b_compat Provider：把 E2B 兼容 API 面投影成 openJiuwen SandboxRegistry 的 Provider 接口。

方案依据（PROP-0008 / PROP-0001 v1.7 §12.8）：openJiuwen 沙箱是"操作协议抽象 + 可插拔
Provider + 生命周期网关"架构，沙箱底座经 Provider 外接；本 Provider 把 E2B
（Firecracker microVM 云沙箱）投影为新的 sandbox_type，与内置的 aio / jiuwenbox /
yuanrong 三个 Provider 并列注册（注册方式见 registry_patch.py，接口形状与
repos/openjiuwen/agent-core/openjiuwen/extensions/sys_operation/sandbox/providers/aio.py
的 ``@SandboxRegistry.provider("aio", "fs"/"shell"/"code")`` 三件套完全同构：
aio.py:199/814/960；注册协议见 core/sys_operation/sandbox/sandbox_registry.py:45-48,62-72）。

语义映射（gap report 结论落地，见 docs/e2b-compat-gap-report.md 第 4 节逐项差距表）：
- ``files.write/read/list`` ↔ E2B filesystem API（差距表 #1：openJiuwen 协议更强，
  这里只投影 E2B 平面子集）；
- ``commands.run`` ↔ E2B process API（差距表 #2/#13：退出码/stdout/stderr 形态一致）；
- connect/reconnect 按 E2B ``sandbox_id``（差距表 #6 在 E2B 侧成立：E2B SDK 原生
  支持 connect by id，本 Provider 把它接到 openJiuwen endpoint.sandbox_id 上）；
- **明确不实现**（诚实边界，抛 NotImplementedError，不做半吊子模拟）：
  - Jupyter 内核语义的代码执行（差距表 #3：富 MIME 输出 / cell 上下文 / 变量驻留
    —— "协议兼容 ≠ 能力等价"的典型点）；
  - connect-by-id 之外的特殊语义：递归列目录、本地文件 upload/download 分块流、
    glob 搜索、append 写、chmod（差距表 #1/#6/#7/#9 中 E2B 平面 API 没有的部分）。

凭据（§12.8 执行面零长期密钥）：E2B API key 只从环境变量 / 注入引用读取
（config.E2BCompatConfig），代码里零默认 key，错误消息只含变量名。

快照边界声明（§12.8 接缝 1）：见 SnapshotScope 与 _E2BCompatMixin.snapshot_scope 的
契约注释 —— 受保护工作区 bind mount 进 AgenticFS 的范围才进快照承诺，容器临时态不进。
"""
from __future__ import annotations

import asyncio
import posixpath
import queue
import threading
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from .config import E2BCompatConfig, ENV_ALLOW_CREATE
from .errors import E2BCompatCredentialError, E2BCompatDependencyError, E2BCompatError
from ._shims import (
    BaseCodeProvider,
    BaseFSProvider,
    BaseShellProvider,
    ExecuteCodeChunkData,
    ExecuteCodeResult,
    ExecuteCmdChunkData,
    ExecuteCmdData,
    ExecuteCmdResult,
    ExecuteCmdStreamResult,
    FileSystemData,
    FileSystemItem,
    ListDirsResult,
    ListFilesResult,
    ReadFileData,
    ReadFileResult,
    SandboxRegistry,
    StatusCode,
    WriteFileData,
    WriteFileResult,
    build_operation_error_result,
)

E2B_SANDBOX_TYPE = "e2b_compat"

__all__ = [
    "E2B_SANDBOX_TYPE",
    "E2BCompatProvider",
    "E2BCompatFSProvider",
    "E2BCompatShellProvider",
    "E2BCompatCodeProvider",
    "SnapshotScope",
]

# ---------------------------------------------------------------------------
# 快照边界声明（PROP-0001 v1.7 §12.8 接缝 1：ws-ckpt vs 容器卷快照边界）
# ---------------------------------------------------------------------------
#
# 与 AgenticFS 的契约：
#   ws-ckpt 快照只承诺 ``protected_paths`` —— 即 bind mount 进 AgenticFS 的受保护
#   工作区（研发手册"环境契约"）；E2B microVM 的容器临时态（ephemeral_paths、镜像层、
#   未挂载路径、沙箱内进程内存态）一律不进承诺 —— 快照/恢复后不得依赖。
#   AgenticFS 快照器以本声明为唯一范围依据；沙箱模板/镜像本身不含状态承诺。
#   因此本声明的语义是"声明 + 禁止扩大"：任何想让临时态进快照的需求都必须走
#   bind mount（改 protected_paths 声明），而不是放宽本契约。
_SNAPSHOT_NOTE = (
    "ws-ckpt 快照仅覆盖 protected_paths（bind mount 进 AgenticFS 的受保护工作区）；"
    "E2B microVM 容器临时态（ephemeral_paths、镜像层、未挂载路径、进程内存态）"
    "不进承诺，快照/恢复后不得依赖。AgenticFS 快照器以本声明为唯一范围依据。"
)


@dataclass(frozen=True)
class SnapshotScope:
    """快照边界声明（12.8 接缝 1 的机器可读形式）。

    - ``protected_paths``：受保护工作区 bind mount 范围，进快照承诺；
    - ``ephemeral_paths``：容器临时态，不进承诺；
    - ``container_temp_state_promised``：恒为 False —— 声明本身禁止容器临时态进承诺；
    - ``mount_kind``：受保护区的挂载方式（默认 bind）。
    """

    protected_paths: Tuple[str, ...]
    ephemeral_paths: Tuple[str, ...]
    mount_kind: str = "bind"
    container_temp_state_promised: bool = False
    note: str = _SNAPSHOT_NOTE

    def __post_init__(self) -> None:
        overlap = set(self.protected_paths) & set(self.ephemeral_paths)
        if overlap:
            raise ValueError(
                f"snapshot scope invalid: protected_paths and ephemeral_paths overlap: {sorted(overlap)}"
            )


# ---------------------------------------------------------------------------
# E2B 客户端获取（零硬 import；connect/reconnect 按 E2B sandbox_id）
# ---------------------------------------------------------------------------

# E2B SDK 对非零退出码的行为在 v1.x 有"返回 CommandResult"与"抛 CommandExitException"
# 两种版本口径，这里两者都兜住（README 差距对照表标注 [待云沙箱实测]）。
def _exception_exit_code(exc: BaseException) -> Optional[int]:
    exit_code = getattr(exc, "exit_code", None)
    if exit_code is None and "Timeout" in type(exc).__name__:
        return 124  # timeout(1) 的超时退出码约定，对齐 aio.py:879-895 的超时语义
    try:
        return int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError):
        return None


def _exc_attr(exc: BaseException, name: str, default: str = "") -> str:
    value = getattr(exc, name, default)
    return str(value) if value is not None else default


def _normalize_stream_payload(payload: Any) -> str:
    """把 E2B 流式回调的载荷归一为文本。

    e2b v1.x 的 on_stdout/on_stderr 载荷可能是 str，也可能是带 .line 属性的消息对象；
    两种都兜住，确切形态 [待云沙箱实测]（README 差距对照表 #13）。
    """
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    line = getattr(payload, "line", None)
    if isinstance(line, str):
        return line
    return str(payload)


class _E2BCompatMixin:
    """三个 operation Provider 共享的 E2B 客户端获取与快照声明逻辑。

    线程模型对齐 aio.py：e2b SDK 是同步客户端，Provider 的 async 方法一律
    ``await asyncio.to_thread(...)`` 包裹（aio.py:214 等）。
    """

    def _resolve_e2b_config(self) -> E2BCompatConfig:
        """解析 E2B 配置：优先读注入在网关 config 上的 ``e2b_compat_config`` 引用，
        否则从环境变量惰性构建（见 config.E2BCompatConfig.from_env）。

        惰性解析使凭据只在"第一次真正需要 E2B 客户端"时被读取——执行面零长期密钥。
        """
        cached = getattr(self, "_e2b_config", None)
        if cached is not None:
            return cached
        injected = getattr(self.config, "e2b_compat_config", None)
        resolved = injected if isinstance(injected, E2BCompatConfig) else E2BCompatConfig.from_env()
        self._e2b_config = resolved
        return resolved

    def get_client(self) -> Any:
        """获取（并缓存）e2b 沙箱客户端。零硬 import：e2b 只在这里延迟导入。

        连接语义（gap report #6）：
        - ``endpoint.sandbox_id`` 存在 → ``e2b.Sandbox.connect(sandbox_id, ...)``，
          即 E2B 的 connect-by-id 重连；进程重启后凭 id 取回沙箱正是 E2B 语义
          在 openJiuwen 侧的落地点（openJiuwen 自身缺跨进程通用 connect，
          见 gap report 差距表 #6）。
        - 无 sandbox_id 且未 opt-in → 明确报错：默认对齐 PreDeploymentLauncher
          "连接已启动沙箱"的语义，不在 Provider 层偷偷造沙箱。
        - 无 sandbox_id 且 opt-in（allow_create=True 或 config 注入）→ 按模板创建
          （接缝 3"高频创建销毁"的实测入口；确切 SDK 调用形状 [待云沙箱实测]）。

        依赖/凭据错误以异常传播（见 errors.py 模块注释）；成功后实例被缓存，
        与 aio.py `_get_client` 的惰性缓存模式一致（aio.py:207-214）。
        """
        if getattr(self, "_client", None) is None:
            cfg = self._resolve_e2b_config()
            try:
                import e2b  # 延迟导入：e2b 是可选 extra，缺失时 Provider 仍可注册
            except ImportError as exc:
                raise E2BCompatDependencyError(
                    "e2b package is not installed; install with: "
                    "pip install 'jiuwen-e2b-compat[e2b]'"
                ) from exc
            sandbox_cls = getattr(e2b, "Sandbox", None)
            if sandbox_cls is None:
                raise E2BCompatDependencyError(
                    "installed e2b package does not expose e2b.Sandbox (expected e2b v1.x)"
                )
            api_key = cfg.resolve_api_key()
            if not api_key:
                raise E2BCompatCredentialError(
                    f"E2B API key not configured: set {cfg.api_key_env} in the execution "
                    "environment or inject E2BCompatConfig(api_key=<reference>) on the "
                    "gateway config; e2b_compat never ships or logs key values."
                )
            client_kwargs: Dict[str, Any] = {"api_key": api_key}
            if cfg.domain:
                client_kwargs["domain"] = cfg.domain
            sandbox_id = getattr(self.endpoint, "sandbox_id", None)
            if sandbox_id:
                self._client = sandbox_cls.connect(sandbox_id, **client_kwargs)
            elif cfg.allow_create:
                create_kwargs = dict(client_kwargs)
                if cfg.template_id:
                    create_kwargs["template"] = cfg.template_id
                create_kwargs["timeout"] = cfg.sandbox_timeout
                create = getattr(sandbox_cls, "create", None)
                self._client = (
                    create(**create_kwargs) if callable(create) else sandbox_cls(**create_kwargs)
                )
            else:
                raise E2BCompatError(
                    "endpoint.sandbox_id is required: e2b_compat connects to an "
                    "already-running E2B sandbox by id (mirrors openjiuwen "
                    "PreDeploymentLauncher semantics); to opt in to on-demand creation "
                    f"set E2BCompatConfig(allow_create=True) (or {ENV_ALLOW_CREATE}=1)."
                )
        return self._client

    def snapshot_scope(self) -> SnapshotScope:
        """快照边界声明（12.8 接缝 1）—— 见模块头 _SNAPSHOT_NOTE 的 AgenticFS 契约。"""
        cfg = self._resolve_e2b_config()
        return SnapshotScope(
            protected_paths=tuple(cfg.protected_paths),
            ephemeral_paths=tuple(cfg.ephemeral_paths),
        )


# ---------------------------------------------------------------------------
# 错误/成功结果构造（对齐 aio.py:52-71 的 StatusCode 错误结果惯例）
# ---------------------------------------------------------------------------

def _build_fs_error(execution: str, error_msg: str, result_cls: type, data: Any = None) -> Any:
    return build_operation_error_result(
        error_type=StatusCode.SYS_OPERATION_FS_EXECUTION_ERROR,
        msg_format_kwargs={"execution": execution, "error_msg": error_msg},
        result_cls=result_cls,
        data=data,
    )


def _build_shell_error(execution: str, error_msg: str, result_cls: type, data: Any = None) -> Any:
    return build_operation_error_result(
        error_type=StatusCode.SYS_OPERATION_SHELL_EXECUTION_ERROR,
        msg_format_kwargs={"execution": execution, "error_msg": error_msg},
        result_cls=result_cls,
        data=data,
    )


def _success(result_cls: type, data: Any) -> Any:
    return result_cls(code=StatusCode.SUCCESS.code, message=StatusCode.SUCCESS.errmsg, data=data)


def _select_text_lines(
    content: str,
    *,
    head: Optional[int],
    tail: Optional[int],
    line_range: Optional[Tuple[int, int]],
) -> Tuple[List[str], Optional[str]]:
    """本地文本行切片（head/tail/line_range 三者互斥，1-based 闭区间）。

    E2B 平面 files API 没有行切片，故读全量后在本地投影（与 aio.py `_read_file_via_shell`
    的"读全量再切"策略同形，aio.py:287-331）；语义与 openJiuwen 协议层一致。
    返回 (行列表, 错误消息或 None)。
    """
    lines = content.splitlines(keepends=True)
    if tail is not None:
        if tail < 0:
            return [], "tail must be non-negative"
        return (lines[-tail:] if tail > 0 else lines), None
    if head is not None:
        if head < 0:
            return [], "head must be non-negative"
        return lines[:head], None
    if line_range is not None:
        start, end = line_range
        if start <= 0 or end <= 0 or start > end:
            return [], "line_range must be 1-based with start <= end"
        if not lines:
            return [], None
        start_idx = start - 1
        if start_idx >= len(lines):
            return [], None
        return lines[start_idx:min(len(lines), end)], None
    return lines, None


def _specified_read_params(head: Any, tail: Any, line_range: Any) -> List[str]:
    specified = []
    if head is not None:
        specified.append("head")
    if tail is not None:
        specified.append("tail")
    if line_range is not None:
        specified.append("line_range")
    return specified


# ---------------------------------------------------------------------------
# FS Provider：files.write / files.read / files.list 投影
# ---------------------------------------------------------------------------

def _fs_not_implemented(method: str, reason: str) -> NotImplementedError:
    return NotImplementedError(
        f"e2b_compat FS provider does not implement {method}: {reason} "
        "(honest boundary; see docs/e2b-compat-provider.md gap table)."
    )


def _read_via_shell_hint() -> str:
    return (
        "E2B files API is flat (read/write/list, no line slicing); e2b_compat reads the "
        "full file and slices locally, like aio.py does for head/tail/line_range."
    )


@SandboxRegistry.provider(E2B_SANDBOX_TYPE, "fs")
class E2BCompatFSProvider(_E2BCompatMixin, BaseFSProvider):
    """sandbox_type="e2b_compat" 的文件系统 Provider（形状对齐 aio.py:199-510）。

    实现：read_file / write_file / list_files / list_directories。
    明确不实现（NotImplementedError）：分块流、本地 upload/download、glob 搜索、
    append 写、chmod —— E2B 平面 API 没有对应物，不做半吊子模拟。
    """

    async def read_file(self, path: str, mode: str = "text", **kwargs: Any) -> ReadFileResult:
        head = kwargs.get("head")
        tail = kwargs.get("tail")
        line_range = kwargs.get("line_range")
        encoding = kwargs.get("encoding", "utf-8") or "utf-8"
        chunk_size = kwargs.get("chunk_size", 0) or 0

        if mode not in ("text", "bytes"):
            return _build_fs_error("read_file", f"unsupported mode: {mode!r}", ReadFileResult)
        if mode == "bytes" and any(p is not None for p in (head, tail, line_range)):
            return _build_fs_error(
                "read_file",
                "Parameters 'head', 'tail', and 'line_range' are only supported in text mode",
                ReadFileResult,
            )
        specified = _specified_read_params(head, tail, line_range)
        if len(specified) > 1:
            return _build_fs_error(
                "read_file",
                f"{' and '.join(specified)} cannot be specified simultaneously",
                ReadFileResult,
            )

        client = await asyncio.to_thread(self.get_client)
        try:
            raw = await asyncio.to_thread(lambda: client.files.read(path, format=mode))
        except Exception as exc:  # noqa: BLE001 - 折叠为协议错误结果（aio 同款兜底）
            return _build_fs_error("read_file", f"{type(exc).__name__}: {exc}", ReadFileResult)

        if mode == "bytes":
            content = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode(encoding)
            content = bytes(content)
            if chunk_size > 0:
                content = content[:chunk_size]
            return _success(ReadFileResult, ReadFileData(path=path, content=content, mode="bytes"))

        text = raw if isinstance(raw, str) else bytes(raw).decode(encoding, errors="replace")
        if specified:
            lines, error = _select_text_lines(text, head=head, tail=tail, line_range=line_range)
            if error:
                return _build_fs_error("read_file", error, ReadFileResult)
            text = "".join(lines)
        return _success(ReadFileResult, ReadFileData(path=path, content=text, mode="text"))

    async def read_file_stream(self, path: str, **kwargs: Any):
        raise _fs_not_implemented(
            "read_file_stream",
            "E2B flat files API has no chunked read; use read_file and slice locally. "
            + _read_via_shell_hint(),
        )

    async def write_file(
        self, path: str, content: Any, mode: str = "text", **kwargs: Any
    ) -> WriteFileResult:
        prepend_newline = kwargs.get("prepend_newline", True)
        append_newline = kwargs.get("append_newline", False)
        append = kwargs.get("append", False)
        create_if_not_exist = kwargs.get("create_if_not_exist", True)
        permissions = kwargs.get("permissions", "644")
        encoding = kwargs.get("encoding", "utf-8") or "utf-8"

        # 诚实边界：E2B 平面 files API 没有 append 与 chmod，明确拒绝而非模拟。
        if append:
            raise _fs_not_implemented(
                "write_file(append=True)",
                "E2B files API has no append semantics; use E2BCompatShellProvider."
                "execute_cmd with shell redirection ('>>') instead.",
            )
        if permissions not in (None, "", "644"):
            raise _fs_not_implemented(
                "write_file(permissions=...)",
                "E2B files API has no chmod equivalent; apply permissions via "
                "E2BCompatShellProvider.execute_cmd('chmod ...') instead.",
            )
        if mode not in ("text", "bytes"):
            return _build_fs_error("write_file", f"unsupported mode: {mode!r}", WriteFileResult)

        payload: Any
        if isinstance(content, (bytes, bytearray)):
            raw = bytes(content)
            if mode == "text":
                try:
                    payload = raw.decode(encoding)
                except UnicodeDecodeError as exc:
                    return _build_fs_error(
                        "write_file", f"cannot decode content with {encoding}: {exc}", WriteFileResult
                    )
            else:
                payload = raw
        else:
            payload = str(content)

        if mode == "text":
            if prepend_newline:
                payload = "\n" + payload
            if append_newline:
                payload = payload + "\n"

        client = await asyncio.to_thread(self.get_client)
        if not create_if_not_exist:
            exists = await self._path_exists(client, path)
            if exists is False:
                return _build_fs_error(
                    "write_file",
                    f"refusing to create new file (create_if_not_exist=False): {path}",
                    WriteFileResult,
                )
        try:
            await asyncio.to_thread(lambda: client.files.write(path, payload))
        except Exception as exc:  # noqa: BLE001
            return _build_fs_error("write_file", f"{type(exc).__name__}: {exc}", WriteFileResult)

        size = len(payload.encode(encoding)) if isinstance(payload, str) else len(payload)
        return _success(WriteFileResult, WriteFileData(path=path, size=size, mode=mode))

    async def write_file_stream(self, path: str, **kwargs: Any):
        raise _fs_not_implemented(
            "write_file_stream",
            "E2B flat files API has no chunked write; buffer locally then write once.",
        )

    async def upload_file(self, local_path: str, target_path: str, **kwargs: Any):
        raise _fs_not_implemented(
            "upload_file",
            "E2B files API takes content (str/bytes), not local paths; read the local "
            "file yourself and call write_file — e2b_compat does not hide filesystem I/O.",
        )

    async def upload_file_stream(self, local_path: str, target_path: str, **kwargs: Any):
        raise _fs_not_implemented("upload_file_stream", "see upload_file")

    async def download_file(self, source_path: str, local_path: str, **kwargs: Any):
        raise _fs_not_implemented(
            "download_file",
            "E2B files API returns content, not local files; call read_file and write "
            "the bytes yourself — e2b_compat does not hide filesystem I/O.",
        )

    async def download_file_stream(self, source_path: str, local_path: str, **kwargs: Any):
        raise _fs_not_implemented("download_file_stream", "see download_file")

    async def list_files(
        self,
        path: str,
        *,
        recursive: bool = False,
        max_depth: Optional[int] = None,
        sort_by: str = "name",
        sort_descending: bool = False,
        file_types: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> ListFilesResult:
        if recursive:
            raise _fs_not_implemented(
                "list_files(recursive=True)",
                "E2B files.list is flat/non-recursive; walk directories yourself via "
                "repeated list_files calls.",
            )
        client = await asyncio.to_thread(self.get_client)
        try:
            entries = await asyncio.to_thread(lambda: client.files.list(path))
        except Exception as exc:  # noqa: BLE001
            return _build_fs_error("list_files", f"{type(exc).__name__}: {exc}", ListFilesResult)

        items: List[FileSystemItem] = []
        for entry in entries or []:
            entry_path = str(getattr(entry, "path", "") or "")
            name = str(getattr(entry, "name", "") or posixpath.basename(entry_path))
            is_dir = bool(getattr(entry, "is_dir", getattr(entry, "is_directory", False)))
            raw_size = getattr(entry, "size", None)
            size = int(raw_size) if isinstance(raw_size, (int, float)) and raw_size is not None else 0
            modified_time = str(getattr(entry, "modified_time", "") or "")
            items.append(
                FileSystemItem(
                    name=name,
                    path=entry_path or posixpath.join(path, name),
                    size=size,
                    modified_time=modified_time,
                    is_directory=is_dir,
                    type=posixpath.splitext(name)[1] or None,
                )
            )
        if file_types:
            items = [item for item in items if item.type in set(file_types)]
        items = _sort_fs_items(items, sort_by, sort_descending)
        # E2B files.list 恒为非递归，recursive/max_depth 在结果里如实反映实际状态。
        data = FileSystemData(
            total_count=len(items),
            list_items=items,
            root_path=path,
            recursive=False,
            max_depth=None,
        )
        return _success(ListFilesResult, data)

    async def list_directories(
        self,
        path: str,
        *,
        recursive: bool = False,
        max_depth: Optional[int] = None,
        sort_by: str = "name",
        sort_descending: bool = False,
        **kwargs: Any,
    ) -> ListDirsResult:
        if recursive:
            raise _fs_not_implemented(
                "list_directories(recursive=True)",
                "E2B files.list is flat/non-recursive.",
            )
        client = await asyncio.to_thread(self.get_client)
        try:
            entries = await asyncio.to_thread(lambda: client.files.list(path))
        except Exception as exc:  # noqa: BLE001
            return _build_fs_error("list_directories", f"{type(exc).__name__}: {exc}", ListDirsResult)

        dirs: List[FileSystemItem] = []
        for entry in entries or []:
            is_dir = bool(getattr(entry, "is_dir", getattr(entry, "is_directory", False)))
            if not is_dir:
                continue
            entry_path = str(getattr(entry, "path", "") or "")
            name = str(getattr(entry, "name", "") or posixpath.basename(entry_path))
            raw_size = getattr(entry, "size", None)
            size = int(raw_size) if isinstance(raw_size, (int, float)) and raw_size is not None else 0
            dirs.append(
                FileSystemItem(
                    name=name,
                    path=entry_path or posixpath.join(path, name),
                    size=size,
                    modified_time=str(getattr(entry, "modified_time", "") or ""),
                    is_directory=True,
                    type=None,
                )
            )
        dirs = _sort_fs_items(dirs, sort_by, sort_descending)
        data = FileSystemData(
            total_count=len(dirs),
            list_items=dirs,
            root_path=path,
            recursive=False,
            max_depth=None,
        )
        return _success(ListDirsResult, data)

    async def search_files(
        self, path: str, pattern: str, exclude_patterns: Optional[List[str]] = None
    ):
        raise _fs_not_implemented(
            "search_files",
            "E2B flat files API has no glob search; run find(1) via "
            "E2BCompatShellProvider.execute_cmd instead.",
        )

    async def _path_exists(self, client: Any, path: str) -> Optional[bool]:
        """用 files.list 探测文件是否存在（create_if_not_exist=False 的守卫）。

        返回 None 表示探测失败（父目录不可列等），此时放行、让真正的写入决定结果。
        """
        normalized = path.rstrip("/") or "/"
        parent = posixpath.dirname(normalized) or "/"
        name = posixpath.basename(normalized)
        try:
            entries = await asyncio.to_thread(lambda: client.files.list(parent))
        except Exception:  # noqa: BLE001 - 探测失败不拦截写入
            return None
        for entry in entries or []:
            if str(getattr(entry, "name", "")) == name:
                return True
        return False


def _sort_fs_items(items: List[FileSystemItem], sort_by: str, sort_descending: bool) -> List[FileSystemItem]:
    if sort_by == "size":
        key = lambda item: item.size  # noqa: E731
    elif sort_by == "modified_time":
        key = lambda item: item.modified_time  # noqa: E731
    else:
        key = lambda item: item.name  # noqa: E731
    return sorted(items, key=key, reverse=sort_descending)


# ---------------------------------------------------------------------------
# Shell Provider：commands.run 投影
# ---------------------------------------------------------------------------

@SandboxRegistry.provider(E2B_SANDBOX_TYPE, "shell")
class E2BCompatShellProvider(_E2BCompatMixin, BaseShellProvider):
    """sandbox_type="e2b_compat" 的 Shell Provider（形状对齐 aio.py:814-957）。

    语义映射（gap report #2/#13）：openJiuwen ``execute_cmd(command, cwd, timeout,
    environment)`` → E2B ``commands.run(command, cwd=, envs=, timeout=)``；
    CommandResult{stdout, stderr, exit_code} → ExecuteCmdData。

    失败传播（PROP-0008 验收项）：
    - e2b 返回 CommandResult → 结果 code=0，exit_code 原样进 data（调用方按
      exit_code 判定命令结果，与 aio.py:877-896 同口径）；
    - e2b 抛 CommandExitException（带 exit_code/stdout/stderr）→ 非零 code 错误结果，
      细节进 data；
    - e2b 抛 TimeoutException 类异常 → 非零 code 错误结果，exit_code=124
      （timeout(1) 约定，对齐 aio.py:879-895）。
    确切抛/返行为 [待云沙箱实测]（README 差距对照表 #2）。
    """

    async def execute_cmd(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = 300,
        environment: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> ExecuteCmdResult:
        if not command or not command.strip():
            return _build_shell_error("execute_cmd", "command can not be empty", ExecuteCmdResult)

        client = await asyncio.to_thread(self.get_client)
        run_kwargs = self._build_run_kwargs(cwd=cwd, timeout=timeout, environment=environment)
        try:
            result = await asyncio.to_thread(lambda: client.commands.run(command, **run_kwargs))
        except Exception as exc:  # noqa: BLE001 - 失败传播：折叠为错误结果并保留细节
            return self._exception_to_result(command, cwd, timeout, exc)

        exit_code = getattr(result, "exit_code", None)
        try:
            exit_code = int(exit_code) if exit_code is not None else None
        except (TypeError, ValueError):
            exit_code = None
        data = ExecuteCmdData(
            command=command,
            cwd=cwd or ".",
            exit_code=exit_code,
            stdout=str(getattr(result, "stdout", "") or ""),
            stderr=str(getattr(result, "stderr", "") or ""),
        )
        return _success(ExecuteCmdResult, data)

    async def execute_cmd_stream(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = 300,
        environment: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[ExecuteCmdStreamResult]:
        """流式执行：E2B on_stdout/on_stderr 回调 → ExecuteCmdChunkData 异步迭代。

        实现方式：同步回调经线程 + Queue 桥接为 async 迭代（e2b SDK 为同步客户端，
        回调发生在 worker 线程）。回调载荷形状（str 或 .line 对象）[待云沙箱实测]，
        归一逻辑见 _normalize_stream_payload。
        """
        if not command or not command.strip():
            yield _build_shell_error(
                "execute_cmd_stream",
                "command can not be empty",
                ExecuteCmdStreamResult,
                data=ExecuteCmdChunkData(text="", type=None, chunk_index=0),
            )
            return

        client = await asyncio.to_thread(self.get_client)
        run_kwargs = self._build_run_kwargs(cwd=cwd, timeout=timeout, environment=environment)

        pending: "queue.Queue" = queue.Queue()
        state: Dict[str, Any] = {"exit_code": None, "error": None}

        def _run() -> None:
            try:
                result = client.commands.run(command, **run_kwargs)
                state["exit_code"] = getattr(result, "exit_code", None)
            except Exception as exc:  # noqa: BLE001
                state["error"] = exc
                state["exit_code"] = _exception_exit_code(exc)
            finally:
                pending.put(None)

        run_kwargs["on_stdout"] = lambda payload: pending.put(("stdout", _normalize_stream_payload(payload)))
        run_kwargs["on_stderr"] = lambda payload: pending.put(("stderr", _normalize_stream_payload(payload)))
        threading.Thread(target=_run, daemon=True, name="e2b-compat-cmd-stream").start()

        cfg = self._resolve_e2b_config()
        per_chunk_timeout = max(1, int(cfg.request_timeout))
        index = 0
        while True:
            try:
                item = await asyncio.to_thread(pending.get, True, per_chunk_timeout)
            except queue.Empty:
                yield _build_shell_error(
                    "execute_cmd_stream",
                    f"stream timed out after {per_chunk_timeout}s without sandbox output",
                    ExecuteCmdStreamResult,
                    data=ExecuteCmdChunkData(text="", type=None, chunk_index=index),
                )
                return
            if item is None:
                break
            kind, text = item
            yield _success(
                ExecuteCmdStreamResult,
                ExecuteCmdChunkData(text=text, type=kind, chunk_index=index, exit_code=None),
            )
            index += 1

        if state["error"] is not None:
            exc = state["error"]
            yield self._exception_to_result(command, cwd, timeout, exc, result_cls=ExecuteCmdStreamResult, chunk_index=index)
            return
        exit_code = state["exit_code"]
        try:
            exit_code = int(exit_code) if exit_code is not None else None
        except (TypeError, ValueError):
            exit_code = None
        yield _success(
            ExecuteCmdStreamResult,
            ExecuteCmdChunkData(text="", type=None, chunk_index=index, exit_code=exit_code),
        )

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _build_run_kwargs(
        *,
        cwd: Optional[str],
        timeout: Optional[int],
        environment: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        run_kwargs: Dict[str, Any] = {"cwd": cwd or "."}
        if environment:
            run_kwargs["envs"] = dict(environment)
        if timeout is not None and int(timeout) > 0:
            run_kwargs["timeout"] = int(timeout)
        return run_kwargs

    def _exception_to_result(
        self,
        command: str,
        cwd: Optional[str],
        timeout: Optional[int],
        exc: BaseException,
        *,
        result_cls: type = ExecuteCmdResult,
        chunk_index: int = 0,
    ) -> Any:
        name = type(exc).__name__
        exit_code = _exception_exit_code(exc)
        data_kwargs: Dict[str, Any] = dict(
            command=command,
            cwd=cwd or ".",
            stdout=_exc_attr(exc, "stdout"),
            stderr=_exc_attr(exc, "stderr"),
            exit_code=exit_code,
        )
        if "Timeout" in name:
            message = f"execution timeout after {timeout} seconds"
        elif exit_code is not None:
            message = f"command exited with code {exit_code}"
        else:
            message = f"{name}: {exc}"
            data_kwargs = dict(command=command, cwd=cwd or ".", exit_code=None)
        if result_cls is ExecuteCmdResult:
            data = ExecuteCmdData(**data_kwargs)
        else:
            data = ExecuteCmdChunkData(text="", type=None, chunk_index=chunk_index, exit_code=exit_code)
        return _build_shell_error("execute_cmd", message, result_cls, data=data)


# ---------------------------------------------------------------------------
# Code Provider：明确不实现（诚实边界）
# ---------------------------------------------------------------------------

_CODE_NOT_IMPLEMENTED_MSG = (
    "e2b_compat deliberately does not implement code execution: E2B code semantics are "
    "Jupyter-kernel based (rich MIME output, cell context, variable residency across "
    "cells), while openjiwen code providers execute via a shell shim — projecting one "
    "onto the other would fake kernel semantics (gap report #3, '协议兼容 ≠ 能力等价'). "
    "Alternatives: run scripts via E2BCompatShellProvider.execute_cmd; for kernel "
    "semantics use the AIO provider against an AIO sandbox and wire its code API "
    "(aio.py:980-1023 notes the same boundary)."
)


@SandboxRegistry.provider(E2B_SANDBOX_TYPE, "code")
class E2BCompatCodeProvider(_E2BCompatMixin, BaseCodeProvider):
    """sandbox_type="e2b_compat" 的代码执行 Provider —— 刻意不实现（gap report #3）。

    E2B 的代码执行是 Jupyter 内核语义（富 MIME 输出 / cell 上下文 / 变量驻留），
    openJiuwen 的 code 协议是 shell shim 语义，两者不能互投影。本 Provider 注册
    该 operation 以占住接口面（SandboxRegistry.create_provider 不再报
    "does not support operation"），但调用即抛 NotImplementedError，并在
    README/文档写明 —— 不做半吊子模拟。
    """

    async def execute_code(
        self,
        code: str,
        *,
        language: str = "python",
        timeout: int = 300,
        environment: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        **kwargs: Any,
    ) -> ExecuteCodeResult:
        raise NotImplementedError(_CODE_NOT_IMPLEMENTED_MSG)

    async def execute_code_stream(
        self,
        code: str,
        *,
        language: str = "python",
        timeout: int = 300,
        environment: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        **kwargs: Any,
    ):
        raise NotImplementedError(_CODE_NOT_IMPLEMENTED_MSG)


# ---------------------------------------------------------------------------
# 使用方门面（非注册单元；注册单元是上面三个 operation Provider）
# ---------------------------------------------------------------------------

class E2BCompatProvider:
    """面向使用方的组合门面：fs/shell/code 三个 Provider + connect/快照声明。

    SandboxRegistry 的注册单元是 (sandbox_type, operation_type) → Provider 类
    （sandbox_registry.py:45-48,62-72），因此实际注册的是
    E2BCompatFSProvider / E2BCompatShellProvider / E2BCompatCodeProvider 三个类
    （与 aio.py 的 AIOFSProvider/AIOShellProvider/AIOCodeProvider 三件套同构）；
    本门面把三者组装在一个对象上，并提供：
    - ``connect()``：按 E2B sandbox_id 取回客户端（gap report #6 的 E2B 侧语义）；
    - ``reconnect(sandbox_id)``：换绑到另一 sandbox_id 的新门面；
    - ``snapshot_scope()``：快照边界声明（12.8 接缝 1）；
    - ``kill()``：销毁当前绑定沙箱（接缝 3 的销毁半边）。
    """

    def __init__(self, endpoint: Any, config: Optional[Any] = None) -> None:
        self.endpoint = endpoint
        self._gateway_config = config
        self.fs = E2BCompatFSProvider(endpoint=endpoint, config=config)
        self.shell = E2BCompatShellProvider(endpoint=endpoint, config=config)
        self.code = E2BCompatCodeProvider(endpoint=endpoint, config=config)

    @property
    def sandbox_id(self) -> Optional[str]:
        return getattr(self.endpoint, "sandbox_id", None)

    def connect(self) -> Any:
        """连接（或 opt-in 创建）并返回 e2b 沙箱客户端。语义见 _E2BCompatMixin.get_client。"""
        return self.fs.get_client()

    def reconnect(self, sandbox_id: str) -> "E2BCompatProvider":
        """换绑到另一个 E2B sandbox_id，返回新门面（原门面不变）。"""
        endpoint_cls = type(self.endpoint)
        try:
            new_endpoint = endpoint_cls(
                base_url=self.endpoint.base_url,
                sandbox_id=sandbox_id,
                isolation_key=getattr(self.endpoint, "isolation_key", None),
            )
        except TypeError:  # 非标准 endpoint 对象时退化为属性拷贝
            new_endpoint = self.endpoint
            try:
                new_endpoint.sandbox_id = sandbox_id
            except AttributeError as exc:
                raise E2BCompatError(f"cannot rebind endpoint to sandbox_id={sandbox_id!r}") from exc
            return E2BCompatProvider(new_endpoint, self._gateway_config)
        return E2BCompatProvider(new_endpoint, self._gateway_config)

    def snapshot_scope(self) -> SnapshotScope:
        """快照边界声明（12.8 接缝 1）—— 委托 fs Provider，三 Provider 声明一致。"""
        return self.fs.snapshot_scope()

    def kill(self) -> Any:
        """销毁当前绑定的 E2B 沙箱（sandbox.kill()）。需要 connect 语义先取回客户端。"""
        client = self.connect()
        return client.kill()

    def __repr__(self) -> str:  # 凭据红线：repr 不含任何配置值
        return f"E2BCompatProvider(sandbox_type={E2B_SANDBOX_TYPE!r}, sandbox_id={self.sandbox_id!r})"
