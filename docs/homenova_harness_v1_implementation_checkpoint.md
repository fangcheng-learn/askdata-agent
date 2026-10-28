# Harness V1最新方案实施检查点

更新：2026-10-29。本轮代码与报告已交付，本地验证通过，真实业务验收未通过。此文件为开发资料，不得注入运行时。

## 最新：原因目标澄清入口与独立 Flash 路由 V8

- 实际任务31ec175c7c77未执行原因调查：整句“2026年6月份相较于5月份GMV下降的原因”被年份选项子串匹配当成year=2026，未进入路由，继续旧查数SQL。原入口还不当丢掉历史年份并制造占位查询。
- 完整选项／简短选择才恢复普通澄清；多个明确选项不接收任意长文本兜底。完整新目标正常路由创建新任务；纯年份同原任务／预算恢复。入口提示保留原因目标并继承明确年份，不新增独立过程Reviewer。
- ROUTE_MODEL默认qwen3.7-flash，仅入口使用且关闭thinking；Planner／SQL／Reviewer／其他下游仍按LLM_MODEL。连接、安全、Embedding／Rerank、预算不变。
- 新增10项受控回归，完整后端258、E适配14、前端类型检查与构建通过。510保护文件和5份旧记录／台账未变，第二题hold保留，新增付费0。真实Flash路由准确率和完整经营调查没有新验收。报告docs/homenova_routing_v8_report.md，产物private/routing_v8。

## 最新：八月 GMV 时间接口修复 V7

- 实际用户任务5d857a88b6f6首条SQL已正确，原time_range.end未定义包含性、普通链路缺统一提示却强制LT造成误拦；原任务Chat4、Embedding1、Rerank1，业务SQL0。已保存原任务，未重发付费请求。
- 日期工具新增只读end_exclusive且MCP Schema可见；新契约显式end_inclusive=false，含末日日期可转次日，DATE<=末日等价<次日；不把TIMESTAMP末日零点当全天。旧格式按SQL证据规范并保留pending，原文／SQL不改写。
- 三份保存候选均通过，2种SQL本地实际执行且独立CSV核算一致；表内8月仅1—22日、1760行，GMV107,191,658.39 CNY，不代表完整八月。覆盖疑问进入最终审核材料；失败页可展开任务ID与候选。
- 后端248、E适配14、前端类型检查／构建通过。510保护文件及5份旧真实记录／台账未变，第二题hold保留，付费0。真实模型完整业务未新增验收，旧失败不回填。报告docs/homenova_time_contract_v7_report.md，产物private/time_contract_v7。

## 最新：普通查数澄清与第4／6类校验修复 V6

- 修复 Safari 实际暴露的电商演示数据“2026年六月GMV→店铺维度GMV”误拦：描述文字不同不判为定义冲突，原文不必包含完整表名；确认状态独立传入校验器。新选项绑定授权真实来源，用户选择后才确认；旧选项保留历史供核对，不猜测来源。
- 组合筛选、单字段派生、常量日期计算、多个聚合表达式等无法证明的等价写法记pending；已确认来源冲突、直接值／简单时间冲突、金额件数冲突继续拒绝。分量与外连接条件不无提示认证为总体筛选。
- 后端230项、E适配14项、前端检查及构建通过。新增30项测试覆盖来源授权／scope、同任务同预算恢复、合法表达与明确错误。两条受控模型输出+真实本地MCP／SQL的续查，各执行一次SQL、query Skill注入两次；独立CSV核对6月店铺GMV 152,286,820.25 CNY一致。
- 510份保护文件及5份旧记录／台账未变，第二题hold保留，新增付费0；无新的真实模型／检索业务验收，不回填旧失败。记录private/contract_validation_v6/offline_flow.json，报告docs/homenova_contract_validation_v6_report.md。8001／5174沿用原启动器更新。

## 最新：用户要求的手编SQL检查V5（本地完成）

