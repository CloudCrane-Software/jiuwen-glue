#!/usr/bin/env bash
# 04-local-model.sh — gpumachine 本地模型部署（vLLM / agent-infer + sm_70 降级路径）（WO-0009 资产，脚本 4/5）
#
# 工单要点:
#   - vLLM / agent-infer 为主路径；sm_70（V100S, Volta）兼容性不确定时的降级路径:
#     ollama / llama.cpp / 老版 vLLM（V0 引擎末期版本）
#   - V100S 关键硬件事实（内部硬件手册实测记录）:
#       sm_70；fp16 可用 / bf16 不可用 → 一切推理只允许 dtype=half(fp16)
#   - 决策点唯一（方案 §4.9 #7）: 本脚本装出的本地端点 **不得** 直接配进 jiuwenswarm。
#     合规接入方式只有一个: 在 srv-1 Higress 把本地端点(经隧道 GPU_TUNNEL_ADDR:PORT)配为上游。
#   - sm_70 上 vLLM 哪个版本可用属于 ADR-0001 的实测决策——本脚本提供候选安装与冒烟，
#     决策栏留空，不在脚本里替 ADR 下结论。
set -Eeuo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SELF_DIR/lib.sh"
load_config
need_root

MODE="${LOCAL_MODEL_MODE:-vllm}"                 # vllm|agent-infer|ollama|llamacpp
VLLM_VER="${VLLM_PINNED_VERSION:-0.9.2}"         # 默认候选: V0 引擎末期主线（ADR-0001 候选 A，未实测）
MODEL_ID="${LOCAL_MODEL_ID:-Qwen/Qwen2.5-0.5B-Instruct}"
PORT="${LOCAL_MODEL_PORT:-8000}"
GPU_FRAC="${LOCAL_MODEL_GPU_FRAC:-0.30}"
BASE=/opt/gpumachine/local-model
PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"

# ---------- GPU 能力探测 ----------
gpu_cc() {
  nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1
}
check_gpu() {
  command -v nvidia-smi >/dev/null || die "nvidia-smi 不可用——先跑 02-docker-nvidia.sh 并确认驱动"
  local cc; cc="$(gpu_cc)"
  log "GPU: $(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader | head -1) / compute capability $cc"
  [ "$cc" = "7.0" ] && log "确认 V100（sm_70）——bf16 不可用，全部推理 dtype=half(fp16)"
  return 0
}

# vLLM 版本 gate: V1-only 版本(>=0.10)不支持 sm_70 是社区共识背景；这里按版本号硬拦截，
# 真正的版本判定以 ADR-0001 实测为准（本 gate 只是防误装，不是兼容性证明）。
vllm_version_ok_for_sm70() {
  local major minor
  IFS=. read -r major minor _ <<<"$1"
  [ "$major" -lt 10 ]
}

smoke_openai_endpoint() { # $1=url_base  冒烟: /v1/models + 一次补全
  local base="$1" rc=0
  curl -fsS --max-time 10 "$base/models" >/dev/null || { warn "$base/models 不可达"; return 1; }
  log "$base/models 可达"
  curl -fsS --max-time 60 "$base/chat/completions" -H 'Content-Type: application/json' \
    -d '{"model":"'"$MODEL_ID"'","messages":[{"role":"user","content":"只回答: pong"}],"max_tokens":8}' \
    | head -c 400 && echo || rc=$?
  return $rc
}

main() {
  log "=== 04 本地模型部署开始（MODE=$MODE）==="
  check_gpu
  mkdir -p "$BASE"

  case "$MODE" in

  vllm)
    if ! vllm_version_ok_for_sm70 "$VLLM_VER"; then
      die "vLLM $VLLM_VER 属 V1-only（>=0.10 不支持 sm_70）。降级: 配置 VLLM_PINNED_VERSION=<0.10 老版（见 ADR-0001 候选 A），或 MODE=ollama/llamacpp"
    fi
    local_venv="$BASE/vllm-venv"
    [ -d "$local_venv" ] || python3 -m venv "$local_venv"
    log "安装 vLLM $VLLM_VER（候选，非兼容性结论）…"
    PIP_INDEX_URL="$PIP_INDEX_URL" "$local_venv/bin/pip" install --upgrade pip
    PIP_INDEX_URL="$PIP_INDEX_URL" "$local_venv/bin/pip" install "vllm==$VLLM_VER" \
      || die "vllm==$VLLM_VER 安装失败（依赖解析问题可先在 CNB 流水线复现，见 .cnb.yml vllm-resolve job）"
    "$local_venv/bin/python" - <<'PYEOF'
