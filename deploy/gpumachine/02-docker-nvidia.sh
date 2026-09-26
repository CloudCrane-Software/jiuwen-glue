#!/usr/bin/env bash
# 02-docker-nvidia.sh — gpumachine 安装 Docker + NVIDIA 容器运行时（WO-0009 资产，脚本 2/5）
#
# 目标系统: Anolis OS 23（dnf-only）。GPU: Tesla V100S-PCIE-32GB（sm_70）。
# 步骤:
#   A. 安装 docker engine（anolis 仓库 docker-engine 优先，失败回退 docker-ce aliyun 源）
#   B. 校验 NVIDIA 驱动（重装系统时优先选"带 GPU 驱动"镜像；缺失时的 .run 安装路径见 RUNBOOK 前置条件，
#      本脚本默认 DRIVER_SETUP=verify——不在 CI/无人值守里偷偷装内核模块）
#   C. 安装 nvidia-container-toolkit 并注册 nvidia runtime
#   D. GPU 冒烟: docker run --rm --gpus all <base-cuda 镜像> nvidia-smi
# 用法: sudo bash 02-docker-nvidia.sh [--dry-run]
#   --dry-run: 逻辑干跑——不装包、不写任何配置、不启服务、不拉镜像；
#              daemon.json 生成逻辑落到 /tmp/daemon.json.preview 供检查。
#              CNB 流水线（anolis-23 容器）用本模式验证脚本逻辑，真机执行不加开关。
set -Eeuo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SELF_DIR/lib.sh"
load_config
need_root

DRY_RUN=0
case "${1:-}" in
  --dry-run|-n) DRY_RUN=1 ;;
  "") ;;
  *) die "用法: 02-docker-nvidia.sh [--dry-run]" ;;
esac

DRIVER_SETUP="${DRIVER_SETUP:-verify}"     # verify | auto(cuda 仓库) | skip(已确认驱动由镜像提供)
GPU_TEST_IMAGE="${GPU_TEST_IMAGE:-nvidia/cuda:12.4.1-base-ubuntu22.04}"
TOOLKIT_REPO_URL="${TOOLKIT_REPO_URL:-https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo}"

main() {
  log "=== 02 Docker + NVIDIA 容器运行时 开始$([ "$DRY_RUN" = 1 ] && echo ' [dry-run]') ==="
  if [ -f /etc/anolis-release ]; then log "系统: $(cat /etc/anolis-release)"; else warn "未见 /etc/anolis-release，非 Anolis 系统，继续但风险自负"; fi

  # ---------- A. Docker ----------
  if command -v docker >/dev/null 2>&1; then
    log "docker 已安装: $(docker --version)"
  elif [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] 将安装 docker-engine（anolis 仓库优先；失败回退 docker-ce，aliyun 源 centos/9）"
  else
    log "安装 docker-engine（anolis 仓库）…"
    if ! dnf_install docker-engine; then
      warn "anolis 仓库无 docker-engine，回退 docker-ce（aliyun 源）"
      dnf_install -y dnf-plugins-core
      cat >/etc/yum.repos.d/docker-ce.repo <<'EOF'
[docker-ce-stable]
name=Docker CE Stable
baseurl=https://mirrors.aliyun.com/docker-ce/linux/centos/9/x86_64/stable
enabled=1
gpgcheck=1
gpgkey=https://mirrors.aliyun.com/docker-ce/linux/centos/gpg
EOF
      dnf_install docker-ce docker-ce-cli containerd.io
    fi
    systemctl enable --now docker
  fi
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] 将确保 docker 服务 active（systemctl enable --now docker）"
  else
    systemctl is-active --quiet docker || die "docker 服务未运行"
  fi

  # 守护进程配置: 镜像加速 + 日志轮转（可逆，文件带日期备份）
  if [ ! -f /etc/docker/daemon.json ] || ! grep -q "gpumachine-managed" /etc/docker/daemon.json 2>/dev/null; then
    if [ "$DRY_RUN" = 1 ]; then
      # 干跑: 同一生成逻辑写到预览文件，验证 JSON 可生成、内容正确（不动 /etc/docker）
      export DOCKER_REGISTRY_MIRRORS DAEMON_JSON_TARGET="/tmp/daemon.json.preview"
      python3 - <<'PYEOF'
import json, os
mirrors = [m for m in os.environ.get("DOCKER_REGISTRY_MIRRORS", "").split() if m]
cfg = {}
p = os.environ.get("DAEMON_JSON_TARGET", "/tmp/daemon.json.preview")
if os.path.exists(p):
    try: cfg = json.load(open(p))
    except Exception: cfg = {}
