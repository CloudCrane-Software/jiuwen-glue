# openJiuwen 沙箱与 E2B 兼容 API 的差距报告（WO-0003 动作 3）

- 日期：2026-09-26
- 依据：PROP-0001《建设方案-多Agent系统与GitOps》v1.6 第 4.9 节 #14（沙箱：openJiuwen 沙箱为主；WO-0003 按研发手册四边界验收 + OpenSandbox 对照，不预选定）与 WO-0003 动作 10；最高原则"协议兼容 ≠ 能力等价"。
- 对照基准：E2B 公开 SDK（Python/JS v1.x）API 面。
- openJiuwen 源码版本：本机工作区克隆 `repos/openjiuwen/`（下文所有 `路径:行号` 均以该目录为根，行号为 2026-09-26 实测）。

---

## 1. 结论（TL;DR）

1. **openJiuwen 沙箱不是 E2B 的实现，也不自带 E2B 兼容 API**。它是一套"操作协议抽象 + 可插拔 Provider + 生命周期网关"的架构（5 层），沙箱底座通过 Provider 外接（AIO / JiuwenBox / Yuanrong 三个现成 Provider）。
2. **能力子集（fs/shell/code）在语义上与 E2B 的 `files.*` / `commands.*` 高度同构**，AIO Provider 借助 `agent_sandbox` SDK（AIO Sandbox，agent-infra 系）接入——这是目前离"E2B 兼容 API"最近的一条路，但代码执行走的是 shell 子进程 shim（`python -c`/`node -e` + base64），**不是** E2B 的 Jupyter 内核语义（无富输出/无内核态/无 cell context）。
3. **主要差距**集中在：代码执行内核语义、模板/镜像构建管线、进程管理 API、重连与跨会话取回（connect by id）、配额计量与多租户、网络出口治理。这些差距与"协议兼容 ≠ 能力等价"的判据一致：接口形似不等于强制边界、审计与恢复语义齐备。
4. 对本方案的影响：**维持 4.9 #14 的裁定——openJiuwen 沙箱为主，不预选定 OpenSandbox/E2B 供应商**。若需要对外提供 E2B 兼容数据面（例如把 gpumachine 沙箱暴露给 Qwen Code/百炼生态的工具链），正确位置是**在 Provider 层新增一个 E2B 兼容 Provider 或用 OpenSandbox 承接**，而不是改 glue；glue 不新增沙箱决策点。

---

## 2. openJiuwen 沙箱实现盘点（代码级）

### 2.1 分层架构（`agent-core/docs/en/2.Development Guide/Advanced Usage/Sandbox.md`）

官方文档给出 5 层：Operation（用户接口）→ Protocol（最小接口集 `BaseFsProtocol/BaseShellProtocol/BaseCodeProtocol`）→ Gateway（单例、创建/路由/生命周期）→ Launcher（启动/暂停/恢复/删除）→ Provider（对接具体沙箱 API）。

### 2.2 关键模块与代码事实

