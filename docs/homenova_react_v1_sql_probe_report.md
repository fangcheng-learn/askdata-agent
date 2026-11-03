# ReAct V1：实施前 SQL 链路探针

日期：2026-10-29。仅离线检查真实本地契约校验、DuckDB 预编译及进程内 MCP 查询；模型、Embedding、Rerank、HTTP 调用均为0。不是完整 Agent 业务验收。

## 固定范围与结果

执行前先保存30条探针及判定标准，计划 SHA256：`5900403a6283a421f9da7441816d5b30491daaa63b9ea630e8dd3a5a32892163`。正例必须通过契约、预编译、真实 MCP 执行，并与独立公开 CSV 计算相符；负例必须拒绝且不执行。没有根据运行结果缩小清单。

| 检查 | 结果 |
|---|---|
| 20条正例 | 全部执行成功，独立数值／完整结果逐行核对一致 |
| 10条负例 | 全部拒绝，没有执行 |
| 数据保护 | payments、order_items、sku_catalog、sessions 原始公开 CSV 前后 SHA256 一致 |
| 校验器修改 | 未发现误拦，未修改 query_contract.py 或 DuckDbEngine |

正例覆盖：8月GMV、7／8月对比、店铺／SKU多指标、同支付订单集合的GMV／订单／客单价、有效会话、CTE列重命名、不同显示别名、COUNT DISTINCT及上游去重、SUM CASE／FILTER、UTC与柏林半开边界、闭合DATE范围、UNION，以及超过200行的授权结果读取。

负例覆盖：数量冒充GMV、SUM／AVG明确冲突、遗漏成功支付条件、遗漏起点、起点偏移1小时、排他终点误写成包含终点、跨快照、写操作、多语句、表权限不足。

## 独立核数

使用 Python 标准库读取绑定8月底快照的公开 CSV，按柏林经营日筛选成功capture，检查每订单只有一次成功capture，再汇总商品行；不读取私有题库、参考答案或生成器。

| 指标 | 数值 |
|---|---:|
| 2026年7月成功支付商品GMV | 3,090,583 EUR cents |
| 2026年8月成功支付商品GMV | 3,989,701 EUR cents |
| 8月成功支付订单数 | 855 |
| 8月成功支付商品件数 | 1,199 |
| 8月商品客单价 | 4,666.316959 EUR cents／订单 |
| 8月排除内部流量的会话数 | 25,066 |

明细探针实际有1,038行，MCP只展示200行预览；通过授权结果引用分两页取回1,038行并逐行匹配公开CSV。未把预览当全集。

公开快照仅有 `HN-DE-01` 一家店、8个SKU。店铺探针验证了单店查询能力，不能据此声称已经验证多店贡献排名。

## 校验边界与限制

- 闭合DATE范围、UNION分期聚合、上游DISTINCT后COUNT仍可能带待核对项，但均允许执行并且独立核数一致。保留这些记录，不把“有限解析器未证明”升级为拒绝。
- 验证成功只说明本清单涉及的 SQL／契约链路可运行，不能保证未来任意生成SQL都正确，也不证明经营原因成立。
- 本轮使用授权快照的公开 Schema，不调用检索模型；因此没有验证真实检索能否取得这些字段和口径。该部分留给后续三个真实场景，不能用探针通过替代。
- 本轮没有发现需要修复的校验错误，因此不为凑修复记录而改动安全规则。后续仍需真实模型、真实检索、真实数据的三个场景验收。

## 文件与运行

- 固定计划：[plan.json](../data/homenova_harness_v1/private/react_v1_20261029/sql_probes/plan.json)
- 每条SQL、契约、检查、真实结果、独立计算、耗时及指纹：[baseline.json](../data/homenova_harness_v1/private/react_v1_20261029/sql_probes/baseline.json)
- 探针脚本：[probe_react_v1_sql.py](../scripts/homenova_harness_v1/probe_react_v1_sql.py)
- 专项回归：[test_react_v1_sql_probes.py](../references/askdata_studio/backend/tests/test_react_v1_sql_probes.py)

专项回归4项通过：固定正例编译、固定负例拒绝、复杂等价表达保留pending而不拒绝、真实MCP预览／完整引用及越权读取。

```sh
references/askdata_studio/backend/.venv/bin/python scripts/homenova_harness_v1/probe_react_v1_sql.py --plan-only
references/askdata_studio/backend/.venv/bin/python scripts/homenova_harness_v1/probe_react_v1_sql.py --output another_run.json
```

输出仅允许位于新的 `react_v1_20261029/sql_probes/` 评测目录；已存在文件拒绝覆盖。这些开发材料不得写入运行时 Skill、提示词或检索源。
