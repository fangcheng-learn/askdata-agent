# 手编SQL实际检查与修复报告

日期：2026-10-28。开发评测资料，不进入运行时知识。

用户要求再编几组SQL实际检查。本轮没有调用模型生成SQL，而是手写查询，经现有校验后在8月底HomeNova公开CSV上执行；故意错误的反例只校验、不执行。**实际发现并修复了新的误拦和错放，不能把前一轮本地通过理解为已经没有其他问题。**

## 检查结果

| 检查 | 最终结果 |
|---|---|
| 主矩阵35组手编SQL | 26组合法查询通过并实际执行；9组反例正确拒绝 |
| 等价写法结果对照 | 21组全部一致 |
| 代表性商品利润SQL | 实际返回8个SKU，各分量与独立CSV核算一致 |
| 新增边界回归 | 14项通过，含额外的范围／字段转换／BETWEEN反序问题 |
| 完整后端／E适配／前端 | 200项／14项／类型检查及构建通过 |
| 付费模型／Agent／HTTP／Embedding／Rerank | 新增调用均为0 |

主矩阵首轮发现6种合法查询误拦、3种错误条件错放；后续独立边界测试又发现字段重命名、BETWEEN顺序、字段转换及子查询范围问题。修复前记录保留，没有删除失败用例或改变其合理预期来凑通过。

## 实际发现的问题与处理

| 写法／问题 | 原行为 | 修复后 |
|---|---|---|
| `'2026-06-01' <= captured_at` | 只认字段在左侧，误拦 | 反转比较方向后识别同一条件 |
| `status IN ('captured')`、`NOT(status <> 'captured')` | 和等号写法不同，误拦 | 识别单值IN及等价否定 |
| 窗口SUM＋DISTINCT、分期聚合UNION ALL | 强求GROUP BY，误拦 | 可执行，记录实际返回粒度待核对 |
| 时间转DATE后使用闭区间 | 与原时间半开表达不同，误拦 | 可执行并记时间表达待核对，不假装证明所有日期转换 |
| CTE列重命名后经星号透传 | 实际SQL可执行，字段来源解析丢失 | 校验与执行共用安全展开后的SQL，按CTE输出位置和新列名追踪 |
| `status='captured' OR status='failed'` | 只要出现captured就当筛选落实 | 按AND／OR／NOT结构检查，不能用OR某一分支代表整个条件 |
| `status='CAPTURED'` | 将数据字符串也忽略大小写，错放 | 标识符可忽略大小写，数据值精确比较 |
| 声明`[start,end)`却写`>start`或`<=end` | 边界值相同就认通过 | 核对包含性；简单明确的边界冲突拒绝 |
| `BETWEEN 2 AND 1`冒充`BETWEEN 1 AND 2` | 值集合相同就认通过 | 保留端点顺序，反序拒绝 |
| `UPPER(status)`冒充原始status | 字段名出现就认通过 | 记录转换表达待核对，不认证与原字段等价 |
| 无关子查询里出现成功支付条件 | 被误认作约束了外层指标 | 标记筛选作用范围未证明，不把另一子查询的条件认证为外层已落实 |

这些改动不新增大模型审核节点。复杂表达保留`pending_checks`，仍可受控查询；不将结构通过、查询成功当成业务正确。真正的权限、只读、跨快照及明确计算冲突保护保留。

## 手编SQL例子

下列写法已在主矩阵实际执行，并与相同口径的普通写法核对一致。

单值IN与明确时间范围：

```sql
SELECT SUM(oi.line_paid_gross_cents) AS total
FROM payments p
JOIN order_items oi ON p.order_id = oi.order_id
WHERE p.status IN ('captured')
  AND p.captured_at >= '2026-06-01T00:00:00+02:00'
  AND p.captured_at < '2026-08-01T00:00:00+02:00';
```

每月窗口汇总，不需要GROUP BY：

```sql
SELECT DISTINCT
  strftime(p.captured_at, '%Y-%m') AS period,
  SUM(oi.line_paid_gross_cents) OVER (
    PARTITION BY strftime(p.captured_at, '%Y-%m')
  ) AS amount
FROM payments p
JOIN order_items oi ON p.order_id = oi.order_id
WHERE p.status = 'captured'
  AND p.captured_at >= '2026-06-01T00:00:00+02:00'
  AND p.captured_at < '2026-08-01T00:00:00+02:00'
ORDER BY period;
```

还实际测试了多层CTE、CTE重命名、已明确中间结果的星号展开、CASE／IF／FILTER条件聚合、预去重后SUM／COUNT、HAVING和汇总后筛选、大小写、类型化时间字面量及分期UNION。每条完整SQL、契约、实际执行SQL、行数据、耗时、待核对项和对照结果见[35组最终探针记录](../data/homenova_harness_v1/private/sql_probe_v5/probes_final.json)。

## 利润查询的额外实际核对

单独手编一条2026年7月SKU查询，各组件先聚合再连接：

- 支付件数按成功capture时间。
- 发货收入和COGS按shipped_at。
- 已发货商品完成退款按completed_at，排除发货前取消。
- 可再售成本恢复按inspected_at，不要求资金退款也在7月完成。
- 净销售额＝发货不含税收入−完成商品不含税退款；商品毛利润＝净销售额−COGS＋成本恢复，净销售额非正时毛利率为NULL。

该SQL通过校验且`pending_checks=[]`，实际返回全部8个SKU；独立标准库CSV核算的件数、收入、成本、退款、恢复成本、净销售、毛利和毛利率逐项一致。53笔成本恢复另按“可再售数量×原订单单位成本”重算一致，其中9笔并非7月完成退款，验证了恢复与资金退款没有被错误绑定。

此处采用各分量发生期，不是同批原订单最终利润；支付件数和利润分量不代表同一订单集合。商品毛利也不是企业净利润。**这证明一条正确口径的手编查询能通过并查对数据，不证明当前Agent会自动生成相同质量的查询。**

[完整利润SQL、结果与独立核算](../data/homenova_harness_v1/private/sql_probe_v5/profit_probe.json) · [利润离线脚本](../scripts/homenova_harness_v1/probe_profit_sql_v5.py)

## 旧问题保护与交付

当前源码重新校验旧GMV总体／下钻共8条保存项，全部通过；利润代表SQL再次校验仍通过且无待核对项，此次复核没有重复执行结果查询。旧结果引用和收尾回归仍在完整200项测试中。

510份受保护文件、两轮真实输出及台账指纹均未变。第二题仍未提交，`validation_v3_real/paid_runs.json`的暂停标记保留，没有自动消耗剩余名额。

服务已按原脚本更新，8001／5174健康，模型配置不变；本次记录后端17476、前端1856，仅代表当次PID。没有新做界面操作或完整实时Agent验收。

[后端200项](../data/homenova_harness_v1/private/sql_probe_v5/backend_final.txt) · [E适配14项](../data/homenova_harness_v1/private/sql_probe_v5/phase_e_final.txt) · [前端构建](../data/homenova_harness_v1/private/sql_probe_v5/frontend_build.txt) · [旧候选复核](../data/homenova_harness_v1/private/sql_probe_v5/saved_candidates_final_validation.json) · [保护文件核对](../data/homenova_harness_v1/private/sql_probe_v5/protection_check.json)

最终结论限定在本轮实际覆盖的SQL与边界：已发现的问题已修、这些查询已查通且对照一致；任意复杂SQL的业务正确性、完整Agent规划和后续经营解释仍不能仅凭这些测试保证。
