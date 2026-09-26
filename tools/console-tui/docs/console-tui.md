# console-tui 治理面作战室（WO-0012 / PROP-0005）

依据：PROP-0001 v1.7 §12.3（TUI 分层与三级干预）、§12.5（Challenge 结构化对象）、
4.9 #13（执行面实例管理 API 与治理面边界，v1.6）。实现位于 `tools/console-tui/`
（独立发行包 `jiuwen-console-tui`），视图 DDL 在 `tools/console-tui/sql/003_console_views.sql`。

## 一、分层边界（写死，12.3）

| 层 | 载体 | 职责 | 本工具的关系 |
| --- | --- | --- | --- |
| 执行面 | jiuwenswarm 自带 TUI（channels/tui + gateway） | 人机交互、对话、任务执行、实例内操作 | **不替代、不直连**。steer 注入只落治理台账，不冒充执行面输入通道 |
| 治理面 | 本工具（Textual，读 glue 库） | 只读展示 + 三级受控干预 + 全量留痕 | 与 jiuwenswarm control API 的实例管理是两回事：**实例配置变更走 PR（company-ops），运行时状态走 API**，本工具只写 glue 的留痕表 |

## 二、布局（借 tower 模式显示形态）

```
┌ Header ────────────────────────────────────────────────────────┐
│ 节点池状态栏：节点数 / GPU份额占用(gpu_frac 合计,分可信) / 不可信节点数 / 并行槽位 │
├ 塔式多列（HorizontalScroll，每列一 agent/团队）──────────────────┤
│ │ alpha-planner-01 │ beta-reviewer-01 │ gamma-ops-01 │ ...  │  │
│ │ working          │ working          │ idle         │      │  │
│ │ 当前任务标题      │ 当前任务标题      │ -            │      │  │
│ │ 心跳 120s 前      │ 心跳 180s 前     │ 心跳 600s 前  │      │  │
├ 五个数据面板（Tab 切换，DataTable）──────────────────────────────┤
│ 工单看板 │ 租约 │ ask 审批队列 │ 决策记录 │ 节点利用率            │
└ Footer（快捷键提示）───────────────────────────────────────────┘
```

- 塔列数据从工单看板推导（owner = agent/团队；CLAIMED→working、BLOCKED→blocked、
  否则 idle；心跳 = 距最近一次任务状态转移）。**心跳只是展示**，超 15 分钟标注
  "可疑"，不自动做任何治理动作（决策点唯一）。
- 节点池状态栏汇总来自 `glue.v_node_utilization`（`gpu_frac` 份额占用合计），
  由 `data.py summarize_nodes` 现算，不在 SQL 物化——将来 WO-0011 调度器引入
  真实占用表时不与本视图打架。

## 三、五个数据面板 ↔ SQL 视图

| 面板 | 视图（sql/003） | 说明 |
| --- | --- | --- |
| 工单看板 | `glue.v_task_board` | team_task + 最近状态转移时间 + `paused` 推导 |
| 租约 | `glue.v_active_lease` | 仅 ACTIVE；显示额度/余量/已用% |
| ask 审批队列 | `glue.v_pending_challenge` | pending 且未过期（过期 fail-closed 出队）；剩余秒倒计时 |
| 决策记录 | `glue.v_recent_decision` | 决策 + 干预留痕同账本回读 |
| 节点利用率 | `glue.v_node_utilization` | 每节点 cpu_frac/gpu_frac/信任等级/并行/在线窗口 |

## 四、三级干预（s / a / p，12.3 写死——不加更多干预键）

| 键 | 动作 | 语义 | 留痕 |
| --- | --- | --- | --- |
| `s` | steer | 向指定 agent 注入 `_steer` 指令（弹窗输入 agent + 文本）。**可逆干预**：只是一条建议性指令记录，不改变任何状态机的状态 | decision_record 一条 `chosen='steer'` 记录（meta.intervention/target/text）+ 审计事件 |
| `a` | 审批 ask 队列 | 选中 Challenge → `y`=approve / `n`=deny。**走 Challenge 结构化对象状态机，不绕过**：只有 pending 可裁决；过期一律拒绝（fail-closed，过期批准无效，缺口重新发起）；终态不可改。pg 模式 UPDATE 带 `state='pending' AND expires_at > now()` 守卫，DDL 触发器 trg_challenge_guard 是第二道闸 | challenge 状态转移（resolved_at/resolved_by）+ decision_record 留痕 |
| `p` | 暂停/恢复 | 工单看板选中任务切换任务级 pause 标记；**同一键位复用为恢复**（不新增干预键）。任务表无 pause 列——暂停态 = 该任务最新一条 pause/resume 决策记录（v_task_board.paused 推导），可恢复、可审计 | decision_record 一条 pause/resume 记录 |

其余键：`r`=刷新（全量重绘），`q`=退出。

### Challenge 留痕语义（GuardrailRun Challenge 语义）

- 每次 s/a/p 都产生**可审计事件**：pg 模式 = append-only `glue.decision_record`
  （agent_ref=console:operator，被干预对象在 meta.target）+ challenge 状态转移
  （a）；mock 模式 = 内存审计表 + 内存决策表。
- **模型接触不到授权码/token**：审批通过只是 Challenge 状态机的一步，批准结果
  如何兑换成可执行凭证由可信 Runtime/OpenBao 负责（challenge.py 同款边界）。
- Agent 不得自确认：resolved_by 必须是明确确认人；本工具默认 `console:operator`。
- 决策记录 append-only：没有 update/delete API（Python 层），DDL 触发器拒绝
  UPDATE/DELETE（第二道闸）。

## 五、运行模式

| 模式 | 条件 | 数据 |
| --- | --- | --- |
| mock | 默认（未设 `CONSOLE_TUI_DSN`） | 内存种子数据，干预落内存审计表，可全功能演示 |
| pg | 设 `CONSOLE_TUI_DSN` 且已装 `psycopg[binary]`（`pip install 'jiuwen-console-tui[pg]'`） | 只读视图 SELECT + 干预写路径；**所有 SQL 参数化**；连接串只从环境变量读，不打印、不进 repr |

启动：`jiuwen-console`（console_scripts）或 `python -m console_tui`。

```bash
# 演示（mock）
jiuwen-console
# 生产（srv-1 落库 003 视图后）
export CONSOLE_TUI_DSN='...'   # 值不进日志/历史
jiuwen-console
```

## 六、测试与边界

- `tools/console-tui`：56 用例全绿（状态机 11 / mock 数据层 15 / pg 桩连接 14 /
  003 静态断言 5 / Textual run_test 11）。textual/pytest-asyncio 未安装时仅
  test_app.py 整体 skip（唯一允许的降级），其余用例纯 stdlib 照跑。
- glue 主仓 91 用例不受影响（未改 src/jiuwen_glue 任何文件）。
- pg 模式的连接冒烟、真实数据展示由主 agent 落库 003 后另行验证（本工单不真连
  srv-1）。

## 七、已知边界（如实）

- 干预只落治理台账：`_steer` 指令如何送达执行面实例（经 jiuwenswarm control API
  还是 gateway push）属于执行面接入，本工具 v1 只留痕不投递（v1.7 §12.3"全部
  干预经控制台留痕"已满足；投递通道在 4.9 #13 的 API 路线上另行接）。
- 塔列心跳是任务转移时间的近似，不是进程级心跳（执行面 gateway/heartbeat 才有
  真心跳，本面不复制该状态）。
- 节点池 gpu_frac 是**份额声明**的占用合计，非实时利用率（实时占用待 WO-0011）。
