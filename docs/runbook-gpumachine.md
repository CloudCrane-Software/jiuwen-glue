# RUNBOOK（公开镜像）— gpumachine 开通后零到一上线清单（WO-0009）

> **公开镜像说明**：本文为去私有化版本。完整可执行脚本、含占位地址的配置模板在
> 公司私有仓（CNB `company-ops` → `deploy/gpumachine/`）；本文占位符以 `<...>` 表示。
> 方案依据：PROP-0001 §1 / §2（OS 策略）/ §2.5 / §11（V100S sm_70 兼容性风险）/ §4.9。
> 工单依据：WO-0009"gpumachine 执行面准备（不变 + OS 转换动作 7）"。
>
> ⚠️ 说明：PROP-0001 v1.6 对 WO-0009 全文标注"不变（同 v1.5）"，v1.5 全文未存档；
> 本清单的验收映射基于 v1.6 可见条款（§2"动作 7"、§4.9 边界表、工单文本）。

目标机器：gpumachine（Tesla V100S-PCIE-32GB，sm_70，8 核 / 62G 内存 / 99G 盘），
系统重装为 **Anolis 23 / Agentic Edition**。配套决策：[ADR-0001 vLLM sm_70 兼容性](adr-0001-vllm-sm70.md)。

## P. 前置条件（开机前/开通时一次性确认）

| # | 前置条件 | 说明 |
| --- | --- | --- |
| P1 | 系统重装为 Anolis 23（Agentic Edition），**选带 NVIDIA 驱动的镜像** | WO-0009 动作 7（内核级组件随重装解锁）；驱动缺失则 02 脚本会停在驱动校验 |
| P2 | WireGuard 隧道：gpumachine=`<gpu-wg-addr>`，srv-1=`<srv-1-wg-addr>` | 所有脚本按 `config.env` 占位地址取值 |
| P3 | 克隆私有仓部署资产到机器 `/opt/gpumachine/deploy/` | `deploy/gpumachine/` 是执行面配置真相源 |
| P4 | srv-1 侧经隧道放行：Higress 8080 / Postgres 5432 / OpenBao 8200 | 三者当前只绑回环（WO-0001 `LISTEN_IP` 机制）；放行 + 团队库建库属 M1 WO-0003 联调 |
| P5 | OpenBao：写入模型入口 key 与团队库 DSN（路径 `<vault-path>`）；给机器发短期 worker token（0600，ttl≤1h） | worker policy 已由 WO-0005 建好（只读模型凭据）；token 续期在 M1 落地 |
| P6 | CNB 云构建绿色（已跑）：anolis-23 容器内 jiuwenswarm[distribute] 安装 + vLLM 依赖解析 | 结论见私有仓 `.cnb.yml` 头注释 |

## 执行序列（开通当日，按序）

```
P1 重装完成 → P2 隧道 → P3 克隆资产 → cp config.example.env config.env（按真机改）
→ 01 磁盘清理 → 02 docker+NVIDIA → 03 jiuwenswarm → 04 本地模型(按 ADR-0001) → 05 观测
```

| 步 | 动作 | 通过标准 | 失败处置 |
| --- | --- | --- | --- |
| S1 | 复制 `config.example.env` 为 `config.env` 并按真机修改 | 与 P2/P4/P5 一致 | — |
| S2 | `01-disk-cleanup.sh`：磁盘清到 <70%（白名单隔离策略） | 退出码 0 且根分区已用 <70% | 退出码 2 = 白名单清完仍不达标：按脚本输出的 top 分析**人工裁决**（脚本不自动扩权） |
| S3 | `02-docker-nvidia.sh`：docker + NVIDIA 容器运行时 | GPU 容器冒烟打印 `Tesla V100` | 驱动缺失 → 回 P1；toolkit 拉取失败 → 离线放 `/opt/gpumachine/pkgs/` 重跑 |
| S4 | `03-jiuwenswarm.sh`：`pip install jiuwenswarm[distribute]` + systemd | 安装成功 + import 通过 + `JIUWENSWARM_CONFIG_URL=off` 断言过 | Higress 预检不可达 = P4 未完成（预期内）；pip 失败 → 对照 CNB 流水线同 job 日志 |
| S5 | `04-local-model.sh`：MODE 按 ADR-0001 决策（默认候选老版 vLLM） | 冒烟 `/v1/models` + 一次补全；dtype=half | sm_70 被拒 → 按 ADR-0001 降级序（ollama → llama.cpp → 不自部署） |
| S6 | `05-telemetry.sh`：OTel 边缘缓冲 + AgentSight | collector health_check 通过 | agentsight 二进制未就位 = 显式 TODO（不用假容器充数） |
| S7 | M1 联调完成后启动服务 | AgentServer 端口监听 + 经 Higress 一次真实模型调用 | 查部署日志与 `journalctl -u jiuwenswarm-app` |

## 关键设计约束（脚本已内置断言）

- **执行面零长期密钥**：脚本零密钥明文；API key / 团队库 DSN 在服务启动时从 OpenBao
  拉进 tmpfs（0600）；worker token 短期（≤1h）。
- **模型端点唯一 = srv-1 Higress**：jiuwenswarm 只配一个 `API_BASE`；本地模型（04）
  只允许注册为 Higress 上游，禁止直接配进 jiuwenswarm（§4.9 #7）。
- **无华为通道出站**：`JIUWENSWARM_CONFIG_URL=off`（关官网远端配置=华为登录/免费模型通道）。
- **磁盘清理隔离**：只清白名单路径；保护区（工作区/数据卷/库目录）永不触碰。

## 验收条款映射（WO-0009）

| 条款（来源） | 对应动作 | 验收方法 | 状态 |
| --- | --- | --- | --- |
| 执行面转 Anolis+Agentic OS（动作 7，方案 §2） | P1 重装 | `cat /etc/anolis-release` 为 Anolis 23 | 开通后执行 |
| 磁盘清到 <70%（工单动作 1） | 01 脚本 | S2 退出码 0 | 开通后执行 |
| docker + NVIDIA 容器运行时（工单动作 1） | 02 脚本 | S3 GPU 冒烟见 `Tesla V100` | 开通后执行 |
| jiuwenswarm distribute、Postgres 指 srv-1 隧道占位、CONFIG_URL=off、端点指 Higress（工单动作 1） | 03 脚本 | S4 + 配置断言 | 安装层经 CNB anolis-23 验证；起服依赖 P4 |
| vLLM/agent-infer + sm_70 降级路径（工单动作 1） | 04 脚本 + ADR-0001 | S5 冒烟通过；ADR §6 回填 | ADR 决策留空（待真机实测） |
| 观测：OTel 边缘缓冲 + AgentSight（工单动作 1；§4.9 #6） | 05 脚本 | S6 health_check；端到端可见属 WO-0006 | 采集端代码就绪 |
| 资产入 CNB + GitHub 去私有化镜像（工单动作 4） | 本次提交 | 私有仓全量 + 本镜像 | 已完成 |
| 三条铁律失败用例（工单动作 5） | — | 不在本工单：留待胶水层工单（WO-0003） | N/A |
| Anolis 环境验证经 CNB 云构建 | `.cnb.yml` | 构建记录绿色 | 已通过（2026-09-26，详细结论在私有仓 `.cnb.yml` 头注释） |

## 红线

- 密钥值不进对话/日志/git/落盘文件；只在 OpenBao 与 tmpfs。
- 磁盘清理白名单之外路径一律人工裁决。
- 不得出现第二个模型端点；本地模型只能经 Higress 上游接入。
- 执行面不落长期密钥；自演进 `auto_save` 保持 false。
