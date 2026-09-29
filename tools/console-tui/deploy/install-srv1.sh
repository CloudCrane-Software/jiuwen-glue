#!/bin/sh
# install-srv1.sh — console-tui 在 srv-1 的安装脚本（线E，2026-09-29）
# 前置：jiuwen-glue 仓已 clone 到本机（默认 /opt/company-ops/src/jiuwen-glue），
#       以 root 运行（psql 经 docker exec；crane-tui 落 /usr/local/bin）。
# 用法：sh tools/console-tui/deploy/install-srv1.sh [repo_root]
# 步骤：① venv + pip install（PyPI 失败自动换清华镜像）② DDL：003 v2 视图 +
#       010 补授权（幂等可重放）③ crane-tui 落位 ④ tui.env 骨架（DSN 经 bao
#       现取写入 0600 文件——DSN 只进 srv-1 本机文件，脚本零回显零落日志）。
set -e

REPO=${1:-/opt/company-ops/src/jiuwen-glue}
TOOL="$REPO/tools/console-tui"
VENV=/opt/company-ops/console-tui-venv
COLDSTART=/opt/company-ops/coldstart
PG_CONTAINER=company-pg-1

echo "[1/5] venv + 依赖安装 → $VENV"
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV"
fi
if ! "$VENV/bin/pip" install -q "$TOOL[pg]" 2>/dev/null; then
    echo "  PyPI 不可达，改用清华镜像"
    "$VENV/bin/pip" install -q -i https://pypi.tuna.tsinghua.edu.cn/simple "$TOOL[pg]"
fi

echo "[2/5] DDL：003 v2 视图（节点心跳投影）"
docker exec -i $PG_CONTAINER psql -U postgres -d jiuwen_team -v ON_ERROR_STOP=1 \
    < "$TOOL/sql/003_console_views.sql" > /dev/null

echo "[3/5] DCL：010 补授权（v_usage / v_signal_timeline → jiuwen）"
docker exec -i $PG_CONTAINER psql -U postgres -d jiuwen_team -v ON_ERROR_STOP=1 \
    < "$TOOL/sql/010_console_grants.sql" > /dev/null

echo "[4/5] crane-tui → /usr/local/bin/"
cp "$TOOL/deploy/crane-tui" /usr/local/bin/crane-tui
chmod 755 /usr/local/bin/crane-tui

echo "[5/5] tui.env（0600；DSN 经 bao 现取，不回显）"
mkdir -p "$COLDSTART"
if [ -s "$COLDSTART/tui.env" ]; then
    echo "  已存在，跳过（保持既有值；重取请先删该文件）"
else
    # argv-free 形态（对齐 SYSTEM-GUIDE §7）：BAO_TOKEN 只进 docker 客户端 environ
    DSN="$(BAO_ADDR=http://127.0.0.1:8200 \
        BAO_TOKEN="$(cat /etc/bao/root-token)" \
        docker exec -e BAO_ADDR -e BAO_TOKEN company-bao-1 \
        bao kv get -field=dsn kv/company/gpu/team-db)"
    if [ -z "$DSN" ]; then
        echo "  bao 取 DSN 失败——建空骨架退出（补值后 crane-tui 即用）" >&2
        umask 077
        printf 'CONSOLE_TUI_PROBE_TARGETS=anuser@36.139.118.235:jiuwenswarm-app,vllm-local\n' \
            > "$COLDSTART/tui.env"
        exit 1
    fi
    umask 077
    {
        printf '# console-tui 运行 env（线E 2026-09-29；0600，勿提交勿打印）\n'
        printf 'CONSOLE_TUI_DSN=%s\n' "$DSN"
        # 探针走 GPU 机公网 IP（tailnet 数据面单向劣化实测：ICMP 通但 TCP/22 超时；
        # 公网 sshd 仅密钥认证，与 tailnet 同一 server key）
        printf 'CONSOLE_TUI_PROBE_TARGETS=anuser@36.139.118.235:jiuwenswarm-app,vllm-local\n'
    } > "$COLDSTART/tui.env"
fi
chmod 600 "$COLDSTART/tui.env" 2>/dev/null || true

echo "完成。验证：crane-tui（mock 无 DSN 也可）；headless 冒烟见 README-srv1.md"
