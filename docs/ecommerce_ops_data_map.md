# AskData `ecommerce_ops` 数据地图（只读盘点）

盘点日期：2026-10-26。范围：`references/askdata_studio/backend/data/databases/ecommerce_ops/` 中现存的 50 个 CSV；仅查询现有文件，未运行生成脚本，未修改业务数据或应用代码。

## 1. 数据源与证据级别

- AskData Studio 的 `DuckDbEngine.connect()` 为每次连接创建 `:memory:` DuckDB，将 `data/databases/<database>/*.csv` 用 `read_csv_auto(..., header=true, sample_size=-1)` 注册为视图。`ecommerce_ops` 指向上述独立 CSV 目录，**不是**持久 DuckDB 文件或远程数据库。见 [`duckdb_engine.py`](../references/askdata_studio/backend/app/querying/duckdb_engine.py)、[`database.py`](../references/askdata_studio/backend/app/database.py)。
- 静态 Schema 由 `_schema.json` 注入，含 50 张表、79 条**逻辑**关系及逻辑主键；CSV/视图没有数据库强制的 `PRIMARY KEY`、`UNIQUE`、`FOREIGN KEY` 或 `NOT NULL` 约束。以下“主键”均表示 Schema 声明且已用数据检验的候选键；“关系”均为 Schema 描述与实际数据支持的候选关联，非物理外键。
- `askdata_mock` 是同一 AskData Studio 的另一套 5 表 CSV 场景；`askdata_mock.db`、`session_archive.db` 不是本场景业务库。仓库另有 `data/olist.duckdb` 和主应用 SQLite/fixture，均未混入本次盘点。
- 清单 `_database_manifest.json` 说明这是固定随机种子 `20260823` 生成的**合成零售样本**，声明整体日期 `2025-01-01` 至 `2026-08-22`。实测有部分后续事件晚于上限，见数据缺口。

## 2. 全表字段、记录数和日期覆盖

类型来自当前 `read_csv_auto` 的 DuckDB 推断：`S` = VARCHAR，`I` = BIGINT，`F` = DOUBLE，`D` = DATE，`T` = TIMESTAMP。`~n` 表示该字段实测有 n 个 NULL；未标 `~` 的字段在现有文件中空值数为 0。**这是观察结果，不代表 NOT NULL 约束。** 布尔样字段如 `enabled`、`clicked` 目前被推断为整数。日期范围为该日期字段的非空最小值至最大值。

