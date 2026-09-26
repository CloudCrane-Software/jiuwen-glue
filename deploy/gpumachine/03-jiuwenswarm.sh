#!/usr/bin/env bash
# 03-jiuwenswarm.sh — gpumachine 部署 jiuwenswarm（distribute 模式）（WO-0009 资产，脚本 3/5）
#
# 工单要点（逐条落实）:
#   - distribute 模式: pip install "jiuwenswarm[distribute]"（extra 即 openjiuwen[postgres,zmq]，
#     见 jiuwenswarm pyproject.toml [project.optional-dependencies].distribute）
#   - Postgres 指 srv-1 隧道地址占位 <srv-1-wg-addr>（团队库在控制面 pg，srv-1 侧放行属 M1 WO-0003）
#   - JIUWENSWARM_CONFIG_URL=off：彻底关闭 openJiuwen 官网远端配置 = 无华为账号登录/免费模型
#     通道出站（jiuwenswarm/common/auth/remote_config.py:6,45；方案 WO-0003 验收"无华为通道出站"）
#   - 模型端点指 srv-1 Higress（唯一强制入口，方案 §4.9 #7）：本脚本只写这一个 API_BASE，
#     任何"第二个模型端点"都违反边界（RUNBOOK 有巡检项）
# 密钥纪律: API_KEY（Higress consumer key）与团队库 DSN 在服务启动时经 OpenBao 拉取进 tmpfs，
#   不落盘、不入库、不写日志（执行面零长期密钥，方案 §0.5）。
set -Eeuo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SELF_DIR/lib.sh"
load_config
need_root

VENV="${JIUWENSWARM_VENV:-/opt/gpumachine/jiuwenswarm/venv}"
ETC_DIR=/etc/gpumachine
RUN_DIR=/run/jiuwenswarm
PORT="${JIUWENSWARM_PORT:-18092}"
MODEL_NAME="${JIUWENSWARM_MODEL_NAME:?JIUWENSWARM_MODEL_NAME 未配置}"
CONFIG_URL="${JIUWENSWARM_CONFIG_URL:-off}"
SRV1_ADDR="${SRV1_TUNNEL_ADDR:?SRV1_TUNNEL_ADDR 未配置}"
HIGRESS_BASE="${HIGRESS_BASE:-http://${SRV1_ADDR}:8080/v1}"

[ -f /etc/anolis-release ] && log "系统: $(cat /etc/anolis-release)"
[ -x "$(command -v docker)" ] || die "docker 未就绪——先跑 02-docker-nvidia.sh"

# ---------- 1. 系统依赖 + Python 3.11~3.13 ----------
python_ok() { python3 -c 'import sys; sys.exit(0 if (3,11)<=sys.version_info[:2]<=(3,13) else 1)'; }
if ! python_ok; then
  log "安装 Python 3.12（jiuwenswarm requires-python >=3.11,<3.14）…"
  dnf_install python3.12 python3.12-pip python3.12-devel || die "python3.12 安装失败（检查 anolis 仓库）"
  alternatives --set python3 /usr/bin/python3.12 2>/dev/null || ln -sf /usr/bin/python3.12 /usr/local/bin/python3
fi
python3 --version | grep -qE '3\.(11|12|13)' || die "Python 版本不在 3.11~3.13: $(python3 --version)"
python_ok || die "python3 未指向 3.11~3.13"
dnf_install git gcc
log "Python: $(python3 --version)"

# ---------- 2. venv + pip 安装（distribute 模式） ----------
if ! step_is_done 03-pip-install; then
  mkdir -p "$(dirname "$VENV")"
  [ -d "$VENV" ] || python3 -m venv "$VENV"
  export PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
  export PIP_EXTRA_INDEX_URL="${PIP_EXTRA_INDEX_URL:-}"
  log "pip 安装 jiuwenswarm[distribute]（index=$PIP_INDEX_URL）…"
  # 包名自证: 官方 Quickstart 文档用 `pip install jiuwenswarm`；仓库 pyproject 的 project.name
  # 是 "workswarm"。两个名字都试，哪个成功记录哪个（CNB 流水线会先在 Anolis 容器里验证一次）。
  if "$VENV/bin/pip" install --upgrade pip >/dev/null \
     && "$VENV/bin/pip" install "jiuwenswarm[distribute]"; then
    log "已安装 PyPI 包: jiuwenswarm"
  elif "$VENV/bin/pip" install "workswarm[distribute]"; then
    log "已安装 PyPI 包: workswarm（jiuwenswarm 名下 PyPI 无此包，文档与包名不一致，已在日志留证）"
  else
    die "jiuwenswarm[distribute] 与 workswarm[distribute] 均安装失败——查看上方 pip 输出"
  fi
  "$VENV/bin/python" -c 'import jiuwenswarm' || die "import jiuwenswarm 失败"
  "$VENV/bin/pip" show jiuwenswarm 2>/dev/null | grep -E '^(Name|Version)' || \
    "$VENV/bin/pip" show workswarm | grep -E '^(Name|Version)' || true
  step_done 03-pip-install
else
  log "pip 安装步骤已完成（幂等跳过）"
fi

