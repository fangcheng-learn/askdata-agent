# 查询校验简化实施报告

日期：2026-10-28。开发评测资料，不进入运行时Skill、提示词或检索源。

**结果：已减少措辞、命名和等价写法造成的前置阻断，保留并补强真实来源、金额单位及权限边界。旧GMV候选可通过新校验，并在一次本地只读执行中得到与独立公开CSV一致的数字。没有新增付费模型或Agent任务，完整Agent闭环仍未真实复验。**

## 改了什么

实际对象为`references/askdata_studio`，后端8001、前端5174。

| 原行为 | 当前行为 |
|---|---|
| 要求模型逐字复制指标／字段定义 | definition可省略；按已识别注册ID或真实字段绑定，原值及补齐记录保留 |
| 显示名、技术别名不同可能阻断 | 按真实来源识别输出；原始计量的陌生显示名映射到物理字段，不注册或认证新业务指标 |
| 契约只接受`!=`等固定写法 | 兼容`<>`、运算符大小写、表／字段大小写；权限使用规范表ID |
| SQL多了未重复写进说明的筛选就阻断 | 保存实际SQL摘要及`undeclared_sql_predicate`待核对项，允许受控执行 |
| 声明去重计数却未直接写COUNT DISTINCT就拒绝 | 允许上游去重再SUM等写法，保留`distinct_count_not_proven`；不自动认证等价 |
| 相同错误反复生成至修复额度耗尽 | 同一错误且相关产物未变时提前停止；SQL不变而契约得到有效修正可继续 |

`query_contract.validation`新增`normalizations`、`pending_checks`、`sql_summary`；这些记录从现有工具结果／证据进入后续Executor、草稿、最终审核、同会话追问及所选历史表，不增加独立模型判断或过程Reviewer。

待核对数据可以用于继续取证；未解决的疑问不能被用于推翻前提、跳过相关调查。最终Reviewer结合SQL和结果判断可解释的等价写法，不因存在记录就强制partial。`basic_valid`和`business_definition_verified=false`不代表业务已通过。

明确声明的时间、筛选、来源和明显聚合冲突继续检查；显示别名的兼容不取消下游确实需要的输入字段契约。权限、只读、授权快照、单条SQL、结果引用和原预算没有放宽。

## 补强的真实冲突与权限检查

- GMV等金额指标必须具备相符税口径的真实金额来源，不能用quantity代替。
- 金额只出现在CASE条件或无关MAX列中，不能满足SUM金额的要求；检查聚合值的实际字段来源，包含CTE改名情况。不声称已证明任意复杂公式正确。
- 原先全局按CTE名字排除表，可能让同名CTE遮住真实受限物理表。已改为按查询作用域识别实际物理来源，两层校验均使用真实表ID检查权限；正常CTE和大小写兼容保留。
- 同会话历史表会带回有效证据的校验记录，排除superseded版本；服务端缺失校验元数据时明确标记不可用，不把空数组解释为已核实无问题。

## 旧SQL离线回放

只读取已保存候选、公开Schema／语义和8月底公开CSV。没有调用运行时路由、Planner、Executor模型、Embedding或Rerank。候选SQL没有修改，独立核算不进入运行时。

| 保存任务 | 候选校验 | 本地业务SQL执行 | 结果 |
|---|---:|---:|---|
| GMV：`0c46fb221421` | 3／3通过，均保留去重表达待核对项 | 相同SQL只执行1次，其余复用 | 6月／7月关键数字与独立CSV核算一致 |
| 商品利润：`7a7949e48e2f` | 0／3通过 | 0次 | GMV错误绑定quantity，明确单位冲突被拦截 |

独立按成功capture、Europe/Berlin月份加总公开商品行，与原GMV SQL核对金额、订单、支付件数及客单价，共8项一致：

| 月份 | 商品GMV | 支付订单 | 支付商品件数 | 商品客单价 |
|---|---:|---:|---:|---:|
| 2026年6月 | €39,200.44 | 946 | 1,331 | €41.44 |
| 2026年7月 | €30,905.83 | 687 | 917 | €44.99 |

原SQL还返回2025年7月为0，但覆盖未确认，**该0不能当作有效同比证据**。SQL可执行不代表原规划全部正确，也没有产生GMV下降原因结论。

回放记录保留当时源码指纹；随后强化物理表权限识别后，仅重新校验候选、没有再次执行SQL，结论不变。原两次真实任务仍是failed，不回填成成功，不增加或重置付费台账。

[完整离线回放](../data/homenova_harness_v1/private/query_validation_v3/replay.json) · [最终源码再次校验，execute=0](../data/homenova_harness_v1/private/query_validation_v3/replay_final_validation.json) · [离线脚本](../scripts/homenova_harness_v1/replay_query_validation_v3.py)

## 检查与运行

- 最终后端153项、E适配14项通过；前端vue-tsc与生产构建通过。包含兼容正例、真实来源／金额／聚合／日期／权限负例、无进展停止、待核对项传递及追问恢复。
- 原有把显示名／定义差异当失败的断言按用户确认策略调整；错误源、错聚合、错日期、越权、写操作等负例保留或补强。LEFT JOIN无法确认计数等价时改为pending，不宣称已经证明等价。
- 510份保护文件及原两次输出／付费台账指纹未变；本轮模型、Agent、Embedding、Rerank付费调用为0。
- 原启动器已更新本项目服务，8001／5174均健康；沿用原模型和安全配置。当前启动记录后端PID12392、前端PID1856，仅代表该次状态。
- 没有新增浏览器自动操作，本轮界面交互未验证；构建不代替实际界面验收。

```sh
references/askdata_studio/backend/.venv/bin/python scripts/homenova_phase_e/start.py restart --record-dir data/homenova_harness_v1/private/query_validation_v3/runtime
```

[本地界面](http://127.0.0.1:5174/) · [后端153项记录](../data/homenova_harness_v1/private/query_validation_v3/backend_final_permissions.txt) · [E适配](../data/homenova_harness_v1/private/query_validation_v3/phase_e_tests_final.txt) · [前端构建](../data/homenova_harness_v1/private/query_validation_v3/frontend_build.txt) · [保护文件核对](../data/homenova_harness_v1/private/query_validation_v3/protection_check.json)

## 仍待完成

完整GMV调查的新模型执行、后续取证、最终交付及界面效果没有新增真实验收。利润题的事件期间和成本恢复口径、预处理擅加比较期，以及宽依赖上下文问题没有因兼容格式而自动解决。没有建设通用SQL业务正确性证明器，也没有通过关闭校验追求执行成功。后续真实复验需要新的明确付费预算，原八类业务验收目标不降低。
