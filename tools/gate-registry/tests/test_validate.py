# coding: utf-8
"""tools/gate-registry/validate.py 单测（≥6 场景）.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import validate as gate_validate  # noqa: E402

VALID_LAYERS = gate_validate.VALID_LAYERS
VALID_MODES = gate_validate.VALID_MODES
VALID_SCOPES = gate_validate.VALID_SCOPES
VALID_TIERS = gate_validate.VALID_TIERS
_load_registry = gate_validate._load_registry
_extract_ci_testpaths = gate_validate._extract_ci_testpaths
check_drift = gate_validate.check_drift
main = gate_validate.main
validate_schema = gate_validate.validate_schema


# ── 辅助 ──────────────────────────────────────────────────────────────────

REPO = Path(__file__).resolve().parents[3]


def _write_yaml(path: Path, data: dict) -> None:
    import yaml
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _base_registry(extra_gates=None) -> dict:
    gates = [
        {
            "id": "gate-a",
            "title": "A",
            "description": "desc",
            "layer": "pr",
            "tier": "T1",
            "mode": "blocking",
            "scope": "full-repo",
            "cache_key": "hash(a)",
            "retry": 1,
            "flaky_policy": "none",
            "fail_action": "block",
            "reason_codes": ["IMPL_BUG"],
            "write_time_mirror": "none",
            "evidence_ledger": "gates/health-ledger.md#a",
            "implementation": {"src": "a.py", "test": "test_a.py"},
            "metrics": {"unique_captures": "pending", "false_positives": "pending"},
        },
    ]
    if extra_gates:
        gates.extend(extra_gates)
    return {
        "schema_version": "gates-registry/0.9",
        "description": "test",
        "last_updated": "2026-09-30",
        "gates": gates,
    }


def _write_base(tmp_path: Path, extra_gates=None) -> Path:
    p = tmp_path / "registry.yaml"
    _write_yaml(p, _base_registry(extra_gates))
    return p


# ── 测试 ──────────────────────────────────────────────────────────────────

class TestSchemaValidation:
    def test_valid_registry_passes(self, tmp_path: Path) -> None:
        p = _write_base(tmp_path)
        registry = _load_registry(p)
        errs = validate_schema(registry)
        assert errs == []

    def test_cli_valid_registry_returns_0(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        p = _write_base(tmp_path)
        rc = main(["--registry", str(p)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "schema 校验通过" in out

    def test_missing_required_field_fails(self, tmp_path: Path) -> None:
        """去掉 description 字段 → 一条 schema 错误。"""
        data = _base_registry()
        data["gates"][0].pop("description")
        p = tmp_path / "bad.yaml"
        _write_yaml(p, data)
        errs = validate_schema(_load_registry(p))
        assert len(errs) == 1
        assert "description" in errs[0]

    def test_invalid_enum_values_fail(self, tmp_path: Path) -> None:
        """layer/tier/mode/scope 各一个非法值 → 多条错误。"""
        bad_gate = {
            "id": "gate-bad",
            "title": "B",
            "description": "d",
            "layer": "invalid-layer",
            "tier": "T9",
            "mode": "invalid-mode",
            "scope": "invalid-scope",
            "cache_key": "h",
            "retry": -1,
            "flaky_policy": "f",
            "fail_action": "x",
            "reason_codes": "not-a-list",
            "write_time_mirror": "none",
            "evidence_ledger": "#",
            "implementation": {"src": "", "test": ""},
            "metrics": {"unique_captures": "", "false_positives": ""},
        }
        p = _write_base(tmp_path, [bad_gate])
        errs = validate_schema(_load_registry(p))
        msgs = " ".join(errs)
        assert "invalid-layer" in msgs
        assert "T9" in msgs
        assert "invalid-mode" in msgs
        assert "invalid-scope" in msgs
        assert "retry" in msgs
        assert "reason_codes" in msgs

    def test_duplicate_id_fails(self, tmp_path: Path) -> None:
        dup = {
            "id": "gate-a",  # 与 base 重复
            "title": "A2",
            "description": "d",
            "layer": "pr",
            "tier": "T1",
            "mode": "blocking",
            "scope": "full-repo",
            "cache_key": "h",
            "retry": 0,
            "flaky_policy": "n",
            "fail_action": "b",
            "reason_codes": [],
            "write_time_mirror": "none",
            "evidence_ledger": "#",
            "implementation": {"src": "a.py", "test": "test_a2.py"},
            "metrics": {"unique_captures": "pending", "false_positives": "pending"},
        }
        p = _write_base(tmp_path, [dup])
        errs = validate_schema(_load_registry(p))
        assert any("重复 id" in e and "gate-a" in e for e in errs)

    def test_dangling_write_time_mirror_fails(self, tmp_path: Path) -> None:
        base = _base_registry()
        g = base["gates"][0].copy()
        g["id"] = "gate-ref"
        g["write_time_mirror"] = "non-existent-gate,also-nonexistent"
        data = _base_registry([g])
        p = tmp_path / "bad_ref.yaml"
        _write_yaml(p, data)
        errs = validate_schema(_load_registry(p))
        msgs = " ".join(errs)
        assert "non-existent-gate" in msgs
        assert "also-nonexistent" in msgs


class TestDriftDetection:
    def _make_registry(self, gates) -> dict:
        return {
            "schema_version": "gates-registry/0.9",
            "description": "drift-test",
            "last_updated": "2026-09-30",
            "gates": gates,
        }

    def test_check_no_drift_when_aligned(self, tmp_path: Path) -> None:
        """registry 声明与 ci.yml 四 testpaths 完全对齐 → rc=0."""
        ci = textwrap.dedent("""\
            name: ci
            jobs:
              pytest:
                steps:
                  - run: python -m pytest tests/ providers/e2b_compat/ tools/console-tui/ tools/spec-gate/tests/
        """)
        ci_file = tmp_path / "ci.yml"
        ci_file.write_text(ci, encoding="utf-8")

        gates = [
            {
                "id": "ci-pytest-suite",
                "title": "suite",
                "description": "d",
                "layer": "pr",
                "tier": "T1",
                "mode": "blocking",
                "scope": "full-repo",
                "cache_key": "h",
                "retry": 1,
                "flaky_policy": "n",
                "fail_action": "b",
                "reason_codes": [],
                "write_time_mirror": "none",
                "evidence_ledger": "#",
                "implementation": {
                    "src": "src/",
                    "test": "tests/ providers/e2b_compat/ tools/console-tui/ tools/spec-gate/tests/",
                    "ci_ref": "tests/ providers/e2b_compat/ tools/console-tui/ tools/spec-gate/tests/",
                },
                "metrics": {"unique_captures": "pending", "false_positives": "pending"},
            },
        ]
        reg = self._make_registry(gates)
        reg_path = tmp_path / "registry.yaml"
        _write_yaml(reg_path, reg)
        rc = main(["--registry", str(reg_path), "--ci", str(ci_file)])
        assert rc == 0

    def test_check_detects_gate_not_in_ci(self, tmp_path: Path) -> None:
        """注册表声明了门禁但 ci.yml 未跑对应路径 → rc=1（drift warning）。"""
        ci = textwrap.dedent("""\
            name: ci
            jobs:
              pytest:
                steps:
                  - run: python -m pytest tests/
        """)
        ci_file = tmp_path / "ci.yml"
        ci_file.write_text(ci, encoding="utf-8")

        gates = [
            {
                "id": "gate-in-ci",
                "title": "in",
                "description": "d",
                "layer": "pr",
                "tier": "T1",
                "mode": "blocking",
                "scope": "full-repo",
                "cache_key": "h",
                "retry": 1,
                "flaky_policy": "n",
                "fail_action": "b",
                "reason_codes": [],
                "write_time_mirror": "none",
                "evidence_ledger": "#",
                "implementation": {
                    "src": "a.py",
                    "test": "tests/test_a.py",
                    "ci_ref": "tests/",
                },
                "metrics": {"unique_captures": "pending", "false_positives": "pending"},
            },
            {
                "id": "gate-drift",
                "title": "drift",
                "description": "d",
                "layer": "pr",
                "tier": "T0",
                "mode": "blocking",
                "scope": "full-repo",
                "cache_key": "h",
                "retry": 0,
                "flaky_policy": "n",
                "fail_action": "b",
                "reason_codes": [],
                "write_time_mirror": "none",
                "evidence_ledger": "#",
                "implementation": {
                    "src": "tools/foo/tool.py",
                    "test": "tools/foo/test_tool.py",
                    "ci_ref": "tools/foo/",
                },
                "metrics": {"unique_captures": "pending", "false_positives": "pending"},
            },
        ]
        reg = self._make_registry(gates)
        reg_path = tmp_path / "registry.yaml"
        _write_yaml(reg_path, reg)

        rc = main(["--registry", str(reg_path), "--ci", str(ci_file), "--check"])
        assert rc == 1

    def test_extract_ci_testpaths_from_real_ci(self) -> None:
        """ci.yml 解析：能提取 pytest 四 testpaths."""
        ci_text = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        paths = _extract_ci_testpaths(ci_text)
        joined = " ".join(paths)
        assert "tests/" in joined
        assert "providers/e2b_compat/" in joined or "providers" in joined
        assert "tools/console-tui/" in joined or "console-tui" in joined
        assert "tools/spec-gate/tests/" in joined or "spec-gate" in joined
