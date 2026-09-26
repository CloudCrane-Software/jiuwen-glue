# fleet 设计 — 节点池注册协议（PROP-0003）+ 调度器 P1（PROP-0004）

> WO-0011 · 依据 PROP-0001 v1.7 §12.6（节点池纳管）/ §13（GPU_FRAC→份额字段、
> 调度收敛）/ §14（PROP-0003/0004）；边界依据 v1.6 §4.9 #10（任务对象三层，
> 跨层只传引用）、#13（jiuwenswarm control=实例管理 API，调度器只派工单）；
> 令牌形状依据《独立审计报告-M0》B 组（execution-worker policy 只读
> `kv/data/company/gpu/*`、≤1h 短时令牌）。

## 1. 定位

- **PROP-0003（registration.py）**：任何实体机经 OpenBao JWT 注册为 worker 节点，
  声明能力（CPU/GPU 份额/工具/在线窗口）+ 信任等级 + max_parallel。能力声明
  **复用** `routes.NodeCapacity`（`GPU_FRAC` 概念吸收为 `gpu_frac` 份额字段，
  字段与 CNB company-ops `ops/sql/002_glue_v2.sql` 的 `glue.node` 表对齐），
  不重造。
- **PROP-0004 P1（scheduler.py）**：贪心调度——过滤 → 排序取首 → 份额落账；
  两种取活模式：**调度制**（`DispatchMode`，控制台直接派）与**自取制**
  （`SelfPickMode`，节点轮询工单队列认领 PENDING，租约 TTL 过期回收）。
- **worker.py**：自取制客户端**协议**（模拟侧）——`WorkerLoop.tick()` =
  轮询 → 认领 → 本地执行回调 → 回报；不实现真实网络。

三阶段路线：**P1 贪心（本次实现）→ P2 利用率优化（只设计）→ P3 云突发伸缩（只设计）**。

## 2. 注册协议（PROP-0003）

```
实体机                         glue（srv-1 控制面）
  │  1. bao jwt auth 领短时 JWT（[待真实 bao 接入]）
  │  2. NodeRegistration{node_id, capacity, attestation, heartbeat_ttl, agent_ref}
  ├──────────────────────────→ FleetRegistry.register()
  │        校验：claims 齐全（sub/exp）、未过期、sub==node_id、
  │              trust_level 声明与 capacity 一致、online_window 可解析
  │  3. heartbeat() 周期续活；超窗（last_seen + heartbeat_ttl）→ STALE
  │  4. STALE 唯一复活路径 = 带新 attestation 重新注册（不许悄悄复活）
```

**协议级校验 vs 签名验证**：本层只做 claims 解码后的协议校验（缺失/过期/主体
嫁接/信任等级冲突 → `AttestationError` 拒绝注册）；JWT 签名验证归真实 OpenBao
（jwt auth role，M0 审计 B3 骨架已就位）——**[待真实 bao 接入]**，本工单不连 bao。
**零密钥落盘**：JWT 明文只在注册调用的内存参数中出现一次，校验后仅保留派生的
非敏感字段（iat/exp/sub/trust/aud）与 `jwt_ref` 引用，原始 token 永不存储。

### 字段映射表一：注册与容量

| glue.node（DDL）        | NodeCapacity（routes.py，复用） | NodeRegistration / NodeRecord（fleet） |
| ---                     | ---                             | ---                                    |
| tenant_id               | tenant_id（默认 t0）            | 租户隔离在调度过滤链执行               |
| node_id（PK 之一）      | node_id                         | NodeRegistration.node_id（两处必须一致，否则拒绝注册） |
| cpu_frac NUMERIC [0,1]  | cpu_frac                        | —                                      |
| gpu_frac NUMERIC [0,1]  | gpu_frac（GPU_FRAC 吸收为字段） | 份额记账见 ShareLedger（§4）           |
| tools JSONB             | tools（capability 标签）        | 调度要求 required_tools ⊆ tools        |
| trust_level CHECK       | trust_level（trusted/untrusted）| attestation.trust_level 交叉校验       |
| max_parallel INT ≥1     | max_parallel                    | 并行槽位记账                           |
| online_window TEXT      | online_window（"always"/"HH:MM-HH:MM[+HH]"） | 注册入口 fail-closed 解析（解析不了 → 拒绝注册） |
| registered_at           | —                               | NodeRecord.registered_at / last_seen_at |
| —（DDL 未含状态列）     | —                               | NodeRecord.status（ACTIVE/STALE/DEREGISTERED）；真实 SQL 持久化需另加状态/心跳表（schema 变更走 PR，company-ops） |