| 表（用途） | 实测行数 | 逻辑主键 | 全部字段（类型/实测空值） | 日期字段范围 |
|---|---:|---|---|---|
| `acquisition_channels`（用户和流量来源渠道） | 12 | `channel_id` | `channel_id`:S, `channel_name`:S, `channel_type`:S, `channel_group`:S, `enabled`:I | — |
| `ad_campaigns`（广告投放计划） | 100 | `ad_campaign_id` | `ad_campaign_id`:S, `ad_campaign_name`:S, `store_id`:S, `channel_id`:S, `billing_mode`:S, `budget`:F, `start_date`:D, `end_date`:D, `status`:S | `start_date` 2025-01-04～2026-08-21<br>`end_date` 2025-02-03～2026-08-22 |
| `ad_daily_stats`（广告计划每日效果） | 5,000 | `ad_stat_id` | `ad_stat_id`:S, `ad_campaign_id`:S, `stat_date`:D, `impressions`:I, `clicks`:I, `conversions`:I, `ad_cost`:F, `attributed_revenue`:F, `roi`:F | `stat_date` 2025-01-09～2026-08-22 |
| `brands`（商品品牌） | 150 | `brand_id` | `brand_id`:S, `brand_name`:S, `origin_type`:S, `enabled`:I | — |
| `campaign_products`（营销活动商品） | 1,500 | `campaign_product_id` | `campaign_product_id`:S, `campaign_id`:S, `product_id`:S, `campaign_price`:F, `stock_limit`:I, `per_user_limit`:I | — |
| `cart_items`（购物车商品明细） | 12,000 | `cart_item_id` | `cart_item_id`:S, `cart_id`:S, `sku_id`:S, `quantity`:I, `unit_price`:F, `added_at`:T, `selected`:I | `added_at` 2024-12-02 16:41:48～2026-08-21 05:17:07 |
| `coupon_receipts`（用户领券记录） | 8,000 | `receipt_id` | `receipt_id`:S, `coupon_id`:S, `user_id`:S, `received_at`:T, `receipt_status`:S, `source`:S | `received_at` 2025-01-13 23:02:02～2026-08-22 23:45:02 |
| `coupon_usages`（优惠券核销记录） | 3,632 | `usage_id` | `usage_id`:S, `receipt_id`:S, `coupon_id`:S, `order_id`:S, `user_id`:S, `discount_amount`:F, `used_at`:T | `used_at` 2025-01-01 02:21:13～2026-08-22 18:20:28 |
| `coupons`（优惠券定义） | 180 | `coupon_id` | `coupon_id`:S, `coupon_name`:S, `coupon_type`:S, `threshold_amount`:I, `discount_amount`:F, `valid_from`:D, `valid_to`:D, `issue_limit`:I, `scope_type`:S, `status`:S | `valid_from` 2025-01-13～2026-08-21<br>`valid_to` 2025-01-24～2026-08-22 |
| `daily_product_metrics`（商品日运营指标） | 25,000 | `metric_id` | `metric_id`:S, `stat_date`:D, `product_id`:S, `store_id`:S, `impressions`:I, `page_views`:I, `unique_visitors`:I, `buyers`:I, `paid_orders`:I, `gmv`:F, `refund_orders`:I, `favorite_additions`:I | `stat_date` 2025-08-23～2026-08-22 |
| `daily_store_metrics`（店铺日经营指标） | 16,000 | `metric_id` | `metric_id`:S, `stat_date`:D, `store_id`:S, `visitors`:I, `buyers`:I, `paid_orders`:I, `gmv`:F, `refund_amount`:F, `net_revenue`:F, `conversion_rate`:F, `service_score`:F, `fulfillment_rate`:F | `stat_date` 2026-02-04～2026-08-22 |
| `favorite_products`（用户收藏商品） | 8,000 | `favorite_id` | `favorite_id`:S, `user_id`:S, `product_id`:S, `favorited_at`:T, `source_page`:S | `favorited_at` 2025-01-01 01:29:49～2026-08-22 21:34:29 |
| `inventory_movements`（库存出入库流水） | 10,000 | `movement_id` | `movement_id`:S, `sku_id`:S, `warehouse_id`:S, `movement_type`:S, `quantity_delta`:I, `occurred_at`:T, `reference_no`:S, `source_system`:S | `occurred_at` 2025-01-01 02:30:09～2026-08-22 17:25:29 |
| `marketing_campaigns`（平台营销活动） | 100 | `campaign_id` | `campaign_id`:S, `campaign_name`:S, `campaign_type`:S, `starts_at`:T, `ends_at`:T, `budget`:F, `sponsor_type`:S, `status`:S | `starts_at` 2025-01-05 21:34:04～2026-08-22 04:31:15<br>`ends_at` 2025-01-15 21:34:04～2026-08-22 23:59:59 |
| `member_point_transactions`（会员积分变动流水） | 12,000 | `point_transaction_id` | `point_transaction_id`:S, `user_id`:S, `change_type`:S, `point_delta`:I, `occurred_at`:T, `reference_no`:S | `occurred_at` 2025-01-01 01:13:07～2026-08-22 23:18:09 |
| `order_items`（订单商品明细） | 20,461 | `order_item_id` | `order_item_id`:S, `order_id`:S, `product_id`:S, `sku_id`:S, `quantity`:I, `unit_price`:F, `discount_amount`:F, `paid_amount`:F, `cost_amount`:F | — |
| `orders`（交易订单主表） | 12,000 | `order_id` | `order_id`:S, `user_id`:S, `store_id`:S, `channel_id`:S, `ordered_at`:T, `order_status`:S, `item_amount`:F, `discount_amount`:F, `shipping_fee`:I, `payable_amount`:F, `paid_amount`:F, `region`:S, `province`:S, `city`:S | `ordered_at` 2025-01-01 00:12:54～2026-08-22 23:30:34 |
| `page_views`（页面浏览明细） | 50,000 | `view_id` | `view_id`:S, `session_id`:S, `user_id`:S~5051, `page_type`:S, `product_id`:S~31021, `source_position`:S, `viewed_at`:T, `stay_seconds`:I, `scroll_depth`:F | `viewed_at` 2025-01-01 00:09:36～2026-08-23 00:00:11 |
| `payments`（订单支付流水） | 11,922 | `payment_id` | `payment_id`:S, `order_id`:S, `user_id`:S, `payment_method`:S, `payment_amount`:F, `payment_status`:S, `paid_at`:T, `transaction_no`:S | `paid_at` 2025-01-01 02:27:13～2026-08-23 00:29:34 |
| `product_categories`（商品类目层级） | 40 | `category_id` | `category_id`:S, `category_name`:S, `parent_category_id`:S~8, `category_level`:I, `sort_order`:I, `enabled`:I | — |
| `product_prices`（SKU价格变更历史） | 6,000 | `price_id` | `price_id`:S, `sku_id`:S, `price`:F, `effective_from`:D, `effective_to`:D~3000, `price_type`:S | `effective_from` 2025-01-01～2026-08-22<br>`effective_to` 2025-02-07～2026-08-22 |
| `product_reviews`（商品评价） | 6,000 | `review_id` | `review_id`:S, `order_item_id`:S, `order_id`:S, `user_id`:S, `product_id`:S, `rating`:I, `review_content`:S, `has_image`:I, `created_at`:T, `review_status`:S | `created_at` 2025-01-04 17:16:30～2026-09-18 12:11:38 |
| `product_skus`（SKU销售单元） | 3,000 | `sku_id` | `sku_id`:S, `product_id`:S, `sku_name`:S, `color`:S, `specification`:S, `market_price`:F, `sale_price`:F, `cost_price`:F, `barcode`:I, `sku_status`:S | — |
| `products`（SPU商品主表） | 1,500 | `product_id` | `product_id`:S, `product_name`:S, `store_id`:S, `brand_id`:S, `category_id`:S, `supplier_id`:S, `product_status`:S, `created_at`:T, `weight_kg`:F, `warranty_months`:I | `created_at` 2024-03-07 09:55:07～2026-08-21 14:09:19 |
| `purchase_order_items`（采购单商品明细） | 3,000 | `purchase_item_id` | `purchase_item_id`:S, `purchase_order_id`:S, `sku_id`:S, `ordered_qty`:I, `received_qty`:I, `unit_cost`:F, `line_amount`:F | — |
| `purchase_orders`（采购单） | 800 | `purchase_order_id` | `purchase_order_id`:S, `supplier_id`:S, `warehouse_id`:S, `created_at`:T, `expected_date`:D, `status`:S, `purchase_amount`:F, `currency`:S | `created_at` 2025-01-01 21:06:55～2026-08-20 16:45:34<br>`expected_date` 2025-01-07～2026-09-12 |
| `recommendation_exposures`（推荐位曝光和点击明细） | 25,000 | `exposure_id` | `exposure_id`:S, `user_id`:S, `product_id`:S, `position_name`:S, `strategy_name`:S, `exposed_at`:T, `clicked`:I, `converted`:I | `exposed_at` 2025-01-01 01:25:24～2026-08-22 23:18:25 |
| `refund_items`（退款商品明细） | 1,990 | `refund_item_id` | `refund_item_id`:S, `refund_id`:S, `order_item_id`:S, `sku_id`:S, `refund_quantity`:I, `refund_amount`:F, `refund_type`:S | — |
| `refunds`（退款申请） | 1,600 | `refund_id` | `refund_id`:S, `order_id`:S, `user_id`:S, `refund_reason`:S, `refund_amount`:F, `refund_status`:S, `applied_at`:T, `completed_at`:T~413 | `applied_at` 2025-01-02 07:42:55～2026-09-12 12:00:47<br>`completed_at` 2025-01-04 07:42:55～2026-09-15 15:15:14 |
| `review_replies`（店铺评价回复） | 1,000 | `reply_id` | `reply_id`:S, `review_id`:S, `store_id`:S, `reply_content`:S, `replied_at`:T, `reply_type`:S | `replied_at` 2025-01-05 13:17:51～2026-09-17 09:25:42 |
| `sales_targets`（店铺月度经营目标） | 640 | `target_id` | `target_id`:S, `target_month`:S, `store_id`:S, `gmv_target`:F, `net_revenue_target`:F, `order_target`:I, `conversion_rate_target`:F, `owner_name`:S | `target_month` 2026-01～2026-08 |
| `search_events`（站内搜索行为） | 12,000 | `search_id` | `search_id`:S, `session_id`:S, `user_id`:S~1250, `keyword`:S, `category_id`:S, `result_count`:I, `clicked`:I, `converted`:I, `searched_at`:T | `searched_at` 2025-01-01 00:08:43～2026-08-23 00:00:10 |
| `service_tickets`（客服工单） | 2,200 | `ticket_id` | `ticket_id`:S, `user_id`:S, `order_id`:S~481, `issue_type`:S, `priority`:S, `ticket_status`:S, `created_at`:T, `resolved_at`:T~334, `source_channel`:S | `created_at` 2025-01-01 05:17:50～2026-08-31 17:19:40<br>`resolved_at` 2025-01-02 21:17:50～2026-09-03 14:19:40 |
| `shipment_tracks`（物流轨迹） | 38,536 | `track_id` | `track_id`:S, `shipment_id`:S, `track_status`:S, `location`:S, `occurred_at`:T | `occurred_at` 2025-01-01 15:20:13～2026-08-28 16:19:10 |
| `shipments`（订单发货单） | 10,438 | `shipment_id` | `shipment_id`:S, `order_id`:S, `warehouse_id`:S, `carrier_name`:S, `tracking_no`:S, `shipped_at`:T, `delivered_at`:T~1608, `shipment_status`:S, `package_count`:I | `shipped_at` 2025-01-01 15:20:13～2026-08-25 18:30:34<br>`delivered_at` 2025-01-02 15:20:13～2026-08-28 16:19:10 |
| `shopping_carts`（用户购物车） | 6,000 | `cart_id` | `cart_id`:S, `user_id`:S, `updated_at`:T, `cart_status`:S, `platform`:S | `updated_at` 2025-01-01 02:06:14～2026-08-22 23:33:48 |
| `sku_inventory`（SKU当前库存） | 3,000 | `sku_id`, `warehouse_id` | `sku_id`:S, `warehouse_id`:S, `available_qty`:I, `reserved_qty`:I, `safety_stock`:I, `max_stock`:I, `updated_at`:T | `updated_at` 2026-08-22 23:59:59～2026-08-22 23:59:59 |
| `store_staff`（店铺运营人员） | 400 | `staff_id` | `staff_id`:S, `store_id`:S, `staff_name`:S, `role_name`:S, `email`:S, `joined_date`:D, `employment_status`:S | `joined_date` 2023-08-21～2026-08-11 |
| `stores`（平台店铺） | 80 | `store_id` | `store_id`:S, `store_name`:S, `store_type`:S, `region`:S, `province`:S, `city`:S, `opened_date`:D, `status`:S, `commission_rate`:F | `opened_date` 2022-11-06～2025-10-16 |
| `suppliers`（商品供应商） | 150 | `supplier_id` | `supplier_id`:S, `supplier_name`:S, `region`:S, `province`:S, `city`:S, `supplier_grade`:S, `lead_time_days`:I, `on_time_rate`:F, `status`:S | — |
| `ticket_messages`（客服会话消息） | 6,000 | `message_id` | `message_id`:S, `ticket_id`:S, `sender_type`:S, `message_content`:S, `sent_at`:T, `message_type`:S | `sent_at` 2025-01-01 05:20:50～2026-09-01 01:08:40 |
| `user_addresses`（用户收货地址） | 7,500 | `address_id` | `address_id`:S, `user_id`:S, `province`:S, `city`:S, `district`:S, `address_detail`:S, `receiver_name`:S, `masked_mobile`:S, `is_default`:I, `region`:S | — |
| `user_devices`（用户常用设备） | 7,000 | `device_id` | `device_id`:S, `user_id`:S, `device_type`:S, `platform`:S, `app_version`:S, `last_active_at`:T, `trusted`:I | `last_active_at` 2025-01-01 02:51:18～2026-08-22 23:05:40 |
| `user_memberships`（会员等级和有效期） | 4,000 | `membership_id` | `membership_id`:S, `user_id`:S, `member_level`:S, `joined_date`:D, `expire_date`:D, `available_points`:I, `growth_level`:I, `status`:S | `joined_date` 2025-01-05～2026-12-15<br>`expire_date` 2026-01-05～2027-12-15 |
| `user_profiles`（用户画像和人口属性） | 5,000 | `user_id` | `user_id`:S, `mock_name`:S, `gender`:S, `birthday`:D, `region`:S, `province`:S, `city`:S, `occupation`:S, `income_band`:S, `updated_at`:T | `birthday` 1965-01-06～2004-12-28<br>`updated_at` 2025-01-16 19:38:34～2027-09-21 08:35:49 |
| `user_sessions`（访问会话） | 20,000 | `session_id` | `session_id`:S, `user_id`:S~1993, `device_id`:S, `channel_id`:S, `started_at`:T, `ended_at`:T, `duration_seconds`:I, `landing_page`:S, `converted`:I | `started_at` 2025-01-01 00:08:35～2026-08-22 23:58:16<br>`ended_at` 2025-01-01 00:14:41～2026-08-23 00:00:29 |
| `user_tag_relations`（用户和标签的关联） | 8,000 | `relation_id` | `relation_id`:S, `user_id`:S, `tag_id`:S, `score`:F, `assigned_at`:T, `source`:S | `assigned_at` 2025-01-01 00:18:53～2026-08-22 19:45:26 |
| `user_tags`（用户标签定义） | 16 | `tag_id` | `tag_id`:S, `tag_name`:S, `tag_type`:S, `enabled`:I | — |
| `users`（商城注册用户主表） | 5,000 | `user_id` | `user_id`:S, `nickname`:S, `email`:S, `masked_mobile`:S, `register_channel_id`:S, `registered_at`:T, `account_status`:S, `register_platform`:S | `registered_at` 2025-01-01 00:57:05～2026-08-22 22:06:02 |
| `warehouses`（履约仓库） | 16 | `warehouse_id` | `warehouse_id`:S, `warehouse_name`:S, `region`:S, `province`:S, `city`:S, `capacity_units`:I, `status`:S | — |