import torch
print("torch:", torch.__version__)
print("cuda archs compiled:", torch.cuda.get_arch_list())
caps = torch.cuda.get_device_capability(0)
print("device cc:", caps)
assert caps == (7, 0), "不是 sm_70?!"
print("sm_70 in compiled archs:", any(a.startswith("7.0") or a == "sm_70" for a in torch.cuda.get_arch_list()))
PYEOF
    log "启动冒烟服务（ModelScope 源、dtype=half、显存占用 ${GPU_FRAC}、用完即停）…"
    export VLLM_USE_MODELSCOPE=1
    "$local_venv/bin/vllm" serve "$MODEL_ID" \
      --dtype half --max-model-len 4096 --gpu-memory-utilization "$GPU_FRAC" \
      --port "$PORT" >"$BASE/vllm-smoke.log" 2>&1 &
    local pid=$!
    local up=0
    for _ in $(seq 1 60); do
      sleep 5
      if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1; then up=1; break; fi
      kill -0 "$pid" 2>/dev/null || break
    done
    if [ "$up" = 1 ] && smoke_openai_endpoint "http://127.0.0.1:$PORT/v1"; then
      log "vLLM($VLLM_VER) sm_70 冒烟通过——把结果记入 docs/adr-0001-vllm-sm70.md 实测记录"
    else
      warn "vLLM($VLLM_VER) 冒烟失败（日志 $BASE/vllm-smoke.log）——按 ADR-0001 降级候选处置"
    fi
    kill "$pid" 2>/dev/null || true
    ;;

  agent-infer)
    log "安装 openJiuwen 独立仓 agent-infer（vLLM 推理服务仓）…"
    warn "注意: agent-infer 的 vLLM 版本 pin 未在本期审计（工作区未克隆该仓）；若其 pin>=0.10 则在 sm_70 上不可用，回落 MODE=vllm/ollama"
    mkdir -p "$BASE"
    if [ ! -d "$BASE/agent-infer" ]; then
      git clone --depth 1 https://github.com/openJiuwen-ai/agent-infer.git "$BASE/agent-infer" \
        || git clone --depth 1 https://gitcode.com/openJiuwen/agent-infer.git "$BASE/agent-infer" \
        || die "agent-infer 克隆失败（github/gitcode 均不可达，检查 GITHUB_DL_MIRROR）"
    fi
    grep -rn "vllm" "$BASE/agent-infer"/pyproject.toml "$BASE/agent-infer"/requirements*.txt 2>/dev/null | head -5 \
      || warn "未在 agent-infer 清单中找到 vllm pin，人工核对"
    [ -d "$BASE/agent-infer-venv" ] || python3 -m venv "$BASE/agent-infer-venv"
    PIP_INDEX_URL="$PIP_INDEX_URL" "$BASE/agent-infer-venv/bin/pip" install -e "$BASE/agent-infer" \
      || die "agent-infer 安装失败"
    log "agent-infer 安装完成（服务启动参数以仓内 README 为准；sm_70 冒烟同 vllm 分支步骤）"
    ;;

  ollama)
    log "安装 ollama（降级路径 B）…"
    if ! command -v ollama >/dev/null 2>&1; then
      if [ -n "${GITHUB_DL_MIRROR:-}" ]; then
        curl -fsSL "${GITHUB_DL_MIRROR%/}/https://ollama.com/install.sh" | OLLAMA_VERSION= sh
      else
        curl -fsSL https://ollama.com/install.sh | sh
      fi
    fi
    systemctl enable --now ollama || (ollama serve >"$BASE/ollama.log" 2>&1 &)
    sleep 3
    ollama pull qwen2.5:0.5b || die "ollama 拉模型失败"
    log "ollama OpenAI 兼容端点: http://127.0.0.1:11434/v1（注册为 Higress 上游时用隧道地址）"
    curl -fsS --max-time 5 http://127.0.0.1:11434/v1/models >/dev/null && log "ollama /v1/models 冒烟通过"
    ;;

  llamacpp)
    log "安装 llama.cpp server（降级路径 C）…"
    if ! command -v nvcc >/dev/null 2>&1; then
      warn "机器无 CUDA toolkit（历史事实: nvcc 不在 PATH）。CUDA 版 llama.cpp 需先装 toolkit(~3G) 或用 CPU 版（吞吐低）。"
      warn "继续 CPU 版构建作为链路验证；若需 CUDA 版，先装 toolkit 再重跑本脚本。"
      dnf_install gcc-c++ make git cmake
      [ -d "$BASE/llama.cpp" ] || git clone --depth 1 https://github.com/ggml-org/llama.cpp "$BASE/llama.cpp" \
        || die "llama.cpp 克隆失败"
      cmake -S "$BASE/llama.cpp" -B "$BASE/llama.cpp/build" -DGGML_CUDA=OFF
      cmake --build "$BASE/llama.cpp/build" --target llama-server -j"$(nproc)"
    else
      dnf_install gcc-c++ make git cmake
      [ -d "$BASE/llama.cpp" ] || git clone --depth 1 https://github.com/ggml-org/llama.cpp "$BASE/llama.cpp"
      cmake -S "$BASE/llama.cpp" -B "$BASE/llama.cpp/build" -DGGML_CUDA=ON
      cmake --build "$BASE/llama.cpp/build" --target llama-server -j"$(nproc)"
    fi
    log "llama-server 构建完成: $BASE/llama.cpp/build/bin/llama-server（gguf 模型自行下载后启动，端点 OpenAI 兼容）"
    ;;

  *)
    die "未知 MODE=$MODE（可用: vllm|agent-infer|ollama|llamacpp）"
    ;;
  esac

  cat >"$BASE/README-endpoint.txt" <<EOF
本地模型端点接入规则（方案 §4.9 #7，决策点唯一）:
  1. 本地端点只在 srv-1 Higress 配为上游（经隧道: ${GPU_TUNNEL_ADDR:-<gpu-wg-addr>}:$PORT）；
  2. jiuwenswarm 的 API_BASE 永远只指向 Higress——不得新增第二个模型端点；
  3. 变更走 CNB company-ops PR + Higress 控制台 API（运行时状态）。
EOF
  log "=== 04 本地模型部署完成（端点接入规则见 $BASE/README-endpoint.txt）==="
}

main "$@"
