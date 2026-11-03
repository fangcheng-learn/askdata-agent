# 调查记录与引用兼容修复

2026-10-29。范围为 `references/askdata_studio`，入口8001／5174。新增应用模型API调用0、真实Agent试跑0；不包含Codex开发对话用量。

## 这次为什么停

用户任务 `c4f90734fc70`：“7月GMV怎么比6月少了？感觉销量明明还可以啊”。原运行已取得真实R1／R2，包含两月GMV、支付订单数、商品件数及客单价。随后模型用F1／F2／F3引用本轮已声明、且有R1／R2来源的事实。

旧程序只允许直接写结果编号，把这种可解析的事实引用当成未知证据；整份记录原子拒绝，有效事实也没保存，下一步查询未进入SQL生成。模型连续三次遇到同一格式问题，耗尽react_protocol首次后两次额外修复，最终部分完成。另有一次可选explanations=null也存在格式误拦风险。

原任务记录：主动166.096秒、1轮业务取证、4次工具；chat10、embedding1、rerank1。SQL模块有过一次实际修复，但此次整轮停止由进度引用协议触发。原历史状态与结果保留，不改写成成功。

## 通用修复

- 后端实际类型定义生成进度Schema，进入正常ReAct输入；提示词与程序使用同一份字段／状态定义。
- 在当前用户、任务、数据源、快照的有效结果目录内，沿明确事实链解析引用，保存真实R引用及F关系；同轮新增事实、已有事实和唯一结果简写均可解析。不按编号尾数猜来源。
- 记录逐条处理：null不更新；坏的可选笔记只拒绝该条，有效更新和独立查询继续。未知、失效、循环、歧义、越范围、无实际来源及假设冒充证据仍不能使用。
- 动作依赖单独解析；当轮F更新失败不能偷偷退回旧F。单结果动作若指向多个结果，要求明确选择。原问题不能改写或降级；新核心问题的引用或关闭有误时，保留问题为open，不把它丢掉后宣布完成。
- 保存字段路径、错误引用和转换记录，反馈给后续正常决策及收尾审核。可转换写法不新增模型纠错调用、不消耗协议修复额度。格式转换仅验证来源身份，不证明数字、文字或因果判断正确。

主要改动：`workflows/progress_protocol.py`、`workflows/react_harness.py`、`workflows/react_prompts.py`及相关测试；方案第15节同步记录。未增加过程Reviewer，未改变SQL总共3次、5轮业务取证、20工具或时间限制。

## 验证与边界

验收范围在测试前固定于新记录目录的acceptance.md。三个真实失败决策**分别从相同原始检查点离线回放**：均接受F1／F2／F3及相关记录，并进入原先被阻断的下一步动作派发；react_protocol修复扣次0。回放使用原模型输出与原结果，动作派发由本地观察器接住，未执行新SQL，也不冒充真实业务完成。

专项覆盖独立查询走真实本地MCP、错误笔记隔离、动作依赖阻断、跨范围拒绝、旧F回退禁止、重复结果编号拒绝、核心问题保留及收尾上下文。外部网络在后端回归进程中禁用。

| 项目 | 结果 |
|---|---|
| 原始失败输出独立回放 | 3/3进入下一步动作派发，无协议修复扣次 |
| 后端完整回归 | 543项通过，140.570秒；外部网络禁用 |
| E阶段回归 | 14项通过，0.805秒 |
| 前端 | 类型检查及生产构建通过 |
| 数据与历史保护 | 134份CSV与原任务载荷、预算保持不变 |
| 应用模型／真实Agent | 新增调用0／试跑0 |

首轮542项通过后，独立复核补上重复结果编号拒绝测试，再完成上述543项最终回归。此过程未新增付费验收。服务通过现有脚本重启，部署健康记录与完整回归日志保存在新目录。

后续真实模型仍可能产生不可执行动作、错误SQL或不足的经营解释。本次已验证的是已知合理引用的兼容与错误隔离，没有认证新模型运行已经完成整项调查。

记录目录：`data/homenova_harness_v1/private/progress_reference_compat_20261029/`，与E阶段及此前评测隔离。


## 运行与记录

界面：<http://127.0.0.1:5174/>，后端8001。重启后需重新登录；未自动恢复或重跑历史任务。主模型与Flash路由配置沿用原值。

```sh
references/askdata_studio/backend/.venv/bin/python scripts/homenova_phase_e/start.py restart --record-dir data/homenova_harness_v1/private/progress_reference_compat_20261029/runtime
```

- [验收清单](../data/homenova_harness_v1/private/progress_reference_compat_20261029/acceptance.md)、[三帧原始输出回放](../data/homenova_harness_v1/private/progress_reference_compat_20261029/replay_receipt.json)。
- [最终后端回归](../data/homenova_harness_v1/private/progress_reference_compat_20261029/backend_tests.log)、[E回归](../data/homenova_harness_v1/private/progress_reference_compat_20261029/phase_e_tests.log)、[构建](../data/homenova_harness_v1/private/progress_reference_compat_20261029/frontend_build.log)。
- [验证收据](../data/homenova_harness_v1/private/progress_reference_compat_20261029/verification.json)、[部署健康](../data/homenova_harness_v1/private/progress_reference_compat_20261029/runtime/health.json)。