**合计：50 表，381,963 行。** 全部逻辑主键在现有文件中均非空且无重复；每张表的用途采用生成脚本/静态 Schema 的描述，并非独立确认的真实业务定义。

## 3. 核心数据粒度与安全样例

| 表 | 一行的粒度；主要分组/筛选/关联字段 | 可用数值字段；尚未确认的含义 |
|---|---|---|
| `orders` | 1 个订单；`order_id`，按 `ordered_at`、`order_status`、`store_id`、`channel_id`、`region` 分组；用 `user_id`、`order_id` 关联。12,000 行。 | `item_amount`、`discount_amount`、`shipping_fee`、`payable_amount`、`paid_amount`；实测 `payable=item-discount+shipping`，`paid_amount>0` 为 11,045 单。但 GMV 是否含运费、用下单/支付时点，需确认。 |
| `order_items` | 1 个订单商品明细；`order_item_id`，`order_id` 接订单，`product_id`/`sku_id` 接商品/SKU。20,461 行，覆盖全部 12,000 单。 | `quantity`、`unit_price`、`discount_amount`、`paid_amount`、`cost_amount`。**未支付和取消订单的明细也有正金额**，须联结订单状态后计算成交商品额；商品成本口径未确认。 |
| `products`/`product_skus`/`product_categories` | `products` 1 个 SPU，`product_skus` 1 个 SKU，类目表含 8 个一级、32 个二级类目；商品可按店铺、品牌、供应商、类目筛选。 | 售价/成本为目录值或 SKU 值，不应替代订单成交价；`parent_category_id` 构成类目树。 |
| `stores`/`users` | 各 1 个店铺/注册用户；分别由 `store_id`、`user_id` 连接交易、流量和维度表。店铺可按区域、类型、状态筛选，用户可按注册渠道、平台、状态筛选。 | 地理字段可能代表店铺所在地、用户注册地或订单归属地，不能无条件互换；用户画像和地址涉及个人信息，本报告不展示其值。 |
| `payments` | 1 条支付尝试，按 `payment_status`、`paid_at`、`payment_method` 分组，`order_id` 关联。11,922 条覆盖 11,045 个已付订单。 | 877 条 `failed` 仍有正 `payment_amount`；仅 `success` 合计与订单实付一致。 |
| `refunds`/`refund_items` | 1 个退款申请 / 1 个退款商品明细；按 `refund_status`、`applied_at`、`completed_at`、`refund_type` 分组。 | 1,600 个申请、1,990 个明细；头表与明细退款额逐单一致。申请、批准、完成金额不可混称“实际退款”；是否发生退货需看 `refund_type`。 |
| `shipments`/`shipment_tracks` | 1 个发货单 / 1 条物流轨迹；当前每个有发货单的订单最多 1 个发货单，每单可有多条轨迹；可按仓库、承运商、物流状态、发货/签收时间筛选。 | `package_count` 与物流轨迹条数不是同一概念；签收率应在发货单粒度计算，`delivered_at` 有空值。 |
| `coupons`/`coupon_receipts`/`coupon_usages` | 优惠券定义 / 用户领券 / 核销记录；当前每张领券记录最多 1 次核销，订单可关联核销。 | 核销优惠额可汇总，但与 `orders.discount_amount` 的组成关系未从数据定义确认，不应再加到订单折扣上。 |
| `purchase_orders`/`purchase_order_items` | 采购单 / 采购 SKU 明细；可按供应商、仓库、状态、创建及预计到货时间筛选。 | `purchase_amount`、`ordered_qty`、`received_qty`、`line_amount` 用于采购分析；与销售成本/库存流水的对账关系未确认。 |
| `daily_product_metrics` | 1 个 `product_id` + `stat_date`，现有 25,000 组合均唯一；`store_id` 与商品店铺一致，日期覆盖 365 天但商品×日期并不完整。 | `gmv`、`paid_orders`、`buyers`、流量数、`refund_orders`；生成脚本独立随机合成，不能当作订单表的聚合结果。 |
| `daily_store_metrics` | 1 个 `store_id` + `stat_date`，80 店×200 天完整，2026-02-04～08-22。 | `gmv`、`refund_amount`、`net_revenue`、`paid_orders`、`visitors` 等；`net_revenue=gmv-refund_amount` 在数据中成立，但与订单/退款明细未对齐。比率/评分字段不能直接求和。 |
| `ad_daily_stats`/`ad_campaigns` | 广告统计 1 行是一次广告计划的某日统计记录，但 `ad_campaign_id` + `stat_date` **不唯一**：5,000 行仅 2,770 个组合，重复组最大 28 行。 | `impressions`、`clicks`、`conversions`、`ad_cost`、`attributed_revenue`、`roi`；重复行是否分渠道/批次未知，未定义归因窗口。 |
| `page_views`/`user_sessions`/`search_events` | 浏览事件 / 会话 / 搜索事件；可用会话、用户、渠道和事件时间筛选。 | 有访客匿名空值，事件计数可做，但去重访客与转化口径需确认。 |
| `sku_inventory`/`inventory_movements` | 当前 SKU 库存快照 / 出入库事件；库存快照时间均为 2026-08-22 23:59:59。 | `available_qty`、`reserved_qty`、`quantity_delta`；不能从单个快照推断历史库存。 |
| `marketing_campaigns`/`campaign_products` | 1 个营销活动 / 1 条活动商品配置；活动×商品组合当前并非唯一，按活动时间、状态、商品筛选时需保留配置记录粒度。 | `budget`、`campaign_price`、`stock_limit` 是配置值；缺少直接的活动成交事实，无法只凭配置算活动销售贡献。 |
| `service_tickets`/`ticket_messages` | 1 个客服工单 / 1 条消息；工单可按问题类型、优先级、状态、创建/解决时间筛选，订单关联可为空。 | 可算工单量与解决时长；不等于退款或履约事件，文本内容不在本报告展示。 |
| `sales_targets` | 1 个店铺×目标月份，现有 640 组合唯一；`target_month` 为 `YYYY-MM` 文本。 | `gmv_target`、`net_revenue_target`、`order_target` 等；与实际值比对需先确认同口径。 |

