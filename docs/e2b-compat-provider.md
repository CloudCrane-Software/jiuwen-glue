# e2b_compat Provider（PROP-0008）交付说明

- 日期：2026-09-26
- 方案依据：PROP-0001 v1.7 §12.8（沙箱 × Agentic OS 三接缝，"唯一写代码项 = e2b_compat
  Provider，SandboxRegistry 标准扩展点；写完云突发沙箱自动成为第三 backend"）；产品需求
  文档为 `docs/e2b-compat-gap-report.md`（M0 交付的 14 行差距报告）。
- 代码位置：本仓 `providers/e2b_compat/`（独立子包，包名 `jiuwen-e2b-compat`，分支
  `e2b/wave2`；不影响 glue 主包，主包 91 项测试不受影响）。

## 1. 安装

```bash
pip install jiuwen-e2b-compat[e2b]   # 真连 E2B 云沙箱时
pip install jiuwen-e2b-compat        # 或仅作为 openjiwen Provider 注册面/开发（e2b 为可选 extra）
```

核心代码零强制第三方依赖；`e2b` / `openjiuwen` 缺失时的降级行为见子包 README
（可导入、可实例化，调用时抛带指引的错误，不静默假装成功）。

## 2. 与 AIO / JiuwenBox / Yuanrong 三 Provider 的并列关系

openJiuwen 沙箱的 Provider 注册位是 `(sandbox_type, operation_type) → provider_cls`
（`agent-core/openjiuwen/core/sys_operation/sandbox/sandbox_registry.py:45-48`，实例化
约定 `provider_cls(endpoint=..., config=...)` 见 :62-72）。e2b_compat 以相同方式注册
三件套，与内置三 Provider 并列（行号为 repos/openjiuwen 工作区克隆 2026-09-26 实测）：

| sandbox_type | fs / shell / code 注册点（形状来源） |
| --- | --- |
| aio | extensions/.../providers/aio.py:199 / 814 / 960 |
| jiuwenbox | extensions/.../providers/jiuwenbox.py:2152 / 2519 / 2887 |
| yuanrong | extensions/.../providers/yuanrong.py:538 / 883 / 973 |
| **e2b_compat（新增）** | glue 仓 `providers/e2b_compat/src/e2b_compat/provider.py`（同构三件套 + `registry_patch.register_e2b_compat_provider()` 注册路径） |

接入方式（openjiwen 环境内）：

```python
from e2b_compat.registry_patch import register_e2b_compat_provider
register_e2b_compat_provider()   # 幂等；未装 openjiwen 时抛带安装指引的 ImportError
```

注册后网关即可把 `sandbox_type="e2b_compat"` 的沙箱请求路由到 E2B 底座；glue 侧无任何
改动——glue 不新增沙箱决策点（§12.8 原则，维持 WO-0003 裁定：openJiuwen 沙箱为主，
不预选定 E2B/OpenSandbox 供应商，云突发只是新增可插拔底座）。

## 3. 语义映射与诚实边界（摘要）

- `fs.read_file/write_file/list_files(/list_directories)` ↔ E2B `files.*`（差距表 #1，投影子集）；
- `shell.execute_cmd(/_stream)` ↔ E2B `commands.run`（差距表 #2/#13，退出码/stdout/stderr 原样）；
- connect/reconnect 按 E2B `sandbox_id`（差距表 #6 的 E2B 侧落点；默认 connect-only，
  创建需 `E2B_COMPAT_ALLOW_CREATE` 显式 opt-in，对齐 PreDeploymentLauncher 语义）；
- **明确不实现**（调用即 NotImplementedError，不做半吊子模拟）：Jupyter 内核语义的
  execute_code（差距表 #3："协议兼容 ≠ 能力等价"——富 MIME/cell 上下文/变量驻留不可投影）、
  递归列目录、upload/download 分块流、glob 搜索、append 写、chmod（差距表 #1/#6/#7 中
  E2B 平面 API 没有的部分）。逐项 implement/skip 对照表见子包 README 与 gap report 第 4 节。

## 4. 三接缝验收清单（§12.8，进 WO-0009 真机验收）

| 接缝 | 对策与落点 | 验收状态 |
| --- | --- | --- |
| ws-ckpt vs 容器卷快照边界 | 快照边界声明：`E2BCompat*.snapshot_scope()` 返回 `SnapshotScope`（protected_paths bind mount 进 AgenticFS 才进承诺；ephemeral_paths 恒不进承诺，重叠即 ValueError）。声明内容与 AgenticFS 契约写在 provider.py 模块头 | 声明+测试已交付（mock）；真机快照行为 [待GPU实测]，进 WO-0009 RUNBOOK 步骤"沙箱快照边界核对" |
| AgentSecCore vs SecurityRail 双决策 | 策略同源：本 Provider 不内置任何策略决策；沙箱出口/执行策略沿用 AgentSecCore 从同一份 OPA bundle 生成的规则（OPA 唯一 PDP）。e2b_compat 只透传 `E2B_DOMAIN` 等网络配置，不新增第二个策略面 | 引用关系已写明；OPA bundle 在云沙箱侧生效验证 [待云沙箱实测] |
| 高频创建销毁性能 | 纯增益：`allow_create` opt-in 创建 + `kill()` 销毁半边，走 E2B 模板创建/销毁 API | 接口已实现（mock 测试）；创建/销毁吞吐与配额实测 [待云沙箱实测] |

## 5. 凭据（执行面零长期密钥）

- E2B API key 只从环境变量 `E2B_API_KEY` 或注入引用（网关 config 上的
  `e2b_compat_config=E2BCompatConfig(api_key=<引用>)`）读取；代码零默认 key；
- 错误消息只含环境变量名；config repr 屏蔽 key 字段；与 OpenBao/JIT 密钥下发的对接
  由调用方完成（key 值只经内存传递，符合 EXECUTION-PROTOCOL.md 红线 3）；
- 其余可配置项（域名/超时/模板 id/快照路径）见子包 README 配置表。

## 6. 当前状态与证据（如实声明）

- 子包 pytest 41 项全绿（语义映射 23 + 注册路径 6 + 降级/凭据 7 + 快照边界 5），
  全部基于假 e2b / 假 openjiwen 客户端，不联网、不装真包；
- glue 主包 91 项测试不受影响（子包不在主包 pytest 路径下）；
- **未验证内容**：未连真实 E2B 云（E2B SDK 具体调用形状、commands 抛/返口径、流式回调
  载荷、创建/销毁吞吐均标注 [待云沙箱实测]）；未做真机快照验收（[待GPU实测]，WO-0009）；
- openjiwen 接口形状引用自本机 `repos/openjiuwen/` 只读克隆（2026-09-26 实测行号），
  未修改、未复制其源码。