`FleetRegistry` 内存版为权威实现；`RegistryPersistence` 是可选 SQL 持久化接口
（upsert_node / record_heartbeat / update_status），真实 Postgres 实现属后续工单。

## 3. 两种取活模式（§12.6）

| 模式 | 类 | 流程 | 兜底死亡语义 |
| ---  | --- | --- | --- |
| 调度制 | `DispatchMode` | 控制台/调度器 `dispatch(offering)` 直接派 → 节点被动接单 | 超时 `timeout()` 释放份额 |
| 自取制 | `SelfPickMode` | 节点 `claim(node_id, queue)` 从 PENDING 工单按（priority 降序、提交时间升序、order_id 升序）认领第一单**可接**的工单 | ① 工单层：租约 TTL 过期 → `reclaim_expired()` 把 CLAIMED 工单回 PENDING + 释放份额 + 终结租约；② 节点层：心跳超窗 → STALE → 不可认领，须重注册 |

自取制适用间歇在线的线下机器；两条死亡语义互为兜底（节点死了工单可被别的节点
重领，节点活着但工单超时则工单回队）。

## 4. P1 贪心调度（PROP-0004）

**决策与记账分离**：模块级 `assign(offering, registry)` 是纯决策（dry-run 可复用）；
`GreedyScheduler.assign()` 用同一决策 + `ShareLedger` 真实落账。

1. **过滤链**（`_evaluate`，顺序即优先级）：
   ① 硬规则：不可信节点只接沙箱任务类（`capacity.sandbox_only and not offering.sandbox_class` → 拒）
   ② trust_required ③ required_tools ⊆ tools ④ gpu 需求 ≤ 空闲份额（gpu_frac − 已承诺）
   ⑤ 并行槽位 ⑥ 在线窗口 `is_online(now)` ⑦ `NodeCapacity.can_take` 既有谓词兜底（复用，正常路径不可达）。
2. **排序键**（确定性）：信任等级降序 → 空闲 gpu_frac 降序 → max_parallel 余量降序
   → node_id 升序（最终 tie-break；同池同状态重复派发结果可复现，有测试锁定）。
3. **份额记账**：Assignment 落账即扣减 gpu_frac 份额与槽位；`complete()/timeout()/
   release()`（含 TTL 回收）全部释放。比较带 1e-9 容差防浮点噪声。
4. **拒绝码**：guardrail 非 PASS / TTL 超限直接给精确码；节点侧落选若**全部候选**
   因同一能力原因 → 给该原因码（如沙箱硬规则 `SANDBOX_ONLY_NODE`），混合原因 →
   `NO_ELIGIBLE_NODE` + 各原因计数（detail），运维可见。
5. **Budget Lease 复用**：接入 `BudgetLedger` 时，每笔 Assignment 派生一笔额度 1 的
   短时租约（自取制带 TTL ≤ `MAX_SELF_PICK_LEASE_SECONDS`=3600s，对齐 execution-worker
   ≤1h 形状；调度制无 TTL），释放/回收/完成即 `revoke`（级联撤销复用既有实现）。
   工单可携带 `parent_lease_id` + `effective_perms`（identity 五集交集的
   `EffectivePerms`）——派生时既有"子快照 ⊆ 父快照"不变式强制收敛，越界派生被拒
   且份额账回滚。
6. **GuardrailRun 唯一门控输出被消费**：offering 可携带 `guardrail_run_ref` +
   `guardrail_verdict`；verdict ≠ PASS（含 UNKNOWN/缺失）→ 拒绝派发（fail-closed）。
   调度器不新增第二个决策点。

