# deploy/gpumachine — 执行面一键部署（WO-0009 资产）

> **公开镜像说明**：本目录为公司私有仓（CNB `company-ops` → `deploy/gpumachine/`）的
> 去私有化镜像（2026-09-26 起），真相源在私有仓；占位地址以 `<...>` 表示，真机执行时在
> `config.env` 按实际隧道/服务地址填写。

> 机器：gpumachine（Tesla V100S-PCIE-32GB，sm_70，8C/62G/99G；系统将重装为 Anolis 23 / Agentic Edition）。
> 状态：**代码就绪，机器未开通**（所有者裁定：本期不开机，全部做成代码，开通后按 `RUNBOOK.md` 直接上手）。
> 方案依据：PROP-0001 §1（资源拓扑）、§2（OS 策略：Agentic OS 只装在跑 agent 的机器上）、§2.5（执行面）、§4.9（边界总表）。

## 执行顺序（开通后）

```
cp config.example.env config.env   # 按真机改（隧道地址、模型名等）
bash 01-disk-cleanup.sh            # 磁盘清到 <70%（白名单隔离策略）
bash 02-docker-nvidia.sh           # docker + NVIDIA 容器运行时（GPU 冒烟必须过）
bash 03-jiuwenswarm.sh             # jiuwenswarm[distribute]（模型端点=Higress 唯一入口）
bash 04-local-model.sh             # 本地模型（vLLM/agent-infer + sm_70 降级路径）——按 ADR-0001 决策执行
bash 05-telemetry.sh               # OTel 边缘缓冲 + AgentSight
```

每个脚本幂等可重跑；日志 `/var/log/gpumachine-deploy/`；步骤状态 `/var/lib/gpumachine/steps/`。

## 边界纪律（写死）

| 纪律 | 落实位置 |
| --- | --- |
| **执行面零长期密钥**（§0.5） | 脚本零密钥明文；API key/团队库 DSN 由 systemd `gpumachine-bao-env`（03）在启动时从 OpenBao 拉进 tmpfs（`/run/jiuwenswarm/secrets.env`，0600）；worker token 短期（≤1h，WO-0005 role） |
| **模型端点唯一 = srv-1 Higress**（§4.9 #7） | 03 只写一个 `API_BASE`（隧道占位 <srv-1-wg-addr>:8080/v1）并有端点数量断言；本地模型（04）只允许注册为 Higress 上游，禁止直接配进 jiuwenswarm |
| **无华为通道出站**（WO-0003 验收） | `JIUWENSWARM_CONFIG_URL=off`（关官网远端配置=华为登录/免费模型通道），03 有断言 |
| **配置变更走 PR** | 本目录是实例配置真相源；改任何配置提交 CNB company-ops PR，运行时状态走 API/控制台 |

## 磁盘清理隔离策略（01）

- 只清**白名单路径**（包缓存/journal/轮转日志/语言包缓存/临时目录），路径级守卫函数强制校验；
- 永不触碰 `/root/workspace`、`/opt/company`、`/var/lib/postgresql`、docker volumes、`/opt/gpumachine`；
- docker 只清 build cache 与 dangling 层（禁 `-a`/`--volumes`）；
- 白名单清完仍 ≥70% → 输出 top 消耗分析后**退出码 2**（人工裁决，不自动扩权）。

## OpenBao 引用约定（密钥只在 bao）

| 变量 | bao 路径（WO-0005 布局 `<vault-path>`） | 用途 |
| --- | --- | --- |
| `BAO_ADDR` | — | 隧道内 bao 地址（占位 <srv-1-wg-addr>:8200，暴露方式 M1 定案） |
| `BAO_TOKEN_FILE` | 文件 `/etc/gpumachine/bao-worker-token` | 短期 worker token（≤1h） |
| `BAO_PATH_HIGRESS_KEY` | `<vault-path>#key` | Higress consumer key（模型入口鉴权） |
| `BAO_PATH_PG_URL` | `<vault-path>#dsn` | 团队库 DSN（Postgres 在 srv-1） |

> 路径是约定占位：开通时若 WO-0005 实际布局不同，改 `config.env` 即可，脚本不写死。

## 与其他工单的依赖关系

- **隧道（WireGuard）**：占位 `<srv-1-wg-addr>`（srv-1）/`<gpu-wg-addr>`（本机）；隧道建立属开通后动作，联调属 M1 WO-0003。
- **srv-1 侧放行**：Higress gateway(8080)/pg(5432)/bao(8200) 当前只绑回环（WO-0001 `LISTEN_IP` 机制），隧道暴露在 M1 收尾。
- **srv-1 Collector**：边缘 Collector 的前送目标（占位 <srv-1-wg-addr>:4317），其部署属 WO-0006。
- **Anolis/Agentic OS**：系统重装（WO-0009 动作 7，内核级组件解锁）是开机后第一步，见 RUNBOOK。

## Anolis 23 验证（CNB 云构建，2026-09-26 实测通过）

`.cnb.yml`（CNB 云构建）在 `openanolis/anolisos:23` 容器里验证两件事：
1. `jiuwenswarm[distribute]` pip 安装（Anolis 23 + Python 3.12.13）：**通过**（601s）——
   import OK，entry_points（jiuwenswarm-app 等）断言通过，PyPI 包名 `jiuwenswarm` 可安装；
2. vLLM 候选版本依赖解析：**通过**（35s，uv 元数据级解析）——
   `vllm==0.9.2` → `torch==2.7.0` + `xformers==0.0.30`（V0 世代栈）；
   最新 `vllm==0.29.0` 亦可解析（运行时 sm_70 兼容性不在 CI 范围）。

结论与失败序列（三次失败→修复→成功的完整记录）见 `.cnb.yml` 头注释。
GPU 真机推理冒烟只能在机器开通后执行（ADR-0001 第 4 节 / RUNBOOK S5）。