- 新目录private/sql_probe_v5保留首轮发现问题及修复后记录。35组主探针最终26合法通过实际执行、9负例拒绝，21等价结果一致；原始失败没有覆盖或删除。
- 修复SQL条件只按字段／值出现的错误：AND／OR／NOT、单值IN／反向比较、数据字符串大小写、严格半开时间边界、BETWEEN端点顺序；复杂字段／日期／子查询范围记pending不认证。
- 取消强制GROUP BY，窗口／UNION聚合可执行；validator与engine共用安全展开SQL，CTE列重命名按输出位置追踪。
- 额外手编2026年7月商品利润SQL实际返回8SKU，支付／发货／退款完成／验收按各自时间，独立CSV全部核对一致；53笔成本恢复另重算，其中9笔不是7月完成资金退款。没有新跑Agent或模型。
- 最新后端200项、E适配14项、前端构建通过；旧GMV保存项8条和利润SQL复核通过。510保护文件及旧任务／台账未变，validation_v3_real仍1／2且hold=true；第二题未提交、付费0。
- 服务8001／5174已更新且健康；当次后端17476、前端1856。报告docs/homenova_sql_probe_v5_report.md，原SQL与每次结果见probes_final.json、profit_probe.json。完整实时Agent／UI未新增验证，不得外推所有SQL都已保证正确。

## 最新：用户再次授权的引用与收尾修复V4（已完成本地验证）

- 已统一StepDecision／比较／analysis／Reviewer的结果解析，只在当前作用域内唯一映射id/result_ref；模型只见logical id，user/task/snapshot/有效性限制保留。跨任务复用绑定当前身份与克隆读取引用。
- 内层no_progress传到外层；_repair_roots只返回实际可恢复上游，耗尽上游的blocked下游不再空转。已审核边界文案可直接partial交付，不伪装completed、不重复无变化审核。
- 进一步回放原S2发现内层CTE SELECT*误拦；已对明确单Scope输出安全展开，原物理表／递归／语义不确定星号仍拒绝。
- 保存的3份S2引用都可规范化；受控适配器读取原模型输出，原S2 SQL只在本地执行1次，返回5SKU、35项值与独立公开CSV核算一致。没有新模型／MCP／HTTP／Agent任务；原收尾审核受控回放一次接受partial。
- 后端186项、E适配14项和前端构建通过；510份保护文件及旧任务／两轮台账未变。第二题未提交，validation_v3_real的hold=true保留，本轮付费0。
- 最新报告docs/homenova_reference_controller_v4_report.md；新产物private/reference_controller_v4（backend_final.txt、replay.json、protection_check.json）。原partial任务状态不回填。
- 已按原启动器更新8001／5174，健康；记录后端16105、前端1856仅代表当次。完整实时GMV／S3／利润／UI仍未新验收，不得宣称所有问题已修完，不得自动清除hold或重跑付费题。

## 当前：用户新增授权的两题真实复验（首题暴露问题，待商量）

用户随后要求“修好的话你重新启动跑那两个任务试试，没有修好的话，再跟我商量”。核查后判断已知工程问题已修，153项回归通过，具备复跑条件；没有把旧利润候选错误等同新模型必错，也没有保证业务一定通过。

此次为新的明确授权，固定新目录`private/validation_v3_real`，台账`paid_runs.json`最多2次，仍是两个原自然问题，各发起一次。第1题任务73765c84d353已结束，183.303秒、12Chat、199,907Chat token，partial且未接受交付；S1真实SQL与独立核算一致，但S2误用R1.result_ref代替逻辑证据ID被拦，S3阻塞后无效修复引发三次收尾审核。没有修改应用源码后重跑。

**按用户“没有修好再商量”的要求，当前新台账1／2，第二题未提交，hold_for_user_discussion=true。限额入口已阻止自动继续。** 不得自行清除hold、运行第二题或重发GMV。先就引用安全映射、依赖可恢复性与收尾循环两处修法跟用户确认。详见docs/homenova_validation_v3_real_report.md及新目录逐任务报告。旧implementation_v2台账和记录保持不变。服务按原脚本重启后健康，后端12944、前端1856仅代表当次状态。

## 最新后续：查询校验简化V3（以下旧实现／两题历史保留）

- 用户已授权减少措辞／命名阻断及兼容等价表达。最新代码按注册ID／真实字段补齐定义、兼容运算符／大小写／别名；复杂表达及额外SQL筛选记pending_checks，不自动认证业务正确。
- 明确来源／金额单位／聚合／时间／筛选冲突继续拦截；金额仅出现在CASE条件或无关列不满足聚合。实际物理表按查询作用域识别，修复同名CTE遮住权限判断的问题。
- 待核对项随结果传递至后续Executor、草稿、最终Reviewer和同会话历史表；未解决疑问不能用于推翻前提并跳过调查。没有新增模型节点或过程审核。
- 同一错误且相关产物未变化时提前停止；预算不增加。原两次付费名额仍2／2且旧记录指纹未变，严禁继续付费试运行。
- 本轮仅离线：旧GMV候选3／3通过，原SQL只实际执行1次，6月／7月8项数字与独立CSV核算一致；利润3候选均因GMV／quantity冲突阻断。权限修复后只重新validate，execute=0。2025年7月返回0不代表覆盖已确认。
- 最终本地后端153项、E适配14项、前端构建通过；510份保护文件未变。8001／5174健康；query_validation_v3/runtime记录后端12392、前端1856（仅当次PID）。本轮界面交互与完整Agent真实闭环仍未验证。
- 最新报告：docs/homenova_query_validation_v3_report.md；脚本：scripts/homenova_harness_v1/replay_query_validation_v3.py；新记录：data/homenova_harness_v1/private/query_validation_v3/。原两题报告不回填成功。

