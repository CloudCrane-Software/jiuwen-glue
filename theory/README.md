# theory/ — 理论层（theory 圈）

蓝图 v2.0 §7：每个治理对象三件套 **spec / eval / contract**。本目录是 theory 圈在
glue 仓的落点（首次案例：guardrail、leases）。

## 圈边界（写死，蓝图 §1.2）

- **theory 不 import 任何圈的代码**：本目录只有 Markdown 文档，无 `.py`，
  CI/import 边界自查以 `grep -rn "^import \|^from " theory/` 零命中为证据。
- **product 只读 theory 文件路径，不 import theory 代码**（文件引用形态）：
  `src/jiuwen_glue/meta_governance.py` 仅以字符串常量登记本目录路径，
  不存在 `import theory`。
- theory 与参考实现（product 服务+glue）**互相独立演进**：理论版本 bump →
  实例注册表为每个实例生成升级建议单（蓝图 §7；骨架见
  `src/jiuwen_glue/meta_governance.py` instance_registrar）。

## 唯一人工写入点

`theory.Approval`（蓝图 §7）：三件套 draft → owner Approval 才定稿生效；
执行期 Challenge 留在权限轨（TeamPermissionRail ask），不进 theory。

## 目录

| 对象 | spec（治理语义） | eval（语义一致性对照/oracle） | contract（接口冻结） |
| --- | --- | --- | --- |
| guardrail | [spec.md](guardrail/spec.md) | [eval.md](guardrail/eval.md) | [contract.md](guardrail/contract.md) |
| leases | [spec.md](leases/spec.md) | [eval.md](leases/eval.md) | [contract.md](leases/contract.md) |

## 行号基线声明

eval.md 中的 `file:line` 引用以 2026-09-27 工作树为准（main=7895c23，含并行
W-01/W-02 修订）；行号会随后续提交位移，复核以函数名 + `git blame` 为准。