脱敏样例（只含业务 ID/枚举/金额，不含用户联系方式和地址）：`orders.order_id=O00000001`，`ordered_at=2025-08-12 09:48:09`，`order_status=completed`，`paid_amount=4142.31`；对应 `order_items.order_item_id=OI000000001`、`product_id=P000687`、`sku_id=SKU0001373`、`quantity=1`、`paid_amount=1105.50`；`product_categories` 示例 `C001 数码家电`（一级）、`C002 手机通讯`（父类 `C001`）；`refunds.refund_id=RF0000001`、`refund_status=completed`、`refund_amount=863.11`；`daily_product_metrics` 样例 `stat_date=2025-11-15`、`product_id=P000767`、`gmv=180528.06`。这些样例仅用于展示字段形态，不代表指标口径。

## 4. 逻辑关系与基数

下面逐条列出静态 Schema 的 79 个候选关联。方向为父表字段 → 子表字段。`1:0..1`/`1:0..N` 是**当前数据的观察基数**；CSV 未强制未来数据保持此基数。所有 79 条关联的父侧字段当前唯一，子侧非空键均能匹配到父侧（实测孤儿数为 0）。`空n` 为子侧外键 NULL 数。

| 关联字段 | 观察基数 | 子侧空键 |
|---|---|---:|
| `users.user_id` → `user_profiles.user_id` | 1:0..1（最多 1） | 0 |
| `users.user_id` → `user_addresses.user_id` | 1:0..N（最多 7） | 0 |
| `users.user_id` → `user_memberships.user_id` | 1:0..1（最多 1） | 0 |
| `users.user_id` → `member_point_transactions.user_id` | 1:0..N（最多 10） | 0 |
| `users.user_id` → `user_tag_relations.user_id` | 1:0..N（最多 8） | 0 |
| `user_tags.tag_id` → `user_tag_relations.tag_id` | 1:0..N（最多 532） | 0 |
| `acquisition_channels.channel_id` → `users.register_channel_id` | 1:0..N（最多 976） | 0 |
| `users.user_id` → `user_devices.user_id` | 1:0..N（最多 8） | 0 |
| `users.user_id` → `user_sessions.user_id` | 1:0..N（最多 11） | 1,993 |
| `user_devices.device_id` → `user_sessions.device_id` | 1:0..N（最多 12） | 0 |
| `acquisition_channels.channel_id` → `user_sessions.channel_id` | 1:0..N（最多 3948） | 0 |
| `user_sessions.session_id` → `page_views.session_id` | 1:0..N（最多 11） | 0 |
| `users.user_id` → `page_views.user_id` | 1:0..N（最多 32） | 5,051 |
| `products.product_id` → `page_views.product_id` | 1:0..N（最多 26） | 31,021 |
| `user_sessions.session_id` → `search_events.session_id` | 1:0..N（最多 6） | 0 |
| `users.user_id` → `search_events.user_id` | 1:0..N（最多 12） | 1,250 |
| `product_categories.category_id` → `search_events.category_id` | 1:0..N（最多 426） | 0 |
| `stores.store_id` → `store_staff.store_id` | 1:0..N（最多 5） | 0 |
| `stores.store_id` → `products.store_id` | 1:0..N（最多 19） | 0 |
| `brands.brand_id` → `products.brand_id` | 1:0..N（最多 20） | 0 |
| `product_categories.category_id` → `products.category_id` | 1:0..N（最多 57） | 0 |
| `suppliers.supplier_id` → `products.supplier_id` | 1:0..N（最多 19） | 0 |
| `products.product_id` → `product_skus.product_id` | 1:0..N（最多 2） | 0 |
| `product_skus.sku_id` → `product_prices.sku_id` | 1:0..N（最多 2） | 0 |
| `product_skus.sku_id` → `sku_inventory.sku_id` | 1:0..1（最多 1） | 0 |
| `warehouses.warehouse_id` → `sku_inventory.warehouse_id` | 1:0..N（最多 232） | 0 |
| `product_skus.sku_id` → `inventory_movements.sku_id` | 1:0..N（最多 10） | 0 |
| `warehouses.warehouse_id` → `inventory_movements.warehouse_id` | 1:0..N（最多 787） | 0 |
| `suppliers.supplier_id` → `purchase_orders.supplier_id` | 1:0..N（最多 13） | 0 |
| `warehouses.warehouse_id` → `purchase_orders.warehouse_id` | 1:0..N（最多 63） | 0 |
| `purchase_orders.purchase_order_id` → `purchase_order_items.purchase_order_id` | 1:0..N（最多 4） | 0 |
| `product_skus.sku_id` → `purchase_order_items.sku_id` | 1:0..N（最多 6） | 0 |
| `marketing_campaigns.campaign_id` → `campaign_products.campaign_id` | 1:0..N（最多 23） | 0 |
| `products.product_id` → `campaign_products.product_id` | 1:0..N（最多 5） | 0 |
| `coupons.coupon_id` → `coupon_receipts.coupon_id` | 1:0..N（最多 61） | 0 |
| `users.user_id` → `coupon_receipts.user_id` | 1:0..N（最多 9） | 0 |
| `coupon_receipts.receipt_id` → `coupon_usages.receipt_id` | 1:0..1（最多 1） | 0 |
| `coupons.coupon_id` → `coupon_usages.coupon_id` | 1:0..N（最多 32） | 0 |
| `orders.order_id` → `coupon_usages.order_id` | 1:0..N（最多 5） | 0 |
| `users.user_id` → `coupon_usages.user_id` | 1:0..N（最多 5） | 0 |
| `stores.store_id` → `ad_campaigns.store_id` | 1:0..N（最多 5） | 0 |
| `acquisition_channels.channel_id` → `ad_campaigns.channel_id` | 1:0..N（最多 14） | 0 |
| `ad_campaigns.ad_campaign_id` → `ad_daily_stats.ad_campaign_id` | 1:0..N（最多 50） | 0 |
| `users.user_id` → `recommendation_exposures.user_id` | 1:0..N（最多 14） | 0 |
| `products.product_id` → `recommendation_exposures.product_id` | 1:0..N（最多 29） | 0 |
| `users.user_id` → `shopping_carts.user_id` | 1:0..N（最多 6） | 0 |
| `shopping_carts.cart_id` → `cart_items.cart_id` | 1:0..N（最多 9） | 0 |
| `product_skus.sku_id` → `cart_items.sku_id` | 1:0..N（最多 12） | 0 |
| `users.user_id` → `favorite_products.user_id` | 1:0..N（最多 8） | 0 |
| `products.product_id` → `favorite_products.product_id` | 1:0..N（最多 13） | 0 |
| `orders.order_id` → `order_items.order_id` | 1:0..N（最多 4） | 0 |
| `products.product_id` → `order_items.product_id` | 1:0..N（最多 27） | 0 |
| `product_skus.sku_id` → `order_items.sku_id` | 1:0..N（最多 17） | 0 |
| `users.user_id` → `orders.user_id` | 1:0..N（最多 9） | 0 |
| `stores.store_id` → `orders.store_id` | 1:0..N（最多 192） | 0 |
| `acquisition_channels.channel_id` → `orders.channel_id` | 1:0..N（最多 2374） | 0 |
| `orders.order_id` → `payments.order_id` | 1:0..N（最多 2） | 0 |
| `users.user_id` → `payments.user_id` | 1:0..N（最多 10） | 0 |
| `orders.order_id` → `shipments.order_id` | 1:0..1（最多 1） | 0 |
| `warehouses.warehouse_id` → `shipments.warehouse_id` | 1:0..N（最多 676） | 0 |
| `shipments.shipment_id` → `shipment_tracks.shipment_id` | 1:0..N（最多 4） | 0 |
| `orders.order_id` → `refunds.order_id` | 1:0..1（最多 1） | 0 |
| `users.user_id` → `refunds.user_id` | 1:0..N（最多 4） | 0 |
| `refunds.refund_id` → `refund_items.refund_id` | 1:0..N（最多 2） | 0 |
| `order_items.order_item_id` → `refund_items.order_item_id` | 1:0..1（最多 1） | 0 |
| `product_skus.sku_id` → `refund_items.sku_id` | 1:0..N（最多 5） | 0 |
| `order_items.order_item_id` → `product_reviews.order_item_id` | 1:0..1（最多 1） | 0 |
| `orders.order_id` → `product_reviews.order_id` | 1:0..N（最多 4） | 0 |
| `users.user_id` → `product_reviews.user_id` | 1:0..N（最多 8） | 0 |
| `products.product_id` → `product_reviews.product_id` | 1:0..N（最多 12） | 0 |
| `product_reviews.review_id` → `review_replies.review_id` | 1:0..1（最多 1） | 0 |
| `stores.store_id` → `review_replies.store_id` | 1:0..N（最多 25） | 0 |
| `users.user_id` → `service_tickets.user_id` | 1:0..N（最多 5） | 0 |
| `orders.order_id` → `service_tickets.order_id` | 1:0..N（最多 3） | 481 |
| `service_tickets.ticket_id` → `ticket_messages.ticket_id` | 1:0..N（最多 3） | 0 |
| `products.product_id` → `daily_product_metrics.product_id` | 1:0..N（最多 33） | 0 |
| `stores.store_id` → `daily_product_metrics.store_id` | 1:0..N（最多 349） | 0 |
| `stores.store_id` → `daily_store_metrics.store_id` | 1:0..N（最多 200） | 0 |
| `stores.store_id` → `sales_targets.store_id` | 1:0..N（最多 8） | 0 |