## 硬限制与运行现状

- 只修改references/askdata_studio及必要开发脚本／文档；8001、5174。保留其他项目、业务数据和用户已有改动。
- 用户授权最多两次不同自然问题的真实付费试运行，现已全部用完。禁止再运行case1/case2、额外探测模型、绕过台账或重跑全量经营题。无新的明确预算，不新增付费调用。
- 台账：data/homenova_harness_v1/private/implementation_v2/paid_runs.json；两题均recorded且failed，无后台试运行进程。
- 第1题“7月GMV怎么下降了？感觉销量明明还可以啊。”任务0c46fb221421；02:50发起，136.271秒，5Chat，59,201Chat token，S1三候选在契约前失败，无业务SQL／审核。
- 第2题“哪些商品卖得多却不怎么赚钱？热卖款利润应该也高吧。”任务7a7949e48e2f；11:52发起，88.590秒，4Chat，88,095Chat token，单步三候选在契约前失败，无业务SQL／审核。
- 当前模型服务正常，未更换qwen3.7-plus或安全配置；不是欠费卡住。不要以两个失败任务冒充新版端到端通过。
- 原02:52备用心跳homenova已取消，额度自然重置后本轮继续；没有待运行自动验收。当前未接近剩余5%，未使用额度重置券。

## 实现与验证

- Planner条件／依赖／要求编号；Executor原有同次调用中的前置判断及SQL／分析；Controller验证证据、数值及作用范围，保留独立目标，正确传播相关分支不适用。
- 执行图移除过程Reviewer。最终审核退回可执行工作，同草稿／证据签名去重，实质修改必要时再审；已解决历史stop_reason不永久影响完成。
- 原始数值计量多期输出别名按真实血缘绑定；保留定义／来源／聚合及权限检查。Planner提示不擅加无覆盖同比、不猜透视字段名。
- 用户随后要求全过程含Skill：新增budget.events记录入口选择、实际节点注入名称／正文指纹／字符数／主动时间，以及工具、修复、重规划。失败响应仍有预算事件；前端可查看。此打点早于未来任务、晚于本次两题，不能补造旧事件。
- 最终本地后端129项、E适配14项通过；前端类型检查和生产构建通过。记录位于implementation_v2，末尾with_trace为最终检查。
- 510份保护文件核对未变。最终源码、两题源码分别有指纹，不能将早期运行声称为最终源码验证。
- 按原启动器重启，8001／5174均健康，最终启动记录implementation_v2/runtime_final_with_trace；本次启动输出后端1842、前端1856（PID只代表当次状态）。
- 浏览器自动操作被Work设置拒绝Local browser use is disabled，未绕过。本轮界面交互未验证，构建通过不替代界面。

## 交付文件

- docs/homenova_harness_v1_implementation_v2_report.md：最新完整报告。
- docs/homenova_harness_v1_report.md：保留旧历史并链接新版。
- data/homenova_harness_v1/private/implementation_v2/process_reports/index.html：两题逐任务报告，包含问题、计划、SQL候选／实际执行、Skill还原边界、审核及usage。
- scripts/homenova_harness_v1/build_latest_reports.py：仅离线生成两题报告，无模型／网络。
- independent_review.json、run_summary.json、independent_facts.json、protected_check_final.json均在implementation_v2。
- 设计共识、修正方案、固定验收文档已同步已实现／已验证／仍待完成，原八类标准不降低。

## 尚未解决

完整真实GMV闭环和新版最终交付未验证；原始计量／注册业务指标的生成与校验契约仍不稳定。第2题将quantity标成GMV、混用发货队列与事件期间、成本恢复缺少验收约束；不应只移除末尾筛选错误。检索宽依赖扩展至105项导致单次SQL输入2.6万token以上，修复重复携带进一步放大成本。先离线处理这些问题；有新的明确付费预算后再做一条完整业务／界面验收。任务及结果引用仍是进程内，重启不能恢复。
