# coding: utf-8
"""语义映射测试：files.write/read/list 与 files/commands 投影（全部 mock e2b，不联网）。"""
from __future__ import annotations

import asyncio

import pytest

from _fake_e2b import FakeCommandExitException, FakeCommandResult, FakeTimeoutException


def _fs_provider(ns, **kwargs):
    return ns.provider.E2BCompatFSProvider(endpoint=ns.make_endpoint(ns, **kwargs), config=None)


def _shell_provider(ns, **kwargs):
    return ns.provider.E2BCompatShellProvider(endpoint=ns.make_endpoint(ns, **kwargs), config=None)


def _code_provider(ns, **kwargs):
    return ns.provider.E2BCompatCodeProvider(endpoint=ns.make_endpoint(ns, **kwargs), config=None)


# ---------------------------------------------------------------------------
# files.write
# ---------------------------------------------------------------------------

def test_write_file_text_framing_and_size(fallback_stack, fake_e2b, env_api_key):
    provider = _fs_provider(fallback_stack, sandbox_id="sbx-w")
    result = asyncio.run(provider.write_file("/ws/hello.txt", "hello"))
    assert result.code == 0
    # openjiwen 协议默认 prepend_newline=True / append_newline=False → "\n" + content
    assert fake_e2b.files["/ws/hello.txt"] == "\nhello"
    assert result.data.path == "/ws/hello.txt"
    assert result.data.size == len("\nhello".encode("utf-8"))
    assert result.data.mode == "text"


def test_write_file_append_newline_framing(fallback_stack, fake_e2b, env_api_key):
    provider = _fs_provider(fallback_stack, sandbox_id="sbx-w")
    result = asyncio.run(
        provider.write_file("/ws/hello.txt", "hello", prepend_newline=False, append_newline=True)
    )
    assert result.code == 0
    assert fake_e2b.files["/ws/hello.txt"] == "hello\n"


def test_write_file_bytes_passthrough(fallback_stack, fake_e2b, env_api_key):
    provider = _fs_provider(fallback_stack, sandbox_id="sbx-w")
    payload = bytes([0, 1, 2, 255])
    result = asyncio.run(provider.write_file("/ws/blob.bin", payload, mode="bytes"))
    assert result.code == 0
    assert fake_e2b.files["/ws/blob.bin"] == payload
    assert result.data.size == 4
    assert result.data.mode == "bytes"


def test_write_file_append_is_honest_not_implemented(fallback_stack, fake_e2b, env_api_key):
    provider = _fs_provider(fallback_stack, sandbox_id="sbx-w")
    with pytest.raises(NotImplementedError, match="append"):
        asyncio.run(provider.write_file("/ws/hello.txt", "more", append=True))
    assert "/ws/hello.txt" not in fake_e2b.files  # 半吊子模拟被拒绝：什么都没写


def test_write_file_permissions_is_honest_not_implemented(fallback_stack, fake_e2b, env_api_key):
    provider = _fs_provider(fallback_stack, sandbox_id="sbx-w")
    with pytest.raises(NotImplementedError, match="permissions"):
        asyncio.run(provider.write_file("/ws/hello.txt", "x", permissions="755"))


def test_write_file_create_guard_refuses_new_file(fallback_stack, fake_e2b, env_api_key):
    provider = _fs_provider(fallback_stack, sandbox_id="sbx-w")
    result = asyncio.run(
        provider.write_file("/ws/missing.txt", "x", create_if_not_exist=False)
    )
    assert result.code != 0
    assert "create_if_not_exist" in result.message
    assert "/ws/missing.txt" not in fake_e2b.files  # 未越权创建


def test_write_file_create_guard_allows_existing(fallback_stack, fake_e2b, env_api_key):
    fake_e2b.files["/ws/exists.txt"] = "old"
    provider = _fs_provider(fallback_stack, sandbox_id="sbx-w")
    result = asyncio.run(
        provider.write_file("/ws/exists.txt", "new", prepend_newline=False, create_if_not_exist=False)
    )
    assert result.code == 0
    assert fake_e2b.files["/ws/exists.txt"] == "new"


# ---------------------------------------------------------------------------
# files.read
# ---------------------------------------------------------------------------

def test_read_file_text_roundtrip(fallback_stack, fake_e2b, env_api_key):
    fake_e2b.files["/ws/a.txt"] = "alpha\nbeta\n"
    provider = _fs_provider(fallback_stack, sandbox_id="sbx-r")
    result = asyncio.run(provider.read_file("/ws/a.txt"))
    assert result.code == 0
    assert result.data.content == "alpha\nbeta\n"
    assert result.data.mode == "text"


