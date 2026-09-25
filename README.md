# jiuwen-glue

openJiuwen 胶水层（glue layer）。一句话：**只做 openJiuwen 原生没有的那几件事，其余一律用原生。**

## 定位

依据《建设方案-多Agent系统与GitOps》（PROP-0001，存 CNB `company-ops`）第 0 节第 4 条与第 4.9 节边界总表，glue 只承载原生没有的能力：

1. **GuardrailRun 协议聚合** — 安全检查协议薄层；检查执行本身用原生 `core.security.guardrail`；
2. **Budget Lease** — 预算租约（发放/占用/过期）；
3. **能力治理台账** — 技能机读声明、命中率/成功率指标、版本准入；发现与编排一律用原生 Symphony，禁止自建发现机制；
4. **记忆晋升管线** — 个人→组织资产晋升（质量门 + 脱敏门 + 版本化入库），不回写执行面记忆；附**决策层接口**——高频决策与分类走原生 JevProvider 三原语（PROP-0001 第 0 节第 6 条），大模型只做推理与生成。

## 边界（写死）

- 原生层管"怎么做"，glue 管"准不准进"，控制台管"看得见"。
- 凡可能有两个决策点的，必须收敛为一个；冲突记 ADR。
- 第 4.9 节 14 项边界表之外，glue 不新增决策点。

## 状态

M0 骨架（README / LICENSE / .gitignore）。最小实现随 WO-0003 落地：Budget Lease、Evidence 三态、能力元数据注册，以及三条铁律的失败用例。

## License

Apache-2.0
