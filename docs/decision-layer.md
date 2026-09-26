# 决策层 MVP（JevProvider 三原语）+ WO-0007 glue 侧两件 — 设计与注入点

- 工单：**WO-0010**（决策层 MVP）+ **WO-0007 收缩版件 1/2**（消融验证、GuardrailRun 准入记录）
- 方案依据：PROP-0001 v1.7 §0 总则 6、§12.7；v1.6 §4.7、§6（L1 级联漏斗）、§4.9 #5/#9；
  《AI Native 研发范式实践手册》§3.3.1 原则三（由确定性系统决策）
- 代码落点：`src/jiuwen_glue/decision.py`、`src/jiuwen_glue/ablation.py`、`src/jiuwen_glue/admission.py`
- 状态（如实）：MVP，进程内零依赖纯 Python；**ModelBackend 只留形状，未发任何真实模型调用**。

---

## 1. JEV 命名口径（原文备案，与模块 docstring 逐字一致）

> "JEV"为方案 v1.4 引入的概念名（v1.5 原文已散佚不可考）。本 MVP 按方案 0.6 的功能语义固化三原语为 **classify（分类）/ score（评分）/ judge（裁决）**，acronym 备案解释为 Judge-Evaluate-Verify；若 v1.5 原文找回且定义不同，以 ADR 收敛。

## 2. 三原语语义与决策留痕

总则 6（v1.7）："高频决策与分类走 JevProvider 三原语，大模型只做推理与生成。" 代码含义：模型后端只出推理结果；"选哪个/打几分"的决策行为在决策层收敛，并且**每次成功决策落一条 append-only DecisionRecord**（复用 `decisions.py`；context 经其 canonical-JSON + SHA-256 哈希，原文不落账）——"大模型只做推理与生成，决策留痕在决策层"。

| 原语 | 签名 | DecisionRecord 编码 | 典型高频场景 |
| --- | --- | --- | --- |
| classify | `classify(item, labels) -> (label, confidence, decision_ref)` | options=labels；chosen=label；meta.confidence / meta.backend | 意图/语种/类别路由、经验条目分级 |
| score | `score(item, rubric) -> (value, decision_ref)` | score 是连续量、无离散选项空间——options 退化为 `(str(value),)`；float 值在 meta.value；rubric 与 item 进 context 哈希 | 晋升候选打分、eval-gate L1 灰区 |
| judge | `judge(question, options, constraints) -> (chosen, rationale_ref, decision_ref)` | options=options；chosen=chosen；rationale_ref 由后端给出并原样返回调用方 | 低风险自动裁决（约束分域）、追问分流 |

返回的 `decision_ref` 可在 `DecisionLog.get()` 解析为完整 DecisionRecord（agent_ref / context_hash / options / chosen / rationale_ref / guardrail_run_ref / tenant_id）。

### UNKNOWN 语义（fail-closed，写死）

五种弃权路径全部收口为 **返回 None + 拒绝理由**，绝不静默编造：

| # | 弃权路径 | 拒绝理由（RefusalRecord.reason 摘要） |
| --- | --- | --- |
| 1 | 原语未配置后端 | `no backend configured for primitive …` |
| 2 | 后端弃权（声明式规则无命中） | `backend abstained: no rule matched` |
| 3 | 后端异常 | `backend … raised: …` |
| 4 | 返回值越出声明选项空间（classify 的 label ∉ labels / judge 的 chosen ∉ options） | `outside declared labels/options — refused, never fabricate` |
| 5 | 分值非法（score 非数值/非有限；confidence 越界） | `non-finite/non-numeric score` / `out-of-range confidence` |

弃权经 `DecisionLayer.refusals`（`RefusalRecord` 列表）留痕可观测；**弃权不落 DecisionRecord**（账本只记真实发生的决策）。调用方参数不合法（空 labels、空 options、空 question）直接抛 `DecisionSchemaError`——调用方 bug 要大声失败，不算"无法判定"。

## 3. 后端可插拔

