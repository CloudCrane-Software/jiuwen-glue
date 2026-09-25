# ADR-0001 — vLLM 在 V100S（sm_70）上的本地推理兼容性决策

> 公开镜像说明：本文与公司私有仓（CNB `company-ops` → `docs/`）同源；私有仓内另有可执行的
> 部署脚本（`deploy/gpumachine/`）。本文只含公开硬件事实与开源软件版本事实，无内网拓扑。

| | |
| --- | --- |
| 状态 | **Proposed（决策栏留空，待真机实测）** |
| 工单 | WO-0009（gpumachine 执行面准备·资产化） |
| 关联 | 方案 PROP-0001 §1（gpumachine=V100S-32G）、§2.5、§11（风险登记册：V100S sm_70 兼容性风险）、§4.9 #7（模型路由唯一入口） |
| 日期 | 2026-09-26（骨架就绪）· 实测：待 GPU 机开通 |

---

## 1. 背景

- 执行面 gpumachine 配 **Tesla V100S-PCIE-32GB**：compute capability **7.0（sm_70, Volta）**。
  已实测的硬件事实（内部硬件手册实测记录，2026-09-07 冒烟）：
  **fp16 可用 / bf16 不可用**；驱动 580.173.02；无 CUDA toolkit（nvcc 不在 PATH）；
  torch 2.4.1+cu121 轮子在 sm_70 上 `torch.cuda.is_available()=True`（matmul ≈4.2 TFLOPS fp32）。
- 方案要求本地模型能力上机（§1"本地模型（一律经 Higress）"、§4.5 承载矩阵），openJiuwen
  独立仓 `agent-infer` 以 vLLM 为推理底座（组织仓清单，2026-09-24 审计）。
- vLLM 新引擎（V1）以 **compute capability ≥ 8.0** 为支持门槛（社区公开路线；V0 旧引擎
  支持 sm_70 一线的最后版本区段 **待本 ADR 实测确认**）。因此"装最新版 vLLM"在 V100S 上
  大概率不可用，必须做版本选择决策。
- 本 ADR 不在文档阶段下结论：**开通后按第 4 节实测步骤执行，把证据填进第 6 节，再做决策**。

## 2. 候选方案

| # | 方案 | 描述 | 优点 | 代价/风险 |
| --- | --- | --- | --- | --- |
| A | **老版 vLLM**（V0 引擎末期，候选 pin `0.9.x`，`--dtype half`） | 钉住最后支持 sm_70 的主线版本 | 原生 OpenAI 兼容、吞吐与批处理最好、与 agent-infer 技术栈同源 | 版本老化（安全补丁停更）；轮子内编译 arch 清单需实测（`torch.cuda.get_arch_list()`） |
| B | **ollama** | GGML/llama.cpp 内核的模型管理器，官方支持 V100 | 安装即用、模型管理省事、自带 OpenAI 兼容端点 | 推理吞吐低于 vLLM；量化优先（fp16 大模型 32G 装不下几只）；社区版更新快需钉版 |
| C | **llama.cpp server** | 自建 CUDA 版 llama-server（OpenAI 兼容） | 可控性强、显存占用最小 | 机器无 CUDA toolkit——CUDA 版需先装 toolkit(~3G) 或编译 CPU 版（吞吐差一个量级） |
| D | **agent-infer 原样部署** | 直接用 openJiuwen 独立仓 agent-infer | 与全家桶同源 | 其 vLLM 版本 pin 未审计；若 pin≥0.10 则在 sm_70 上不可用，仅当其依赖可下调时可选 |
| E | **本期不自部署**（全走 Higress 云上游：百炼/千问免费额度/stepfun） | 本地模型延后 | 零风险、零维护 | 离线/私有推理能力缺席；方案"本地模型经 Higress"目标未落地 |

> 约束（全部硬性）：① dtype 只允许 **half（fp16）**，bf16 硬件不可用；② 模型端点唯一——
> 无论如何选择，本地端点只能作为 **srv-1 Higress 的上游**，禁止直接配进 jiuwenswarm；
> ③ 机器 99G 盘、62G 内存、共享使用纪律（显存先看余量）。

## 3. 决策（留空）

> **Decision: （待实测后填写）**
> 候选选定：＿＿＿＿；钉版版本：＿＿＿＿；理由与实测证据引用：＿＿＿＿。
> 决策人/日期：＿＿＿＿。

## 4. 实测步骤（开通后执行，逐条记录证据到第 6 节）

