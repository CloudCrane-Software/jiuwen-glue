#!/usr/bin/env bash
# 05-telemetry.sh — gpumachine 观测采集部署（OTel Collector 边缘缓冲 + AgentSight）（WO-0009 资产，脚本 5/5）
#
# 链路（方案 §4.9 #6）: tracer_otel → 本机边缘 Collector → srv-1 Collector（WO-0006）→ Langfuse/MinIO ATIF。
# 本脚本职责:
#   A. OTel Collector（contrib，钉版与 jiuwenswarm 官方 deploy/observability 同版 0.154.0）
#      承担"执行面边缘缓冲"：file_storage + sending_queue + retry，隧道断了数据不丢；
#   B. AgentSight（ANOLISA 组件，github.com/alibaba/anolisa releases 只发二进制 tar，WO-0001 已核查）：
#      eBPF 采集进程内行为；Anolis 内核 BTF/CO-RE 必可用（方案 §2"移至 gpumachine 即可用"路径）。
# 依赖: 02-docker-nvidia.sh（docker）。
set -Eeuo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SELF_DIR/lib.sh"
load_config
need_root

OTEL_TARGET="${OTEL_EXPORT_TARGET:?OTEL_EXPORT_TARGET 未配置（srv-1 Collector 隧道地址占位）}"
OTEL_PROTO="${OTEL_EXPORT_PROTOCOL:-grpc}"
EDGE_MB="${OTEL_EDGE_BUFFER_MB:-512}"
AGENTSIGHT_VER="${AGENTSIGHT_VERSION:-v0.13.0}"
TELE_DIR=/opt/gpumachine/telemetry
GH_MIRROR="${GITHUB_DL_MIRROR:-}"

main() {
  log "=== 05 观测采集部署开始（边缘缓冲 ${EDGE_MB}MB → ${OTEL_TARGET}）==="

  # ---------- A. OTel Collector（容器承载） ----------
  [ -x "$(command -v docker)" ] || die "docker 未就绪——先跑 02-docker-nvidia.sh"
  mkdir -p "$TELE_DIR" /var/lib/otelcol/gpumachine
  cp "$SELF_DIR/otel/otel-collector-config.yaml" "$TELE_DIR/otel-collector-config.yaml"
  # 目标地址注入配置（${env:OTEL_EXPORT_TARGET} 语法由 collector 自身解析）
  export OTEL_EXPORT_TARGET="$OTEL_TARGET"

  if [ ! -f "$TELE_DIR/docker-compose.yml" ]; then
    cat >"$TELE_DIR/docker-compose.yml" <<EOF
# gpumachine 执行面边缘采集（由 05-telemetry.sh 生成；实例配置真相源在 CNB company-ops）
services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.154.0
    container_name: gpumachine-otel-collector
    command: ["--config=/etc/otel-collector-config.yaml"]
    environment:
      - OTEL_EXPORT_TARGET=${OTEL_TARGET}
    volumes:
      - ./otel-collector-config.yaml:/etc/otel-collector-config.yaml:ro
      - /var/lib/otelcol/gpumachine:/var/lib/otelcol/gpumachine
    ports:
      - "127.0.0.1:4317:4317"
      - "127.0.0.1:4318:4318"
      - "127.0.0.1:13133:13133"
    restart: unless-stopped
    mem_limit: 512m
EOF
    log "$TELE_DIR/docker-compose.yml 已生成（OTLP 端口只绑回环——tracer_otel 在本机）"
  fi

  cd "$TELE_DIR"
  docker compose up -d 2>/dev/null || docker-compose up -d
  sleep 3
  docker ps --filter name=gpumachine-otel-collector --format '{{.Names}} {{.Status}}' | grep -q . \
    || { docker logs gpumachine-otel-collector --tail 30; die "otel-collector 启动失败"; }
  curl -fsS --max-time 5 http://127.0.0.1:13133/ >/dev/null && log "collector health_check 通过"
  # 边缘缓冲断链演练（可逆）: 停 collector 再看 file_storage 是否落盘
  log "边缘缓冲目录: /var/lib/otelcol/gpumachine（$(du -sh /var/lib/otelcol/gpumachine 2>/dev/null | awk '{print $1}')）"

  # ---------- B. AgentSight（eBPF，二进制 tar） ----------
  AG_DIR=/opt/gpumachine/agentsight
  if [ -x "$AG_DIR/agentsight" ] || ls "$AG_DIR"/*/agentsight >/dev/null 2>&1; then
    log "agentsight 已就位（幂等跳过）"
  else
    mkdir -p "$AG_DIR" /opt/gpumachine/pkgs
    log "agentsight ${AGENTSIGHT_VER}（sight 组件）：github.com/alibaba/anolisa releases 仅发二进制 tar（WO-0001 已核查）"
    # 下载策略: 直连 GitHub；配置了 GITHUB_DL_MIRROR 则走镜像。都不通时离线放入
    # /opt/gpumachine/pkgs/ 再重跑——找不到包就显式 TODO，不用假货充数（方案纪律）。
    asset_url="https://github.com/alibaba/anolisa/releases/download/${AGENTSIGHT_VER}/"
    [ -n "$GH_MIRROR" ] && asset_url="${GH_MIRROR%/}/$asset_url"
    warn "发布资产确切文件名以 releases 页为准（${asset_url}）；本脚本只自动解包 /opt/gpumachine/pkgs/ 内已就位的 tar"
    if ls /opt/gpumachine/pkgs/agentsight*.tar* >/dev/null 2>&1 || ls /opt/gpumachine/pkgs/sight*"$AGENTSIGHT_VER"*.tar* >/dev/null 2>&1; then
      for t in /opt/gpumachine/pkgs/agentsight*.tar* /opt/gpumachine/pkgs/sight*"$AGENTSIGHT_VER"*.tar*; do
        [ -e "$t" ] || continue
        tar -xf "$t" -C "$AG_DIR" 2>/dev/null || true
      done
    fi
    if ! ls "$AG_DIR"/agentsight >/dev/null 2>&1; then
      cat >"$AG_DIR/TODO-INSTALL.md" <<EOF
# AgentSight 安装 TODO（开通后执行）
1. 从 https://github.com/alibaba/anolisa/releases/tag/${AGENTSIGHT_VER} 取 sight 二进制 tar
   （境内取不到时用 GITHUB_DL_MIRROR 或离线拷入 /opt/gpumachine/pkgs/）；
2. 解包到 /opt/gpumachine/agentsight/ 并确认 agentsight 可执行；
3. systemctl enable --now agentsight && 验证 eBPF 挂载（journalctl -u agentsight）。
（WO-0001 核查：anolisa 仅发二进制 tar，无容器镜像——不得用假容器充数。）
EOF
      warn "agentsight 二进制未就位，已留 TODO-INSTALL.md（方案纪律: 不用假容器充数）"
    fi
  fi
  if [ -x "$AG_DIR/agentsight" ]; then
    cat >/etc/systemd/system/agentsight.service <<EOF
[Unit]
Description=AgentSight eBPF in-process behavior capture (ANOLISA, gpumachine)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${AG_DIR}/agentsight
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now agentsight 2>/dev/null \
      && log "agentsight 服务已启动（eBPF 挂载验收: journalctl -u agentsight）" \
      || warn "agentsight 启动失败——eBPF/BTF 验收留待真机，记录到 ADR/验收"
  fi

  step_done 05-telemetry
  log "=== 05 观测采集部署完成（端到端可见性验收属 WO-0006 观测管道）==="
}

main "$@"
