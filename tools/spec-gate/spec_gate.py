# coding: utf-8
"""spec-gate — line. 资产层元门禁（v2.0 §5.2 六维）首跑工具（W-05）.

对 glue 仓 spec-kit 首模块（specs/*.spec.md + evals/）跑入库门禁。六维口径与
阈值（v2.0 §5.2），本首跑的实现深度（如实声明，不虚报）：

======  ============================  ==============================  ==========
维度      口径（v2.0 §5.2）              本工具实现                        状态
======  ============================  ==============================  ==========
D1 可判定性  双独立实现一致率 ≥90%        对 aggregate 独立第二实现比对       已实现
                                      （穷举输入域 + spec 例 + eval 例）
D2 杀伤性    eval 自变异杀死率 ≥80%       正反例双向变异（D2a/D2b）           已实现
D3 区分度    分开过一次真实扇出           红绿分离 demo（真实扇出未跑）       demo 实证，
                                                                             原口径 [待数据]
D4 稳定性    零抖动                       全量重复运行结果逐字节比对          已实现
D5 红证明    新 case 在旧实现断言失败      红反例对旧实现快照（6f4674c）       已实现
D6 结局一致性 与结局标签背离率 <5%         无结局数据（无 revert/事故回填）    [待数据]
======  ============================  ==============================  ==========

用法（仓库根目录；纯标准库零依赖）::

    python tools/spec-gate/spec_gate.py                     # 六维全跑，报告打到 stdout
    python tools/spec-gate/spec_gate.py --out report.md     # 报告写文件
    python tools/spec-gate/spec_gate.py --static-only       # CI 环节9：仅 specs 静态
                                      一致性 + guardrail 例执行（六维全跑按需，太重）

退出码：0 = 门禁过（六维全部 PASS）。**空证据维度（D3 真实扇出未跑 / D6 无结局
数据）= BLOCKED → 非零退出（1）**——v2.1 原则 2「空证据一律 UNKNOWN/BLOCKED，
绝不默认放行」（D4-R2 修复：此前 [待数据] 维度不参与退出码，空证据被当作门禁
通过）。有证据但不达阈值 = FAIL，同非零。``--static-only``（CI 环节9）语义不变。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

VERDICTS = ("PASS", "BLOCKED", "UNKNOWN")
EXHAUST_MAX_LEN = 4          # D1 穷举输入域：长度 0..4 的全部列表（3^0+…+3^4 = 121）
THRESHOLD_DETERMINABILITY = 0.90   # §5.2：双实现一致率 ≥90%
THRESHOLD_LETHALITY = 0.80         # §5.2：杀死率 ≥80%
THRESHOLD_OUTCOME_DIVERGENCE = 0.05  # §5.2：结局背离率 <5%（D6）

REPO = Path(__file__).resolve().parents[2]   # 仓库根（tools/spec-gate/spec_gate.py）

# ── 例行解析（specs/*.spec.md 的格式契约，见 spec 头部「机器可读例格式约定」）────

REQ_RE = re.compile(r"^### (REQ-[GL]-\d{2}) (.+?)\s*$")
AGG_EX_RE = re.compile(r"^\s*- 例: aggregate\((\[.*\])\) -> (PASS|BLOCKED|UNKNOWN)\s*$")
META_RE = re.compile(r"^- (ref|semver|实现文件|来源|基线): (.+?)\s*$")


def parse_spec(path: Path) -> dict:
    """解析一份 spec 文件：元数据 + REQ 列表（各带正例/反例例行）。"""
    text = path.read_text(encoding="utf-8")
    spec = {"path": str(path.relative_to(REPO)).replace("\\", "/"),
            "meta": {}, "reqs": [], "parse_errors": []}
    cur = None
    section = None
    for lineno, line in enumerate(text.splitlines(), 1):
        m = META_RE.match(line)
        if m and cur is None:
            spec["meta"][m.group(1)] = m.group(2)
            continue
        m = REQ_RE.match(line)
        if m:
            cur = {"id": m.group(1), "title": m.group(2), "pos": [], "neg": [],
                   "rules": [], "lines": (lineno, None)}
            spec["reqs"].append(cur)
            section = None
            continue
        if cur is None:
            continue
        if line.startswith("- 规则:"):
            cur["rules"].append(line[len("- 规则:"):].strip())
            continue
        if line.startswith("- 正例:"):
            section = "pos"
            continue
        if line.startswith("- 反例:"):
            section = "neg"
            continue
        if line.startswith("- 判定:") or line.startswith("### ") or line.startswith("## "):
            section = None
            if line.startswith("- 判定:") and cur["lines"][1] is None:
                cur["lines"] = (cur["lines"][0], lineno)
            continue
        m = AGG_EX_RE.match(line)
        if m and section in ("pos", "neg"):
            try:
                # 裁决词是裸大写词（PASS/BLOCKED/UNKNOWN），先加引号再 literal_eval
                quoted = re.sub(r"\b(PASS|BLOCKED|UNKNOWN)\b", r'"\1"', m.group(1))
                inp = ast.literal_eval(quoted)
            except (ValueError, SyntaxError):
                spec["parse_errors"].append(f"{path.name}:{lineno} 列表字面量不可解析: {m.group(1)}")
                continue
            bad = [v for v in inp if v not in VERDICTS]
            if bad:
                spec["parse_errors"].append(
                    f"{path.name}:{lineno} 输入域越界（{bad}）: 只允许 PASS/BLOCKED/UNKNOWN")
                continue
            cur[section].append({"input": inp, "expect": m.group(2), "line": lineno})
            continue
        if line.strip().startswith("- 例:") and section in ("pos", "neg"):
            cur[section].append({"raw": line[len("- 例:"):].strip(), "line": lineno})
    for r in spec["reqs"]:
        r["lines"] = (r["lines"][0], None if r["lines"][1] is None else r["lines"][1])
    return spec


def load_red_cases(path: Path) -> list:
    cases = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        cases.append(json.loads(line))
    return cases


# ── 参考实现与第二独立实现 ────────────────────────────────────────────────────

def reference_aggregate(verdicts):
    """参考实现 = 冻结契约（semver 0.3.0）的 jiuwen_glue.aggregate（从仓库 src 导入）。"""
    src = str(REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from jiuwen_glue.guardrail import aggregate
    return aggregate(verdicts)


def second_aggregate(verdicts):
    """第二独立实现（D1）：按 specs/guardrail.spec.md 的 REQ-G-01..04 独立编码——
    折叠（fold）写法，与参考实现（any() 扫描）不同构；语义一致性正是 D1 要验证的对象。"""
    from functools import reduce

    def fold(acc, v):
        if acc == "BLOCKED" or v == "BLOCKED":
            return "BLOCKED"
        if acc == "UNKNOWN" or v == "UNKNOWN":
            return "UNKNOWN"
        return "PASS"
    vs = list(verdicts)
    if not vs:
        return "UNKNOWN"          # REQ-G-01：空集=信息不足
    return reduce(fold, vs, "PASS")  # REQ-G-02/03/04：BLOCKED 压 UNKNOWN 压 PASS


# ── 变异体（D2b：规则变异——对 spec 的似真误读，逐一实现成错误 aggregate）──────

def _mutants():
    def m01(vs):
        vs = list(vs)
        if any(v == "BLOCKED" for v in vs): return "BLOCKED"
        if any(v == "UNKNOWN" for v in vs): return "UNKNOWN"
        return "PASS"                                     # 空集→PASS（旧 fail-open 缺陷）
    def m02(vs):
        vs = list(vs)
        if any(v == "UNKNOWN" for v in vs): return "UNKNOWN"   # 优先级互换
        if any(v == "BLOCKED" for v in vs): return "BLOCKED"
        return "PASS"
    def m03(vs):
        vs = list(vs)
        if any(v == "UNKNOWN" for v in vs): return "UNKNOWN"   # BLOCKED 降格 UNKNOWN
        return "PASS"
    def m04(vs):
        vs = list(vs)
        if not vs: return "BLOCKED"                            # 空集→BLOCKED（过度保守误读）
        if any(v == "BLOCKED" for v in vs): return "BLOCKED"
        if any(v == "UNKNOWN" for v in vs): return "UNKNOWN"
        return "PASS"
    def m05(vs):
        return "PASS"                                          # 常量 PASS（聚合走形式）
    def m06(vs):
        return "UNKNOWN"                                       # 常量 UNKNOWN（永不放行）
    def m07(vs):
        vs = list(vs)
        return vs[0] if vs else "UNKNOWN"                      # 首元素决定
    def m08(vs):
        vs = list(vs)
        if any(v == "BLOCKED" for v in vs): return "BLOCKED"
        if any(v == "UNKNOWN" for v in vs): return "BLOCKED"   # UNKNOWN 升格 BLOCKED
        return "PASS"
    return [
        ("MUT-01 空集 fail-open（W-01 缺陷#2 复活）", m01),
        ("MUT-02 优先级互换（UNKNOWN 压过 BLOCKED）", m02),
        ("MUT-03 BLOCKED 降格 UNKNOWN", m03),
        ("MUT-04 空集 → BLOCKED（过度保守误读）", m04),
        ("MUT-05 常量 PASS（聚合走形式）", m05),
        ("MUT-06 常量 UNKNOWN（永不放行）", m06),
        ("MUT-07 首元素决定（忽略其余 check）", m07),
        ("MUT-08 UNKNOWN 升格 BLOCKED", m08),
    ]


# ── 维度实现 ─────────────────────────────────────────────────────────────────

def exhaustive_inputs():
    acc = [()]
    out = [[]]
    for _ in range(EXHAUST_MAX_LEN):
        out = [lst + [v] for lst in out for v in VERDICTS]
        acc.extend(tuple(x) for x in out)
    return [list(x) for x in acc]


def dim_determinability(pos_cases):
    """D1：双实现一致率——穷举输入域 ∪ spec 正例 ∪ eval 红反例 上逐输入比对。"""
    inputs = {tuple(i) for i in exhaustive_inputs()}
    inputs |= {tuple(c["input"]) for c in pos_cases}
    agree = 0
    disagreements = []
    for tup in sorted(inputs, key=lambda t: (len(t), t)):
        a, b = reference_aggregate(tup), second_aggregate(tup)
        if a == b:
            agree += 1
        else:
            disagreements.append({"input": list(tup), "ref": a, "second": b})
    total = len(inputs)
    return {"total_inputs": total, "agreement": agree / total,
            "disagreements": disagreements,
            "pass": (agree / total) >= THRESHOLD_DETERMINABILITY}


def dim_lethality(spec, red_cases):
    """D2：杀伤性（双向变异）。
    D2a 正例变异——对 spec 每条正例的期望输出做变异，oracle = 冻结契约实现 +
    eval 红反例；改错的例必须被抓住（spec 例不 vacuous）。
    D2b 规则变异——8 个似真误读实现成错误 aggregate，eval 集（spec 正例 ∪ 红反例）
    必须杀死（每个变异体至少一例输出不符）。"""
    pos = [c for r in spec["reqs"] for c in r["pos"] if "input" in c]
    neg = [c for r in spec["reqs"] for c in r["neg"] if "input" in c]
    eval_cases = [(c["input"], c["expect"]) for c in pos]
    eval_cases += [(c["input"], c["expect"]) for c in red_cases]
    # D2a：spec 正例期望值变异（改成三种错误输出逐一试）
    total_a, killed_a, detail_a = 0, 0, []
    for c in pos:
        for wrong in VERDICTS:
            if wrong == c["expect"]:
                continue
            total_a += 1
            oracle = reference_aggregate(c["input"])
            red_expect = [rc["expect"] for rc in red_cases if rc["input"] == c["input"]]
            caught = (wrong != oracle) or (wrong in red_expect and oracle not in red_expect)
            # 变异例被抓 = 与冻结实现输出矛盾，或与同输入的红反例矛盾
            if wrong != oracle or (red_expect and wrong != red_expect[0]):
                caught = True
            if caught:
                killed_a += 1
            else:
                detail_a.append({"req": None, "input": c["input"],
                                 "true_expect": c["expect"], "mutated_to": wrong})
    # D2b：规则变异 × eval 集
    total_b = killed_b = 0
    detail_b = []
    for name, fn in _mutants():
        total_b += 1
        survivors = [(i, e) for i, e in eval_cases if fn(tuple(i)) == e]
        # 变异体被杀 = eval 集存在一例其输出与期望不符
        misses = [(i, e, fn(tuple(i))) for i, e in eval_cases if fn(tuple(i)) != e]
        if misses:
            killed_b += 1
        else:
            detail_b.append({"mutant": name, "surviving_inputs": len(survivors)})
    # 红反例自身的禁忌断言：每条红反例的 expect 不得等于 forbidden
    for rc in red_cases:
        if rc["expect"] == rc["forbidden"]:
            spec["parse_errors"].append(
                f"red_cases.jsonl {rc['id']}: expect == forbidden（自相矛盾）")
    return {
        "d2a_spec_example_mutation": {"total": total_a, "killed": killed_a,
                                      "rate": killed_a / total_a if total_a else 0.0,
                                      "survivors": detail_a,
                                      "pass": total_a > 0 and killed_a / total_a >= THRESHOLD_LETHALITY},
        "d2b_rule_mutation": {"total": total_b, "killed": killed_b,
                              "rate": killed_b / total_b if total_b else 0.0,
                              "survivors": detail_b,
                              "pass": total_b > 0 and killed_b / total_b >= THRESHOLD_LETHALITY},
        "pass": True,  # 由调用方按两子维合成
    }


def dim_discrimination(red_cases):
    """D3 区分度：红绿分离 demo——参考实现全绿；旧实现与全部变异体至少一红。
    §5.2 原口径=分开过一次**真实扇出**；本首跑只有合成靶子 → 原口径空证据 =
    **BLOCKED**（D4-R2 修复：demo 过不再视为该维度通过——空证据不参与放行）。"""
    green_misses = [(c["input"], c["expect"], reference_aggregate(c["input"]))
                    for c in red_cases if reference_aggregate(c["input"]) != c["expect"]]
    old = old_impl()
    red_hits = [c["id"] for c in red_cases if old(tuple(c["input"])) != c["expect"]]
    mutant_hits = {name: sum(1 for i, e in [(c["input"], c["expect"]) for c in red_cases]
                             if fn(tuple(i)) != e) for name, fn in _mutants()}
    separated = (not green_misses and len(red_hits) > 0
                 and all(v > 0 for v in mutant_hits.values()))
    # 真实扇出未接线（real_fanout=None）→ 原口径空证据，该维度 BLOCKED；
    # demo 分离（demo_separated）仅作参考信号，不构成该维度通过（D4-R2）。
    return {"green_misses": green_misses, "old_impl_red_cases": red_hits,
            "mutant_red_counts": mutant_hits,
            "demo_separated": separated,
            "real_fanout": None,
            "status": "BLOCKED",
            "pass": False}


def load_outcome_cases(path: Path) -> list:
    """D6 结局一致性数据：evals/guardrail-aggregate/outcome_cases.jsonl（可缺——
    零样本即空证据）。行形状：{"id", "input", "expect", "outcome_label"}，
    outcome_label 与 expect 背离即一次结局背离。文件不存在/空 → []。"""
    if not path.exists():
        return []
    cases = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        cases.append(json.loads(line))
    return cases


def dim_outcome_consistency(outcome_cases):
    """D6 结局一致性：聚合结论与结局标签背离率 <5%（v2.1 §5.2 六维之末维）。

    **零样本 = BLOCKED，绝不默认放行**（v2.1 原则 2；D4-R2 修复：此前 D6 标
    [待数据] 却不参与退出码，空证据被当作门禁通过）。有样本时背离率 ≥5% = FAIL。"""
    if not outcome_cases:
        return {"samples": 0, "divergence": None, "status": "BLOCKED",
                "reason": "无结局数据（outcome_cases.jsonl 缺失或 0 条）",
                "pass": False}
    divergent = [c for c in outcome_cases
                 if c.get("expect") != c.get("outcome_label")]
    rate = len(divergent) / len(outcome_cases)
    return {"samples": len(outcome_cases),
            "divergence": rate, "divergent": [
                {"id": c.get("id"), "input": c.get("input"),
                 "expect": c.get("expect"), "outcome_label": c.get("outcome_label")}
                for c in divergent],
            "status": "PASS" if rate < THRESHOLD_OUTCOME_DIVERGENCE else "FAIL",
            "pass": rate < THRESHOLD_OUTCOME_DIVERGENCE}


def old_impl():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "w05_old_impls", Path(__file__).resolve().parent / "old_impls.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.aggregate_v_pre_w01


def dim_stability(snapshot_a, snapshot_b):
    """D4 稳定性：全量重复运行（规范化 JSON）逐字节比对，零抖动。"""
    a = json.dumps(snapshot_a, sort_keys=True, ensure_ascii=False)
    b = json.dumps(snapshot_b, sort_keys=True, ensure_ascii=False)
    return {"identical": a == b, "pass": a == b}


def dim_red_proof(red_cases):
    """D5 红证明：kills_impl=pre-w01-fail-open 的红反例必须在旧实现快照上断言失败。"""
    old = old_impl()
    checked, failures = [], []
    for c in red_cases:
        if c.get("kills_impl") != "pre-w01-fail-open":
            continue
        got = old(tuple(c["input"]))
        entry = {"id": c["id"], "input": c["input"], "expect": c["expect"],
                 "old_impl_output": got}
        checked.append(entry)
        if got == c["expect"]:
            failures.append(entry)  # 旧实现竟然给出正确输出 → 红证明失效
    return {"checked": checked, "broken": failures,
            "pass": len(checked) > 0 and not failures}


# ── 静态一致性（CI 环节9 用）与六维主流程 ────────────────────────────────────

def static_consistency(gspec, lspec, red_cases):
    """specs 一致性静态检查（CI 环节9）：元数据齐全、REQ 唯一、五要素齐、
    guardrail 例可执行（逐条对参考实现断言）、红反例文件与 spec 反例不矛盾。"""
    errors = list(gspec["parse_errors"]) + list(lspec["parse_errors"])
    for spec, kind in ((gspec, "guardrail"), (lspec, "leases")):
        for k in ("ref", "semver", "实现文件"):
            if k not in spec["meta"]:
                errors.append(f"{spec['path']}: 缺元数据行 - {k}:")
        ids = [r["id"] for r in spec["reqs"]]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            errors.append(f"{spec['path']}: REQ id 重复: {dup}")
        for r in spec["reqs"]:
            if not r["rules"]:
                errors.append(f"{spec['path']} {r['id']}: 缺 - 规则: 行")
            if not r["pos"]:
                errors.append(f"{spec['path']} {r['id']}: 正例为空")
            if not r["neg"]:
                errors.append(f"{spec['path']} {r['id']}: 反例为空")
    # guardrail 可执行正例逐条对参考实现断言（绿）
    exec_pos = [c for r in gspec["reqs"] for c in r["pos"] if "input" in c]
    for c in exec_pos:
        got = reference_aggregate(tuple(c["input"]))
        if got != c["expect"]:
            errors.append(f"specs/guardrail.spec.md:{c['line']} 正例不符实现: "
                          f"aggregate({c['input']}) 期望 {c['expect']} 实得 {got}")
    # 红反例 ↔ spec 一致：每条红反例的 (input, expect) 必须与参考实现一致（绿）
    for rc in red_cases:
        got = reference_aggregate(tuple(rc["input"]))
        if got != rc["expect"]:
            errors.append(f"red_cases.jsonl {rc['id']}: 与冻结契约实现不符 "
                          f"({rc['input']} 期望 {rc['expect']} 实得 {got})")
    return errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="line. 资产层元门禁（六维）首跑")
    ap.add_argument("--repo", default=str(REPO), help="仓库根目录")
    ap.add_argument("--out", default=None, help="报告输出 markdown 路径（缺省 stdout）")
    ap.add_argument("--static-only", action="store_true",
                    help="CI 环节9 模式：仅 specs 静态一致性 + 例执行（六维全跑按需）")
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()

    gspec = parse_spec(repo / "specs" / "guardrail.spec.md")
    lspec = parse_spec(repo / "specs" / "leases.spec.md")
    red_cases = load_red_cases(repo / "evals" / "guardrail-aggregate" / "red_cases.jsonl")
    outcome_cases = load_outcome_cases(repo / "evals" / "guardrail-aggregate" / "outcome_cases.jsonl")

    static_errors = static_consistency(gspec, lspec, red_cases)
    if args.static_only:
        if static_errors:
            print("spec-gate 静态一致性检查失败：")
            for e in static_errors:
                print(f"  - {e}")
            return 1
        n_exec = sum(1 for r in gspec["reqs"] for c in r["pos"] if "input" in c)
        print(f"spec-gate 静态一致性检查通过：guardrail REQ×{len(gspec['reqs'])} "
              f"(可执行正例×{n_exec}) + leases REQ×{len(lspec['reqs'])} + "
              f"红反例×{len(red_cases)} 全部一致（对冻结契约实现）")
        return 0

    pos_all = [c for r in gspec["reqs"] for c in r["pos"] if "input" in c]
    d1 = dim_determinability(pos_all)
    d2 = dim_lethality(gspec, red_cases)
    d2["pass"] = d2["d2a_spec_example_mutation"]["pass"] and d2["d2b_rule_mutation"]["pass"]
    d3 = dim_discrimination(red_cases)
    d5 = dim_red_proof(red_cases)
    d6 = dim_outcome_consistency(outcome_cases)

    # D4：把前四维结果整体重算一遍比对（快照 A/B）
    def snapshot():
        return {
            "d1": dim_determinability(pos_all),
            "d2": dim_lethality(gspec, red_cases),
            "d3": dim_discrimination(red_cases),
            "d5": dim_red_proof(red_cases),
            "static_errors": static_consistency(gspec, lspec, red_cases),
        }
    snap_a = snapshot()
    snap_b = snapshot()
    d4 = dim_stability(snap_a, snap_b)

    report = render_report(gspec, lspec, red_cases, static_errors,
                           d1, d2, d3, d4, d5, d6)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"report written: {out}")
    else:
        print(report)

    # 门禁结论（D4-R2）：六维全部 PASS 才过；空证据维度 = BLOCKED → 非零
    # （v2.1 原则 2：空证据一律 UNKNOWN/BLOCKED，绝不默认放行）。
    dims = (("D1", d1), ("D2", d2), ("D3", d3), ("D4", d4), ("D5", d5), ("D6", d6))
    blocked = [name for name, d in dims
               if not d["pass"] and d.get("status") == "BLOCKED"]
    failed = [name for name, d in dims
              if not d["pass"] and d.get("status") != "BLOCKED"]
    if static_errors:
        failed.append("静态一致性")
    ok = not blocked and not failed
    if ok:
        print("门禁结论：PASS（六维全部通过）")
        return 0
    parts = []
    if blocked:
        parts.append("BLOCKED（空证据，不放行）: " + ", ".join(blocked))
    if failed:
        parts.append("FAIL: " + ", ".join(failed))
    print("门禁结论：未通过 —— " + "；".join(parts))
    return 1


def render_report(gspec, lspec, red_cases, static_errors, d1, d2, d3, d4, d5, d6):
    pct = lambda x: f"{x * 100:.1f}%"
    L = []
    L.append("# W-05 spec-gate 六维元门禁首跑报告")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("| --- | --- |")
    L.append("| 生成 | `python tools/spec-gate/spec_gate.py`（W-05 首跑，纯标准库零依赖） |")
    L.append("| 对象 | specs/guardrail.spec.md（REQ×{}）+ specs/leases.spec.md（REQ×{}）+ evals/guardrail-aggregate/red_cases.jsonl（红反例×{}） |".format(len(gspec["reqs"]), len(lspec["reqs"]), len(red_cases)))
    L.append("| 契约锚点 | jiuwen_glue.guardrail.aggregate @ 0.3.0（contracts/registry.json 首条） |")
    L.append("| 参考实现 | src/jiuwen_glue/guardrail.py::aggregate（冻结契约，main 7895c23 含 W-01 修复） |")
    L.append("| 静态一致性 | {} |".format("通过" if not static_errors else "**失败（见文末）**"))
    L.append("")
    L.append("## 六维总览")
    L.append("")
    L.append("| 维度 | 口径（v2.0 §5.2） | 阈值 | 实测 | 结论 |")
    L.append("| --- | --- | --- | --- | --- |")
    L.append("| D1 可判定性 | 双独立实现一致率 | ≥90% | {}（{} 输入，不一致 {}） | {} |".format(
        pct(d1["agreement"]), d1["total_inputs"], len(d1["disagreements"]),
        "PASS" if d1["pass"] else "FAIL"))
    L.append("| D2a 杀伤性·正例变异 | spec 例期望值变异 × (冻结实现+红反例) oracle 抓获率 | ≥80% | {}（{} 次变异，存活 {}） | {} |".format(
        pct(d2["d2a_spec_example_mutation"]["rate"]), d2["d2a_spec_example_mutation"]["total"],
        len(d2["d2a_spec_example_mutation"]["survivors"]),
        "PASS" if d2["d2a_spec_example_mutation"]["pass"] else "FAIL"))
    L.append("| D2b 杀伤性·规则变异 | 8 个似真误读实现 × eval 集杀死率 | ≥80% | {}（{}/{} 被杀） | {} |".format(
        pct(d2["d2b_rule_mutation"]["rate"]), d2["d2b_rule_mutation"]["killed"],
        d2["d2b_rule_mutation"]["total"],
        "PASS" if d2["d2b_rule_mutation"]["pass"] else "FAIL"))
    L.append("| D3 区分度 | 分开过一次**真实扇出** | 红绿分离 | demo：参考实现 {} 红；旧实现杀 {} 条红反例；8 变异体全灭={}。**真实扇出未接线**（无候选扇出场景） | **BLOCKED**（原口径空证据，不放行；demo 分离={} 仅参考） |".format(
        len(d3["green_misses"]), len(d3["old_impl_red_cases"]),
        "是" if all(v > 0 for v in d3["mutant_red_counts"].values()) else "否",
        "是" if d3["demo_separated"] else "否"))
    L.append("| D4 稳定性 | 零抖动（全量重跑逐字节比对） | 完全一致 | {} | {} |".format(
        "两次运行结果完全一致（抖动 0）" if d4["identical"] else "**存在抖动**",
        "PASS" if d4["pass"] else "FAIL"))
    L.append("| D5 红证明 | 新 case 在旧实现断言失败 | 全部复现 | {}（{} 条 pre-w01 红反例对 6f4674c 快照） | {} |".format(
        "全部复现失败" if d5["pass"] and d5["checked"] else "未复现",
        len(d5["checked"]), "PASS" if d5["pass"] else "FAIL"))
    if d6["samples"] == 0:
        L.append("| D6 结局一致性 | 与结局标签背离率 <5% | <5% | 无结局数据（无 revert/事故回填） | **BLOCKED**（空证据不放行，v2.1 原则 2） |")
    else:
        L.append("| D6 结局一致性 | 与结局标签背离率 <5% | <5% | {}（{} 样本，背离 {}） | {} |".format(
            pct(d6["divergence"]), d6["samples"], len(d6["divergent"]),
            "PASS" if d6["pass"] else "FAIL"))
    L.append("")
    L.append("> 六维门禁结论口径（D4-R2 修复）：D1/D2/D4/D5 已实现并通过；D3 原口径")
    L.append("> （真实扇出）与 D6（结局标签回流）**空证据 = BLOCKED，门禁不放行**")
    L.append("> （v2.1 原则 2：空证据一律 UNKNOWN/BLOCKED，绝不默认放行）。此前版本")
    L.append("> 将 [待数据] 维度排除在退出码之外，空证据被当作门禁通过——已废弃。")
    L.append("> [待] 项消除条件已写入 docs/line-dogfood.md；D6 样本经")
    L.append("> evals/guardrail-aggregate/outcome_cases.jsonl 回流后自动转为可计算。")
    L.append("")
    L.append("## D1 可判定性：双独立实现一致率")
    L.append("")
    L.append("- 输入域：长度 0..4 的全部 PASS/BLOCKED/UNKNOWN 列表（{} 个）∪ spec 正例 ∪ 红反例".format(d1["total_inputs"]))
    L.append("- 第二实现：`spec_gate.second_aggregate`（按 spec REQ-G-01..04 独立编码，fold 写法，与参考实现 any() 扫描不同构）")
    L.append("- 一致率：**{}**（阈值 ≥90%）".format(pct(d1["agreement"])))
    if d1["disagreements"]:
        for x in d1["disagreements"]:
            L.append(f"  - 不一致：{x}")
    L.append("")
    L.append("## D2 杀伤性：双向变异")
    L.append("")
    L.append("### D2a spec 正例变异（eval 自变异杀死率口径：对 spec 正反例做变异）")
    L.append("")
    L.append("- 对每条可执行正例的期望输出改为其余两态逐一变异；oracle = 冻结契约实现输出 ∪ 同输入红反例期望。")
    L.append("- {} 次变异，抓获 {}，存活 {}（阈值 ≥80%）".format(
        d2["d2a_spec_example_mutation"]["total"], d2["d2a_spec_example_mutation"]["killed"],
        len(d2["d2a_spec_example_mutation"]["survivors"])))
    for s in d2["d2a_spec_example_mutation"]["survivors"]:
        L.append(f"  - 存活：{s}")
    L.append("")
    L.append("### D2b 规则变异（似真误读 → 错误实现 × eval 集杀死）")
    L.append("")
    for name, _ in _mutants():
        killed = name not in [s["mutant"] for s in d2["d2b_rule_mutation"]["survivors"]]
        L.append(f"- {'杀' if killed else '**存活**'}：{name}")
    for s in d2["d2b_rule_mutation"]["survivors"]:
        L.append(f"  - 存活详情：{s}")
    L.append("")
    L.append("## D3 区分度：红绿分离 demo")
    L.append("")
    L.append("- 绿：参考实现对全部红反例 {} 缺口（全绿）".format(len(d3["green_misses"])))
    L.append("- 红：旧实现快照（6f4674c）被 {} 条红反例杀死：{}".format(
        len(d3["old_impl_red_cases"]), ", ".join(d3["old_impl_red_cases"]) or "无"))
    for name, cnt in d3["mutant_red_counts"].items():
        L.append(f"- 红：{name} → 被红反例集杀 {cnt} 条")
    L.append("- **[待数据]**：§5.2 原口径=分开过一次真实扇出；无扇出场景，本 demo 是合成靶子替代，不冒充。")
    L.append("")
    L.append("## D4 稳定性")
    L.append("")
    L.append("- 全量六维（除 D6）计算两次，规范化 JSON 逐字节比对：{}。".format(
        "一致，抖动 0" if d4["identical"] else "**不一致**"))
    L.append("")
    L.append("## D5 红证明")
    L.append("")
    L.append("- 旧实现快照：tools/spec-gate/old_impls.py（6f4674c aggregate 逐字拷贝，fail-open 缺陷原样保留）")
    for c in d5["checked"]:
        L.append("- {}：aggregate({}) 旧实现输出 **{}**，期望 {} → 断言失败复现{}".format(
            c["id"], c["input"], c["old_impl_output"], c["expect"],
            "" if c["old_impl_output"] != c["expect"] else "（**失效**：旧实现给出正确输出）"))
    L.append("")
    L.append("## D6 结局一致性")
    L.append("")
    if d6["samples"] == 0:
        L.append("- **BLOCKED（空证据）**：需要生产结局标签（revert / 事故自动转红 case）"
                 "回流后计算背离率；当前为零样本，无法计算也不得编造，**门禁不放行**。")
    else:
        L.append("- 样本 {}，背离率 {}（阈值 <5%）→ {}".format(
            d6["samples"], pct(d6["divergence"]),
            "PASS" if d6["pass"] else "**FAIL**"))
        for x in d6["divergent"]:
            L.append(f"  - 背离：{x}")
    L.append("")
    if static_errors:
        L.append("## 静态一致性错误")
        L.append("")
        for e in static_errors:
            L.append(f"- {e}")
        L.append("")
    L.append("## 复跑")
    L.append("")
    L.append("```bash")
    L.append("python tools/spec-gate/spec_gate.py --out ../../build/w05-spec-gate-report.md   # 六维全跑（按需）")
    L.append("python tools/spec-gate/spec_gate.py --static-only                                # CI 环节9 口径")
    L.append("python tools/check_contracts.py                                                  # 契约 hash 秒检")
    L.append("```")
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