### 4.0 前置
- [ ] T0 系统与驱动基线：`cat /etc/anolis-release`、`nvidia-smi`（V100S + 驱动版本 + compute_cap=7.0）、`nvcc --version`（预期无）、`df -h`。
- [ ] T1 磁盘/显存基线：`nvidia-smi --query-gpu=memory.total,memory.used --format=csv`（共享纪律：别人占用记档）。

### 4.1 PyTorch/vLLM 轮子层验证（无 GPU 环境也可做一部分）
- [ ] T2 `torch.cuda.get_arch_list()`：确认所选 torch 轮子编译目标含 sm_70（`compute_70/sm_70`）。
      命令：`python -c "import torch; print(torch.__version__, torch.cuda.get_arch_list())"`。
- [ ] T3 vLLM 候选版本依赖解析：`pip install --dry-run --report /tmp/r.json "vllm==<ver>"`
      （CNB 流水线 job `vllm-resolve` 已做解析层验证；本步在真机 venv 完成正式安装）。

### 4.2 vLLM 真机冒烟（候选 A）
- [ ] T4 安装：`pip install "vllm==<0.9.x pin>"`（04-local-model.sh MODE=vllm）。
- [ ] T5 引擎判定：启动日志确认使用 V0 引擎（或 `VLLM_USE_V1=0` 显式回退），无 "compute capability 7.0 not supported" 致命错。
- [ ] T6 精度约束：`--dtype half` 启动成功；故意 `--dtype bfloat16` 应失败（复核硬件事实）。
- [ ] T7 冒烟请求：`curl /v1/models` + 一次 chat completion（冒烟模型 `Qwen/Qwen2.5-0.5B-Instruct`，`--gpu-memory-utilization 0.3`）。

### 4.3 对照组（降级路径实测）
- [ ] T8 ollama：`MODE=ollama` 安装 + `qwen2.5:0.5b` 冒烟；记录 `/v1` 端点可用性。
- [ ] T9 llama.cpp：`MODE=llamacpp`（无 toolkit 时 CPU 版仅链路验证；若 A/B 全败再评估装 toolkit）。
- [ ] T10 agent-infer：克隆仓审计 `pyproject/requirements` 中 vllm pin；可下调则按 T4~T7 复测。

### 4.4 决策级基准（若 A 与 B 均可用，用数据说话）
- [ ] T11 吞吐/延迟：同一模型（小模型冒烟 + 目标生产模型）以 8 并发×100 请求压测，
      记录 tokens/s、p50/p95 首 token 延迟、显存峰值。
- [ ] T12 稳定性：连续 24h 低负载运行无 OOM/无泄漏（`nvidia-smi` 曲线留档）。
- [ ] T13 Higress 上游接入验证：本地端点注册为 srv-1 Higress 上游（经隧道），从 jiuwenswarm
      走 Higress 完成一次真实调用（证明"端点唯一"纪律可执行）。

## 5. 影响面

- `deploy/gpumachine/04-local-model.sh`：决策版本的 pin（`VLLM_PINNED_VERSION`）与默认 MODE。
- `deploy/gpumachine/config.example.env`：候选版本注释更新。
- RUNBOOK 对应步骤的验收条款（本地模型段）。
- M2 eval-gate / agent_evolving 的推理底座选型输入（L2 oracle 差分测试跑在哪）。

## 6. 实测记录（开通后回填）

| 项 | 结果 | 证据（命令/日志摘要） | 日期 |
| --- | --- | --- | --- |
| T0 驱动基线 | 待测 | | |
| T2 torch arch list 含 sm_70 | 待测 | | |
| T5 vLLM 引擎判定 | 待测 | | |
| T7 vLLM 冒烟 | 待测 | | |
| T8 ollama 冒烟 | 待测 | | |
| T11 压测对比 | 待测 | | |
| T12 24h 稳定性 | 待测 | | |
| T13 Higress 上游接入 | 待测 | | |

## 7. 参考

- openJiuwen 组织仓清单（agent-infer：vLLM 推理；2026-09-24 审计）。
- jiuwenswarm 官方部署文档（`pip install jiuwenswarm`；模型配置 `MODEL_NAME/API_BASE/API_KEY/MODEL_PROVIDER`）。
- vLLM 公开发布说明（V1 引擎算力门槛、V0 退役时间线——**具体版本以实测 T4/T5 为准**）。
- V100S 硬件实测（fp16✓/bf16✗、无 nvcc、驱动 580.x）：内部硬件手册记录。