### 字段映射表二：工单与租约（跨层只传引用，4.9 #10）

| 对象 | 引用字段 | 指向 |
| --- | --- | --- |
| TaskOffering / WorkOrder | task_ref | glue Task（三层任务对象的工单层引用） |
| WorkOrder | payload_ref | 任务体（不复制任务内容） |
| WorkOrder | secret_refs | OpenBao 密钥引用（`bao://...` 形状；`WorkerContext.lease_scoped_secrets` 只接受 scheme 限定引用，裸值 → `SecretScopeError`——执行面零长期密钥红线在协议层的体现） |
| Assignment | lease_ref | Budget Lease（派生短时租约） |
| Assignment | artifact_ref | 产物引用（按 artifact_routes 发走，不复制产物） |
| NodeRegistration | agent_ref | 三层复合身份引用（identity.composite_ref 产出） |

### 字段映射表三：不可信节点产物隔离（→ WO-0012 控制台）

| Assignment 字段 | 语义 | 下游 |
| --- | --- | --- |
| sandbox_only | 节点 trust_level=untrusted（产物隔离） | 复核流程归控制台（WO-0012） |
| review_required | 本单需人工复核（= sandbox_only） | 控制台 TUI 复核队列 |

## 5. P1 → P2 → P3 路线（P2/P3 只设计不实现）

### P2 利用率优化（设计）

P1 是"首个可行节点"贪心，会留份额碎片、不感知负载。P2 在**不改对外签名**的前提下替换决策内核：

- **碎片合并**：gpu_frac 需求排序从贪心改 bin-packing 视角（best-fit），优先把大需求派给剩余份额最接近的节点，减少"0.3 需求占了 0.9 余量节点"的浪费；
- **负载感知评分**：`ShareLedger` 的槽位/份额占用率 + 指标回流（capabilities 指标事件 / Langfuse 既有观测，不新建采集）作为软评分项；
- **抢占与 backfill**：高优先级 offering 可抢占低优先级 Assignment（先 graceful timeout 再重派）；低优先级在不动已承诺份额的前提下 backfill 空余槽位；
- **老化防饿死**：PENDING 工单等待时长计入排序，防低优先级永久饥饿；
- **亲和性**：pipeline_label 粘性（同流水线尽量同节点，复用工作区/缓存），仅在负载均衡允许时生效。

### P3 云突发伸缩（设计）

- **同协议弹性节点**：云突发沙箱（E2B 兼容 Provider，PROP-0008 的 SandboxRegistry 第三 backend）作为**普通节点**走同一注册协议进出池——burst 节点 trust_level=untrusted（天然不可信）→ 只接沙箱任务类；
- **触发器**：PENDING 工单积压阈值 × 持续时间（如 >N 单持续 >T 分钟）→ 申请 burst 容量；队列清空后 deregister；
- **护栏**：burst 节点的 Budget Lease 硬上限（单租约/单日额度）+ 在线窗口声明为 burst 时段；销毁前产物必须先按 `artifact_routes` 发走（显式错误防产物丢在沙箱里）；
- **信任边界**：云节点密钥只经 OpenBao ≤1h 短时租约注入（execution-worker policy 同形状），产物隔离 + 人工复核同 12.6。

## 6. 边界（写死）

- 不连真实 OpenBao / Postgres：协议 + 内存实现；签名验证与 SQL 持久化属后续工单（上文标注）。
- 调度器**只派工单**，不碰 jiuwenswarm control 实例管理 API——实例内部（config/session/生命周期）归 control 面，调度面只见工单（4.9 #13）。
- 跨层只传引用：task_ref / payload_ref / secret_refs / lease_ref / artifact_ref / agent_ref / jwt_ref 全部是引用。
- 不可信节点只接沙箱任务类是硬规则（`_evaluate` 第一道 + `can_take` 兜底），产物隔离与复核标记随 Assignment 下发，复核流程归 WO-0012。
- glue 不新增第二个决策点：本模块消费 GuardrailRun 聚合 verdict，不产出门控结论。
