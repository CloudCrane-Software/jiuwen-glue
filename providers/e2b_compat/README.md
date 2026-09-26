# jiuwen-e2b-compat（e2b_compat Provider）

> PROP-0008：openJiuwen SandboxRegistry 的 E2B 兼容 Provider——"沙箱 × Agentic OS"唯一写代码项
> （PROP-0001 v1.7 §12.8）。写完即把云突发沙箱接成 openJiuwen 沙箱的新 backend，与内置
> AIO / JiuwenBox / Yuanrong 三个 Provider 并列注册，不改 openjiwen 任何代码，glue 不新增沙箱决策点。

- 产品需求文档：`docs/e2b-compat-gap-report.md`（WO-0003 动作 3 的 14 行差距报告）
- 集成说明（三接缝验收清单 / 差距对照表）：glue 仓 `docs/e2b-compat-provider.md`
- 当前状态：**mock 级交付**——全部测试用假 e2b / 假 openjiwen 客户端，未连真实 E2B 云；
  标注 [待云沙箱实测] 的行为口径以 docs.e2b.dev 对应页为准（gap report 第 3 节声明）。

## 安装

```bash
pip install jiuwen-e2b-compat[e2b]   # 核心零依赖；[e2b] 才装 e2b SDK（真连云沙箱时）
pip install jiuwen-e2b-compat[dev]   # 开发/测试（pytest）
```

- 核心代码零强制第三方依赖（EXECUTION-PROTOCOL.md 工程纪律）：不装 `e2b` 时 Provider
  仍可导入、注册、实例化，真正调用时抛带安装指引的 `E2BCompatDependencyError`；
- 不装 openjiuwen 时同样可用（`e2b_compat.OPENJIUWEN_AVAILABLE` 如实反映；注册路径会
  给出指引而不是静默降级，见 `registry_patch.py`）。

## 定位与并列关系

openJiuwen 沙箱是"操作协议抽象 + 可插拔 Provider + 生命周期网关"架构（5 层，见
`agent-core/docs` Sandbox.md），沙箱底座经 Provider 外接。本包注册 `sandbox_type="e2b_compat"`
三件套，注册位与内置三 Provider 完全同构（接口形状照抄自 openjiuwen 仓实测行号）：

| sandbox_type | fs | shell | code | 形状来源（repos/openjiuwen/ 实测行号） |
| --- | --- | --- | --- | --- |
| `aio` | aio.py:199 | aio.py:814 | aio.py:960 | `@SandboxRegistry.provider(...)` |
| `jiuwenbox` | jiuwenbox.py:2152 | jiuwenbox.py:2519 | jiuwenbox.py:2887 | 同上 |
| `yuanrong` | yuanrong.py:538 | yuanrong.py:883 | yuanrong.py:973 | 同上 |
| `e2b_compat`（本包） | provider.py `E2BCompatFSProvider` | `E2BCompatShellProvider` | `E2BCompatCodeProvider` | 同构；注册协议 sandbox_registry.py:45-48，实例化约定 sandbox_registry.py:62-72 |

`E2BCompatProvider` 是使用方门面（组装 fs/shell/code + `connect()`/`reconnect()`/
`snapshot_scope()`/`kill()`），不是注册单元——注册单元是上面三个 operation Provider 类。

## 用法

```python
# 1) 注册进 openJiuwen SandboxRegistry（openjiwen 已安装时）
from e2b_compat.registry_patch import register_e2b_compat_provider, registration_status
register_e2b_compat_provider()      # sandbox_type="e2b_compat" 三件套
registration_status()               # 自检：{"openjiuwen": True, "registered": {...}}

# 2) 经门面使用（openjiwen 缺失也可用）
from e2b_compat import E2BCompatProvider, SandboxEndpoint, E2BCompatConfig

provider = E2BCompatProvider(
    SandboxEndpoint(base_url="https://api.e2b.example", sandbox_id="<E2B sandbox_id>"),
)
result = await provider.fs.read_file("/workspace/notes.md")        # E2B files.read
result = await provider.shell.execute_cmd("pytest -q", cwd="/workspace")  # E2B commands.run
provider.snapshot_scope()   # 快照边界声明（12.8 接缝 1）
provider.reconnect("<另一 sandbox_id>")  # connect-by-id 重连（gap #6 的 E2B 侧语义）
```

## 语义映射（gap report 结论落地）

| openJiuwen 协议 | e2b_compat 投影 | 说明 |
| --- | --- | --- |
| `fs.read_file` | `sandbox.files.read(path, format=)` | head/tail/line_range 在本地切片（E2B 平面 API 无行切片，与 aio.py 读全量再切同策略）；bytes+chunk_size 本地截断 |
| `fs.write_file` | `sandbox.files.write(path, data)` | prepend/append_newline 本地组帧；text/bytes 直传 |
| `fs.list_files` / `list_directories` | `sandbox.files.list(path)` | E2B 恒非递归（`recursive=True` → NotImplementedError）；排序/扩展名过滤本地完成 |
| `shell.execute_cmd` | `sandbox.commands.run(cmd, cwd=, envs=, timeout=)` | 退出码/stdout/stderr 原样投影；流式经 on_stdout/on_stderr 回调桥接 |
| connect / reconnect | `e2b.Sandbox.connect(sandbox_id, ...)` | 按 E2B sandbox_id 重连；无 id 时默认拒绝（对齐 PreDeploymentLauncher"连接已启动沙箱"），`allow_create=True` 显式 opt-in 后才按模板创建 |
| `code.execute_code(_stream)` | **不实现** | 见下"明确不实现" |

### 明确不实现（诚实边界，抛 NotImplementedError，不做半吊子模拟）