| 事实 | 位置（相对 `repos/openjiuwen/`） |
| --- | --- |
| Provider 注册表：`SandboxRegistry`，类级字典 `_launchers` / `_operations[(sandbox_type, operation_type)]`，装饰器 `@SandboxRegistry.provider("aio","fs")` 等 | `agent-core/openjiuwen/core/sys_operation/sandbox/sandbox_registry.py:11-80` |
| 三个内置 Provider 各注册 fs/shell/code 三件套：`aio`（aio.py:199/814/960）、`jiuwenbox`（jiuwenbox.py:2152/2519/2887）、`yuanrong`（yuanrong.py:538/883/973） | `agent-core/openjiuwen/extensions/sys_operation/sandbox/providers/` |
| AIO Provider 复用外部 SDK：`from agent_sandbox import Sandbox`（延迟导入，`Sandbox(base_url=...)`） | `.../providers/aio.py:208,823,974` |
| AIO 代码执行是 shell shim：base64 编码后 `python -c` / `node -e`（或落临时文件执行），仅支持 python/javascript | `.../providers/aio.py:980-1023`（`_build_code_command`）、`aio.py:1007`（`execute_code`） |
| JiuwenBox 是华为自研 HTTP 沙箱守护：REST `/api/v1/sandboxes`（POST 创建，GET 查询/文件/搜索），Bearer 认证 `JIUWENBOX_API_TOKEN`，支持 TCP 与 UDS 两种端点 | `.../providers/jiuwenbox.py:306,322-380,392,422,454,541,555` |
| Gateway 单例 + 端点解析 + Provider 缓存；`handle_request/handle_stream_request` 全链路路由；`_get_endpoint` 处理 RUNNING/PAUSED/丢失重建；`release_sandbox(on_stop=keep/pause/delete)`；空闲驱逐 `_evict_idle(idle_ttl_seconds)` | `agent-core/openjiuwen/core/sys_operation/sandbox/gateway/gateway.py:41-48,53,84,158,174,178,192,278` |
| 隔离粒度 `isolation_key`：`{container_scope}_{launcher_type}_{sandbox_type}_{prefix}_{session_id|custom}`，SYSTEM/SESSION/CUSTOM 三档（`ContainerScope`） | `agent-core/openjiuwen/core/sys_operation/sys_operation.py:22-70`；`config.py:43` |
| 本地/沙箱双实现：`OperationMode.LOCAL/SANDBOX`；本地模式以 `LocalWorkConfig.sandbox_root` 做路径白名单（"fs operations reject paths outside every sandbox_root entry"） | `sys_operation.py:83,111`；`config.py:10-34` |
| Launcher 抽象：`SandboxLauncher.launch/pause/resume/delete/check_status`；内置唯一实现 `PreDeploymentLauncher`（连接"已启动"的沙箱，不负责拉起容器） | `.../sandbox/launchers/base.py:33-60`；`.../launchers/pre_deployment_launcher.py:7-14`；`gateway.py:60-64`（只注册 pre_deploy） |
| 协议层 fs 接口很富：`read_file` 支持 text/bytes、head/tail/line_range、分块流；`write_file` 支持 append/权限/编码；upload/download 分块流 | `.../sandbox/providers/base_provider.py:27-80` |
| jiuwenswarm 服务端：`JiuwenBoxRunner` 以 uvicorn 子进程方式管理本地 `jiuwenbox.server.app`（默认 `http://127.0.0.1:8321`，端口冲突则换随机空闲口），带 `prctl PR_SET_PDEATHSIG` 与 atexit 清理；ws 服务器经 `/sandbox enable` 触发 | `jiuwenswarm/jiuwenswarm/server/sandbox/jiuwenbox_runner.py:1-105`；`server/agent_ws_server.py:116-148` |
| Agent 侧封装：`SysOperationRail`（harness rail，把 fs/shell/code 工具注册给 DeepAgent） | `agent-core/openjiuwen/harness/rails/sys_operation_rail.py:15-40` |

### 2.3 官方文档的自述边界

`Sandbox.md`（en 版）明确："**The current version supports connecting to already-started AIO sandboxes.** For local execution, Docker or lightweight sandboxes can be used; for large-scale remote/production deployment, cloud services like Kubernetes (K8s) are recommended."——即上游自己承认当前不内置"拉起并编排沙箱底座"的完整能力，生产形态依赖外部编排。

---

## 3. E2B API 基准

基准取 E2B 官方文档（docs.e2b.dev）与 e2b Python/JS SDK v1.x 公开 API 面（2026-09-26 经 web 检索复核，命中官方示例 `sandbox.files.write(...)`、`sandbox.commands.run(...)` 与 `create/connect/pause/resume/kill` 生命周期）：

- 生命周期：`Sandbox.create()`（按模板建微VM）、`Sandbox.connect(sandbox_id)`（重连既有沙箱）、`sandbox.setTimeout()`、`sandbox.pause()` / `resume()`（快照态持久化）、`sandbox.kill()`。
- 文件：`sandbox.files.read/write/list`（v1.x 还有 rename/make_dir/remove 等扩展）。
- 进程/命令：`sandbox.commands.run(cmd, envs=, cwd=, timeout=, on_stdout/on_stderr)` → `CommandResult{stdout, stderr, exit_code}`；`sandbox.commands.list`；`sandbox.processes.list/kill`。
- 代码执行：Jupyter 内核语义（`run_code` / code context），支持富 MIME 输出（图/表）、增量执行与变量驻留。
- 模板：自定义 Dockerfile → `Template` 构建 → 毫秒级从模板创建沙箱；公开模板仓库。
- 底座：Firecracker microVM；官方云托管，亦可自托管（`e2b infra`）。
- 认证：平台级 access token / 沙箱级签名 URL（`getHostPort` / connect URL）。

