# 独立 SQL 模块实施报告

2026-10-29。实施对象仅为 `references/askdata_studio`，8001／5174。方案状态已同步更新。

**已完成代码接入、本地回归和真实数据探针；本轮付费模型调用0、真实Agent任务0。真实经营调查效果仍待另行授权验收，历史三题失败没有被改写为成功。** 这里的0次统计本项目模型API，不含Codex开发对话用量。

## 已实现

- **经营与取数分工。** 新调查使用执行协议2：Flash只理解和路由，经营ReAct选业务方法、读取证据并提出 `query_batch`，不生成SQL。普通查数和调查共用 `SqlQueryModule` 的生成、校验及MCP执行；没有新增部署服务或动态subagent。
- **共享资料包。** Schema、登记口径、样例／状态、单位、覆盖和空值信息按授权资料组装；保留完整桥接路径及连接键。缓存继承、新缺口检索、去重和组装清单由代码完成，不逐轮追加旧提示词。已修复字段样例丢失、桥接裁断、缓存误挡新维度检索。
- **批次与修复。** 一批一次首生成，逐项校验、执行、保存；一次重写只包含失败子集，成功项不重生成。后端绑定批次及查询ID，模型调用前持久预占次数。格式无效仍计数；服务／网络异常保留未知状态，不冒充SQL错误自动重试。同批重复需求在预占前去重，结构化 `conditions` 区分不同查询条件。
- **连续调查。** 早期事实和实际依赖可带回真实结果，不仅保留最后两份；进度按条目更新，未提及的有效事实保留。SQL模块澄清后恢复同一查询及已用次数。旧任务继续保留协议1、原结果和预算，不自动重跑入口或返还额度。
- **预算。** 五轮业务取证、每条SQL总共两次生成、20次工具、主动300秒／调查240秒继续共用。技术修复不另占业务轮；Token估算与实际用量分开记录，不设应用硬上限。新增测试确认第5批后进入收尾，没有第6次业务决策。
- **报告。** 调查收尾直接调用同一DataQaAgent生成报告草稿，替代原草稿调用，原data_qa问答入口保留。正常路径无过程Reviewer，最终审核检查正文、表和图；只展示定稿。图表绑定具体结果ID和版本，使用冻结数据；错误图表省略并提示，不阻断有效正文。前端避免报告与同一正文／最后一张表重复展示，证据及SQL仍可展开查看。

主要代码：`backend/app/querying/{sql_module,query_context,single_database_agent,data_qa_agent}.py`、`backend/app/workflows/{sql_batch,react_harness,react_prompts}.py`。三个业务Skill和查询／报告Skill已适配；运行时仍只使用应用白名单，不扫描docs或私有评测目录。

## 验证结果

测试前固定范围见[本地验收清单](../data/homenova_harness_v1/private/sql_module_20261029/acceptance.md)。受控模型输出用于验证调度，不代表真实模型业务正确。

| 检查 | 实际结果 |
|---|---|
| 后端完整回归 | **467项通过，126.824秒**；测试进程禁用网络 |
| E阶段适配／评测回归 | **14项通过，0.682秒** |
| 前端 | 类型检查、生产构建通过 |
| 新模块专项 | 样例／桥接闭包、早期证据、批量首生成／失败子集、两次格式错误、澄清继承、五批停止、报告引用／版本／图表均有本地测试 |
| 八类原验收控制流 | 简单查数、调查、前提推翻、缺参数／能力、修复、预算停止、追问、交付的既有相关回归继续通过 |
| 真实模型三个经营场景／十题业务回归 | **本轮未运行，仍待验证** |

首轮完整回归有37个错误实例，集中在两处旧测试上下文与新增报告接入的兼容问题；修复后完整回归通过。初次失败日志保留，没有删测试或降低业务标准。

另外，两条人工SQL均通过**新的共享SQL模块→原校验器→真实本地MCP→真实数据**，再由独立Python直接读取CSV核算：

| 数据源／口径 | 2026年6月 | 2026年7月 | 结果／耗时 |
|---|---:|---:|---|
| ecommerce_ops／daily_store_metrics.gmv，原始金额单位 | 152,286,820.25 | 159,979,535.75 | 完整2行，核数一致，2.039秒 |
| HomeNova八月底／成功支付商品GMV，欧分 | 3,920,044 | 3,090,583 | 完整2行，核数一致，0.652秒 |

各调用1次真实查询工具、0次模型。演示数据该期间上涨约5.05%，HomeNova下降约21.16%，不能混源解释。两套所查CSV和历史三题JSON共68份文件，在探针前后指纹一致。没有修改数据、权限、生成器或参考答案。

## 运行与查看

界面：<http://127.0.0.1:5174/>。新任务的过程面板显示“经营ReAct决定取证方向／独立SQL模块取数／DataQa整理报告”，并保留逐次模型用量、Skill读取与注入、结果和SQL。

已用原启动脚本重启本项目，后端PID62363、前端PID62377。健康状态确认协议2、共享SQL模块、DataQa报告、主模型 `qwen3.7-plus`、入口 `qwen3.7-flash`、5轮／SQL2次。部署前旧进程返回500，日志为文件访问PermissionError；重启后两个服务均200。脚本新增可选的本项目Vite重启参数，按完整进程命令核对归属，不停止其他应用。

```sh
references/askdata_studio/backend/.venv/bin/python scripts/homenova_phase_e/start.py restart --restart-frontend --record-dir data/homenova_harness_v1/private/sql_module_20261029/runtime
```

重启后原登录令牌失效，页面如提示鉴权失败需重新登录。

新记录目录：`data/homenova_harness_v1/private/sql_module_20261029/`，不是运行知识源。

- [最终后端回归](../data/homenova_harness_v1/private/sql_module_20261029/backend_tests_final.log)、[首轮错误](../data/homenova_harness_v1/private/sql_module_20261029/backend_tests_initial.log)、[E回归](../data/homenova_harness_v1/private/sql_module_20261029/phase_e_tests.log)。
- [实际SQL、契约、结果引用依据、独立核数及数据指纹](../data/homenova_harness_v1/private/sql_module_20261029/offline_sql_checks.json)。
- [类型检查](../data/homenova_harness_v1/private/sql_module_20261029/frontend_typecheck.log)、[前端构建](../data/homenova_harness_v1/private/sql_module_20261029/frontend_build.log)、[代码指纹](../data/homenova_harness_v1/private/sql_module_20261029/source_manifest.json)。
- [启动记录](../data/homenova_harness_v1/private/sql_module_20261029/runtime/startup.json)、[部署健康状态](../data/homenova_harness_v1/private/sql_module_20261029/runtime/health.json)。

## 仍待验证及限制

拆开SQL职责和补齐资料不保证模型永不写错，实际选指标、SQL生成质量、下钻方向、原因证据和报告可读性仍需少量真实任务验收。HTTP200、SQL成功、本地控制测试的completed均不算业务正确。

代码校验能证明的权限和明确冲突继续强制；复杂业务等价、额外筛选和因果强度仍需证据及最终审核。不同查询条件须结构化表达，不能仅靠任意自然语言措辞实现完美去重。没有明确输出列单位元信息时，图表不会猜币种；超过提供范围的结果不冒充完整图表，必要时须补聚合或读取。

后续建议按原先固定的全盘、指定对象、看板已有发现三个场景做少量真实验收，分别检查数字、方向和交付；不自动重跑历史失败或扩张付费次数。
