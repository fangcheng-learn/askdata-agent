# HomeNova E 阶段：公开语义、AskData接入与真实Agent首轮报告

**开发与评测私有资料，不进入运行时检索。** 已完成数据源、公开语义、真实混合索引、权限与界面接入。基础查数已经通过；复杂经营题尚未达标，不能据本报告宣称完整经营分析Agent已可用。

## 1. 实际项目与入口

- 实际修改并运行的是 references/askdata_studio：FastAPI 8001、Vue/Vite 5174；不是根目录DataFoundry，也不是askdata_studio_ops_release运营副本。
- 打开 http://127.0.0.1:5174/，沿用原登录。顶部选择“HomeNova · 截至2026-08-31”或尾窗末快照；默认8月底，原示例销售与电商演示数据仍可选择。
- 切换数据源清空会话、字段与参考结果。场景时间来自metadata；尾窗末显示分析日2026-10-15，同时明确新订单覆盖仍截至8月31日。

## 2. 公开语义与AskData索引

| 产物 | 位置 |
|---|---|
| 公开语义源 | [public_semantics](../data/homenova_phase_e/public_semantics) |
| 构建程序 | [build_semantics.py](../scripts/homenova_phase_e/build_semantics.py) |
| 实际混合检索实现 | [HomeNovaSchemaIndex](../references/askdata_studio/backend/app/retrieval/homenova_index.py) |
| 实际索引构建/检索证据 | [hybrid_index_checks.json](../data/homenova_phase_e/private/hybrid_index_checks.json) |

每截面15表、207字段、27关联、40指标，包含粒度、主外键、关联基数、重复风险、逻辑/SQL类型、完整指标依赖、术语澄清、能力边界、单位和来源。仅结构化D中性字典与当前公开快照；未用手册、生成配置或答案作语义来源。

没有新建替代检索引擎。HomeNovaSchemaIndex继承现有SchemaIndex的Embedding、BM25、dense、RRF和Rerank，只替换中性文档来源、签名及存储位置；指标依赖补全发生在真实召回之后。公开JSON不包含dense_vector，不能冒充已建向量库。

| 数据源 | 实际向量字段 | 维度 | BM25/dense/RRF候选 | 实际Rerank来源 |
|---|---:|---:|---|---|
| homenova_20260831 | 207 | 1024 | 30/30/40 | qwen3-rerank |
| homenova_20261015 | 207 | 1024 | 30/30/40 | qwen3-rerank |

上述为独立Schema检索探针，不是经营题正确率。向量模型为text-embedding-v4，两截面各自保存于backend/data/homenova_runtime/<datasource_id>/schema_store.json，原schema_store不覆盖。模型按任务取得字段与指标依赖，不把十五表全量Schema无差别注入。

## 3. 实际隔离与场景时间

- [实际边界检查](../data/homenova_phase_e/private/runtime_boundary_checks.json)：30/30通过。
- 账号授权与显式数据源相交为单库；省略选择的旧请求排除HomeNova。Schema API、索引、数据库工具、会话键、长期记忆及已保存结果引用分别按截面隔离。
- 数据路径固定为对应D公开目录，语义固定为对应E公开目录；没有仓库、docs或private目录扫描。客户端传入的跨会话分析结果不带入HomeNova模型上下文。
- DuckDB只物化所选十五表并关闭external_access；拒绝外部文件/网络表函数、其他库限定名和机器时钟。金额BIGINT欧分、邮区VARCHAR、UTC绝对时间TIMESTAMPTZ；连接业务时区为Europe/Berlin。
- 8月底排他截点2026-08-31T22:00:00Z，尾窗末为2026-10-15T22:00:00Z。resolve_date_range/current_datetime使用场景时钟；观察期与3—8月新交易窗口分别传入模型。
- 题库、标准SQL、手册、生成参数、评测日志与本报告都不在上述检索和工具允许路径内；隔离不依赖一句提示词。

## 4. 实际Agent测试结果

真实调用与界面相同的已登录POST /api/query，模型qwen3.7-plus；没有Mock或用参考SQL替代回答。runner只给题目、单位约定、当前数据源与公开资料。私有参考资产仅由独立evaluate进程及独立审阅读取。

完整第三轮按十题统计：通过0、失败9、部分完成1、阻塞0。Q09拆为两个隔离请求，按11个case统计：10失败、1部分完成。

| 题目 | 完整轮结论 | 实际证据与限制 |
|---|---|---|
| q01 | 失败 | 月度多事件指标：完整轮在查询契约校验阶段停止；收尾定向复测仍因契约维度/候选列名错误失败。 |
| q02 | 失败 | SKU比较：工具前停止；候选把发货件数当支付件数，不能当作真实回答通过。 |
| q03 | 失败 | 竞品与转化：工具前停止；候选有虚构字段、缺SKU/渠道及已知报价时效等问题。 |
| q04 | 失败 | 广告：日期键角色误拦已修；候选预算/消耗/访问有正确部分，回报口径仍不符。 |
| q05 | 失败 | 促销量价利润：工具前停止；候选队列、件数/均价及扣退规则不完整。 |
| q06 | 失败 | 缺货：工具前停止；候选用有阻断事件的天数代替连续库存区间，费用窗口也不符。 |
| q07 | 失败 | 物流反馈：工具前停止；候选有订单聚合优点，但评价粒度及退款运费不完整。 |
| q08 | 失败 | 退款：工具前停止；候选部分月度金额正确，原月时间及发货前后/未验收拆分不全。 |
| q09 | 失败 | 双截面：两次均工具前停止；晚稿部分财务数正确，但固定队列分母和订单级反馈不完整。 |
| q10 | 部分完成 | 直接回答保留因果不确定性，未编造精确反事实；缺可查询的数量证据，故仅部分完成。 |

