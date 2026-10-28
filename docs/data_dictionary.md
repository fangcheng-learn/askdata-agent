# Olist 数据字典（原始九表）

来源为 Kaggle 发布的 [Olist Brazilian E-Commerce Public Dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)。原 CSV 保存在 `data/raw/olist/`，DuckDB 表与 CSV 一一对应；导入仅将字段转换为下列类型，未过滤或去重。以下行数来自 2026-10-24 本地实测。完整字段类型、空值数见 `data/processed/olist_quality.json`。

| 表 | 粒度 / 行数 | 字段（DuckDB 类型） | 关联 |
| --- | --- | --- | --- |
| `olist_customers_dataset` | 一次订单使用的客户记录；99,441 | `customer_id VARCHAR`, `customer_unique_id VARCHAR`, `customer_zip_code_prefix VARCHAR`, `customer_city VARCHAR`, `customer_state VARCHAR` | `orders.customer_id` → `customers.customer_id`；复购用 `customer_unique_id` |
| `olist_geolocation_dataset` | 邮编前缀的一条坐标样本；1,000,163 | `geolocation_zip_code_prefix VARCHAR`, `geolocation_lat DOUBLE`, `geolocation_lng DOUBLE`, `geolocation_city VARCHAR`, `geolocation_state VARCHAR` | 邮编前缀不是唯一键，同一前缀有多个点；先按前缀聚合才可 JOIN |
| `olist_order_items_dataset` | 订单商品行；112,650 | `order_id VARCHAR`, `order_item_id INTEGER`, `product_id VARCHAR`, `seller_id VARCHAR`, `shipping_limit_date TIMESTAMP`, `price DECIMAL(18,2)`, `freight_value DECIMAL(18,2)` | 复合键 `(order_id, order_item_id)`；连 orders、products、sellers |
| `olist_order_payments_dataset` | 订单的一次支付记录；103,886 | `order_id VARCHAR`, `payment_sequential INTEGER`, `payment_type VARCHAR`, `payment_installments INTEGER`, `payment_value DECIMAL(18,2)` | `(order_id, payment_sequential)`；一订单可能多笔支付 |
| `olist_order_reviews_dataset` | 评价记录；99,224 | `review_id VARCHAR`, `order_id VARCHAR`, `review_score INTEGER`, `review_comment_title VARCHAR`, `review_comment_message VARCHAR`, `review_creation_date TIMESTAMP`, `review_answer_timestamp TIMESTAMP` | `order_id` 连 orders；评价与商品行并非同一粒度 |
| `olist_orders_dataset` | 订单；99,441 | `order_id VARCHAR`, `customer_id VARCHAR`, `order_status VARCHAR`, `order_purchase_timestamp TIMESTAMP`, `order_approved_at TIMESTAMP`, `order_delivered_carrier_date TIMESTAMP`, `order_delivered_customer_date TIMESTAMP`, `order_estimated_delivery_date TIMESTAMP` | `order_id` 唯一；时间字段可能为空 |
| `olist_products_dataset` | 商品；32,951 | `product_id VARCHAR`, `product_category_name VARCHAR`, `product_name_lenght INTEGER`, `product_description_lenght INTEGER`, `product_photos_qty INTEGER`, `product_weight_g INTEGER`, `product_length_cm INTEGER`, `product_height_cm INTEGER`, `product_width_cm INTEGER` | `product_id` 唯一；原始字段拼写 `lenght` 保留 |
| `olist_sellers_dataset` | 卖家；3,095 | `seller_id VARCHAR`, `seller_zip_code_prefix VARCHAR`, `seller_city VARCHAR`, `seller_state VARCHAR` | `seller_id` 唯一 |
| `product_category_name_translation` | 葡语类目的一条英文译名；71 | `product_category_name VARCHAR`, `product_category_name_english VARCHAR` | 以葡语类目连接 products；有 13 个非空类目的商品未匹配译名 |

## 关系和风险

`customers 1→N orders 1→N order_items`；`products 1→N order_items`；`sellers 1→N order_items`。payments 和 reviews 也分别通过 `order_id` 连 orders，但均有各自粒度。把 payments、reviews、items 同时直接 JOIN 后求和会形成多对多乘法。分析时先按订单聚合各表，再 JOIN。`customer_id` 是订单侧的客户记录键；跨订单客户身份使用 `customer_unique_id`。邮编前缀和评价 ID 不应未经检查就假设是主键。

原始数据包含取消、未送达和字段缺失。导入器保留全部记录；关于排除条件、空值与指标定义见 [指标口径](metric_definitions.md)。
