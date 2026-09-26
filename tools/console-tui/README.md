# jiuwen-console-tui — 治理面作战室 TUI

**定位**：一人公司多 agent 体系的治理面作战室（WO-0012 / PROP-0005）。
依据 PROP-0001 v1.7 §12.3：接受 TUI 不做 GUI；执行面 = jiuwenswarm 自带 TUI
（不替代、不直连）；治理面 = 本工具（Textual，读 glue 库五个面板 + 三级受控
干预 s/a/p，全部干预经控制台留痕 = GuardrailRun Challenge 语义）。

## 快速开始

```bash
pip install -e .            # textual 为硬依赖
pip install -e '.[pg]'      # 需要连 Postgres 时（可选 extra）
jiuwen-console              # 无 CONSOLE_TUI_DSN → mock 演示模式（内存假数据）
```

pg 模式：先由主 agent 把 `sql/003_console_views.sql` 落到 srv-1 glue 库（依赖
001/002），再 `export CONSOLE_TUI_DSN='...'` 后启动——连接串只读环境变量，
不打印、不进 repr。

## 内容地图

| 路径 | 内容 |
| --- | --- |
| `src/console_tui/app.py` | Textual 塔式布局（状态栏 + 塔列 + 五面板）与 s/a/p 弹窗 |
| `src/console_tui/data.py` | 数据层：Mock / Pg 双实现（面板只读 + 干预写路径，SQL 全参数化） |
| `src/console_tui/state.py` | 干预状态机（Challenge 裁决/暂停二态/canonical_hash，纯逻辑） |
| `sql/003_console_views.sql` | 五个只读视图（PG 方言；消费方 data.py，落库由主 agent 执行） |
| `docs/console-tui.md` | 面板/快捷键/Challenge 留痕语义/分层边界/已知边界 |
| `docs/tui-research.md` | PROP-0007：第三方多 agent TUI 评估（4 项目，先评估后引进） |
| `tests/` | 56 用例（状态机/mock 数据层/pg 桩/SQL 静态断言/Textual run_test） |

## 三级干预（12.3 写死）

- `s` steer：向指定 agent 注入 `_steer` 指令（可逆），落一条决策记录；
- `a` 审批：ask 队列选中 Challenge → y=approve / n=deny（走 Challenge 结构化
  对象状态机，过期 fail-closed 拒绝、终态不可改，不绕过）；
- `p` 暂停/恢复：工单看板选中任务切换任务级 pause 标记（同一键位复用，可恢复）。

每次干预均产生可审计事件（pg：decision_record + challenge 状态转移；mock：内存
审计表）。详细语义见 `docs/console-tui.md`。

## 边界

- 只做治理面：不直连执行面实例、不做执行面交互、不持有授权码/token；
- 实例配置变更走 PR（company-ops），运行时状态走 jiuwenswarm control API——
  本工具两样都不做，只写 glue 留痕表；
- 干预只有 s/a/p 三级，不加更多干预键。