def test_read_file_bytes_format_requested(fallback_stack, fake_e2b, env_api_key):
    seen = {}

    def read_behavior(path, format="text"):  # noqa: A002
        seen["path"], seen["format"] = path, format
        return bytes([1, 2, 3])

    fake_e2b.read_behavior = read_behavior
    provider = _fs_provider(fallback_stack, sandbox_id="sbx-r")
    result = asyncio.run(provider.read_file("/ws/blob", mode="bytes"))
    assert result.code == 0
    assert seen == {"path": "/ws/blob", "format": "bytes"}  # E2B files.read(format=...) 语义
    assert result.data.content == bytes([1, 2, 3])
    assert result.data.mode == "bytes"


def test_read_file_text_slicing_tail_head_line_range(fallback_stack, fake_e2b, env_api_key):
    fake_e2b.files["/ws/lines.txt"] = "l1\nl2\nl3\n"
    provider = _fs_provider(fallback_stack, sandbox_id="sbx-r")
    tail = asyncio.run(provider.read_file("/ws/lines.txt", tail=2))
    assert tail.data.content == "l2\nl3\n"
    head = asyncio.run(provider.read_file("/ws/lines.txt", head=1))
    assert head.data.content == "l1\n"
    window = asyncio.run(provider.read_file("/ws/lines.txt", line_range=(2, 3)))
    assert window.data.content == "l2\nl3\n"


def test_read_file_bytes_mode_rejects_slicing(fallback_stack, fake_e2b, env_api_key):
    provider = _fs_provider(fallback_stack, sandbox_id="sbx-r")
    result = asyncio.run(provider.read_file("/ws/blob", mode="bytes", tail=2))
    assert result.code != 0
    assert "only supported in text mode" in result.message


def test_read_file_param_conflict_is_error_result(fallback_stack, fake_e2b, env_api_key):
    provider = _fs_provider(fallback_stack, sandbox_id="sbx-r")
    result = asyncio.run(provider.read_file("/ws/a.txt", head=1, tail=1))
    assert result.code != 0
    assert "cannot be specified simultaneously" in result.message


# ---------------------------------------------------------------------------
# files.list / list_directories
# ---------------------------------------------------------------------------

def test_list_files_mapping_sort_and_filter(fallback_stack, fake_e2b, env_api_key):
    fake_e2b.files["/ws/a.txt"] = "abc"
    fake_e2b.files["/ws/b.py"] = "x" * 10
    fake_e2b.add_dir("/ws/sub")
    provider = _fs_provider(fallback_stack, sandbox_id="sbx-l")

    listed = asyncio.run(provider.list_files("/ws"))
    assert listed.code == 0
    data = listed.data
    assert data.root_path == "/ws"
    assert data.recursive is False  # E2B 平面 list 恒非递归，如实反映
    assert data.total_count == 3
    by_name = {item.name: item for item in data.list_items}
    assert by_name["a.txt"].is_directory is False
    assert by_name["a.txt"].size == 3
    assert by_name["b.py"].type == ".py"
    assert by_name["sub"].is_directory is True

    desc = asyncio.run(provider.list_files("/ws", sort_by="name", sort_descending=True))
    assert [item.name for item in desc.data.list_items] == ["sub", "b.py", "a.txt"]

    only_py = asyncio.run(provider.list_files("/ws", file_types=[".py"]))
    assert [item.name for item in only_py.data.list_items] == ["b.py"]


def test_list_files_recursive_is_honest_not_implemented(fallback_stack, fake_e2b, env_api_key):
    provider = _fs_provider(fallback_stack, sandbox_id="sbx-l")
    with pytest.raises(NotImplementedError, match="recursive"):
        asyncio.run(provider.list_files("/ws", recursive=True))


def test_list_directories_returns_only_dirs(fallback_stack, fake_e2b, env_api_key):
    fake_e2b.files["/ws/a.txt"] = "abc"
    fake_e2b.add_dir("/ws/sub1")
    fake_e2b.add_dir("/ws/sub2")
    provider = _fs_provider(fallback_stack, sandbox_id="sbx-l")
    result = asyncio.run(provider.list_directories("/ws"))
    assert result.code == 0
    assert sorted(item.name for item in result.data.list_items) == ["sub1", "sub2"]
    assert all(item.is_directory for item in result.data.list_items)


def test_fs_error_result_on_sandbox_failure(fallback_stack, fake_e2b, env_api_key):
    fake_e2b.list_behavior = lambda path: (_ for _ in ()).throw(RuntimeError("sandbox exploded"))
    provider = _fs_provider(fallback_stack, sandbox_id="sbx-l")
    result = asyncio.run(provider.list_files("/ws"))
    assert result.code != 0
    assert "sandbox exploded" in result.message


# ---------------------------------------------------------------------------
# commands.run（execute_cmd / execute_cmd_stream）
# ---------------------------------------------------------------------------