**连接和重复计数风险。** `orders` → `order_items` 为 1:N；订单金额联结明细后直接 `SUM(orders.paid_amount)` 会按明细行重复。`orders` → `payments` 为 1:N（成功+失败支付尝试），须先筛 `payment_status=success` 或按订单聚合。`refunds` → `refund_items` 为 1:N，退款头金额不能按明细重复加。商品→SKU、SKU→价格历史、店铺→日汇总、订单→客服工单、会话→浏览事件等同理。两个事实表若只按 `product_id` 或 `store_id` 联结，会形成多对多乘积；日汇总需带日期及适当键，或先分别聚合。

**额外候选唯一键及反例。** 当前数据中 `payments.transaction_no`、`shipments.tracking_no`、`product_skus.barcode`、`users.email` 唯一，数据库并未声明唯一约束。`daily_product_metrics(product_id,stat_date)`、`daily_store_metrics(store_id,stat_date)`、`sales_targets(store_id,target_month)` 当前唯一。相反，`ad_daily_stats(ad_campaign_id,stat_date)` 重复 1,260 组（最多 28 行）；`campaign_products(campaign_id,product_id)` 重复 7 组，活动商品与商品表关联前要先看目标粒度。`product_categories.parent_category_id` 是自关联候选键，一级类目 8 行为空。

