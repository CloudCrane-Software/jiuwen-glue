# jiuwen-console-tui — 治理面作战室 TUI

**定位**：一人公司多 agent 体系的治理面作战室（WO-0012 / PROP-0005）。
依据 PROP-0001 v1.7 §12.3：接受 TUI 不做 GUI；执行面 = jiuwenswarm 自带 TUI
（不替代、不直连）；治理面 = 本工具（Textual，读 glue 库八个面板 + 三级受控
干预 s/a/p + 下单接口 n，全部写路径经控制台留痕）。

## 快速开始

```bash
pip install -e .            # textual 为硬依赖
pip install -e '.[pg]'      # 需要连 Postgres 时（可选 extra）
jiuwen-console              # 无 CONSOLE_TUI_DSN → mock 演示模式（内存假数据）
```

pg 模式：先由主 agent 把 `sql/003_console_views.sql`（v2）与
`sql/010_console_grants.sql` 落到 srv-1 glue 库（依赖 001/002/004/007/008），
再 `export CONSOLE_TUI_DSN='...'` 后启动——连接串只读环境变量，不打印、不进
repr。srv-1 一键部署见 `deploy/README-srv1.md`（`crane-tui` 包装脚本）。

## 键位

| 键 | 动作 |
| --- | --- |
| `n` | **新建工单（下单接口，线E）**：自然语言指令 → RULES 分解 → `team_task(PENDING, owner=NULL)` → worker 自取；弹窗内实时分解预览 |
| `s` / `a` / `p` | 三级干预 steer / 审批 ask / 暂停恢复（12.3 写死，全部留痕） |
| `r` | 刷新（另有 30s 自动轮询——工单被认领/完成 30s 内面板可见） |
| `o` | 工单看板排序轮换（状态阻塞优先 / 创建时间新→旧 / Owner） |
| `?` | 帮助弹窗：全键位 + 颜色语义（绿=运行 黄=待处理 红=阻塞 灰=终态） |
| `q` | 退出 |

## 内容地图

| 路径 | 内容 |
| --- | --- |
| `src/console_tui/app.py` | Textual 塔式布局（状态栏 + 塔列 + 八面板）与弹窗；下单/帮助/排序/守护刷新/探针线程 |
| `src/console_tui/intake.py` | **下单分解（线E）**：RULES 模板（周报/复审/测试/部署）+ 目录提取 + `[标签]` 保留 + manual 降级——纯函数 |
| `src/console_tui/health.py` | **跨机服务健康探针（线E）**：ssh BatchMode 只读 `systemctl is-active`，状态归一词表，env 门控，TTL 缓存，fail-open |
| `src/console_tui/data.py` | 数据层：Mock / Pg 双实现（面板只读 + 干预/下单写路径，SQL 全参数化） |
| `src/console_tui/state.py` | 干预状态机（Challenge 裁决/暂停二态/canonical_hash，纯逻辑） |
| `sql/003_console_views.sql` | 只读视图（PG 方言；v2：`v_node_utilization` 带 `node_heartbeat` 投影） |
| `sql/010_console_grants.sql` | 补授权 DCL（`v_usage`/`v_signal_timeline` → jiuwen SELECT；幂等+角色守卫） |
| `deploy/` | srv-1 部署：`crane-tui` 包装 + `install-srv1.sh` + `README-srv1.md` |
| `docs/console-tui.md` | 面板/快捷键/留痕语义/分层边界/下单语义/设计借鉴落地清单 |
| `docs/tui-research.md` | PROP-0007：第三方多 agent TUI 评估（4 项目，先评估后引进） |
| `tests/` | 133 用例（状态机/mock 数据层/pg 桩/SQL 静态断言/Textual run_test/下单分解/健康探针） |

## 下单接口（n 键，线E）

自然语言指令 → `intake.decompose`（**RULES 优先，不硬造 LLM 依赖**）：

- 模板命中（周报/复审/测试/部署）→ 结构化 deliverable（worker handler 契约
  `{"handler": ..., "spec": {...}}`），测试类自动提取目录；
- 未命中 → 单张 `[待拆]` 自由单落 PENDING **等人工拆**（1 指令 = 1 工单）；
- 可执行性诚实：不可执行草稿的 deliverable 用 `manual` handler——任何 worker
  注册表都不认识，拒绝执行 → BLOCKED（红色可见），**不拿 report 冒充完成**；
- 指令开头 `[worker-smoke]` 等标签原样保留在标题（冒烟红线兼容）；
- 留痕 = `team_task` 行 + `task_transition`（NULL→PENDING, source='admin'），
  **不写 decision_record**——下单没有方案权衡，不是决策（决策点唯一）。

## 设计借鉴落地清单（docs/tui-research.md → 本版落地）

| 来源（评估结论） | 落地 |
| --- | --- |
| kanban-md（MIT，看板列=状态机） | 工单看板状态颜色语义 + 状态排序视角（`o` 键阻塞优先） |
| lazyagent（MIT，状态归一化词表） | 服务健康归一词表 active/starting/stopped/failed/unreachable/unknown + `?` 帮助图例 |
| claude-squad（AGPL，借形不引码） | 塔列信息密度（每列：名/状态/当前任务/心跳龄）——v1 已落，线E 补心跳超窗红标 |
| mjolnir（GPL，只对照） | 配额展示对照 = 租约面板已用% 列（v1 已落，本版未动） |

## 边界

- 只做治理面：不直连执行面实例、不做执行面交互、不持有授权码/token；
- 实例配置变更走 PR（company-ops），运行时状态走 jiuwenswarm control API——
  本工具两样都不做，只写 glue 台账表；
- 干预只有 s/a/p 三级，不加更多干预键（`n` 是下单不是干预：创建新对象，
  不触碰既有对象状态机；`o`/`?` 是展示层易用性）；
- 服务健康探针 ssh 只读（`systemctl is-active`），对目标机零写操作；
- 单面板数据源缺失只降级该面板（错误行可见），不拖垮整个控制台。