- **Jupyter 内核语义的代码执行**（gap #3）：富 MIME 输出 / cell 上下文 / 变量驻留是内核态
  语义，openjiwen code 协议是 shell shim 语义，互投影就是造假。跑脚本走 shell Provider；
  要内核语义用 AIO Provider 接 AIO 底座自身 code API。
- **connect-by-id 之外的特殊语义**：递归列目录、本地 upload/download 分块流、glob 搜索、
  `append=True` 写、`permissions` chmod——E2B 平面 API 没有对应物，给出替代路径后拒绝。

## 凭据与配置（执行面零长期密钥）

E2B API key 只从环境变量或注入引用读取；代码零默认 key；错误消息只含环境变量名；
`E2BCompatConfig.api_key` 字段 repr 屏蔽。

| 配置 | 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| API key | `E2B_API_KEY` | 无（缺失即报错） | 或经网关 config 注入 `e2b_compat_config=E2BCompatConfig(api_key=<引用>)` |
| 域名 | `E2B_DOMAIN` | E2B 默认 | 自托管/私有部署 |
| 模板 id | `E2B_COMPAT_TEMPLATE_ID` | E2B 默认模板 | 仅 opt-in 创建时生效 |
| 流超时 | `E2B_COMPAT_REQUEST_TIMEOUT` | 60 | 流式逐块等待（秒） |
| 沙箱寿命 | `E2B_COMPAT_SANDBOX_TIMEOUT` | 300 | 仅 opt-in 创建时传 SDK（秒） |
| 允许创建 | `E2B_COMPAT_ALLOW_CREATE` | 0 | 默认只连接已启动沙箱 |
| 受保护路径 | `E2B_COMPAT_PROTECTED_PATHS` | `/workspace` | 冒号分隔；快照承诺范围 |
| 临时态路径 | `E2B_COMPAT_EPHEMERAL_PATHS` | `/tmp:/var/tmp:/dev/shm` | 冒号分隔；不进承诺 |

## 快照边界声明（§12.8 接缝 1）

`snapshot_scope()` 返回机器可读的 `SnapshotScope`：受保护工作区 bind mount 范围
（`protected_paths`）进 ws-ckpt 快照承诺；E2B microVM 容器临时态（`ephemeral_paths`、
镜像层、未挂载路径、进程内存态）**不进承诺**。AgenticFS 快照器以该声明为唯一范围依据；
想让临时态进快照必须改 bind mount（改声明），而不是放宽契约。受保护/临时态路径重叠
在构造时即拒绝（ValueError）。

## 三接缝验收清单（PROP-0001 v1.7 §12.8）

| 接缝 | 本包对策 | 状态 |
| --- | --- | --- |
| ws-ckpt vs 容器卷快照边界 | `SnapshotScope` 声明 + AgenticFS 契约注释 + 重叠拒绝 | 声明已实现并有测试；真机快照行为 [待GPU实测]（进 WO-0009 RUNBOOK） |
| AgentSecCore vs SecurityRail 双决策 | 本包不做策略决策；策略同源引用同一份 OPA bundle（glue AgentSecCore 生成规则，见 docs/e2b-compat-provider.md） | [待云沙箱实测]（沙箱出口策略需 OPA bundle 在云沙箱侧生效） |
| 高频创建销毁性能 | `allow_create` opt-in 创建路径 + `kill()` 销毁半边 | 接口已实现（mock 测试）；吞吐实测 [待云沙箱实测] |

## 差距对照表（gap report 14 项 → implement/skip）

| # | 能力 | 判定 | 本包处置 |
| --- | --- | --- | --- |
| 1 | 文件读写 | openJiuwen 更强 | implement 子集：read/write/list；skip：递归、分块流、upload/download、search（NotImplementedError + 替代路径） |
| 2 | Shell 执行 | 等价 | implement（execute_cmd）；抛/返两种口径兜住 [待云沙箱实测] |
| 3 | 代码执行 | 部分（语义差） | **skip**（NotImplementedError，README/文档写明） |
| 4 | 生命周期 create/kill | 部分 | implement opt-in 创建 + kill；默认 connect-only |
| 5 | pause/resume | 部分 | skip（Launcher 层职责，本包不做 no-op 假实现） |
| 6 | 重连 connect by id | 缺失→E2B 有 | implement（endpoint.sandbox_id → Sandbox.connect） |
| 7 | 进程管理 processes.list/kill | 缺失 | skip（E2B v1 平面 commands API 之外；需要时经 shell `ps/kill`） |
| 8 | 隔离模型 | 等价性存疑 | N/A（安全评审看底座：Firecracker microVM） |
| 9 | 模板/镜像管线 | 缺失 | 部分：`template_id` 透传；构建/分发管线 skip（上游 E2B Template 承担） |
| 10 | 计量/配额 | 缺失 | skip（WO-0006 观测管道 + glue Budget Lease 回路承担） |
| 11 | 网络出口治理 | 部分（绑定底座） | skip（底座层；治理面核对 Higress 唯一入口） |
| 12 | 认证/多租户 | 部分 | implement：平台级 API key（env/注入引用，零默认值）；签名 URL 类沙箱级认证 skip |
| 13 | 流式输出 | 等价 | implement（execute_cmd_stream 回调→Queue→async 迭代；载荷形状 [待云沙箱实测]） |
| 14 | 服务端运维面 | 部分 | skip（E2B 云托管承担；自托管运维不在本包范围） |

## 边界

- 不连真实 E2B 云：全部测试 mock（假 e2b / 假 openjiwen 模块注入 sys.modules，不装真包、不联网）；
- 不改 openjiwen 源码（repos/ 只读参考，只引用接口形状）、不改 glue 主包任何文件；
- 密钥零落盘：本包代码/测试无任何真实 key，测试用 key 均为假值。
