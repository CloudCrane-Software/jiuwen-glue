# coding: utf-8
"""sql/003 视图静态检查：003 是 PG 方言（jsonb/LATERAL/FILTER），sqlite 跑不了，
按工单约定改为对 SQL 文件做关键字段静态断言 + 与 data.py 取数语句交叉对账。
"""
from __future__ import annotations

import re
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
SQL_PATH = TOOL_DIR / "sql" / "003_console_views.sql"
DATA_PATH = TOOL_DIR / "src" / "console_tui" / "data.py"

VIEWS = {
    "glue.v_task_board": ["team_task", "task_transition", "paused",
                          "last_transition_at", "decision_record", "tenant_id"],
    "glue.v_active_lease": ["budget_lease", "status = 'ACTIVE'", "remaining",
                            "parent_lease_id", "granted_at"],
    "glue.v_pending_challenge": ["challenge", "state = 'pending'",
                                 "expires_at > now()", "seconds_left", "who_confirms"],
    "glue.v_recent_decision": ["decision_record", "context_hash", "chosen",
                               "rationale_ref", "meta"],
    "glue.v_node_utilization": ["glue.node", "cpu_frac", "gpu_frac", "trust_level",
                                "max_parallel", "online_window"],
}


def _code_lines(text: str) -> str:
    """去掉注释行后再做破坏性语句检查（注释里的词不算）。"""
    return "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("--"))


def test_sql_file_exists_with_consumer_header():
    text = SQL_PATH.read_text(encoding="utf-8")
    assert "消费方" in text and "data.py" in text          # 工单要求的文件头
    assert "主 agent" in text                               # 落库由主 agent 执行
    assert "PostgreSQL" in text                             # 方言声明


def test_five_views_present_with_key_fields():
    text = SQL_PATH.read_text(encoding="utf-8")
    for view, fields in VIEWS.items():
        assert f"CREATE OR REPLACE VIEW {view}" in text, f"缺视图 {view}"
        for field in fields:
            assert field in text, f"{view} 缺关键字段 {field!r}"


def test_views_are_read_only_pg_dialect():
    code = _code_lines(SQL_PATH.read_text(encoding="utf-8"))
    for banned in ("DROP ", "INSERT INTO", "UPDATE ", "DELETE FROM",
                   "TRUNCATE", "ALTER TABLE"):
        assert banned not in code, f"只读视图文件不得含 {banned!r}"
    assert code.count("CREATE OR REPLACE VIEW") == 5
    # PG 方言特征（sqlite 不支持），证明没写错方言
    assert "LATERAL" in code and "::text" in code and "EXTRACT(EPOCH" in code


def test_data_pg_queries_align_with_views():
    """data.py 的取数 SQL 引用的视图必须在 003 中真实存在。"""
    data = DATA_PATH.read_text(encoding="utf-8")
    sql_text = SQL_PATH.read_text(encoding="utf-8")
    used_views = set(re.findall(r"FROM (glue\.v_\w+)", data))
    assert used_views == set(VIEWS), f"取数视图与 003 不对齐: {used_views}"
    for view in used_views:
        assert f"CREATE OR REPLACE VIEW {view}" in sql_text
    # pg 写路径只允许出现这两张基础表（干预写死的范围：decision_record / challenge）
    written_tables = set(re.findall(r"INTO (glue\.\w+)|UPDATE (glue\.\w+)", data))
    flat = {a or b for a, b in written_tables}
    assert flat <= {"glue.decision_record", "glue.challenge"}, \
        f"干预写路径越界: {flat}"


def test_data_pg_queries_are_parameterized():
    """pg 模式所有 SQL 走 %s 参数化；禁止 f-string/格式化拼接进 SQL 文本。"""
    data = DATA_PATH.read_text(encoding="utf-8")
    assert data.count("%s") >= 10
    assert "f\"SELECT" not in data and "f'SELECT" not in data
    assert ".format(" not in data
    assert re.search(r'execute\(sql, tuple\(params\)\)', data) is not None
