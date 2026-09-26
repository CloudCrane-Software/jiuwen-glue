# lib.sh — gpumachine 部署脚本公共库（WO-0009 资产）
#
# 设计约束（PROP-0001）：
#   - 执行面零长期密钥：本库不持有任何密钥；密钥一律在运行时经 OpenBao 引用获取。
#   - 自动化可逆优先：每步只做可回滚动作；不可逆操作必须显式确认变量开关。
#   - 本库被 01~05 脚本 source，不单独执行。

# ---------- 日志 ----------
GPUMACHINE_LOG_DIR="${GPUMACHINE_LOG_DIR:-/var/log/gpumachine-deploy}"
mkdir -p "$GPUMACHINE_LOG_DIR" 2>/dev/null || GPUMACHINE_LOG_DIR="."
_gp_log_file="$GPUMACHINE_LOG_DIR/$(basename "${0:-lib}").log"

log()  { printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$_gp_log_file"; }
warn() { printf '[%s][WARN] %s\n' "$(date '+%F %T')" "$*" | tee -a "$_gp_log_file" >&2; }
die()  { printf '[%s][FAIL] %s\n' "$(date '+%F %T')" "$*" | tee -a "$_gp_log_file" >&2; exit 1; }
run()  { log "+ $*"; "$@"; }   # 带回显执行（严禁把密钥值传进参数——用文件/环境变量传递）

# ---------- 身份与环境 ----------
need_root() {
  [ "$(id -u)" -eq 0 ] || die "必须以 root 运行（当前 uid=$(id -u)）"
}

# 配置文件查找顺序：$GPUMACHINE_CONFIG > /etc/gpumachine/config.env > 脚本同级 config.env
load_config() {
  local self_dir
  self_dir="$(cd "$(dirname "${BASH_SOURCE[1]:-$0}")" && pwd)"
  if [ -n "${GPUMACHINE_CONFIG:-}" ] && [ -f "$GPUMACHINE_CONFIG" ]; then
    # shellcheck disable=SC1090
    . "$GPUMACHINE_CONFIG"
  elif [ -f /etc/gpumachine/config.env ]; then
    # shellcheck disable=SC1091
    . /etc/gpumachine/config.env
  elif [ -f "$self_dir/config.env" ]; then
    # shellcheck disable=SC1091
    . "$self_dir/config.env"
  else
    warn "未找到 config.env，使用内置默认值（建议 cp config.example.env config.env 后按需修改）"
  fi
}

# ---------- 磁盘水位 ----------
# 输出根分区已用百分比（整数）
df_used_pct() {
  df -P / | awk 'NR==2 {gsub(/%/,"",$5); print $5}'
}

# ---------- OpenBao 引用（执行面零长期密钥的关键实现） ----------
# 用法: gp_bao_fetch <vault_kv_path> <field>
# 依赖: BAO_ADDR、BAO_TOKEN_FILE（含短期 worker token 的文件路径，0600）
# 安全: 值只打到 stdout，绝不写日志；调用方负责落到 tmpfs(0600)。
gp_bao_fetch() {
  local path="$1" field="$2"
  local addr="${BAO_ADDR:?BAO_ADDR 未配置（例如 http://<srv-1-wg-addr>:8200）}"
  local token_file="${BAO_TOKEN_FILE:?BAO_TOKEN_FILE 未配置}"
  [ -r "$token_file" ] || die "worker token 文件不可读: $token_file（见 RUNBOOK 前置条件）"
  [ -x "$(command -v curl || true)" ] || die "缺少 curl"
  local payload rc
  payload="$(mktemp)"
  chmod 600 "$payload"
  # KV v2 读接口；token 只经 header 传输，不进 argv 不进日志
  rc=0
  curl -fsS --max-time 10 \
    -H "X-Vault-Token: $(cat "$token_file")" \
    "$addr/v1/$path?field=$field" >"$payload" || rc=$?
  if [ $rc -ne 0 ]; then
    rm -f "$payload"
    die "OpenBao 读取失败: path=$path field=$field（rc=$rc；检查隧道/worker token 是否过期）"
  fi
  cat "$payload"; rm -f "$payload"
}

# 把 bao 中的字段写进 0600 的 tmpfs env 文件（供 systemd EnvironmentFile 引用）
# 用法: gp_bao_env_put <env_file> <VAR_NAME> <vault_kv_path> <field>
gp_bao_env_put() {
  local env_file="$1" var="$2" path="$3" field="$4"
  mkdir -p "$(dirname "$env_file")"
  [ -f "$env_file" ] || { : >"$env_file"; chmod 600 "$env_file"; }
  local val
  val="$(gp_bao_fetch "$path" "$field")" || die "获取 $path#$field 失败"
  # 覆盖或追加该变量（值不回显）
  grep -q "^${var}=" "$env_file" 2>/dev/null \
    && sed -i "s|^${var}=.*|${var}=${val}|" "$env_file" \
    || printf '%s=%s\n' "$var" "$val" >>"$env_file"
  log "已从 OpenBao 载入变量 $var（来源 $path#$field，值不记录）"
}

# ---------- 步骤状态（幂等/断点续跑） ----------
GP_STATE_DIR="${GP_STATE_DIR:-/var/lib/gpumachine/steps}"
mkdir -p "$GP_STATE_DIR" 2>/dev/null || true
step_done() { mkdir -p "$GP_STATE_DIR"; touch "$GP_STATE_DIR/$1"; }
step_is_done() { [ -f "$GP_STATE_DIR/$1" ]; }

# ---------- 软件包/dnf 辅助（Anolis 23, dnf-only） ----------
# DRY_RUN=1 时（02 脚本 --dry-run 模式设置）只打印不执行——供 CI 干跑验证安装逻辑
dnf_install() {
  [ -x "$(command -v dnf)" ] || die "未找到 dnf（目标系统应为 Anolis OS 23）"
  if [ "${DRY_RUN:-0}" = "1" ]; then
    log "[dry-run] dnf install -y $*"
    return 0
  fi
  dnf install -y "$@"
}
