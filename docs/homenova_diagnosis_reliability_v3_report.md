# 经营调查可靠性改造记录

2026-10-29。仅修改 `references/askdata_studio`，沿用8001／5174及原数据、鉴权、模型配置。

**已实施并完成本地验证；本轮应用模型API调用0，真实Agent任务0。** 此处不包含Codex开发对话用量。真实模型是否能持续选对指标、写对SQL及给出可信解释，仍需后续场景验收。

## 改了什么

1. **查询前明确度量。** 新增共享度量说明，包含统计对象、单位、事件时间、实际来源、依赖和未知要求；进入同一SQL生成调用。注册定义与任务内度量分开，未检索到不自动等于不支持。不同时间集合保留分组提示，不强套一个筛选；复杂业务语义不冒充程序已证明。
2. **SQL总共3次。** 首次＋最多2次修复，派发前保存额度，失败子集修复，成功项保留。具体报错同时提供期望对象／时间、声明来源及实际聚合表达式。原查询关闭后可处理真正独立的取证，换别名、元数据检索后名称变化或拆项不能重开额度。旧任务保持原协议及上限。
3. **通用计算协议。** 后端实际Schema定义求和、统计、逐行比较、跨期比较和两因素分解。未知参数、自比较、重复期间等不产生成功证据；缺期、空值、零分母明确标识。已有长表可直接比较，不必额外查SQL。
4. **调查问题与解释记录。** 新任务使用执行协议3，原问题、核心缺口及竞争解释按稳定ID记录，核心项不能无依据降为可选项。审核逐项处理这些ID；遗漏或仍未回答不能凭completed标签放行。实际依赖失效时，相关事实／问题／解释重新核实。
5. **Skill与报告。** 经营方法加入核心对比、数量／金额关系、条件分支和支持／反证；用户描述保持未核实，销量不以订单代替。报告要求展示正常、异常及正向抵消项、主要解释核查和剩余缺口。沿用DataQa草稿及一次最终审核，不增加过程Reviewer。图表按实际Schema输出，保存具体字段诊断，不增加模型修图调用。
6. **上下文选择。** 后台缓存保留，模型输入只选本批需求及必要关联，manifest留在后端。补齐字段型查询的时间候选，未知维度不能无声丢失；month等时间粒度不再被当成缺失物理字段而重复检索。草稿和审核不重复发送正文，报告阶段不携带整张物理Schema。

同一缓存下，本次会话／转化查询的选择从**6表61字段、5个指标，降为3表29字段、2个指标**；必要定义和关联保留。这是资料选择检查，不能当成已测得的真实API费用降幅。

主要代码：`querying/measurement_context.py`、`querying/calculations.py`、`querying/sql_module.py`、`workflows/sql_batch.py`、`workflows/investigation_progress.py`、`workflows/react_harness.py`。入口、业务／查询／报告Skill和最小界面同步适配。

## 验证结果

范围在测试前固定于[验收清单](../data/homenova_harness_v1/private/diagnosis_reliability_20261029/acceptance.md)。

| 检查 | 结果 |
|---|---|
| 后端全量回归 | **518项通过，136.753秒**；进程阻断外部网络 |
| E阶段回归 | **14项通过，0.763秒** |
| 前端 | 类型检查与生产构建通过 |
| SQL／计算／状态 | 第三次成功、无第四次、部分失败继续、别名及拆项额度、澄清、旧记录、原A3错误、跨期计算、核心问题遗漏及状态枚举均有回归 |
| 应用付费模型与真实Agent验收 | **本轮0次，未用受控输出冒充业务成功** |

首轮513项回归出现两处失败：普通查询执行第三次仍失败时的停止分类，以及旧测试预期恢复为SQL2次。修复普通查询耗尽分支并更新新策略断言后通过。收尾另发现提示词与进度状态枚举不一致，已补齐并增加专项，最终518项通过。初次日志保留。

另执行4条人工SQL，经共享校验／查询模块、真实本地MCP及授权数据，并在Agent外直接读取CSV核算：

| 探针 | 核对结果 |
|---|---|
| 演示数据源月GMV | 6月152,286,820.25；7月159,979,535.75，核算一致，2.142秒 |
| HomeNova八月底支付商品GMV | 6月3,920,044欧分；7月3,090,583欧分，一致，0.680秒 |
| HomeNova支付集合概览 | 订单946→687、商品件数1,331→917及客单价／每单件数／每件金额，均一致，0.726秒 |
| HomeNova会话集合漏斗 | 会话24,932→24,704、加购会话968→755、结账尝试会话968→755、购买会话946→687，均一致，0.034秒 |

各探针1次真实MCP、0次模型。这验证人工SQL与计算链路，不认证模型已能生成相同查询，更不能把漏斗变化自动说成唯一经营原因。65份业务CSV和历史任务 `bda5e7242071` 保持不变；此前真实失败没有改成成功。

## 运行与记录

已沿用启动脚本重启本项目。新任务使用协议3、SQL总共3次；5轮取证、20次工具、调查240秒／主动300秒、Token只观测继续生效。主模型沿用qwen3.7-plus，入口沿用qwen3.7-flash。原任务和预算不自动升级或重跑。

界面：<http://127.0.0.1:5174/>。重启后需重新登录；请用新任务体验新规则。

```sh
references/askdata_studio/backend/.venv/bin/python scripts/homenova_phase_e/start.py restart --record-dir data/homenova_harness_v1/private/diagnosis_reliability_20261029/runtime
```

新记录目录：`data/homenova_harness_v1/private/diagnosis_reliability_20261029/`。

- [最终回归](../data/homenova_harness_v1/private/diagnosis_reliability_20261029/backend_tests_shipping.log)、[首轮失败](../data/homenova_harness_v1/private/diagnosis_reliability_20261029/backend_tests_initial.log)、[E回归](../data/homenova_harness_v1/private/diagnosis_reliability_20261029/phase_e_tests.log)。
- [实际经营指标／漏斗SQL、完整结果及独立核数](../data/homenova_harness_v1/private/diagnosis_reliability_20261029/diagnosis_probe.json)、[两数据源基础探针](../data/homenova_harness_v1/private/diagnosis_reliability_20261029/offline_sql_checks.json)。
- [上下文选择检查](../data/homenova_harness_v1/private/diagnosis_reliability_20261029/context_selection_receipt.json)、[前端构建](../data/homenova_harness_v1/private/diagnosis_reliability_20261029/frontend_build.log)、[部署健康状态](../data/homenova_harness_v1/private/diagnosis_reliability_20261029/runtime/health.json)。

## 尚待验证

真实模型的SQL生成质量、竞争解释调查和报告可读性仍待实际场景确认。公开度量说明及结果列元信息不构成任意SQL语义证明；未确认的显示换算和复杂集合关系保留边界。没有关闭权限／只读／快照保护，也没有用参考SQL或私有答案指导运行模型。

后续按全盘、指定对象、用户已有看板发现三个自然问题验收，分别检查数字、下钻、反证和完成程度，按获授权次数执行，不为通过反复试跑。
