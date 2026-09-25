# jiuwen-glue

openJiuwen 胶水层（glue layer）。一句话：**只做 openJiuwen 原生没有的那几件事，其余一律用原生。**

## 定位

依据《建设方案-多Agent系统与GitOps》（PROP-0001，存 CNB `company-ops`）第 0 节第 4 条、第 4.1 节与第 4.9 节边界总表，glue 只承载原生没有的能力。本包（WO-0003 第 3-5 步）实现其中三个对象与三铁律：

| 对象 | 模块 | 最小说明 |
| --- | --- | --- |
| **Budget Lease**（预算租约） | `jiuwen_glue.leases` | 发放 / 占用 / 过期；子任务派生即从父租约划出额度；撤销**级联**到全部后代；超额占用被拒并留痕 |
| **Evidence 三态** | `jiuwen_glue.evidence` | `draft → verified → finalized` 只进不退；内容仅 draft 可编辑；finalized 封存；能力准入只认 verified/finalized 证据 |
| **能力元数据注册** | `jiuwen_glue.capabilities` | 五类机读声明（副作用/幂等/可安全重试/风险等级/前置条件）+ 版本准入（必须挂已验证证据）+ 指标**单向回流**（只增不改，无手改接口） |
| **协同三铁律** | `jiuwen_glue.rules` | Task 台账 = Team State 唯一承载；消息回执不承载状态语义；拆分任务校验 |

三条铁律（PROP-0001 第 4.3 节；母本为 Handbook 第 11 章 + openJiuwen agent_teams specs）：

1. 消息可以触发任务或补充信息，但**发送成功不代表任务已被承接**；
2. **不能把对话历史当作 Team State**，协作事实由 Task State 与 Artifact 维护（完成任务必须带 TaskRun 与 Artifact 引用）；
3. 只有存在独立交付、不同责任或明确依赖时才拆子任务，**同一成员连续完成的内部步骤不建任务**。

每条铁律各有一条失败用例（`tests/test_iron_rules.py`）：喂入违规输入，断言抛出对应 `IronRuleViolation` 子类 + `violation_log` 留痕 + 任务事实未被篡改——**违规必被检测**。

## 边界（写死）

- 原生层管"怎么做"，glue 管"准不准进"，控制台管"看得见"。
- 凡可能有两个决策点的，必须收敛为一个；冲突记 ADR。
- 第 4.9 节 14 项边界表之外，glue 不新增决策点；**发现与编排一律用原生 Symphony，本包不自建发现机制**（4.9 #1）。
- 协议兼容 ≠ 能力等价（见 `docs/e2b-compat-gap-report.md`）。

## 快速上手

```bash
pip install -e .          # 或 uv pip install -e .
python -m pytest          # 26 项测试，全离线（无需数据库/网络）
```

```python
from jiuwen_glue import BudgetLedger, EvidenceStore, CapabilityRegistry, TaskLedger

# Budget Lease：随子任务派生 + 级联撤销
led = BudgetLedger()
root = led.grant("task-root", 1000)
child = led.grant("task-child", 300, parent_lease_id=root.lease_id)  # 派生即划出
led.acquire(child.lease_id, 120, purpose="run-1")                    # 占用
led.revoke(root.lease_id, reason="owner cancelled")                  # 级联撤销

# Evidence 三态
ev = EvidenceStore().create("task_run:42", {"summary": "..."})
# ev.verify(...); ev.finalize(...)  → 只进不退

# 能力元数据：五类机读声明 + 版本准入
reg = CapabilityRegistry()
reg.register(CapabilityDeclaration(
    capability_id="skill.web", version="1.0.0", name="web skill",
    side_effect="read", idempotent=True, retry_safe=True, risk_level=1,
    preconditions=("network.allow(egress:higress)",)))
```

## Postgres DDL

`sql/001_glue_objects.sql`（可重复执行，Python 层为第一道闸、DDL 约束/触发器为第二道闸）：

- `glue.budget_lease` / `glue.lease_event`（占用留痕 + 超额触发器）
- `glue.evidence` / `glue.evidence_transition`（前向迁移 + 内容冻结触发器）
- `glue.capability_version` / `glue.capability_metric_event` / `glue.capability_metrics` 视图（准入证据触发器 + 只追加指标）
- `glue.team_task` / `glue.task_transition` / `glue.message_receipt` / `glue.rule_violation`（source 白名单把消息/对话历史挡在任务事实之外）

运维权威副本在 CNB `company-ops` 仓库 `ops/sql/001_glue_objects.sql`（私有）；两处内容一致。

## 状态

- WO-0002：M0 骨架（README / LICENSE / .gitignore）。
- WO-0003 第 3-5 步（本包）：三对象最小实现 + Postgres DDL + 三铁律失败用例 + E2B 兼容差距报告（`docs/e2b-compat-gap-report.md`）。纯 Python、零运行时依赖、测试离线可跑。
- 不在本包：GuardrailRun 协议聚合、记忆晋升管线（后续工单）；沙箱/网关/编排等一律用 openJiuwen 原生。

## License

Apache-2.0