| 后端 | 性质 | 说明 |
| --- | --- | --- |
| `RuleBasedBackend`（内置） | 确定性、零模型调用 | 声明式规则映射：`when` 合取匹配（list/tuple/set=成员测试），classify/score 对 item 字段求值，judge 对 constraints 求值（可加 `question_contains` 子串）；**声明顺序即优先级，第一条命中生效**；无命中=弃权。规则未声明 confidence = 规则作者声明满信度 1.0（声明即来源，非模型自评） |
| `ModelBackend`（形状） | 不发真实调用 | 字段约定（Higress 唯一模型入口，v1.6 §4.9 #7 / v1.7 §12.1）：`endpoint_ref` 只存端点**引用名**（如 `higress:model-gateway`）；`api_key_ref` 只存凭据**引用**（如 `openbao:secret/credentials/higress#api_key`），不存任何密钥值（handbook 原则四：长期凭证不进入 Agent）；`model` 经 Higress 上游路由。三原语方法 raise NotImplementedError——真实接入属后续工单（真机联调 + OpenBao 运行时注入短时凭据） |

门面 `DecisionLayer` 按原语路由后端（`set_backend` / `backend_of`），并实现 `JevProvider` 协议（`@runtime_checkable`，消费方只依赖协议不依赖后端）。

**决策点唯一声明**：决策层不是第二个门控——授权三态仍属原生 TeamPermissionRail，协议聚合 verdict 仍是 GuardrailRunStore 的唯一门控输出；决策层是把高频小决策从大模型对话里拿回确定性轨道并留痕（handbook 原则三）。

## 4. TTSE 边界（v1.6 §4.7 原文）

> TTSE 的 FACT/TIP 归纳与 consult 召回是**原生经验通道**，决策层不重复做经验召回，只做第六植入点"晋升候选打分"这一个交叉点。

落点：`make_score_hook` 是决策层与记忆/经验管线的**唯一**交叉点；本包没有任何通往原生记忆库（core.memory / JiuwenMemory / TTSE bank）的读写路径。跨会话轨迹过滤（4.9 #5 三层压缩的第三层）同样由决策层 Score 承担——用 `score` 原语按 rubric 打分，与原生会话压缩、Tokenless 各管一段，开关显式声明防双压缩。

## 5. 注入点 1：promotion.score_hook（已接线，测试锁定）

promotion.py 写死的形状：`ScoreHook = Callable[[候选快照 Mapping], Optional[float]]`；**None = 决策层弃权 = UNKNOWN = 拒绝晋升（fail-closed）**。

```python
from jiuwen_glue import DecisionLayer, DecisionLog, RuleBasedBackend, make_score_hook, PromotionLedger

layer = DecisionLayer(
    decision_log=DecisionLog(), agent_ref="agent:glue/inst-1/task-1",
    backends={"score": RuleBasedBackend([
        {"primitive": "score", "when": {"asset_key": "skill:video-cut"},
         "rubric": "promotion:quality", "value": 0.9, "rationale_ref": "rule:promo-video"},
    ])})
ledger = PromotionLedger(redaction_checker=..., score_hook=make_score_hook(layer))
```

`make_score_hook(layer)` 产出的适配函数不抛异常（后端异常已在层内收口为 None + refusal）；每次打分落一条 DecisionRecord（rubric 缺省 `promotion:quality`），晋升台账里的 `cand.score` 可对账到 `DecisionLog`。注入后原三道门语义不变：质量门 + 脱敏门 +（可选）决策层打分，任一 UNKNOWN/BLOCKED → 拒绝。

## 6. 注入点 2：eval-gate L1 灰区 score_fn（形状对齐，不连线）

L1 级联漏斗（v1.6 §6）：**规则 → 决策层 Score 灰区 → 大模型深度分析**。eval-gate 仓 `eval_gate/funnel.py` 已写死其侧形状（本仓不 import 对方仓，仅以文档对齐）：

- 对方注入点：`ScoreFn = Callable[[Candidate], float]`，`Candidate(candidate_id, kind, payload, metadata)`；
- 对方消费语义：`score >= block_above → BLOCKED`；`score <= pass_below → PASS`；两者之间 = 灰区 → 升级深分析；**score 返回 None/非数值/NaN → 对方 fail-closed 归 UNKNOWN**。

glue 侧适配（演示形状，接入时在装配层写，不改两仓代码）：

```python
def make_l1_score_fn(layer, *, rubric="evalgate:l1"):
    def score_fn(candidate):                     # candidate: eval_gate.funnel.Candidate 形状
        result = layer.score(
            {"candidate_id": candidate.candidate_id, "kind": candidate.kind,
             "payload": dict(candidate.payload), "metadata": dict(candidate.metadata)},
            rubric)
        return None if result is None else result[0]   # None → 对方 fail-closed UNKNOWN
    return score_fn
```

两端语义同形：规则层未命中才进 Score；决策层弃权在对方收敛为 UNKNOWN（任何一层都不得静默放行）。