def test_execute_cmd_success_mapping(fallback_stack, fake_e2b, env_api_key):
    fake_e2b.run_behavior = lambda command, **kw: FakeCommandResult(stdout="out\n", stderr="err\n", exit_code=0)
    provider = _shell_provider(fallback_stack, sandbox_id="sbx-c")
    result = asyncio.run(
        provider.execute_cmd("ls -la", cwd="/ws", timeout=42, environment={"FOO": "bar"})
    )
    assert result.code == 0
    assert result.data.exit_code == 0
    assert result.data.stdout == "out\n"
    assert result.data.stderr == "err\n"
    assert result.data.cwd == "/ws"
    call = fake_e2b.run_calls[-1]
    assert call["command"] == "ls -la"
    assert call["cwd"] == "/ws"
    assert call["timeout"] == 42
    assert call["envs"] == {"FOO": "bar"}  # E2B commands.run 的 envs 参数


def test_execute_cmd_nonzero_exit_propagates_in_data(fallback_stack, fake_e2b, env_api_key):
    fake_e2b.run_behavior = lambda command, **kw: FakeCommandResult(stdout="", stderr="boom\n", exit_code=3)
    provider = _shell_provider(fallback_stack, sandbox_id="sbx-c")
    result = asyncio.run(provider.execute_cmd("false"))
    assert result.code == 0  # 命令确实执行了（aio 同口径：退出码进 data）
    assert result.data.exit_code == 3  # 失败传播：exit_code 原样可见
    assert result.data.stderr == "boom\n"


def test_execute_cmd_exit_exception_becomes_error_result(fallback_stack, fake_e2b, env_api_key):
    def behavior(command, **kw):
        raise FakeCommandExitException(exit_code=2, stdout="partial", stderr="e2b raised")

    fake_e2b.run_behavior = behavior
    provider = _shell_provider(fallback_stack, sandbox_id="sbx-c")
    result = asyncio.run(provider.execute_cmd("bad"))
    assert result.code != 0
    assert "exited with code 2" in result.message
    assert result.data.exit_code == 2
    assert result.data.stdout == "partial"
    assert result.data.stderr == "e2b raised"


def test_execute_cmd_timeout_exception_maps_to_124(fallback_stack, fake_e2b, env_api_key):
    def behavior(command, **kw):
        raise FakeTimeoutException("sandbox command timed out")

    fake_e2b.run_behavior = behavior
    provider = _shell_provider(fallback_stack, sandbox_id="sbx-c")
    result = asyncio.run(provider.execute_cmd("sleep forever", timeout=7))
    assert result.code != 0
    assert "execution timeout after 7 seconds" in result.message
    assert result.data.exit_code == 124  # timeout(1) 约定，对齐 aio.py 超时语义


def test_execute_cmd_empty_command_is_error(fallback_stack, fake_e2b, env_api_key):
    provider = _shell_provider(fallback_stack, sandbox_id="sbx-c")
    result = asyncio.run(provider.execute_cmd("   "))
    assert result.code != 0
    assert "empty" in result.message
    assert fake_e2b.run_calls == []


def test_execute_cmd_stream_yields_chunks_then_exit_code(fallback_stack, fake_e2b, env_api_key):
    def behavior(command, **kw):
        on_stdout = kw["on_stdout"]
        on_stderr = kw["on_stderr"]
        on_stdout("hello ")
        on_stderr("warn ")
        on_stdout("world\n")
        return FakeCommandResult(stdout="", stderr="", exit_code=0)

    fake_e2b.run_behavior = behavior
    provider = _shell_provider(fallback_stack, sandbox_id="sbx-c")

    async def drain():
        chunks = []
        async for chunk in provider.execute_cmd_stream("printf hi", cwd="/ws"):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(drain())
    assert len(chunks) == 4  # 3 个输出块 + 1 个终块
    assert chunks[0].data.type == "stdout" and chunks[0].data.text == "hello "
    assert chunks[1].data.type == "stderr" and chunks[1].data.text == "warn "
    assert chunks[2].data.text == "world\n"
    assert [c.data.chunk_index for c in chunks] == [0, 1, 2, 3]
    assert chunks[3].data.exit_code == 0 and chunks[3].data.text == ""
    assert chunks[0].code == 0
    assert fake_e2b.run_calls[-1]["cwd"] == "/ws"


def test_code_execution_is_honest_not_implemented(fallback_stack, env_api_key):
    """差距 #3：Jupyter 内核语义刻意不实现（富 MIME / cell 上下文 / 变量驻留）。"""
    provider = _code_provider(fallback_stack, sandbox_id="sbx-x")
    with pytest.raises(NotImplementedError) as exc_info:
        asyncio.run(provider.execute_code("print(1)"))
    assert "Jupyter" in str(exc_info.value)
    with pytest.raises(NotImplementedError):
        asyncio.run(provider.execute_code_stream("print(1)"))