第三轮Q01—Q09均在查询契约校验阶段停止，没有进入业务SQL执行。审阅中原样重放候选SQL仅用于定位问题，没有修改候选SQL，也没有把这些离线结果当作Agent成功。重放采用真实连接的Europe/Berlin时区，此前审阅中以UTC连接造成的诊断偏差已纠正并保留纠错记录。

- 第一轮保留3个完整失败响应后暂停，未完成的请求单独记录中止；没有覆盖旧记录。
- 第二轮11次请求均属外部模型阻塞：首题连接被重置，后续命中现有30秒熔断；不算数据口径失败或通过。
- 第三轮完成全部10题/11请求，模型可调用，但发生上述契约及SQL语义问题。
- 收尾修复后对Q01另做定向第四轮，仍失败；没有把修复本身算作通过，也未声称最终源码已重跑完整十题。
- Q09早晚截面用同一问题、不同UUID会话及单库授权，比较在模型外发生；没有把晚期事实提供给早期运行。

## 5. 另行通过的基础查数与界面验收

实际问题：“查询本月成功支付订单量和商品GMV，金额用欧分。”。场景日期2026-08-31；返回成功支付订单量855，商品GMV 3,989,701欧分。该次API耗时38.866秒，实际经过database_query和当前数据库MCP工具。
独立程序在回答之后读取快照payments/orders重新核算，金额和单数精确一致；没有向Agent提供参考数字。浏览器也实际登录、切换截面后提交同问，显示相同结果并展开SQL核对。这一基础烟测单独计分，不抵充十题经营基线。

![实际界面与场景时间](/Users/fangchengdemac/Documents/ChatGPT/dataagent/data/homenova_phase_e/private/ui/relative_month.png)

[实际API响应](../data/homenova_phase_e/private/ui_smoke_api.json) · [独立核对](../data/homenova_phase_e/private/ui_smoke_review.json) · [界面文本和SQL](../data/homenova_phase_e/private/ui/relative_month_ax.txt)

## 6. 主要修正与尚未解决的问题

- 修正显式datasource参数被丢弃、空选择暴露两截面、跨截面上下文及旧索引覆盖风险；保留原登录与旧数据源。
- 从初稿词法分支改回AskData真实混合检索；补指标状态/时间/连接依赖，接入术语和数据能力边界，删除重复Schema描述。
- 修正已登记指标名与物理列标签硬比、外键不能分组、主键日期失去time角色、JOIN ON筛选误拦。用户原文由服务端绑定，保留模型原声明审计；明确半开时间边界与DuckDB日期语法。
- 保存三次前置校验候选SQL/contract/error，增加检索来源审计；补pytz运行依赖与网络读中断处理；前端等待上限与验收一致。
- **仍未解决：**指标依赖补全含表级兜底，部分字段冗余，真实向量库构建成功不代表召回相关性已经达标。失败不能全部归因于模型；还应收窄依赖闭包并完善契约表达。复杂多事实口径、CTE/派生维度与聚合契约仍不稳；候选SQL仍有字段/别名、队列分母、时间归属、退款/验收及反馈粒度错误。现有工作流不是完整经营诊断控制器，不能据基础查数成功外推。
- 模型服务发生过连接重置与熔断，当前少量成功不代表服务稳定。契约通过只说明Schema/编译检查，business_definition_verified仍为false，需要模型外核对。
- 本次没有继续造数、重写生成器、修改参考答案或开发完整Harness。

## 7. 验证、私有记录与复现

- 最终后端回归78项、E适配/评估单测14项通过；它们是单元/边界验证，不替代真实Agent十题结果。前端类型检查与生产构建成功。
- [原始文件保留](../data/homenova_phase_e/private/protected_files_check.json)：353个保护文件全部未变，含A/B/C/D与原数据源、旧索引。
- [完整第三轮记录](../data/homenova_phase_e/private/agent_runs/round_3/run_summary.json)、[自动外部核对](../data/homenova_phase_e/private/agent_runs/round_3/offline_evaluation.json)。
- [独立审阅Q01—Q05](../data/homenova_phase_e/private/review_round_3_q01_q05.json)、[独立审阅Q06—Q10](../data/homenova_phase_e/private/review_round_3_q06_q10.json)、[收尾Q01复测](../data/homenova_phase_e/private/review_round_4_q01.json)。
- [最终汇总](../data/homenova_phase_e/private/final_summary.json)、[源码与公开资料指纹](../data/homenova_phase_e/private/release_manifest.json)。

启动、刷新公开语义、真实索引和逐题验收命令见 [E阶段README](../scripts/homenova_phase_e/README.md)。日常启动使用start.py start；修改后端后restart并重新登录，不能同时再开另一个8001实例。

当前可用于已验证的基础查数试用；复杂经营分析与正式演示验收尚未通过。下一步应先收敛单库查询契约与多事件指标计算，复测现有题目，再决定是否扩展Harness。
