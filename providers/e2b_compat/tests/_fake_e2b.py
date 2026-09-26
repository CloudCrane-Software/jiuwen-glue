# coding: utf-8
"""测试用假 e2b 模块：注入 sys.modules 模拟 e2b v1.x SDK 面，全程不装真包、不联网。

形态依据 E2B v1.x 公开 API 面（gap report 第 3 节）：
- ``e2b.Sandbox.connect(sandbox_id, api_key=..., domain=...)``；
- ``Sandbox.create / Sandbox(...)``（v1.x 的 create 类方法与构造器两种口径都留）；
- ``sandbox.files.read(path, format=) / files.write(path, data) / files.list(path)``；
- ``sandbox.commands.run(cmd, cwd=, envs=, timeout=, on_stdout=, on_stderr=)``
  → ``CommandResult{stdout, stderr, exit_code}``；
- 超时抛 ``TimeoutException``；非零退出可能抛 ``CommandExitException``（README 标注
  [待云沙箱实测] 的两种口径都做成可控行为）。
"""
from __future__ import annotations

import sys
import types
from typing import Any, Callable, Dict, List, Optional


class FakeCommandResult:
    def __init__(self, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class FakeEntryInfo:
    def __init__(
        self,
        name: str,
        path: str,
        is_dir: bool = False,
        size: Optional[int] = None,
        modified_time: Optional[str] = None,
    ) -> None:
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.size = size
        self.modified_time = modified_time


class FakeTimeoutException(Exception):
    """形态对齐 e2b.exceptions.TimeoutException（类名含 Timeout 供探测）。"""


class FakeCommandExitException(Exception):
    """形态对齐 e2b CommandExitException（带 exit_code/stdout/stderr）。"""

    def __init__(self, exit_code: int, stdout: str = "", stderr: str = "") -> None:
        super().__init__(f"command exited with code {exit_code}")
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class FakeFiles:
    def __init__(self, controller: "FakeE2BController") -> None:
        self._controller = controller

    def read(self, path: str, format: str = "text") -> Any:  # noqa: A002 - e2b 用 format
        if self._controller.read_behavior is not None:
            return self._controller.read_behavior(path, format)
        if path not in self._controller.files:
            raise FileNotFoundError(f"no such file: {path}")
        content = self._controller.files[path]
        if format == "bytes":
            return content if isinstance(content, bytes) else content.encode("utf-8")
        return content.decode("utf-8") if isinstance(content, bytes) else content

    def write(self, path: str, data: Any) -> None:
        if self._controller.write_behavior is not None:
            self._controller.write_behavior(path, data)
            return
        self._controller.files[path] = data
        self._controller.write_calls.append((path, data))

    def list(self, path: str) -> List[FakeEntryInfo]:
        if self._controller.list_behavior is not None:
            return self._controller.list_behavior(path)
        normalized = (path or "/").rstrip("/") or "/"
        prefix = normalized + "/"
        entries: List[FakeEntryInfo] = []
        seen = set()
        for stored_path, content in self._controller.files.items():
            if not stored_path.startswith(prefix):
                continue
            rest = stored_path[len(prefix):]
            if "/" in rest:  # 子目录下的文件折叠为其目录项
                dir_name = rest.split("/", 1)[0]
                if dir_name in seen:
                    continue
                seen.add(dir_name)
                entries.append(
                    FakeEntryInfo(dir_name, prefix + dir_name, is_dir=True, size=None, modified_time=None)
                )
            else:
                entries.append(
                    FakeEntryInfo(
                        rest,
                        stored_path,
                        is_dir=False,
                        size=len(content) if content is not None else 0,
                        modified_time="2026-09-26T00:00:00Z",
                    )
                )
        for dir_path in self._controller.dirs:
            if dir_path.startswith(prefix):
                rest = dir_path[len(prefix):]
                if rest and "/" not in rest and rest not in seen:
                    seen.add(rest)
                    entries.append(FakeEntryInfo(rest, dir_path, is_dir=True))
        return entries


class FakeCommands:
    def __init__(self, controller: "FakeE2BController") -> None:
        self._controller = controller

    def run(self, command: str, **kwargs: Any) -> FakeCommandResult:
        self._controller.run_calls.append({"command": command, **kwargs})
        behavior = self._controller.run_behavior
        if behavior is not None:
            return behavior(command, **kwargs)
        # 默认行为：echo 命令输出、退出码 0
        return FakeCommandResult(stdout=command + "\n", stderr="", exit_code=0)


class FakeSandbox:
    """e2b.Sandbox 假体：create/connect 类方法记录调用；实例挂 files/commands。"""

    _controller: Optional["FakeE2BController"] = None

    def __init__(self, **kwargs: Any) -> None:
        controller = FakeSandbox._controller
        assert controller is not None, "fake e2b used outside install_fake_e2b()"
        controller.constructor_calls.append(dict(kwargs))
        self.sandbox_id = kwargs.pop("sandbox_id", controller.default_sandbox_id)
        self.init_kwargs = kwargs
        self.files = FakeFiles(controller)
        self.commands = FakeCommands(controller)
        controller.instances.append(self)

    @classmethod
    def connect(cls, sandbox_id: str, **kwargs: Any) -> "FakeSandbox":
        controller = cls._controller
        assert controller is not None
        controller.connect_calls.append({"sandbox_id": sandbox_id, **kwargs})
        if controller.connect_behavior is not None:
            return controller.connect_behavior(sandbox_id, **kwargs)
        instance = cls(sandbox_id=sandbox_id, **kwargs)
        controller.files.setdefault("/etc/hostname", "fake-e2b\n")
        return instance

    @classmethod
    def create(cls, **kwargs: Any) -> "FakeSandbox":
        controller = cls._controller
        assert controller is not None
        controller.create_calls.append(dict(kwargs))
        if controller.create_behavior is not None:
            return controller.create_behavior(**kwargs)
        return cls(**kwargs)

    def kill(self) -> None:
        controller = self.__class__._controller
        assert controller is not None
        controller.kill_calls.append(self.sandbox_id)


class FakeE2BController:
    """假 e2b 的行为控制器：单测据此注入 canned 行为并断言调用参数。"""

    def __init__(self, default_sandbox_id: str = "sbx-fake-0001") -> None:
        self.default_sandbox_id = default_sandbox_id
        self.files: Dict[str, Any] = {}
        self.dirs: List[str] = []
        self.write_calls: List[Any] = []
        self.run_calls: List[Dict[str, Any]] = []
        self.connect_calls: List[Dict[str, Any]] = []
        self.create_calls: List[Dict[str, Any]] = []
        self.constructor_calls: List[Dict[str, Any]] = []
        self.kill_calls: List[str] = []
        self.instances: List[FakeSandbox] = []
        # 可编程行为（None = 默认行为）
        self.read_behavior: Optional[Callable[..., Any]] = None
        self.write_behavior: Optional[Callable[..., Any]] = None
        self.list_behavior: Optional[Callable[..., Any]] = None
        self.run_behavior: Optional[Callable[..., Any]] = None
        self.connect_behavior: Optional[Callable[..., Any]] = None
        self.create_behavior: Optional[Callable[..., Any]] = None

    def add_dir(self, path: str) -> None:
        self.dirs.append(path)


def install_fake_e2b() -> FakeE2BController:
    """构造假 e2b 模块并注入 sys.modules；返回行为控制器。"""
    module = types.ModuleType("e2b")
    module.__version__ = "1.0.0-fake"
    controller = FakeE2BController()
    FakeSandbox._controller = controller
    module.Sandbox = FakeSandbox
    module.CommandResult = FakeCommandResult
    module.EntryInfo = FakeEntryInfo
    module.TimeoutException = FakeTimeoutException
    module.CommandExitException = FakeCommandExitException
    sys.modules["e2b"] = module
    return controller


def uninstall_fake_e2b() -> None:
    sys.modules.pop("e2b", None)
    FakeSandbox._controller = None
