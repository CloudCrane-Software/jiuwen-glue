# glue-governance — jiuwenswarm Harness 扩展包（真机部署预件）

把 `jiuwen-glue` 的治理对象接进 jiuwenswarm agent：GuardrailRun 五步门控、Challenge 授权三态、append-only 决策账本。三个工具均为 `jiuwen_glue` 薄封装。

规范来源：jiuwenswarm develop@52abe68 `service.py:207-355`、agent-core `extension_loader.py:475-534` / `668-712`、`deep_agent.py:2193` / `2254`；操作细节见 `runbook-片段.md`。

## 包内容

```
glue-governance/
├── harness_config.yaml
├── README.md
├── skills/
│   └── glue-governance/
│       └── SKILL.md
└── tools/
    ├── _governance_core.py
    ├── guardrail_gate_tool.py    # GuardrailGateTool：GuardrailRun 五步门控
    ├── challenge_board_tool.py   # ChallengeBoardTool：Challenge 授权三态审批
    └── decision_log_tool.py      # DecisionLogTool：append-only 决策账本
```

- `GuardrailGateTool`（`glue_guardrail_gate`）：创建 Run、提交 check、finalize、void、执行前 gate；聚合 verdict 是唯一门控输出。
- `ChallengeBoardTool`（`glue_challenge_board`）：结构化 Challenge 的发起、人审裁决、待办与状态查询；不含授权码。
- `DecisionLogTool`（`glue_decision_log`）：执行后追加决策记录并查询；账面只存 `context_hash`。

## 前置依赖

宿主（GPU 机）需先安装 glue：

```bash
pip install "jiuwen-glue>=0.2.0"
```

`jiuwen-glue` 为纯标准库、零三方依赖。可从 PyPI 安装，或在源码树执行 `pip install .`。版本约束写在 `harness_config.yaml` 的 `metadata.dependencies`，供部署自检；该字段在白名单内（`service.py:207-224`）。

jiuwenswarm 按官方 runbook 安装（`jiuwenswarm-init` / `jiuwenswarm-start`，见 `runbook-片段.md` §2）。openjiuwen 运行时存在时，三个工具走真实 `Tool` 基类；缺失时降级为可导入占位，仅用于结构校验与单测，不能当作已加载的 agent 工具。

## 部署序列

1. 把整个 `glue-governance/` 目录放到

   `<用户工作区>/auto-harness/runtime_extensions/<hash>/glue-governance/`

   发现契约是 `runtime_extensions/*/*/harness_config.yaml`（jiuwenswarm `service.py:2421-2426`；数据根见同文件 `:72-74`）。`<hash>` 为扫描层目录名，包本身必须再下一层，且该层含 `harness_config.yaml`。

2. 加载，三选一：

   - 编程接口：`agent.load_harness_config('<包目录>/harness_config.yaml')`（agent-core `deep_agent.py:2193`，返回资源标签列表；已标注 deprecated，替代入口为 `load_plugin`）。或 `agent.enqueue_harness_config(path)`，在下次 `stream()` 前生效（`deep_agent.py:2254`）。
   - Web UI：Auto Harness 页的 Package 管理（导入 / 扫描 / 激活）。
   - WS 协议：先 `harness.packages.scan`，再 `harness.packages.activate`，载荷 `{package_id: pkg_<hash8>_glue-governance_<时间戳>}`。`package_id` 以 scan 结果为准（格式见 `service.py:2340-2349`）。

3. 激活后，agent 获得三个工具 `glue_guardrail_gate`、`glue_challenge_board`、`glue_decision_log`，以及 skill `glue-governance`。

## 结构校验

已完成：`python validate_sample_package.py` **11/11 PASS**（2026-09-27；T0 包发现 + T1a/b/c + T2a + T2b/T2c × 3 个工具文件——原样例包为 9 项，本项目 3 个 file 型工具使 T2b/T2c 各多 1 项）。校验器复刻 jiuwenswarm `service.py:207-355` 与 agent-core `extension_loader.py` 守卫（tool/rail 必须为带 `file` 的 mapping，`extension_loader.py:475-509`；skill 必须有 `dir` 且目录内有带 `name`/`description` frontmatter 的 `SKILL.md`，`:511-534`；`expert_harness.v1` 直接收，`:668-712`）。

真实加载验证：**[待GPU实测]**。本机结构校验不等于 `load_harness_config` / WS activate 已在 jiuwenswarm 进程内执行成功。

## 与 D-10/T6 联调衔接点

GPU 机到达后（工单 D-10/T6 真机部署轨）按序做：

1. 在 GPU 机 venv 安装 jiuwenswarm 与 `jiuwen-glue>=0.2.0`。
2. 把本包放入其 `auto-harness/runtime_extensions/<hash>/glue-governance/`。
3. 走 WS `harness.packages.scan` → `harness.packages.activate`，完成真实加载，并调用一次 `glue_guardrail_gate` 的 `create_run` / `gate` 做冒烟。
4. 用 glue 治理对象验收 D-10 的部署门禁流程：部署动作必须先建 GuardrailRun，且 `gate` 的聚合 verdict 为 `PASS` 才执行。
5. 联调异常按 `runbook-片段.md` 常见错误表排查：`mcps` 字段拒绝、路径越界、`Package not found`（先 scan）、`already bound`（先 unload）。

| 报错 | 处理 |
| --- | --- |
| 不允许的字段（含 `mcps`） | 顶层只保留白名单字段（`service.py:207-258`） |
| 路径解析到包目录外 | `file:` / `dir:` 改为相对包根，去掉绝对路径与 `..`（`service.py:285-355`） |
| `Package not found` | 先 `harness.packages.scan`，再用返回的 `package_id` 激活 |
| `already bound` | 同名工具已挂载；先 unload 再激活（`deep_agent.py` load 批次回滚） |

## 安全边界

- 包内代码不复制 `jiuwen_glue` 源码，只 `import` 引用。依赖声明在 `harness_config.yaml` 的 `metadata.dependencies`。
- 三个工具不含任何密钥或授权码逻辑。授权码与 token 不经工具出入。
- `ASK` 产生的 Challenge 必须由 `who_confirms` 对应的人在独立界面裁决。Agent 不得代批。`UNKNOWN`、`BLOCKED`、未 finalize 一律不得执行。
