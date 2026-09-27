# coding: utf-8
"""契约 hash 秒检（G7 契约门禁的快检半边，v2.0 §5.4）——W-05.

对 contracts/registry.json 每条 (ref, semver) → sha256：重算实现文件哈希并比对。
- 全部一致 → 退出码 0（秒级，纯标准库，无网络无导入被检包）；
- 任何不一致/文件缺失/表损坏 → 退出码 1 并逐条列出差异（fail-closed）。

用法（仓库根目录）::

    python tools/check_contracts.py

 breaking 分类（人工/评审侧）：hash 不匹配时按 registry.json 各条
 breaking_policy 判定是否 breaking——本脚本只负责秒检与拦截，不做分类裁决。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REGISTRY = REPO / "contracts" / "registry.json"


def canonical_sha256(path: Path) -> str:
    """规范形哈希：CRLF → LF（= git blob 内容）。hash 与检出配置（core.autocrlf）
    解耦，跨机/跨检出可比（W-05 实测：同一 blob 在 CRLF 检出下原始字节 hash 不同）。"""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def main() -> int:
    if not REGISTRY.is_file():
        print(f"FAIL: 契约注册表缺失: {REGISTRY.relative_to(REPO)}")
        return 1
    try:
        reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        print(f"FAIL: 契约注册表不可解析: {e}")
        return 1
    entries = reg.get("entries")
    if not isinstance(entries, list) or not entries:
        print("FAIL: 注册表无 entries（空表也是异常——首条已入，不许清空）")
        return 1
    bad = 0
    for ent in entries:
        ref = ent.get("ref", "<无 ref>")
        impl = ent.get("impl_file", "")
        want = ent.get("sha256", "")
        semver = ent.get("semver", "?")
        path = REPO / impl if impl else None
        if not impl or not path.is_file():
            print(f"FAIL {ref}@{semver}: 实现文件缺失: {impl}")
            bad += 1
            continue
        got = canonical_sha256(path)
        if got == want:
            print(f"OK   {ref}@{semver}  sha256={got[:12]}…  ({impl})")
        else:
            print(f"FAIL {ref}@{semver}: hash 不匹配\n"
                  f"     注册表: {want}\n"
                  f"     实算:   {got}\n"
                  f"     → 实现已偏离冻结契约：按 breaking_policy 分类；"
                  f"非 breaking 的演进须 bump semver 并重挂本表（G7）")
            bad += 1
    if bad:
        print(f"check_contracts: {bad} 条不一致（fail-closed）")
        return 1
    print(f"check_contracts: {len(entries)} 条全部一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
