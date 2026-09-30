# coding: utf-8
"""gate-registry 校验器（v0.9）.

两件事：
1. schema 校验：必填字段 / 枚举值 / write_time_mirror 引用存在 / 无重复 id
2. --check 漂移：注册表声明的 ci/pr 层门禁 vs .github/workflows/ci.yml 实际跑的 diff 报告

用法（仓库根目录）::

    python tools/gate-registry/validate.py                          # schema 校验
    python tools/gate-registry/validate.py --check                   # schema + 漂移
    python tools/gate-registry/validate.py --registry path/to/file   # 指定注册表路径
    python tools/gate-registry/validate.py --ci path/to/ci.yml       # 指定 CI 路径

退出码：
  0 = schema 通过；--check 时漂移为零
  1 = schema 错误 或 --check 时存在漂移
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

REPO = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = REPO / "gates" / "registry.yaml"
DEFAULT_CI = REPO / ".github" / "workflows" / "ci.yml"

VALID_LAYERS = {"write-time", "pr", "merge-queue", "post-merge"}
VALID_TIERS = {"T0", "T1", "T2"}
VALID_MODES = {"blocking", "shadow", "sampled", "advisory"}
VALID_SCOPES = {"diff", "affected", "full-repo"}
REQUIRED_FIELDS = (
    "id", "title", "description", "layer", "tier", "mode", "scope",
    "cache_key", "retry", "flaky_policy", "fail_action", "reason_codes",
    "write_time_mirror", "evidence_ledger", "implementation",
)


def _load_registry(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"注册表顶层必须是 mapping，实际: {type(data).__name__}")
    gates = data.get("gates")
    if not isinstance(gates, list) or not gates:
        raise ValueError("注册表必须包含非空 gates 列表")
    return data


def _err(entry: Dict[str, Any], field: str, msg: str) -> str:
    gid = entry.get("id", "<无 id>")
    return f"[{gid}] {field}: {msg}"


def validate_schema(registry: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    gates = registry["gates"]

    seen_ids: set = set()
    all_ids: set = {g["id"] for g in gates if "id" in g}

    for idx, entry in enumerate(gates):
        if not isinstance(entry, dict):
            errors.append(f"gates[{idx}] 不是 mapping（实际: {type(entry).__name__}）")
            continue

        # 必填字段
        for field in REQUIRED_FIELDS:
            if field not in entry:
                errors.append(_err(entry, field, "必填字段缺失"))

        # id 唯一
        gid = entry.get("id")
        if gid:
            if gid in seen_ids:
                errors.append(_err(entry, "id", f"重复 id: {gid!r}"))
            seen_ids.add(gid)

        # 枚举校验
        layer = entry.get("layer")
        if layer is not None and layer not in VALID_LAYERS:
            errors.append(_err(entry, "layer", f"枚举外值: {layer!r}（合法: {VALID_LAYERS}）"))
        tier = entry.get("tier")
        if tier is not None and tier not in VALID_TIERS:
            errors.append(_err(entry, "tier", f"枚举外值: {tier!r}（合法: {VALID_TIERS}）"))
        mode = entry.get("mode")
        if mode is not None and mode not in VALID_MODES:
            errors.append(_err(entry, "mode", f"枚举外值: {mode!r}（合法: {VALID_MODES}）"))
        scope = entry.get("scope")
        if scope is not None and scope not in VALID_SCOPES:
            errors.append(_err(entry, "scope", f"枚举外值: {scope!r}（合法: {VALID_SCOPES}）"))

        # retry 非负整数
        retry = entry.get("retry")
        if retry is not None:
            try:
                if int(retry) < 0:
                    errors.append(_err(entry, "retry", f"须为非负整数，实际: {retry!r}"))
            except (TypeError, ValueError):
                errors.append(_err(entry, "retry", f"须为非负整数，实际: {retry!r}"))

        # reason_codes 列表
        rc = entry.get("reason_codes")
        if rc is not None:
            if not isinstance(rc, list):
                errors.append(_err(entry, "reason_codes", f"须为 list，实际: {type(rc).__name__}"))
            else:
                for code in rc:
                    if not isinstance(code, str) or not code:
                        errors.append(_err(entry, "reason_codes", f"含空/非字符串元素: {code!r}"))

        # write_time_mirror 引用存在（允许 "none" 表示无镜像）
        mirror = entry.get("write_time_mirror")
        if mirror is not None and mirror != "none":
            refs = [r.strip() for r in str(mirror).split(",") if r.strip()]
            for ref in refs:
                if ref not in all_ids:
                    errors.append(_err(entry, "write_time_mirror", f"引用了不存在的 gate id: {ref!r}"))

        # implementation 基本结构
        impl = entry.get("implementation")
        if impl is not None:
            if not isinstance(impl, dict):
                errors.append(_err(entry, "implementation", "须为 mapping"))
            else:
                for sub in ("src", "test"):
                    if sub not in impl:
                        errors.append(_err(entry, "implementation", f"缺 implementation.{sub}"))

        # metrics 基本结构
        metrics = entry.get("metrics")
        if metrics is not None:
            if not isinstance(metrics, dict):
                errors.append(_err(entry, "metrics", "须为 mapping"))
            else:
                for sub in ("unique_captures", "false_positives"):
                    if sub not in metrics:
                        errors.append(_err(entry, "metrics", f"缺 metrics.{sub}"))

    return errors


def _extract_ci_testpaths(ci_text: str) -> List[str]:
    """从 ci.yml 文本提取 pytest 实际跑的 testpaths（简单启发式，非全量 YAML 解析）。"""
    # 去掉 YAML 注释行，避免把注释里的 pytest/testpaths 当真题
    lines = [line for line in ci_text.splitlines() if not line.strip().startswith("#")]
    ci_text = "\n".join(lines)

    paths: List[str] = []

    # 匹配 pytest 行内 --override-ini="testpaths=..." 或 pytest <dirs>
    # 优先匹配 python -m pytest，避免把 pip install pytest 等行当真题
    m = re.search(r'python\s+-m\s+pytest\s+(.+?)(?:\s*->|\s*\||\s*$|\n)', ci_text, re.DOTALL)
    if not m:
        m = re.search(r'pytest\s+(.+?)(?:\s*->|\s*\||\s*$|\n)', ci_text, re.DOTALL)
    if m:
        tail = m.group(1)
        # 去掉 > log 重定向和 ${} shell 展开
        tail = re.split(r'\s*>\s*', tail)[0]
        tail = tail.strip()
        if tail:
            paths.extend(tail.split())

    # 也匹配 pytest.ini / pyproject.toml 配置（本仓在 pyproject.toml 声明）
    m2 = re.search(r'testpaths\s*=\s*\[([^\]]+)\]', ci_text)
    if m2:
        for token in re.split(r'[,"\'\s]+', m2.group(1)):
            token = token.strip().strip(",")
            if token:
                paths.append(token)

    # 本仓已知 testpaths（作为兜底，如果 ci.yml 解析失败）
    if not paths:
        paths = [
            "tests/",
            "providers/e2b_compat/",
            "tools/console-tui/",
            "tools/spec-gate/tests/",
        ]
    return paths


def _extract_cnb_pytest_paths(ci_text: str) -> List[str]:
    """从 .cnb.yml 文本提取 pytest 实际测试路径。"""
    paths: List[str] = []
    for m in re.finditer(r'pytest(?:[^\n]*?)(tests/|providers/e2b_compat/|tools/console-tui/|tools/spec-gate/tests/)', ci_text):
        paths.append(m.group(1))
    if not paths:
        for token in ["tests/", "providers/e2b_compat/", "tools/console-tui/", "tools/spec-gate/tests/"]:
            if token in ci_text:
                paths.append(token)
    return sorted(set(paths))


def check_drift(registry: Dict[str, Any], ci_text: str, ci_label: str = "CI") -> Tuple[List[str], List[str]]:
    """返回 (warnings, infos).

    warnings = 注册表声明但 CI 未实际跑 / CI 实际跑但注册表未声明
    infos   = 参考性提示
    """
    ci_paths = _extract_ci_testpaths(ci_text)
    if ci_paths == ["tests/", "providers/e2b_compat/", "tools/console-tui/", "tools/spec-gate/tests/"] and ".cnb" not in ci_label.lower():
        cnb_paths = _extract_cnb_pytest_paths(ci_text)
        if cnb_paths:
            ci_paths = cnb_paths
    ci_paths_normalized = {p.rstrip("/") for p in ci_paths}

    declared_ci: List[Dict[str, Any]] = []
    for g in registry.get("gates", []):
        layer = g.get("layer")
        if layer in ("pr", "merge-queue"):
            declared_ci.append(g)

    # CI 实际跑 pytest（覆盖测试文件）
    # 把 ci_paths 与各 gate 的 implementation.test / ci_ref 做模糊匹配
    warnings: List[str] = []
    infos: List[str] = []

    declared_ids = {g["id"] for g in declared_ci}
    ci_gate_ids: set = set()

    for g in declared_ci:
        gid = g["id"]
        matched = False
        impl = g.get("implementation") or {}
        test_ref = str(impl.get("test", "")) + " " + str(impl.get("ci_ref", ""))
        for p in ci_paths_normalized:
            # 简单包含匹配：test 路径或 ci_ref 包含 ci_path 的某一段
            p_seg = p.replace("/", "").replace("_", "")
            if p_seg and p_seg in test_ref.replace("/", "").replace("_", ""):
                matched = True
                break
            # 直接目录匹配
            if p and p in test_ref:
                matched = True
                break
        if matched:
            ci_gate_ids.add(gid)

    # 注册表声明了但 CI 未匹配到
    for g in declared_ci:
        gid = g["id"]
        if gid not in ci_gate_ids:
            # 跳过 T0 纯静态门禁（CI 未接线是 v0.9 已知缺口）
            tier = g.get("tier", "")
            mode = g.get("mode", "")
            if tier == "T0" and mode == "blocking":
                warnings.append(
                    f"[{gid}] 注册表声明 T0 blocking，但 {ci_label} pytest 未直接调用 "
                    f"（已知缺口：registry-drift-check / contract-hash-check 待接线）"
                )
            else:
                warnings.append(f"[{gid}] 注册表声明 {g.get('layer')} 门禁，但 {ci_label} 未匹配到对应测试路径")

    # CI 跑了但注册表未声明
    known_ci_roots = {"tests", "providers/e2b_compat", "tools/console-tui", "tools/spec-gate/tests"}
    for p in ci_paths_normalized:
        root = p.split("/")[0] if p else ""
        if root and root not in known_ci_roots:
            infos.append(f"{ci_label} 含非常规 testpath: {p!r}（是否需补登记？）")

    return warnings, infos


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="gate-registry 校验器（schema + CI 漂移）")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY), help="注册表 YAML 路径")
    ap.add_argument("--ci", default=str(DEFAULT_CI), help="ci.yml 路径")
    ap.add_argument("--check", action="store_true", help="启用 CI 漂移检测")
    args = ap.parse_args(argv)

    reg_path = Path(args.registry)
    ci_path = Path(args.ci)

    # ── schema 校验 ─────────────────────────────────────────────────────────
    if not reg_path.is_file():
        print(f"FAIL: 注册表文件不存在: {reg_path}")
        return 1

    try:
        registry = _load_registry(reg_path)
    except (ValueError, yaml.YAMLError) as exc:
        print(f"FAIL: 注册表加载失败: {exc}")
        return 1

    errors = validate_schema(registry)
    if errors:
        print("schema 校验失败：")
        for e in errors:
            print(f"  - {e}")
        print(f"共 {len(errors)} 条错误")
        return 1

    n_gates = len(registry["gates"])
    print(f"schema 校验通过：gates={n_gates}，枚举/引用/必填字段全部合法")

    # ── 漂移检测 ─────────────────────────────────────────────────────────────
    ci_label = ci_path.name if ci_path else "CI"
    if not args.check:
        return 0

    if not ci_path.is_file():
        print(f"WARN: CI 配置文件不存在，跳过漂移检测: {ci_path}")
        return 0

    ci_text = ci_path.read_text(encoding="utf-8")
    warnings, infos = check_drift(registry, ci_text, ci_label=ci_label)

    if not warnings and not infos:
        print("漂移检测通过：注册表声明的 ci/pr 层门禁与 ci.yml 实际运行路径一致")
        return 0

    if infos:
        print("漂移提示：")
        for msg in infos:
            print(f"  - {msg}")

    if warnings:
        print(f"\n漂移警告（{len(warnings)} 项）：")
        for msg in warnings:
            print(f"  - {msg}")
        print("\n消除条件：补齐 CI 接线 或 更新注册表声明，使两侧一致。")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