## 5. 经营指标：能算什么、不能直接认定什么

| 指标 | 当前可执行的计算与结果 / 限制 |
|---|---|
| 订单量与客单价 | `orders` 中按 `paid_amount>0` 有 **11,045 单**，实付总额 **37,206,143.04**，实付客单价 **3,368.60**（总额÷单数）。全部下单 12,000 单；“订单量”选下单、成功支付还是完成单须指定。 |
| GMV | 两张日汇总表可直接求各自 `gmv`：商品日表合计 **4,964,829,525.93**（365 天）、店铺日表合计 **1,017,138,078.35**（200 天）。相同 200 天商品日 GMV **2,752,108,689.68**，店铺日 GMV **1,017,138,078.35**，订单实付仅 **12,423,824.60**。三者不是同一来源的汇总；**无法认定统一、可核账的 GMV**。订单实付含运费，不能自动命名 GMV。 |
| 退款金额 | `refunds` 可按申请状态/日期直接统计。`completed` 1,079 申请，记录金额合计 **2,678,587.52**；全状态相加会包含未完成和拒绝。与 `refund_items`、`order_items` 关联后可按商品/SKU/类目拆分，但要从明细金额汇总。实际入账退款时间、部分退货完成时间口径需确认。 |
| 商品销售排名 | `order_items` 联结 `orders`（筛已付）与 `products`/`product_categories`，按 `product_id`、SKU 或类目汇总数量/商品实付金额可计算。需说明是否扣除完成退款；退款净额应通过 `refund_items` 在同粒度先聚合。`daily_product_metrics.gmv` 也可单独排序，但其覆盖稀疏且与订单不一致。 |
| 时间趋势 | 可按 `orders.ordered_at`、成功 `payments.paid_at`、`refunds.applied_at/completed_at` 分别作趋势；日汇总用 `stat_date`。下单趋势和支付趋势并非同一时间轴。 |
| 流量、广告、库存、目标 | 浏览/会话/搜索事件计数、广告消耗与点击、当前库存、店铺目标可在各自表计算；跨表转化率、广告 ROI、目标达成率需明确关联和统计窗口，广告日表重复粒度需先解释。 |

