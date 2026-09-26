#!/usr/bin/env bash
# 01-disk-cleanup.sh — gpumachine 磁盘清理到 <70%（WO-0009 资产，脚本 1/5）
#
# 依据: 工单 WO-0009 动作 1；机器基线 99G 盘（V100S 机实测，曾出现 84% 占用）。
# 隔离策略（本脚本安全性的核心）:
#   1. 只允许清理"白名单路径"——缓存/日志/临时物，逐项列在 ALLOWLIST_PATTERNS；
#   2. 任何 rm/清理动作前经 path_is_safe 校验，白名单之外一律拒绝；
#   3. 绝不触碰: /root/workspace、/opt/company、/var/lib/postgresql、docker volumes、
#      jiuwenswarm/模型目录（数据与资产盘，属于人管，不属于本脚本管）；
#   4. docker 清理只做 build cache 与 dangling 层，--volumes/-a 被显式禁止；
#   5. 白名单清完仍未达标 → 打印 top 消耗分析后以退出码 2 结束（人工裁决，不自动扩权）。
# 用法: sudo bash 01-disk-cleanup.sh [--dry-run]
set -Eeuo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SELF_DIR/lib.sh"
load_config
need_root

DRY_RUN=0; [ "${1:-}" = "--dry-run" ] && DRY_RUN=1
TARGET_PCT="${DISK_TARGET_PCT:-70}"

# ---------- 隔离策略定义 ----------
# 可清理根（白名单）。每个根下的删除必须匹配 *_PATTERNS。
ALLOWLIST_ROOTS=(
  /var/cache
  /var/log
  /var/tmp
  /tmp
  /var/crash
  /var/lib/systemd/coredump
  "$HOME/.cache"
  /root/.cache
)
# 各根内允许删除的模式（相对该根）
ALLOWLIST_PATTERNS=(
  "dnf" "yum" "PackageKit"        # 包管理缓存
  "journal/*"                     # 由 journalctl --vacuum 处理（见下），目录兜底
  "*.log" "*.log.[0-9]*" "*.gz"   # 轮转日志
  "pip" "npm" "yarn"              # 语言包缓存
  "Google-chrome" "microsoft-edge" "playwright"  # 浏览器/自动化缓存
  "torch" "vllm" "huggingface" "modelscope"      # 框架下载缓存（仅 cache 目录）
  "abrt" "sa"                     # 报告/统计旧档
)
PROTECTED_PATHS=(
  /root/workspace /opt/company /var/lib/postgresql /var/lib/docker/volumes
  /opt/gpumachine /home
)

