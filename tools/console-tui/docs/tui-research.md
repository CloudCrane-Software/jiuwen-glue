# PROP-0007 tui-research：第三方多 agent TUI 开源项目评估（先评估后引进）

工单：WO-0012（M2）。纪律：**先评估后引进**——本文件只做评估与引进建议，
不因评估直接改依赖。评估方法：GitHub 公开仓库 API 逐仓核实（仓库存在性、star、
license、描述、活跃度），未核实的字段一律写"未查到"，不编造。
核实时间：2026-09-26（api.github.com，未认证请求，数值为当日快照）。

## 候选来源与筛选

GitHub 仓库搜索（关键词 multi-agent terminal tui），按 star 排序取前 10，
剔除"单 agent 编码助手"类（执行面已有 jiuwenswarm TUI，不重复建设），
保留与**治理面形态**（多 agent 监控 / 工单看板 / 控制面）同构的 4 个。
其余如 Codewhale（终端编码 agent，执行面重复建设）、pydantic-deepagents、
MiniClaude 等同因"执行面重复建设"未纳入评估（其 star 量级未逐一核实，
不作引用）。

## 评估结论（4 个，逐仓核实）

### 1. smtg-ai/claude-squad —— 多 agent 会话编排（形态最接近"塔"）

- 地址：https://github.com/smtg-ai/claude-squad
- star：8,535 ｜ license：AGPL-3.0 ｜ 最近推送：2026-08-20（活跃）
- 定位：在一个终端里管理多个 AI 编码 agent（Claude Code/Codex/OpenCode/Amp），
  底层 tmux 会话 + git worktree 隔离。
- 与 tower 形态的可借鉴点：
  1. **多 agent 并列面板**的组织方式（每 agent 一块可切换/并列的活面板）——
     我们塔列布局的"每列一 agent"与它同构，它的列内信息密度（状态/分支/会话）
     值得抄；
  2. **会话级 attach/detach**——治理面将来若加"旁观执行面"只读视图，这是
     现成的交互范式（但我们不 attach，只展示，边界见 console-tui.md 分层）。
- 引进建议：**借形不引码**。AGPL-3.0 对我们发行包有传染风险，且它是 Go 写的
  （本栈是 Python/Textual）；只参考面板组织与交互词汇。

### 2. antopolskiy/kanban-md —— 工单看板 × agent 循环（与工单看板面板同构）

- 地址：https://github.com/antopolskiy/kanban-md
- star：221 ｜ license：MIT ｜ 最近推送：2026-08-24（活跃）
- 定位：基于 markdown 文件的看板，供自主 agent 循环认领任务；带 CLI + TUI
  的多 agent 工作流。
- 可借鉴点：
  1. **任务列 = 状态机**的看板语义（待办/进行/阻塞/完成与 glue.team_task 的
     PENDING/CLAIMED/BLOCKED/COMPLETED 一一对应），其 TUI 的列内卡片信息排布
     （标题/负责人/阻塞原因）可直接映射到我们的工单看板 DataTable 列设计；
  2. "文件即事实源"的轻量回执思路，对 demo/离线模式（我们的 mock 模式）有
     参考价值。
- 引进建议：**MIT，可引进局部实现思路甚至小段代码**（注意保留出处注释）。
  但它以文件系统为事实源，我们的事实源是 glue 库——只借交互形态，不引其存储层。

### 3. illegalstudio/lazyagent —— 多 agent 监控聚合（与治理面定位最像）

- 地址：https://github.com/illegalstudio/lazyagent
- star：188 ｜ license：MIT ｜ 最近推送：2026-09-14（活跃）
- 定位：在一个终端里监控多个编码 agent（Claude Code/Cursor/OpenCode 等）的
  状态总览。
- 可借鉴点：
  1. **纯只读监控的信息层级**：总览行（agent 名/状态/最近活动）→ 选中详情，
     与我们"状态栏 → 塔列 → 面板"三层一致，验证了这个布局方向；
  2. 对多异构 agent 的**状态归一化**处理（不同 agent 的状态词映射到统一词表），
     对我们将来接 jiuwenswarm 多实例时统一 status 词表有直接参考价值。
- 引进建议：MIT 可引；体量小（star 少、单体工具），建议当"形态参照物"而非依赖。

### 4. BrokkAi/mjolnir —— ACP agent 控制面（治理面概念同类物）

- 地址：https://github.com/BrokkAi/mjolnir
- star：64 ｜ license：GPL-3.0 ｜ 最近推送：2026-09-26（当日仍活跃）
- 定位：Rust 实现的 agent 客户端协议（ACP）编码 agent 控制面：终端 + Web 双端，
  管理会话持久化、隔离环境、配额。
- 可借鉴点：
  1. **"控制面"能力清单**（会话持久化/隔离/配额展示）可作为我们治理面 v2 的
     feature checklist 对照——其中"配额展示"正对我们 Budget Lease 面板；
  2. 终端 + Web 双端同源的设计，提示我们若未来要 GUI，应从同一数据层出Web
     视图（12.3 明确本阶段不做 GUI，仅存档该思路）。
- 引进建议：**GPL-3.0 不引码**，只作能力清单对照。star 少，观察即可。

## 汇总

| 项目 | star | license | 与 tower 形态关系 | 引进建议 |
| --- | --- | --- | --- | --- |
| smtg-ai/claude-squad | 8,535 | AGPL-3.0 | 多 agent 并列面板 | 借形不引码 |
| antopolskiy/kanban-md | 221 | MIT | 工单看板同构 | 可引交互思路/小段代码 |
| illegalstudio/lazyagent | 188 | MIT | 只读监控总览 | 形态参照，不引依赖 |
| BrokkAi/mjolnir | 64 | GPL-3.0 | 控制面能力清单 | 只对照，不引码 |

**总体结论**：没有可直接引进的"多 agent 治理面 TUI"成品（都是执行面或监控类），
我们的塔式布局 + 三级干预方案与生态不冲突且留痕语义（GuardrailRun Challenge）
在上述项目里均未实现——自建路线成立。短期可落地动作：抄 kanban-md 的看板列
交互细节、lazyagent 的状态归一化词表；两者均 MIT、改动小。

## 未查到/未验证（如实）

- 各项目内部架构细节（代码结构、测试覆盖）未逐一克隆审读——本评估只核实了
  仓库元数据与 README 级定位；进入"引进"阶段前必须再按仓库逐个读源。
- star/推送时间为 2026-09-26 快照，会漂移。
- 未发现专门面向"治理面/审批队列"的开源 TUI（按上述关键词搜索）；若有更
  贴切项目，以同方法补评。
