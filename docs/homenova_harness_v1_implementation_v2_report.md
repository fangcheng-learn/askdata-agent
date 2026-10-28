# HomeNova Harness V1：最新方案实施报告

日期：2026-10-28。开发与评测资料，不能进入运行时知识。**审核调度和步骤决策代码已迁移，本地检查通过；两次真实业务试运行均失败，尚不能宣布首版业务验收通过。**

## 本次实现

实际修改对象为`references/askdata_studio`，后端8001、前端5174，保留原数据源、鉴权、模型配置和用户已有改动。

- 执行图移除独立过程Reviewer，正常和异常路径均不调用过程审核。Planner声明要求编号、步骤依赖、预期输出、前置条件及作用范围。
- 后续Executor在原有单步调用中读取真实结构化依赖结果；适用时同次输出SQL／契约或分析，不适用时只给出结构化动作建议。没有另加一次模型判断调用。
- Controller校验条件ID、证据、数值断言、影响范围和完整任务要求；处理继续、跳过相关分支、澄清、有限修复及重规划。无下降时不自动partial，独立目标继续；相关后继步骤与真正独立目标按实际依赖区分。
- 最终审核发现可执行缺口时返回工作，历史已解决的stop_reason不永久阻止完成。证据／草稿实质签名阻止同稿反复审核，必要修改后才允许再审；只展示审核定稿。
- 修正原始数值指标与多期SQL输出别名的绑定：定义、字段来源、聚合等检查保留，技术别名按实际结果血缘记录；未知业务标签、错误来源／聚合／倍乘仍被拦截。Planner提示避免无请求、无覆盖依据的同比及猜测透视字段名。
- 最小界面增加步骤建议、影响范围、任务要求、过程／最终审核次数及过程事件。任务和结果仍在服务进程内保留，重启不能恢复。

预算保持工具20次、单步额外修复2次、重规划1次、主动300秒／调查240秒、前端等待360秒，未放宽。

## 全过程与Skill记录

[逐任务过程报告入口](../data/homenova_harness_v1/private/implementation_v2/process_reports/index.html)，每题均有HTML、Markdown及原始JSON。报告包括原问题、任务ID、计划、实际依赖、候选SQL与是否实际执行、契约错误、证据、Controller与审核、节点usage及耗时；离线生成报告没有模型或网络调用。

新增运行时事件记录入口路由与Skill选择，以及**实际注入**模型节点的Skill名称、正文SHA-256、字符数、步骤和累计主动时间。工具调度、修复和重规划也保存有序事件；失败响应同样带回预算事件。记录不包含密钥或完整提示词。

Skill是规则内容注入，不是另一次模型请求。“选中业务Skill”和“某节点实际注入Skill”分开统计。当前两题早于该逐节点打点补充：第1题明确选中`business_diagnosis`，Planner和三次`database_query`注入可由当时源码及已完成节点还原；第2题失败响应丢失了入口选择信息，不能把默认空数组解释为未选Skill。历史还原注明依据，不能伪装成当时直接记录的事件。新打点仅通过本地测试，未追加第三次付费复验。

## 两次真实试运行

按用户上限共两个不同自然问题，每题只创建一次任务。台账在发起HTTP前锁定计数，崩溃或失败不退回名额。**同一步初次生成加两次修复是一个任务内三次模型请求，不是重跑三个任务。** 不再重跑GMV、不运行全量经营题、不额外探测模型可用性。

| 项目 | 第1题 | 第2题 |
|---|---|---|
| 用户原问题 | 7月GMV怎么下降了？感觉销量明明还可以啊。 | 哪些商品卖得多却不怎么赚钱？热卖款利润应该也高吧。 |
| 任务ID | `0c46fb221421` | `7a7949e48e2f` |
| 路径 | Harness，形成S1—S4计划 | 原单步查询路径，未进入Planner |
| 结果 | failed；无有效业务证据 | failed；无有效业务证据 |
| Chat／Embedding／Rerank次数 | 5／1／1 | 4／1／1 |
| 工具预算计数 | 1，语义检索；业务SQL执行0次 | 1，语义检索；业务SQL执行0次 |
| 单步额外修复／重规划 | S1修复2／重规划0 | entry修复2／重规划0 |
| 独立过程／最终审核 | 0／0，未到交付阶段 | 0／0，未到交付阶段 |
| API耗时 | 136.271秒 | 88.590秒 |
| Chat输入／输出／合计token | 50,987／8,214／59,201 | 83,146／4,949／88,095 |
| Chat输入中的缓存token | 21,760 | 51,712 |
| Embedding／Rerank token | 91／6,494 | 14／4,264 |

两题Chat合计147,296 token，缓存是输入token的子集，不重复相加。Embedding、Rerank是不同服务用量，分别列出；没有阿里云逐条账单关联，不能据此宣称精确人民币费用。