cfg.setdefault("log-driver", "json-file")
cfg["log-opts"] = {"max-size": "50m", "max-file": "4"}
if mirrors: cfg["registry-mirrors"] = mirrors
cfg["#"] = "gpumachine-managed"
json.dump(cfg, open(p, "w"), indent=2)
print("[dry-run] daemon.json 生成逻辑验证 OK，预览:")
print(open(p).read())
PYEOF
      log "[dry-run] 真实执行将写入 /etc/docker/daemon.json 并 restart docker"
    else
      [ -f /etc/docker/daemon.json ] && cp -n /etc/docker/daemon.json /etc/docker/daemon.json.bak.$(date +%s) || true
      export DOCKER_REGISTRY_MIRRORS
      python3 - <<'PYEOF'
import json, os
mirrors = [m for m in os.environ.get("DOCKER_REGISTRY_MIRRORS", "").split() if m]
cfg = {}
p = "/etc/docker/daemon.json"
if os.path.exists(p):
    try: cfg = json.load(open(p))
    except Exception: cfg = {}
cfg.setdefault("log-driver", "json-file")
cfg["log-opts"] = {"max-size": "50m", "max-file": "4"}
if mirrors: cfg["registry-mirrors"] = mirrors
cfg["#"] = "gpumachine-managed"
json.dump(cfg, open(p, "w"), indent=2)
print("daemon.json written")
PYEOF
      systemctl restart docker
      log "docker daemon.json 已写入（镜像加速 ${DOCKER_REGISTRY_MIRRORS:-无} + 日志轮转）"
    fi
  fi

  # ---------- B. NVIDIA 驱动校验 ----------
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    log "NVIDIA 驱动 OK: $(nvidia-smi --query-gpu=name,driver_version,compute_cap --format=csv,noheader | head -1)"
  elif [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] nvidia-smi 不可用——真实执行将走 DRIVER_SETUP=$DRIVER_SETUP 分支（verify=显式失败转人工 / auto=cuda仓库 / skip=假定镜像提供驱动）"
  else
    case "$DRIVER_SETUP" in
      skip)    warn "DRIVER_SETUP=skip：假定驱动由镜像层提供，跳过校验（GPU 冒烟将验证真相）";;
      auto)    log "尝试从 CUDA 仓库安装驱动（需要重头编译时间与内核头，失败则转人工）"
               dnf_install kernel-devel-$(uname -r) gcc make || warn "内核头/编译链安装失败"
               warn "Anolis 23 无官方 NVIDIA 驱动包承诺——建议改用 .run 安装（NVIDIA-Linux-x86_64-580.173.02.run 与历史驱动同版本），见 RUNBOOK；本脚本不执行 .run";;
      *)       die "nvidia-smi 不可用且 DRIVER_SETUP=verify。处置: 重装系统选带 GPU 驱动镜像，或按 RUNBOOK 前置条件手工装驱动后重跑。";;
    esac
  fi

  # ---------- C. nvidia-container-toolkit ----------
  if command -v nvidia-ctk >/dev/null 2>&1; then
    log "nvidia-container-toolkit 已安装: $(nvidia-ctk --version | head -1)"
  elif [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] 将配置 libnvidia-container 仓库（$TOOLKIT_REPO_URL）并 dnf 安装 nvidia-container-toolkit；不可达时按 RUNBOOK 离线放 /opt/gpumachine/pkgs/"
  else
    log "配置 libnvidia-container 仓库…"
    if curl -fsS --max-time 20 "$TOOLKIT_REPO_URL" -o /etc/yum.repos.d/nvidia-container-toolkit.repo; then
      dnf_install nvidia-container-toolkit
    else
      warn "nvidia.github.io 不可达（境内网络常态）。请按 RUNBOOK 离线安装: 从可达机器下载 toolkit rpm 到 /opt/gpumachine/pkgs/ 后重跑"
      die "nvidia-container-toolkit 安装失败"
    fi
  fi
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] 将执行: nvidia-ctk runtime configure --runtime=docker-files && systemctl restart docker"
  else
    nvidia-ctk runtime configure --runtime=docker-files >/dev/null
    systemctl restart docker
    log "nvidia runtime 已注册并重启 docker"
  fi

  # ---------- D. GPU 容器冒烟 ----------
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] GPU 容器冒烟命令: docker run --rm --gpus all $GPU_TEST_IMAGE nvidia-smi（通过标准: 输出含 Tesla V100）"
    log "=== [dry-run] 02 逻辑干跑通过（未改动任何系统状态）==="
    exit 0
  fi
  log "GPU 容器冒烟（镜像 $GPU_TEST_IMAGE）…"
  if docker run --rm --gpus all "$GPU_TEST_IMAGE" nvidia-smi | tee /dev/stderr | grep -q "Tesla V100"; then
    log "=== GPU 容器冒烟通过（V100 可见于容器内）==="
  else
    die "GPU 容器冒烟未通过——检查驱动/容器运行时（日志: $_gp_log_file）"
  fi
  step_done 02-docker-nvidia
}

main "$@"
