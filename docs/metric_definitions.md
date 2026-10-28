# 第一阶段指标口径

| 指标 | 定义与 SQL 要点 | 边界 |
| --- | --- | --- |
| 订单数 | `COUNT(DISTINCT orders.order_id)`；按 `order_purchase_timestamp` 归属月份 | 商品行数不是订单数；未下单日期不硬填月份 |
| 商品销售额 | `SUM(order_items.price)`，默认只统计 `order_status='delivered'`，并按订单下单月归属 | 是已送达订单商品金额，不含运费，不等于平台收入、利润或到账金额；取消/未送达另列，不把缺失当 0 |
| 支付金额 | 先按 `order_id` 汇总 `payments.payment_value`，再与订单关联 | 不和商品行直接 JOIN 后求和；支付口径与商品金额不必相等 |
| 类目销售额 | 已送达订单的商品行价格，先连 products，再可选连翻译表 | 无类目/无译名需明确列为 unknown 或保留葡语名 |
| 配送时长 | `order_delivered_customer_date - order_purchase_timestamp`，仅在实际送达时间非空时计算 | 可用天数表示；不能把未送达视为 0 天 |
| 配送延迟 | 已实际送达且有预计日期时，`actual_delivery > estimated_delivery` | 比较实际送达与承诺预计日期；按自然日期比较与按时间戳比较不同，本项目用时间戳 |
| 客户评分 | 评价表 `review_score`，先按订单汇总评价，再连订单/订单类目 | 一订单多商品/多评价时避免复制评分；类目评分为“含该类目的订单评分”，不是商品级因果归因 |
| 取消比例 | 购买月内 `order_status='canceled'` 的不同订单数 / 该月全部不同订单数 | 不将取消订单销售额硬设为零；该指标只反映状态比例 |
| 复购客户 | 按 `customer_unique_id` 统计不同 `order_id`，默认至少两笔已送达订单 | `customer_id` 通常是订单客户记录，不能用其当跨订单客户身份 |

Olist 是历史公开数据，不能代表当前业务。第 5、7、10 题只可描述关联、分布和调查线索，不可断言配送延迟导致评分变化。金额使用原始货币数值，数据集通常记为 BRL；本项目不做汇率换算。