## 6. 数据缺口与待确认业务问题

1. **选哪套事实作经营真值？** 商品日/店铺日 GMV 是独立随机生成，不由 `orders` 汇总；同一天也差异很大。例如 2026-08-22：商品日 9,371,109.38，店铺日 4,648,915.68。若做 Data Agent，建议先把交易事实与两套模拟汇总分开命名，并要求每个指标绑定事实表和口径。
2. **GMV 与订单量口径**：GMV 按下单、成功支付还是完成？是否含运费、优惠、取消单、退款？订单量计全部下单还是成功支付/完成？客单价分母如何定？
3. **退款口径**：`completed` 是资金退回完成还是业务审批完成？`refund_only` 与 `return_and_refund` 是否都计退款；按申请日还是完成日归属；退款后订单头的 `paid_amount` 是否应保持原始支付额？当前 `order_status=refunded` 与退款状态并不一致，不能只靠订单状态判断退款完成。
4. **商品口径**：排名按 SPU 还是 SKU、销售件数还是金额；是否排除取消/未付，是否扣完成退款；类目按二级还是一级父类汇总。
5. **汇总表/广告口径**：`daily_product_metrics` 只有 25,000 个商品日组合，非全量面板；缺行是零还是未统计？`ad_daily_stats` 同计划同日多行却缺少区分维度，重复行是否应累加？`attributed_revenue` 的归因窗口是什么？
6. **时间与货币**：源清单上限为 2026-08-22，但 `refunds.applied_at` 最晚 2026-09-12、`product_reviews.created_at` 最晚 2026-09-18、`user_profiles.updated_at` 最晚 2027-09-21；需明确快照日/数据截止日的含义。多数金额表没有货币字段，跨表默认同币种尚无证据。
7. **数据质量边界**：当前 50 个逻辑主键均唯一且非空、79 个候选关系无非空孤儿；这些是本次快照事实。CSV 不强制约束，后续替换数据须重新验证。

核验方式：只使用内存 DuckDB 对现有 CSV 执行 `DESCRIBE`、`COUNT`、`MIN/MAX`、`COUNT(DISTINCT ...)` 和只读连接/汇总查询；未运行生成或校验脚本，未触及密钥与敏感配置值。