## 7. 准入硬规则（WO-0007 件 2，`admission.py`）

**消融通过 且 GuardrailRun 聚合 verdict == PASS 才 ALLOWED**（append-only，记录绑定 Spec 版本 `guardrail_spec_version`、seal 引用、消融证据引用 `ablation_evidence_ref`）：

| 消融 \ GuardrailRun | PASS | BLOCKED | UNKNOWN |
| --- | --- | --- | --- |
| IMPROVED（通过，默认接受集） | **ALLOWED** | REJECTED | HELD |
| NEUTRAL / REGRESSED（未通过） | REJECTED | REJECTED | HELD |
| INCONCLUSIVE（样本不足/出错） | REJECTED（**拒绝准入的合法结论**，WO-0007 原文） | REJECTED | HELD |

- 优先级：**门控协议完备性优先**——UNKNOWN 一律 HELD（补齐后重新准入，不终审）；消融结论只在门控 PASS/BLOCKED 时参与终审。
- 默认接受集 = `{IMPROVED}`；可用 `AdmissionLedger(accept_ablation_verdicts=...)` 放宽——放宽本身是门禁配置变更，走 PR（§9）。
- verdict 派生：`guardrail_verdict_of(run)` 与 `GuardrailRunStore.gate()` 用同一个 `guardrail.aggregate`，语义一致（OPEN/VOID/必填 check 未提交 → UNKNOWN）。

## 8. 消融实验（WO-0007 件 1，`ablation.py`）

原生自演进通道只有置信度阈值与审批，**没有对照实验**——`AblationExperiment` 补上：control（现状）/ treatment（候选经验/技能包）两臂 + `run(evaluator)` 同一输入集双跑、**配对比较**。verdict 四态：IMPROVED / NEUTRAL / REGRESSED / INCONCLUSIVE。

统计口径（零第三方依赖）：配对差 = treatment − control；显著性 = 双侧精确符号检验（`sign_test_p`，纯标准库）；判定需"方向 × 显著"同时成立。求值链上任一异常/非有限分数 → 该对丢弃，出现任何错误对 → 整体 INCONCLUSIVE（fail-closed：带洞的证据不支撑准入）。样本 < min_samples（默认 10）→ INCONCLUSIVE。**结果一次定论**：`run()` 只允许一次，`AblationResult` frozen、自带 `evidence_ref`（`ablation://<experiment_id>`）——准入证据不可漂移。

## 9. 外发形状（skill-pack OKF review 段）

`export_to_skillpack(record)` 把 **ALLOWED** 的准入记录外发为对方仓 manifest `review` 段形状（对齐 skill-pack docs/okf-spec.md §3/§6；本仓不 import 对方仓）：

```json
{ "gate_ref": "guardrail://admission/<admission_id>",
  "evidence_ref": "ablation://<experiment_id>",
  "approved_at": "YYYY-MM-DD" }
```

`gate_ref` 必须 `guardrail://` 前缀、`evidence_ref` 非空（对方校验器强制）；非 ALLOWED 的记录外发直接抛 `AdmissionError`（把拒绝/搁置装扮成 pack review 是违规）。

## 10. P 后续：决策层选项空间生长必过门禁（v1.7 §12.7）

骨架人设计、血肉 agent 生长：决策层的**选项空间**（labels / 规则表 / rubric / 后端装配）每次生长 = 演进审批 + **消融（ablation.py）+ 准入（admission.py，绑定 Spec 版本与证据）+ 外发（export_to_skillpack）**，控制台可见 diff。约束写死：

- `set_backend` 仅限启动装配；规则/后端/rubric 变更走 PR（不在运行时热插未知后端）；
- 新原语、新后端类型（如真实 ModelBackend 接线）必须经 PROP 诞生，不走暗门；
- 治理面（WO-0012 TUI）读 `DecisionLog` 与 `refusals` 展示决策流——本层留痕即其数据源。

## 11. 测试与状态

`python -m pytest`：140 passed（既有 91 + 新增 49：decision 21 / ablation 13 / admission 15）。`tests/conftest.py` 增加了 src 路径自举（本仓为独立克隆，全局 editable 安装可能指向其他克隆——测试必须测本仓代码）。

未验证内容（如实）：ModelBackend 未发真实模型调用（属后续工单：Higress 联调 + OpenBao 短时凭据注入）；skill-pack / eval-gate 形状以对方仓文档与代码为契约，未做跨仓集成测试。