# ---------- 3. 非敏感运行配置（/etc，0600；密钥不在其中） ----------
mkdir -p "$ETC_DIR" "$RUN_DIR"
[ -e "$RUN_DIR/secrets.env" ] || { : >"$RUN_DIR/secrets.env"; chmod 600 "$RUN_DIR/secrets.env"; }
cat >"$ETC_DIR/jiuwenswarm.env" <<EOF
# 由 03-jiuwenswarm.sh 生成（CNB company-ops deploy/gpumachine/ 的实例配置）
# 模型端点唯一（Higress，方案 4.9 #7）——本文件之外不得再出现模型端点
MODEL_NAME=${MODEL_NAME}
API_BASE=${HIGRESS_BASE}
MODEL_PROVIDER=OpenAI
# 关华为通道出站（远端配置=官网接口下发；off 即一个请求都不发）
JIUWENSWARM_CONFIG_URL=${CONFIG_URL}
AGENT_SERVER_PORT=${PORT}
JIUWENSWARM_HOME=/opt/gpumachine/jiuwenswarm/home
EOF
chmod 600 "$ETC_DIR/jiuwenswarm.env"
grep -q '^JIUWENSWARM_CONFIG_URL=off$' "$ETC_DIR/jiuwenswarm.env" \
  || die "CONFIG_URL 必须=off（无华为通道出站），当前值见 $ETC_DIR/jiuwenswarm.env"
ep_count="$(grep -cE '^(API_BASE|OPENAI_BASE_URL|OPENAI_API_BASE)=' "$ETC_DIR/jiuwenswarm.env" || true)"
[ "$ep_count" = "1" ] || die "模型端点数量检查失败：只允许一个 API_BASE（决策点唯一），当前 $ep_count 个"

# ---------- 4. systemd：bao 密钥拉取（ExecStartPre 模式） + 服务 ----------
install -m 755 /dev/stdin /opt/gpumachine/bin/jiuwenswarm-bao-env <<'EOF'
#!/usr/bin/env bash
# 启动前从 OpenBao 拉取短期凭据到 tmpfs（/run/jiuwenswarm/secrets.env, 0600）
set -Eeuo pipefail
SELF_DIR="/opt/gpumachine/deploy"
. "$SELF_DIR/lib.sh"
load_config
RUN_DIR=/run/jiuwenswarm
mkdir -p "$RUN_DIR"; rm -f "$RUN_DIR/secrets.env"; : >"$RUN_DIR/secrets.env"; chmod 600 "$RUN_DIR/secrets.env"
gp_bao_env_put "$RUN_DIR/secrets.env" API_KEY      "$BAO_PATH_HIGRESS_KEY" key
gp_bao_env_put "$RUN_DIR/secrets.env" TEAM_PG_DSN  "$BAO_PATH_PG_URL"    dsn
grep -q '^API_KEY=..*' "$RUN_DIR/secrets.env" || { echo "API_KEY 为空"; exit 1; }
EOF
mkdir -p /opt/gpumachine/bin /opt/gpumachine/deploy /opt/gpumachine/jiuwenswarm/home
# 把本资产目录同步到运行时位置（脚本与 otel 配置的真相源仍在本仓）
rsync -a --delete "$SELF_DIR/" /opt/gpumachine/deploy/ 2>/dev/null || cp -a "$SELF_DIR"/. /opt/gpumachine/deploy/

cat >/etc/systemd/system/gpumachine-bao-env.service <<'EOF'
[Unit]
Description=Fetch short-lived credentials from OpenBao for jiuwenswarm (zero long-term keys)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/opt/gpumachine/bin/jiuwenswarm-bao-env
# 失败要显式：拿不到凭据就别起服务（不许静默降级成"无密钥运行"）
RemainAfterExit=no
EOF

cat >/etc/systemd/system/jiuwenswarm-app.service <<EOF
[Unit]
Description=JiuWenSwarm AgentServer + Gateway (gpumachine execution plane, distribute mode)
After=network-online.target gpumachine-bao-env.service
Wants=network-online.target
Requires=gpumachine-bao-env.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/gpumachine/jiuwenswarm/home
EnvironmentFile=${ETC_DIR}/jiuwenswarm.env
# 短期凭据（每次启动由 ExecStartPre 从 bao 现拉进 tmpfs）：
#   API_KEY     = Higress consumer key（模型唯一入口的鉴权）
#   TEAM_PG_DSN = 团队库 DSN（含口令；distribute 模式 Postgres 在 srv-1，占位 <srv-1-wg-addr>，
#                 放行与建库见 RUNBOOK 前置条件 P4；DSN 注入团队配置属 M1 WO-0003 联调）
EnvironmentFile=${RUN_DIR}/secrets.env
ExecStartPre=/opt/gpumachine/bin/jiuwenswarm-bao-env
ExecStart=${VENV}/bin/jiuwenswarm-app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

# ---------- 5. 健康检查（隧道未通时会显式失败——这是预期行为） ----------
log "连通性预检: srv-1 Higress ${HIGRESS_BASE} …"
if curl -fsS --max-time 5 -o /dev/null "${HIGRESS_BASE%/v1}/healthz" 2>/dev/null \
   || curl -fsS --max-time 5 -o /dev/null "$HIGRESS_BASE/models" 2>/dev/null; then
  log "Higress 可达"
else
  warn "Higress 经隧道暂不可达（预期内：srv-1 放行属 M1 WO-0003；服务可安装但起不来时先查隧道与 LISTEN_IP）"
fi

if [ "${START_NOW:-0}" = "1" ]; then
  systemctl enable --now jiuwenswarm-app.service
  sleep 3
  systemctl is-active --quiet jiuwenswarm-app.service || { journalctl -u jiuwenswarm-app -n 50 --no-pager; die "jiuwenswarm-app 启动失败"; }
  timeout 5 bash -c "</dev/tcp/127.0.0.1/$PORT" && log "AgentServer 端口 $PORT 已监听"
  log "=== jiuwenswarm 已启动（distribute 模式）==="
else
  log "未启动服务（START_NOW=1 可立即拉起；首次启动前先完成 RUNBOOK 前置条件 P1~P5）"
fi
step_done 03-jiuwenswarm
log "=== 03 jiuwenswarm 部署完成 ==="