> 声明：上表生命周期/文件/命令三组在 docs.e2b.dev 命中的官方示例中有直接印证；代码执行内核语义、模板管线、Firecracker 底座为 SDK v1.x 公开资料的既有知识，本次未逐字段复核——引用时请以 docs.e2b.dev 对应页为准。

---

## 4. 逐项差距表

判定口径：**等价** = 语义可直接对上；**部分** = 有对应物但语义/覆盖有缺口；**缺失** = openJiuwen 侧无对应实现；**上游** = 依赖所选沙箱底座（Provider 外部）。

| # | 能力 | openJiuwen 现状 | E2B 基准 | 判定 | 差距与影响 |
| --- | --- | --- | --- | --- | --- |
| 1 | 文件读写 | 协议层 `read_file/write_file/list_files/search/upload/download`，支持 head/tail/line_range、分块流、权限（base_provider.py:27-80） | `files.read/write/list` 简单平面 API | **openJiuwen 更强** | 反向差距；若做 E2B 兼容 Provider 只需投影子集，无能力缺口 |
| 2 | Shell 执行 | `execute_cmd`（cwd/timeout/environment）+ 流式（aio.py:849,927） | `commands.run` + 流式回调 | **等价** | 形态一致（退出码/stdout/stderr） |
| 3 | 代码执行 | shell shim：base64 + `python -c`/`node -e`，仅 python/js，无富输出（aio.py:980-1023） | Jupyter 内核 + 富 MIME 输出 + 变量驻留 | **部分（语义差）** | "协议兼容 ≠ 能力等价"的典型点：plot 输出、增量 cell、内核异常恢复均缺失；需要内核语义时 AIO 底座自身有 code API 但 Provider 未接 |
| 4 | 生命周期 create/kill | Gateway `_create_new_sandbox`（经 Launcher）；`release_sandbox(on_stop=delete)`（gateway.py:192,254,158） | `Sandbox.create/kill` | **部分** | 内置 Launcher 仅 `pre_deploy`（连接已启动沙箱，pre_deployment_launcher.py:7-14），**不负责从镜像拉起沙箱**——创建语义依赖外部预部署 |
| 5 | pause/resume | Launcher 协议有 `pause/resume`，Gateway 有 PAUSED→resume 路径（gateway.py:174,192 段内） | `pause/resume`（快照持久化） | **部分** | 接口形状在，但真实暂停/恢复取决于底座；PreDeploymentLauncher 对这些是 no-op（base.py:36 注明） |
| 6 | 重连（connect by id） | Provider 缓存 + `isolation_key` 复用 + `JIUWENBOX_SANDBOX_ID` 环境变量复用（jiuwenbox.py:639,864）；无跨进程通用 connect API | `Sandbox.connect(sandbox_id)` 通用重连 | **缺失** | 进程重启后无法凭 ID 取回任意沙箱；E2B 语义下可恢复会话 |
| 7 | 进程管理 | 无进程列举/杀死 API（仅 shell 层面 + 本地 `shell_process_registry`） | `processes.list/kill` | **缺失** | 长驻任务治理（查/杀）缺接口 |
| 8 | 隔离模型 | `isolation_key` 三档 SYSTEM/SESSION/CUSTOM + JiuwenBox 策略文件/policy（jiuwenbox_runner.py docstring）+ 本地 sandbox_root 白名单（config.py:23-34） | 一沙箱一 microVM（硬件级隔离）+ 模板 | **等价性存疑（模型不同）** | openJiuwen 是"复用边界"治理，隔离强度取决于底座（容器 vs microVM）；安全评审必须看底座而非框架 |
| 9 | 模板/镜像管线 | 无（`container_config_hash` 只用于配置指纹，gateway.py:20-40） | Dockerfile→Template→秒级创建 | **缺失（上游可补）** | 环境即代码的"构建-分发-缓存"整段缺失；生产化时需 OpenSandbox/K8s 侧补 |
| 10 | 计量/配额 | 无沙箱内用量计量（方案中预算由 glue Budget Lease 承担，但沙箱资源用量无采集） | 平台级用量/时长计量 | **缺失** | 与 glue 的 Budget Lease 之间目前没有"沙箱实际消耗 → 租约占用"的自动回路（WO-0006 观测管道可补） |
| 11 | 网络出口治理 | JiuwenBox 有 policy 文件注入（JIUWENBOX_POLICY_PATH，jiuwenbox_runner.py:11-13）；AIO/通用层无出口白名单 API | 官方云默认受控出口 | **部分（绑定底座）** | 本方案"无华为通道出站"与 Higress 唯一入口的核对点在底座层，不能指望框架层 |
| 12 | 认证/多租户 | JiuwenBox Bearer token（jiuwenbox.py:306-315）；AIO 无鉴权层（base_url 直连） | 平台 token + 沙箱签名 URL | **部分** | AIO Provider 明文 base_url 直连，仅适合内网；多租户计量/审计缺 |
| 13 | 流式输出 | `execute_cmd_stream` / 分块 fs 流（协议层齐备） | commands 回调 + code 输出流 | **等价** | — |
| 14 | 服务端运维面 | jiuwenswarm `JiuwenBoxRunner`（本地 uvicorn 子进程、健康检查、PDEATHSIG、atexit）（jiuwenbox_runner.py:43-105） | 云托管托管面（自托管需自建 infra） | **部分** | 单机守护进程级；无集群调度/镜像分发 |