path_is_under() { # $1=候选 $2=根
  case "$1" in "$2"|"$2"/*) return 0;; *) return 1;; esac
}
path_is_protected() {
  local p
  for p in "${PROTECTED_PATHS[@]}"; do path_is_under "$1" "$p" && return 0; done
  return 1
}
# 白名单校验: 路径必须落在某个 ALLOWLIST_ROOT 之下，且不落在保护区
path_is_safe_to_clean() {
  local p="$1" root
  for root in "${ALLOWLIST_ROOTS[@]}"; do
    if path_is_under "$p" "$root"; then
      path_is_protected "$p" && return 1
      return 0
    fi
  done
  return 1
}

safe_rm_globs() { # $@ = 相对 ALLOWLIST_ROOT 的 glob 组合，逐根展开
  local root pat g expanded
  for root in "${ALLOWLIST_ROOTS[@]}"; do
    [ -d "$root" ] || continue
    for pat in "$@"; do
      for expanded in "$root"/$pat; do
        [ -e "$expanded" ] || continue
        path_is_safe_to_clean "$expanded" || { warn "隔离策略拒绝: $expanded"; continue; }
        if [ "$DRY_RUN" = 1 ]; then
          log "[dry-run] 将清理: $expanded ($(du -sh "$expanded" 2>/dev/null | awk '{print $1}'))"
        else
          log "清理: $expanded"
          rm -rf --one-file-system "$expanded"
        fi
      done
    done
  done
}

report_df() {
  df -h / | awk 'NR==2 {print "根分区: 已用 "$3" / 共 "$2"（"$5"）"}'
}

main() {
  log "=== 01 磁盘清理开始（目标: 已用 <${TARGET_PCT}%）$([ "$DRY_RUN" = 1 ] && echo ' [dry-run]') ==="
  report_df
  local used; used="$(df_used_pct)"
  if [ "$used" -lt "$TARGET_PCT" ]; then
    log "当前 ${used}% 已达标，无需清理。"
    exit 0
  fi

  # 1) 包管理缓存（白名单: /var/cache/*）
  if [ "$DRY_RUN" = 1 ]; then log "[dry-run] dnf clean all"; else dnf clean all >/dev/null 2>&1 || warn "dnf clean 失败(忽略)"; fi

  # 2) journald 限量到 200M（系统机制，不直接 rm journal 文件）
  if [ "$DRY_RUN" = 1 ]; then log "[dry-run] journalctl --vacuum-size=200M"; else journalctl --vacuum-size=200M >/dev/null 2>&1 || warn "journal vacuum 失败(忽略)"; fi

  # 3) docker：只清 build cache 与 dangling（显式不用 -a / --volumes）
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] docker builder prune -f; docker image prune -f（仅 dangling）"
  elif command -v docker >/dev/null 2>&1; then
    docker builder prune -f >/dev/null 2>&1 || warn "builder prune 失败(忽略)"
    docker image prune -f >/dev/null 2>&1 || warn "image prune 失败(忽略)"
  else
    log "docker 未安装（先跑 02-docker-nvidia.sh），跳过 docker 清理"
  fi

  # 4) 白名单模式清理（临时/缓存/轮转日志，>3 天的才动日志类）
  #    注意: find 只对实际存在的根目录执行——不存在的根（如容器里没有 /root/.cache）
  #    会让 find 返回非 0，在 set -o pipefail 下杀死整个脚本（CNB 流水线实测踩坑）。
  safe_rm_globs "*.log.[0-9]*" "*.gz"
  existing_roots=()
  for r in "${ALLOWLIST_ROOTS[@]}"; do
    if [ -d "$r" ]; then existing_roots+=("$r"); fi
  done
  if [ "${#existing_roots[@]}" -gt 0 ]; then
    find "${existing_roots[@]}" -mindepth 1 -maxdepth 1 -type f -name "*.log" -mtime +3 2>/dev/null | while read -r f; do
      path_is_safe_to_clean "$f" || continue
      [ "$DRY_RUN" = 1 ] && log "[dry-run] 将清理: $f" || { log "清理旧日志: $f"; rm -f -- "$f"; }
    done || true   # pipefail 兜底: 根目录被并发清理等极端情况不致死
  fi
  safe_rm_globs "pip" "npm" "yarn" "torch" "vllm" "huggingface" "modelscope" "playwright" "abrt" "sa"

  # 5) 临时目录（>7 天）
  for d in /tmp /var/tmp; do
    [ "$DRY_RUN" = 1 ] && log "[dry-run] find $d -mtime +7 -delete" \
      || find "$d" -mindepth 1 -mtime +7 -delete 2>/dev/null || true
  done

  used="$(df_used_pct)"
  report_df
  if [ "$used" -lt "$TARGET_PCT" ]; then
    log "=== 达标: 当前 ${used}% < ${TARGET_PCT}% ==="
    exit 0
  fi

  # 6) 未达标 → 输出分析，人工裁决（隔离策略第 5 条：不自动扩权）
  warn "白名单清理后仍为 ${used}% >= ${TARGET_PCT}%。按隔离策略不自动删除白名单之外内容。"
  log "---- 大头分析（top15，仅供人工决策）----"
  du -x -d1 / 2>/dev/null | sort -rn | head -15 | awk '{printf "%8.1fM  %s\n", $1/1024, $2}'
  for d in /var/lib/docker /root /var/log; do
    [ -d "$d" ] && { log "---- $d top10 ----"; du -x -d1 "$d" 2>/dev/null | sort -rn | head -10 | awk '{printf "%8.1fM  %s\n", $1/1024, $2}'; }
  done
  log "处置建议: 人工确认后把目标路径加入 ALLOWLIST_ROOTS/PATTERNS 再跑本脚本；数据资产迁移走 COS/归档，不走本脚本。"
  exit 2
}

main "$@"