**第1题过程。** 入口预处理→公开语义检索→Planner→S1三次SQL候选→契约失败→相关依赖阻塞→无可靠结果，返回失败，不调用无意义的草稿／最终审核。首候选的件数定义未逐字匹配公开字段描述；后两次改了定义，但`sales_volume_items`未绑定多期输出。计划还擅自加入2025年7月，而未确认有该比较期覆盖。该误拦和规划提示随后作了本地修复，未真实重跑；旧候选另有COUNT DISTINCT声明与SQL表达的潜在不一致，不能说整个查询已经验证正确。

第1题的各次Chat为：预处理1,632、Planner 14,357、SQL初次13,225、修复14,998及14,989 token。**三次查询生成合计43,212，约占73%；本次消耗没有来自Reviewer。**

**第2题过程。** 预处理→检索→单步Coder三次候选→契约失败→明确失败。首候选使用契约不支持的`<>`；后两次报未声明`order_items.quantity`筛选（实际SQL含聚合数量大于零的筛选）。更深的问题是把`quantity`标为成功支付GMV、用发货队列限定退款替代注册发生期口径、成本恢复缺少验收时点约束，最后候选还存在LEFT JOIN缺失退款时金额变NULL的风险。没有执行这些候选，也没有把“最后一项校验修掉”当成正确。

第2题Coder三次共86,642 token，占该题约98%；单次输入26,377—27,912 token。检索先命中20项，再按公开指标依赖扩展85项，合计105项，主要是多张表的宽依赖；这些上下文在修复时重复携带，是本次最明显的消耗放大来源。第2题未覆盖新Harness正常交付路径，保留该事实，不改问题凑通过。

[真实原始记录1](../data/homenova_harness_v1/private/implementation_v2/case_1.json) · [真实原始记录2](../data/homenova_harness_v1/private/implementation_v2/case_2.json) · [用量汇总](../data/homenova_harness_v1/private/implementation_v2/run_summary.json) · [独立审阅与Skill还原依据](../data/homenova_harness_v1/private/implementation_v2/independent_review.json)

## 验证结论

- 后端129项本地回归通过，含同次前置判断／SQL、真实依赖传递、越界跳过拦截、独立目标保留、相关后继不适用、必要目标补路径、最终审核退回工作、无变化不复审、文稿实质修正后再审、预算及付费试运行台账限制，以及Skill实际注入记录。
- E适配14项通过；前端vue-tsc与生产构建通过。以上均无新增付费调用，不能替代业务验收。
- 510份原始产物、历史资料及受保护副本指纹一致。未改A/B/C/D、生成器、答案、参考SQL或原数据库。
- 原八类验收标准保留；正常调查、无下降、澄清恢复、多轮、最终交付等新策略业务路径尚未完成真实验证。旧十题记录保留，本轮依用户限制未重跑，不把旧成绩记成新版本通过。
- 两题均无Agent数字结果可核对；独立公开CSV已核算6月GMV3,920,044欧分／1,331件，7月3,090,583欧分／917件，8月3,989,701欧分／1,199件，保存于独立进程产物，未注入Agent。
- 界面自动检查被Work设置拒绝：`Local browser use is disabled`。未绕过限制，本轮界面交互未验证；构建通过不能代替界面验收。

[后端回归](../data/homenova_harness_v1/private/implementation_v2/backend_tests_final_with_trace.txt) · [E适配](../data/homenova_harness_v1/private/implementation_v2/phase_e_tests_final.txt) · [前端构建](../data/homenova_harness_v1/private/implementation_v2/frontend_build_final_with_trace.txt) · [保护文件核对](../data/homenova_harness_v1/private/implementation_v2/protected_check_final.json) · [独立事实](../data/homenova_harness_v1/private/implementation_v2/independent_facts.json)

两次真实运行间的本地修复、之后的Skill打点均有源码指纹区分。第1题、第2题与最终源码不是同一版本；不能合并成“最终版两题已验证”。

## 启动与下一步

```sh
references/askdata_studio/backend/.venv/bin/python scripts/homenova_phase_e/start.py restart --record-dir data/homenova_harness_v1/private/implementation_v2/runtime_final_with_trace
```

打开[本机界面](http://127.0.0.1:5174/)，沿用原登录，选择HomeNova 8月底快照；模型仍为原配置`qwen3.7-plus`。8001／5174健康，服务启动不调用付费模型。定时备用续跑已取消，本轮不再有后台付费验收安排。

仍需处理的是查询生成／契约表达及业务事件口径稳定性，不是继续增加Reviewer。建议先离线明确原始计量、注册指标、分量事件与结果列的可验证契约，收紧按整表扩展的检索依赖，减少错误修复时重复上下文；在新的明确付费预算下，才安排一条完整GMV闭环及界面复验。本次保留失败，不把Mock或受控单测当真实成功。