---

## 5. 对本方案的具体建议（不新增决策点）

1. **维持原生优先**：fs/shell 语义已覆盖 M1 需求（WO-0003 验收的"跨机双任务"不依赖 E2B 语义）；代码执行内核语义若有需求（如评测需要富输出），优先检查 AIO 底座自身的 code API 并在 `AIOCodeProvider` 内接入，而不是引入第二套沙箱框架。
2. **若必须对外提供 E2B 兼容数据面**（把 gpumachine 沙箱接入百炼/Qwen Code 生态的 E2B 客户端）：在 `SandboxRegistry` 注册一个新的 `e2b_compat` Provider（实现 BaseFs/Shel/Code 三协议、投影到 E2B API 面），这是 openJiuwen 预留的标准扩展点（sandbox_registry.py:44-80），**不需要动 glue**。
3. **沙箱消耗 → Budget Lease 回路**：建议在 WO-0006（观测管道）里把沙箱时长/用量作为 span 属性回流，由 glue 的 `BudgetLedger.acquire()` 落账——本报告第 4 节 #10 的缺口才有闭环。
4. **验收提醒**：方案 WO-0003 动作 10 要求"沙箱四边界核对"（研发手册）+ OpenSandbox 对照。四边界原文出自《AI Native 研发范式实践手册》，本工作区仅有扫描版 PDF（`raw/ai-native-handbook/ai-native-handbook.pdf`，无文本层，无法程序化摘引），**本报告未覆盖四边界逐条核对**——待该手册可引文本或人工判读后补一份对照附录，OpenSandbox 对照同理（4.9 #14 明确"不预选定"，本报告不做选型结论）。

## 6. 证据与复核方式

- openJiuwen 侧结论全部来自本机 `repos/openjiuwen/` 源码与 `agent-core/docs/` 文档的逐文件检读（上表均给出 `路径:行号`），检索命令：`find … -iname '*sandbox*'` + 逐文件 `grep -n`。
- E2B 侧经 WebSearch 复核（docs.e2b.dev 官方示例页命中 `files.write`、`commands.run`、`pause/resume/kill/connect`）；另检索到阿里云百炼沙箱文档（2026-09 版）自述经 E2B SDK 操作其沙箱数据面，印证"E2B 兼容 API 是事实交换契约"的前提。
- 本机未运行任何沙箱实例（WO-0003 范围为本地开发 + 推仓库，不部署）；所有"能力差距"为代码/文档级判定，非运行时实测——运行时行为以 WO-0009 gpumachine 实机验收为准。
