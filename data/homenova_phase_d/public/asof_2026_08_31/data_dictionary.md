# HomeNova 经营数据字典

本数据包为德国单店、单仓电商的合成交易数据，币种统一为 EUR。它支持商品、访问、订单、支付、采购、库存、履约与售后的关联核对。本阶段包含十五张业务表，其中广告、访问行为、竞品价格及类型明确的投诉、客服回复、解决记录和评价均为可观察业务事实。广告归因不是增量因果证据，库存不足也不能直接解释为确定的潜在失单。

## 1. 数据范围、类型与查询约定

- 默认新会话及新订单业务窗口为当地时间 `2026-03-01 00:00 <= t < 2026-09-01 00:00`；期初库存及已下达采购可以包含窗口前已知的业务信息。付款、取消、发货、签收、退款和退货验收可以在新订单窗口结束后继续发生；准确窗口以 `dataset_metadata.json` 为准。
- 本包为固定观察截面的业务快照，准确UTC截点、左闭右开边界和数据覆盖以 `dataset_metadata.json` 为准。没有落入该截点前可知范围的后续结果不属于本包事实；不能把观察截点当作新订单持续经营日期。
- 所有时间列逻辑类型为 `UTC_TIMESTAMP`，CSV 中使用带 `Z` 的 ISO8601 UTC 文本。经营日、周、月需转换到 `Europe/Berlin` 后分组；不能全年使用固定 UTC 偏移。CSV 的自动类型推断不替代这一时间约定。
- `TEXT` 表示字符串；`INTEGER` 表示整数；`BOOLEAN` 在 CSV 中为 `true/false`。CSV 空字段表示 NULL，不是空字符串、零或尚未发生事件的日期。
- 所有金额列为整数欧分，展示为 EUR 时除以 100。后缀 `_gross_cents` 为含税金额，`_ex_vat_cents` 为不含税金额，`_vat_cents` 为税额。数量按 SKU 销售单位计，套装仍按套计数。
- 税率为本场景固定业务参数 19%。原始商品行税额按 `round_half_up(行折后含税金额 × 19 / 119)` 计算到欧分，不含税金额等于含税金额减该税额；运费单独拆税。汇总税额应加总行税，不能再次按订单总额拆税覆盖行结果。
- 订单商品折后含税金额达到 59.00 EUR 则顾客免运，否则收取 4.90 EUR 含税运费；部分退款后不重新追收运费。顾客运费与承运商费用分别记录。
- `recorded_at` 为该记录首次可知时间；`capture_recorded_at` 等字段为后续结果首次可知时间。业务发生时间与可知时间不是同一概念。`status_as_of` 是本包截点状态，不是任意历史日期状态。
- PK、FK 为本数据包的逻辑键约定，CSV 本身不强制执行。一个订单可有多商品、多支付尝试、多退款分摊；查询前应分别聚合，避免多对多连接放大金额。
- 订单采用同一可连接访客、下单之前的末次合格付费点击归因；成功支付距点击不得超过 7×24 小时，每单最多归一个计划。自然回访不覆盖仍有效的付费点击；未成功支付、身份无法连接或无合格点击时归因键为空，不得随机补齐。

## 2. 表与关联概览

| 表 | 一行的粒度 | 主键 | 主要关联 |
|---|---|---|---|
| `sku_catalog` | 一个可销售 SKU | `sku_id` | 商品价格、订单行、采购、库存与会话落地商品 |
| `price_intervals` | 一个 SKU 的一个有效价格区间 | `price_interval_id` | `sku_id -> sku_catalog` |
| `sessions` | 一次成功落地访问会话 | `session_id` | 可空 `landing_sku_id -> sku_catalog` |
| `orders` | 一笔完成库存预占、成功创建的订单 | `order_id` | `checkout_session_id -> sessions`，该键在订单中唯一 |
| `order_items` | 一个订单中的一个 SKU 商品行 | `order_item_id`；另有唯一 `(order_id, sku_id)` | 订单、商品、价格区间 |
| `payments` | 一次支付尝试 | `payment_id` | `order_id -> orders`，每单最多一个成功收款 |
| `procurements` | 一个 SKU 的一次计划收货批次 | `procurement_id` | `sku_id -> sku_catalog` |
| `inventory_movements` | 一次 SKU 库存状态变化 | `movement_id` | 按事件类型关联订单行、采购、发货或退款行 |
| `shipments` | 一笔订单的一次全量发货 | `shipment_id`；`order_id` 唯一 | `order_id -> orders` |
| `refunds` | 一次退款申请分配到某原商品行的一笔分摊 | `refund_line_id`；`refund_id` 为非唯一分组键 | `order_item_id -> order_items`、`payment_id -> payments` 的成功收款记录 |
| `campaigns` | 一个广告计划不可变操作版本 | `campaign_id` | `target_sku_id -> sku_catalog`；同系列由 `campaign_family_id` 关联 |
| `ad_daily` | 一个广告计划版本×当地经营日 | `(campaign_id, stat_local_date)` | `campaign_id -> campaigns` |
| `visit_events` | 一个计费点击或站内行为事件 | `event_id` | 按类型关联会话、计划和商品 |
| `competitor_prices` | 一次可比竞品商品价格观测 | `observation_id` | `mapped_sku_id -> sku_catalog` |
| `customer_feedback` | 一次类型明确的投诉、客服回复、解决或评价事件 | `feedback_event_id` | `order_id -> orders`，可空 `order_item_id -> order_items`，跟进事件的 `parent_event_id -> customer_feedback` 投诉根事件 |

## 3. 全字段说明

### 3.1 `sku_catalog`：商品主档

单店、单仓；商品类目代码为 `POWER`（智能用电）、`LIGHT`（智能照明）、`SECURITY`（家庭安防）。标准到仓成本固定，不含税且已包含分摊入库费用，不再次作为经营费用扣除。

| 字段 | 逻辑类型与单位 | 可空及含义 |
|---|---|---|
| `sku_id` | TEXT，PK | 非空；SKU 标识 |
| `sku_name` | TEXT | 非空；商品名称 |
| `category_code` | TEXT | 非空；上述三个类目之一 |
| `pack_units` | INTEGER，包装内物理件数 | 非空正整数；不改变按套计量的库存单位 |
| `store_id` | TEXT | 非空；经营店铺标识 |
| `currency` | TEXT | 非空；EUR |
| `standard_cost_ex_vat_cents` | INTEGER，不含税欧分/销售单位 | 非空；固定标准到仓单位成本 |
| `vat_rate_bps` | INTEGER，基点 | 非空；1900 表示 19% |
| `active_from` | UTC_TIMESTAMP | 非空；商品可用起点 |
| `active_to` | UTC_TIMESTAMP | 可空；空表示尚未登记结束，不代表库存无限或未来永久可售 |
| `recorded_at` | UTC_TIMESTAMP | 非空；主档首次可知时间 |

### 3.2 `price_intervals`：商品有效价格

同一 SKU 在可下单时刻应恰好命中一个价格区间；区间为 `[valid_from, valid_to)`，空结束时间表示开放。价格在下单时锁定，后续价格变化不重写旧订单。

| 字段 | 逻辑类型与单位 | 可空及含义 |
|---|---|---|
| `price_interval_id` | TEXT，PK | 非空；价格区间标识 |
| `sku_id` | TEXT，FK | 非空；指向商品主档 |
| `valid_from` | UTC_TIMESTAMP | 非空；价格生效时间 |
| `valid_to` | UTC_TIMESTAMP | 可空；区间结束，空表示当前开放 |
| `list_unit_gross_cents` | INTEGER，含税欧分/销售单位 | 非空正整数；正常单位标价 |
| `sale_unit_gross_cents` | INTEGER，含税欧分/销售单位 | 非空正整数；最终成交单位价，不大于正常标价 |
| `promotion_code` | TEXT | 可空；空表示没有登记促销标识 |
| `published_at` | UTC_TIMESTAMP | 非空；面向顾客公布时间，不晚于生效时间 |
| `recorded_at` | UTC_TIMESTAMP | 非空；价格记录首次可知时间 |

### 3.3 `sessions`：访问会话

会话是访问单位，不是用户、浏览事件或订单。访客使用伪匿名标识；只有 `identity_linkable=true` 的会话才支持相应跨会话关联。内部流量不纳入默认访问和会话转化指标。

| 字段 | 逻辑类型与单位 | 可空及含义 |
|---|---|---|
| `session_id` | TEXT，PK | 非空；成功落地会话标识 |
| `visitor_id` | TEXT | 非空；伪匿名访客标识，不等于真实自然人身份 |
| `identity_linkable` | BOOLEAN | 非空；当前观测是否支持使用访客键跨会话连接 |
| `started_at` | UTC_TIMESTAMP | 非空；会话开始时间 |
| `ended_at` | UTC_TIMESTAMP | 可空；空表示截至截点尚未观察到会话结束 |
| `source_channel` | TEXT | 非空；organic_search、direct，以及 paid_search_brand、paid_search_generic、paid_social；表示本次会话来源，不等于整笔订单最终广告归因 |
| `source_click_event_id` | TEXT，可空 FK | 付费落地会话必填，指向 visit_events 的 ad_click；自然/直接会话为空；点击未成功落地也可没有会话 |
| `landing_sku_id` | TEXT，可空 FK | 可空；指向落地商品；空表示非商品落地页或未记录商品 |
| `device_type` | TEXT | 非空；mobile、desktop 或 tablet |
| `destination_postal_prefix` | TEXT，邮区前两位 | 可空；空表示该次访问没有可知目的邮区，不填造顾客地址 |
| `is_internal_traffic` | BOOLEAN | 非空；是否内部访问；默认经营访问排除 true |
| `recorded_at` | UTC_TIMESTAMP | 非空；会话首次可知时间 |

### 3.4 `orders`：订单头

一笔订单必须先完成全部商品的原子库存预占。订单最多成功收款一次、全量发货一次。默认支付有效期为下单后 30 分钟；未成功支付到期释放预占。`status_as_of` 取值为 `pending_payment`（待支付）、`paid`（已付未发货）、`expired`（支付过期）、`cancelled`（已取消）、`shipped`（已发货未送达）、`delivered`（已送达）。售后不删除原支付和发货事实，也不靠订单终态代替退款计算。

| 字段 | 逻辑类型与单位 | 可空及含义 |
|---|---|---|
| `order_id` | TEXT，PK | 非空；订单标识 |
| `visitor_id` | TEXT | 非空；与创建该订单的会话访客一致 |
| `checkout_session_id` | TEXT，唯一 FK | 非空；指向形成该订单的结账会话 |
| `ordered_at` | UTC_TIMESTAMP | 非空；成功创建订单时间 |
| `expires_at` | UTC_TIMESTAMP | 非空；未支付订单失效时间 |
| `canceled_at` | UTC_TIMESTAMP | 可空；仅主动取消时填写，状态为 cancelled；支付超时 expired 不填写，以 expires_at 表示失效时点 |
| `cancel_recorded_at` | UTC_TIMESTAMP | 可空；主动取消首次可知时间；没有主动取消或仅支付超时失效时为空 |
| `status_as_of` | TEXT，状态枚举 | 非空；上述截点订单状态之一 |
| `item_list_gross_cents` | INTEGER，含税欧分 | 非空；商品行正常标价总金额 |
| `item_discount_gross_cents` | INTEGER，含税欧分 | 非空；商品优惠总额，没有优惠为 0 |
| `item_paid_gross_cents` | INTEGER，含税欧分 | 非空；商品折后金额；这是锁定金额，不代表该订单已收款 |
| `shipping_gross_cents` | INTEGER，含税欧分 | 非空；向顾客收取的运费，免运为 0 |
| `shipping_vat_cents` | INTEGER，欧分 | 非空；顾客运费税额 |
| `shipping_ex_vat_cents` | INTEGER，不含税欧分 | 非空；顾客运费不含税金额 |
| `payable_gross_cents` | INTEGER，含税欧分 | 非空；商品折后金额加顾客运费 |
| `currency` | TEXT | 非空；EUR |
| `destination_postal_prefix` | TEXT，邮区前两位 | 非空；订单履约目的邮区，不是完整地址 |
| `attributed_click_event_id` | TEXT，可空 FK | 成功支付时确定的最后合格付费点击，指向 visit_events；未支付或无有效归因时为空 |
| `attributed_campaign_id` | TEXT，可空 FK | 指向 campaigns，须等于归因点击的 campaign_id；无有效归因时为空 |
| `attribution_recorded_at` | UTC_TIMESTAMP | 可空；归因首次可知时间；未成功支付或无归因时为空 |
| `recorded_at` | UTC_TIMESTAMP | 非空；订单首次可知时间 |

头金额满足 `item_list_gross_cents - item_discount_gross_cents = item_paid_gross_cents`，且 `payable_gross_cents = item_paid_gross_cents + shipping_gross_cents`。未付明细存在不等于已成交，必须关联成功支付事实。

### 3.5 `order_items`：订单商品明细

每个订单同一 SKU 合并成一行；数量可大于一。原价减折扣等于最终商品金额；成本列是锁定快照，只有发生发货才确认销售成本。

| 字段 | 逻辑类型与单位 | 可空及含义 |
|---|---|---|
| `order_item_id` | TEXT，PK | 非空；订单商品行标识 |
| `order_id` | TEXT，FK | 非空；指向订单 |
| `sku_id` | TEXT，FK | 非空；指向商品 |
| `price_interval_id` | TEXT，FK | 非空；指向下单时生效的价格区间 |
| `quantity` | INTEGER，销售单位 | 非空正整数；购买数量 |
| `list_unit_gross_cents` | INTEGER，含税欧分/销售单位 | 非空；正常标价快照 |
| `sale_unit_gross_cents` | INTEGER，含税欧分/销售单位 | 非空；实际成交单位价快照 |
| `line_list_gross_cents` | INTEGER，含税欧分 | 非空；数量×正常单位标价 |
| `line_discount_gross_cents` | INTEGER，含税欧分 | 非空；该行优惠差额，无优惠为 0 |
| `line_paid_gross_cents` | INTEGER，含税欧分 | 非空；折后商品行金额，是否成交仍需核实支付 |
| `line_vat_cents` | INTEGER，欧分 | 非空；由本行最终含税金额计算的税额 |
| `line_ex_vat_cents` | INTEGER，不含税欧分 | 非空；本行含税金额减本行税额 |
| `unit_cost_ex_vat_cents` | INTEGER，不含税欧分/销售单位 | 非空；固定标准到仓单位成本快照 |
| `line_cost_ex_vat_cents` | INTEGER，不含税欧分 | 非空；数量×成本快照，不是直接计入当期利润的费用 |
| `recorded_at` | UTC_TIMESTAMP | 非空；明细首次可知时间 |

### 3.6 `payments`：支付尝试

`status` 为 `failed` 或 `captured`。失败尝试不增加实收和 GMV。成功支付的商品折后金额加运费应与扣款完全一致；退款另由退款表记录，不写负支付重复冲减。

| 字段 | 逻辑类型与单位 | 可空及含义 |
|---|---|---|
| `payment_id` | TEXT，PK | 非空；支付尝试标识 |
| `order_id` | TEXT，FK | 非空；指向订单 |
| `attempted_at` | UTC_TIMESTAMP | 非空；尝试时间 |
| `captured_at` | UTC_TIMESTAMP | 可空；成功扣款时间；失败为空 |
| `capture_recorded_at` | UTC_TIMESTAMP | 可空；成功扣款首次可知时间；失败为空 |
| `status` | TEXT，状态枚举 | 非空；failed/captured |
| `method` | TEXT | 非空；card、paypal 或 bank_debit |
| `attempted_gross_cents` | INTEGER，含税欧分 | 非空；本次尝试收取的订单金额 |
| `captured_gross_cents` | INTEGER，含税欧分 | 非空；成功扣款金额；失败为 0 |
| `payment_fee_ex_vat_cents` | INTEGER，不含税欧分 | 非空；成功扣款产生的支付费用；失败为 0 |
| `provider_reference` | TEXT | 非空；支付服务参考号，不是顾客身份 |
| `recorded_at` | UTC_TIMESTAMP | 非空；支付尝试首次可知时间 |

### 3.7 `procurements`：采购与收货批次

一个采购参考号可以包含多个单 SKU 批次，不能把它当唯一主键。状态为 `ordered`（已下达、未到仓）、`arrived`（已到仓、未验收上架）、`accepted`（已验收）。只有验收上架数量进入库存；采购资产支出不直接作为当期商品销售成本。

| 字段 | 逻辑类型与单位 | 可空及含义 |
|---|---|---|
| `procurement_id` | TEXT，PK | 非空；单 SKU 收货批次标识 |
| `purchase_reference` | TEXT，非唯一 | 非空；采购参考号 |
| `sku_id` | TEXT，FK | 非空；采购商品 |
| `warehouse_id` | TEXT | 非空；收货仓库 |
| `supplier_code` | TEXT | 非空；供应商代码 |
| `ordered_at` | UTC_TIMESTAMP | 非空；采购下达时间，可早于本包新订单窗口 |
| `expected_receipt_at` | UTC_TIMESTAMP | 非空；原计划收货时间，不以实际到货覆盖 |
| `actual_arrival_at` | UTC_TIMESTAMP | 可空；未观察到到仓时为空 |
| `arrival_recorded_at` | UTC_TIMESTAMP | 可空；实际到仓首次可知时间 |
| `accepted_at` | UTC_TIMESTAMP | 可空；未验收上架时为空 |
| `acceptance_recorded_at` | UTC_TIMESTAMP | 可空；验收首次可知时间 |
| `ordered_qty` | INTEGER，销售单位 | 非空正整数；采购计划量 |
| `accepted_qty` | INTEGER，销售单位 | 非空；已验收合格量，尚未验收时为 0，不大于计划量 |
| `unit_cost_ex_vat_cents` | INTEGER，不含税欧分/销售单位 | 非空；与商品固定标准到仓单位成本一致 |
| `status_as_of` | TEXT，状态枚举 | 非空；ordered/arrived/accepted |
| `recorded_at` | UTC_TIMESTAMP | 非空；采购批次首次可知时间 |

### 3.8 `inventory_movements`：库存事件账

按 `occurred_at, event_seq` 处理事件，按 SKU 与仓库分别累计。`event_seq` 是同时间业务排序依据，不可用 CSV 行顺序代替。`on_hand` 是可进入销售库存的在库量，`reserved` 为订单预占，可售量为二者之差。

| 字段 | 逻辑类型与单位 | 可空及含义 |
|---|---|---|
| `movement_id` | TEXT，PK | 非空；库存移动标识 |
| `event_seq` | INTEGER | 非空；有序事件序号，用于稳定处理同时间事件 |
| `sku_id` | TEXT，FK | 非空；商品 |
| `warehouse_id` | TEXT | 非空；仓库 |
| `movement_type` | TEXT，事件枚举 | 非空；见下表 |
| `occurred_at` | UTC_TIMESTAMP | 非空；库存动作生效时间 |
| `recorded_at` | UTC_TIMESTAMP | 非空；库存动作首次可知时间 |
| `on_hand_delta` | INTEGER，销售单位 | 非空；实物在库增减，可正、负或 0 |
| `reserved_delta` | INTEGER，销售单位 | 非空；订单预占增减，可正、负或 0 |
| `order_item_id` | TEXT，可空 FK | reserve/release/ship 必填；其他不适用时为空 |
| `procurement_id` | TEXT，可空 FK | receipt 必填；其他不适用时为空 |
| `refund_line_id` | TEXT，可空 FK | return_restock 必填；其他不适用时为空 |
| `shipment_id` | TEXT，可空 FK | ship 必填；其他不适用时为空 |

| `movement_type` | 在库变化 | 预占变化 | 解释 |
|---|---|---|---|
| `opening` | +期初数量 | 0 | 期初库存，不重复关联为当期采购或销售成本 |
| `receipt` | +验收合格量 | 0 | 采购验收后上架；在途和仅到仓不入账 |
| `reserve` | 0 | +购买量 | 创建订单时预占 |
| `release` | 0 | −预占量 | 取消或超时释放 |
| `ship` | −发货量 | −发货量 | 发货时同时扣在库和预占；支付不再扣一次 |
| `return_restock` | +验收可再售量 | 0 | 实际退回并验收可再售后入库 |

任一事件后必须满足 `on_hand >= reserved >= 0`。库存是时点余额，不可跨日期相加。日均库存应声明日终快照平均还是按实际持续时间加权；缺货时长需重建状态区间，不能只看日末是否为零。

### 3.9 `shipments`：履约与配送

一笔订单最多一个发货单，覆盖该订单全部明细，不做部分发货。状态为 `in_transit` 或 `delivered`。实际发货确认收入与销售成本；预计送达时间不随事后延迟修改。

| 字段 | 逻辑类型与单位 | 可空及含义 |
|---|---|---|
| `shipment_id` | TEXT，PK | 非空；发货单标识 |
| `order_id` | TEXT，唯一 FK | 非空；已成功支付、未取消的订单 |
| `warehouse_id` | TEXT | 非空；出库仓库 |
| `carrier_code` | TEXT | 非空；承运商代码 |
| `postal_prefix` | TEXT，邮区前两位 | 非空；订单目的邮区 |
| `shipped_at` | UTC_TIMESTAMP | 非空；交仓发货及收入/成本确认时间 |
| `promised_delivery_at` | UTC_TIMESTAMP | 非空；发货时已确定的承诺送达时间 |
| `delivered_at` | UTC_TIMESTAMP | 可空；尚未观察到签收时为空，不等于 0 配送时长 |
| `delivery_recorded_at` | UTC_TIMESTAMP | 可空；实际签收首次可知时间 |
| `status_as_of` | TEXT，状态枚举 | 非空；in_transit/delivered |
| `carrier_cost_ex_vat_cents` | INTEGER，不含税欧分 | 非空；该次发货承运费用，不是向顾客收取的运费 |
| `recorded_at` | UTC_TIMESTAMP | 非空；发货记录首次可知时间 |

### 3.10 `refunds`：退款与退货分摊

同一 `refund_id` 的多行属于同一申请；相同原商品行可以顺序发生多次部分退款。状态为 `requested`（已申请）、`approved`（已批准、未完成资金退款）、`completed`（资金退款完成）、`rejected`（已拒绝）。申请/批准不计已完成现金退款；退货验收可以早于或晚于资金退款完成。

`refund_kind` 取值：`pre_ship_cancel` 为已付未发货取消；`return_and_refund` 为退货退款；`refund_only` 为仅退款、不恢复商品库存或成本；`shipping_only` 为单独退还原已收顾客运费。仅商品退款按原实际成交商品件数计算，不做任意比例金额补偿。运费退款在同申请各商品行间分摊一次，不能每行重复整笔运费。

| 字段 | 逻辑类型与单位 | 可空及含义 |
|---|---|---|
| `refund_line_id` | TEXT，PK | 非空；退款分摊行标识 |
| `refund_id` | TEXT，非唯一分组键 | 非空；退款申请标识 |
| `order_item_id` | TEXT，FK | 非空；原订单商品行 |
| `payment_id` | TEXT，FK | 非空；原订单的成功收款记录 |
| `reason_code` | TEXT | 非空；业务记录的申请理由，不等于已证实的唯一原因 |
| `refund_kind` | TEXT，分支枚举 | 非空；上述四个分支之一 |
| `requested_at` | UTC_TIMESTAMP | 非空；申请时间 |
| `approved_at` | UTC_TIMESTAMP | 可空；未批准或已拒绝时为空 |
| `approval_recorded_at` | UTC_TIMESTAMP | 可空；批准首次可知时间 |
| `completed_at` | UTC_TIMESTAMP | 可空；尚未完成资金退款时为空 |
| `completion_recorded_at` | UTC_TIMESTAMP | 可空；资金完成首次可知时间 |
| `status_as_of` | TEXT，状态枚举 | 非空；requested/approved/completed/rejected |
| `refund_qty` | INTEGER，销售单位 | 非空；申请对应商品件数，shipping_only 为 0；本字段非零不代表资金已经退回 |
| `item_refund_gross_cents` | INTEGER，含税欧分 | 非空；本行拟退/已完成商品含税额；是否已支付须按 completed_at/status 判断 |
| `item_refund_vat_cents` | INTEGER，欧分 | 可空；未完成资金退款时为空；完成后为本次商品退税分摊 |
| `item_refund_ex_vat_cents` | INTEGER，不含税欧分 | 可空；未完成资金退款时为空；完成后为商品含税退款减税额 |
| `shipping_refund_gross_cents` | INTEGER，含税欧分 | 非空；本行分摊的拟退/已完成顾客运费；未退运费为 0，未完成不计现金退款 |
| `shipping_refund_vat_cents` | INTEGER，欧分 | 可空；未完成资金退款时为空；完成后为本次运费退税分摊，可为 0 |
| `shipping_refund_ex_vat_cents` | INTEGER，不含税欧分 | 可空；未完成资金退款时为空；完成后为运费含税退款减税额 |
| `returned_at` | UTC_TIMESTAMP | 可空；尚未观察到退回或不适用退货时为空 |
| `return_recorded_at` | UTC_TIMESTAMP | 可空；退回首次可知时间 |
| `inspected_at` | UTC_TIMESTAMP | 可空；尚未验收或不适用时为空 |
| `inspection_recorded_at` | UTC_TIMESTAMP | 可空；验收首次可知时间 |
| `returned_qty` | INTEGER，销售单位 | 非空；已实际退回数量，尚未发生或不适用为 0 |
| `restock_qty` | INTEGER，销售单位 | 非空；已验收可再售数量，尚未发生或不可再售为 0 |
| `recovered_cost_ex_vat_cents` | INTEGER，不含税欧分 | 非空；可再售数量×原订单单位成本；尚未验收或不恢复成本为 0 |
| `return_carrier_cost_ex_vat_cents` | INTEGER，不含税欧分 | 非空；已发生退运费用，尚未发生或不适用为 0 |
| `handling_cost_ex_vat_cents` | INTEGER，不含税欧分 | 非空；已发生售后处理费用，尚未发生或不适用为 0 |
| `return_cost_incurred_at` | UTC_TIMESTAMP | 可空；退运/处理费用确认时点，尚未发生或不适用为空 |
| `return_cost_recorded_at` | UTC_TIMESTAMP | 可空；退运/处理费用首次可知时间 |
| `recorded_at` | UTC_TIMESTAMP | 非空；申请首次可知时间 |

累计商品退款金额与件数不得超过原商品行实收金额与数量；累计顾客运费退款不得超过原订单运费。多次完成退款按原税额及累计完成退款比例分摊税额，末次全退恰好补齐原税额；拒绝申请不形成现金退款。累计退回与可再售数量均不得超过原销售数量，同一件不能恢复成本两次。

### 3.11 `campaigns`：广告计划版本

每个计划版本只对应一个目标 SKU、一个渠道和一种定位。定位或执行版本变更形成新 `campaign_id`，同系列通过 `campaign_family_id` 关联；同系列版本有效区间不可重叠。广告目标 SKU 与最终成交 SKU 可以不同，不能将跨品成交删掉。

| 字段 | 逻辑类型与单位 | 可空及含义 |
|---|---|---|
| `campaign_id` | TEXT，PK | 非空；不可变计划版本标识 |
| `campaign_family_id` | TEXT，非唯一 | 非空；同一投放系列的跨版本标识 |
| `target_sku_id` | TEXT，FK | 非空；广告目标商品，不强制等于最终购物篮商品 |
| `channel_code` | TEXT | 非空；paid_search_brand、paid_search_generic 或 paid_social |
| `targeting_type` | TEXT | 非空；brand、high_intent 或 broad 的定位类别 |
| `device_focus` | TEXT | 非空；投放设备范围代码，不等于每次实际访问设备 |
| `valid_from` | UTC_TIMESTAMP | 非空；版本生效时间 |
| `valid_to` | UTC_TIMESTAMP | 可空；版本结束，空表示尚未登记结束；区间左闭右开 |
| `action_recorded_at` | UTC_TIMESTAMP | 非空；该计划动作首次可知时间，不提前填入未来执行结果 |
| `currency` | TEXT | 非空；EUR |

### 3.12 `ad_daily`：广告平台日结

复合主键 `(campaign_id, stat_local_date)`；同一版本同一天至多一行。预算是费用上限，实际费用来自该日计费点击，不保证花完。平台日结不保存独立随机转化数、归因收入或 ROAS，这些须从交易与点击关联计算。本包只导出已经完整上报的广告日，不能把缺行当零活动。

| 字段 | 逻辑类型与单位 | 可空及含义 |
|---|---|---|
| `campaign_id` | TEXT，复合 PK、FK | 非空；指向 campaigns |
| `stat_local_date` | DATE，YYYY-MM-DD，复合 PK | 非空；Europe/Berlin 的广告投放经营日 |
| `budget_cap_ex_vat_cents` | INTEGER，不含税欧分 | 非空；该版本该日预算上限，不是已经发生的费用 |
| `budget_action_at` | UTC_TIMESTAMP | 非空；适用预算动作时间 |
| `impressions` | INTEGER，次 | 非空非负；平台实际曝光数 |
| `clicks` | INTEGER，次 | 非空非负；该版本当地经营日计费点击数，不大于曝光数 |
| `spend_ex_vat_cents` | INTEGER，不含税欧分 | 非空非负；对应计费点击费用之和，不超过适用预算 |
| `report_complete` | BOOLEAN | 非空；本包导出的平台日记录为已完整上报 |
| `recorded_at` | UTC_TIMESTAMP | 非空；完整日结首次可知时间，通常为该经营日结束后 |

### 3.13 `visit_events`：计费点击与站内行为

追加事件，不把“潜在想买”当成可见事实。点击与库存事件共用有序 `event_seq`，可用于重建行为发生前的可售库存。每个成功落地的付费点击只对应一个会话；未落地点击仍可能产生媒体费用。

| 字段 | 逻辑类型与单位 | 可空及含义 |
|---|---|---|
| `event_id` | TEXT，PK | 非空；点击或行为事件标识 |
| `event_seq` | INTEGER | 非空；全局有序事件序号，不以 CSV 行顺序替代 |
| `event_type` | TEXT，枚举 | 非空；ad_click、product_view、add_to_cart、checkout_attempt、inventory_blocked、substitute_click |
| `occurred_at` | UTC_TIMESTAMP | 非空；事件发生时间 |
| `recorded_at` | UTC_TIMESTAMP | 非空；事件首次可知时间 |
| `session_id` | TEXT，可空 FK | 站内事件必填并指向 sessions；未落地 ad_click 可空 |
| `visitor_id` | TEXT | 非空；可观察访客标识，不是隐含个人需求 |
| `campaign_id` | TEXT，可空 FK | 仅 ad_click 填对应投放计划；站内浏览等事件为空 |
| `sku_id` | TEXT，可空 FK | 适用事件的目标/浏览/尝试商品，指向 sku_catalog |
| `click_cost_ex_vat_cents` | INTEGER，不含税欧分 | 仅 ad_click 填计费成本；非点击为空，不混成零成本点击 |
| `displayed_unit_gross_cents` | INTEGER，含税欧分/销售单位 | 适用页面展示或结账事件填当时价格；不适用为空 |
| `requested_qty` | INTEGER，销售单位 | 适用加购/结账事件填请求数量；不适用为空 |
| `available_qty_observed` | INTEGER，销售单位 | 适用库存响应事件填当时可见可售量；未知/不适用为空；0表示实际不可售 |
| `failure_reason` | TEXT | 可空；有失败或不可售响应时的业务代码；空不等于成功成交 |

广告日点击数与费用须按 campaign 和点击当地日期从 `ad_click` 计数/加总；不因后续多次访问而重复计算同一点击。浏览、加购、结账尝试、库存阻断和替代点击是不同事件，事件次数不能当作独立顾客数或精确失单数。

### 3.14 `competitor_prices`：可比竞品观测

一行是一次观测，数据不是完整市场价格，也不是对方成交记录。价格只在观测和记录已知以后可使用；观察间隔内不允许用未来记录向过去插值。超过明确新鲜度阈值的旧观测应标明过期，本包默认使用 72 小时阈值。包装、规格和运费比较规则要一致。

| 字段 | 逻辑类型与单位 | 可空及含义 |
|---|---|---|
| `observation_id` | TEXT，PK | 非空；观测标识 |
| `competitor_code` | TEXT | 非空；虚构竞品代码 |
| `comparable_product_code` | TEXT | 非空；竞品可比商品代码 |
| `mapped_sku_id` | TEXT，FK | 非空；对应本店 SKU |
| `observed_at` | UTC_TIMESTAMP | 非空；观测或采集尝试发生时间 |
| `competitor_unit_gross_cents` | INTEGER，含税欧分/可比销售单位 | success 时为正价格；failed 时为空，不以 0 伪造免费商品 |
| `shipping_gross_cents` | INTEGER，含税欧分 | 可空；观察到的竞品运费，0表示明确免运，未知为空 |
| `in_stock_observed` | BOOLEAN | 可空；已观测有货/无货；failed 时为空，未知不等于缺货 |
| `comparability_grade` | TEXT | 非空；A为规格包装基本可比，B仅作辅助比较 |
| `collection_status` | TEXT，枚举 | 非空；success/failed；采集失败不代表竞品没有商品 |
| `recorded_at` | UTC_TIMESTAMP | 非空；本次观测首次可知时间 |

### 3.15 `customer_feedback`：投诉、客服处理与评价

一行是一条反馈事件，不是一笔订单，也不是一份去重投诉。`complaint_opened` 是客户咨询/投诉根事件，可包含配送查询、安装帮助及售后进度咨询，不全部等价于严重投诉；`complaint_replied` 是客服回复；`complaint_resolved` 是解决记录；`review_submitted` 是评价。客服回复与解决事件直接引用投诉根事件，继承相同订单、商品范围和问题分类；不能把跟进回复计作新投诉。

订单级事件的 `order_item_id` 为空，不强行分配给所有 SKU；商品级事件必须指向属于该订单的商品行。已发货但尚未送达的订单可以发生物流查询等投诉；商品或订单评价只可在实际签收后产生。未收到评价不等于 0 分。

| 字段 | 逻辑类型与单位 | 可空及含义 |
|---|---|---|
| `feedback_event_id` | TEXT，PK | 非空；反馈事件标识 |
| `parent_event_id` | TEXT，可空自引用 FK | 投诉根事件与独立评价为空；客服回复和解决记录必填，直接指向同订单同范围的 complaint_opened |
| `order_id` | TEXT，FK | 非空；指向业务订单，不按评论文本猜订单 |
| `order_item_id` | TEXT，可空 FK | 商品级必填，且商品行属于 order_id；订单级为空，不表示关联损坏 |
| `event_type` | TEXT，枚举 | 非空；complaint_opened、complaint_replied、complaint_resolved、review_submitted |
| `occurred_at` | UTC_TIMESTAMP | 非空；反馈、回复、解决或评价实际发生时间 |
| `recorded_at` | UTC_TIMESTAMP | 非空；事件首次可知时间，不早于 occurred_at；发生及记录均须早于本包观察截点 |
| `issue_code` | TEXT | 非空；delivery_query、installation_help、compatibility、product_condition、refund_query、order_experience 或 product_experience；为业务记录分类，不是已证实的唯一原因 |
| `rating` | INTEGER，1—5分 | 仅 review_submitted 填评分，其他事件为空；3分、中低分与高分均属于实际评价，不能把未评价填0 |
| `status_code` | TEXT，按类型枚举 | complaint_opened 对应 open；complaint_replied 对应 in_progress；complaint_resolved 对应 resolved；review_submitted 对应 published |
| `short_text` | TEXT | 非空；顾客反馈或客服处理简述，不作为账务金额或确定性因果证明 |

客服回复不早于根投诉，解决不早于相应处理过程；截至观察截点未解决的投诉保持未解决，不补造未来解决事件。业务主诉可以与配送、兼容性、退货等事实一起调查，但本表不创建新退款、不更新库存、不改变已有支付或履约事实。反馈与退款之间不存在“每次投诉必退款”或“每次退款必投诉”的一一对应关系。

## 4. 核心指标口径

所有日期过滤均先明确业务时区和区间。发生期与原订单 cohort 是两种不同归属方式，不能将它们混成一个“月销售额”。下表只给公式，不提供预先计算的结果。

| 指标 | 事实及计算 | 时间归属与纳入规则 | 税、运费、退款及边界 |
|---|---|---|---|
| 成功支付商品 GMV | 对唯一成功 captured 支付对应订单，合计 `order_items.line_paid_gross_cents` | 按 `payments.captured_at`；失败尝试、未付订单排除；后续退款不回写历史 GMV | 含税、折后、不含顾客运费、退款前；加该批订单顾客运费应等于成功扣款金额 |
| 成功支付订单量 | 成功 captured 对应的不同 `order_id` 数 | 按 capture 时间；每单一次 | 失败重试不增单；退款不删除历史成交单；SKU 订单数不可简单加总为全店订单数 |
| 商品客单价 | 同口径商品 GMV / 成功支付订单量 | 分子分母使用同一批支付订单和时间窗口 | 含税、折后、运费排除、退款前；零分母为 NULL |
| 发货商品收入 | 已发货订单商品 `line_ex_vat_cents` 合计 | 按 `shipments.shipped_at`，每单全量明细确认一次 | 不含税、折后、不含顾客运费、退款前；未发货收入为零，即使已经支付 |
| 完成现金退款 | completed 分摊行 `item_refund_gross_cents + shipping_refund_gross_cents` 合计 | 按 `refunds.completed_at`；申请、批准或拒绝不纳入 | 含税；商品与运费分开列示；包括发货前取消退款，但它不属于发货收入冲减 |
| 商品净销售额 `net_sales` | 发货商品不含税收入 − 已发货商品完成退款不含税额 | 发生期分别按发货时间、资金退款完成时间计入；也可按原订单 cohort 截至指定时点累计 | 不含税、不含顾客运费；发货前取消退款排除，不能用含税 GMV直接减含税现金退款代替 |
| 发货商品销售成本 COGS | 已发货商品行 `line_cost_ex_vat_cents` 合计 | 按 `shipped_at`；未付或未发货明细的成本快照不计入 | 不含税标准到仓成本；采购支出、广告、支付费及出站物流不在本项重复扣除 |
| 退货成本恢复 | 实际可再售验收的 `recovered_cost_ex_vat_cents` 合计 | 按 `refunds.inspected_at`；不要求与资金退款同日 | 仅 `return_and_refund` 的可再售数量恢复；仅退款、未退回、不可再售均不恢复 |
| 商品毛利润 | 商品净销售额 − 发货 COGS + 退货成本恢复 | 各分项按自身发生时点进入期间；另可按原订单 cohort 截至某时点重算 | 不含税、不含顾客运费与广告/支付/出站物流等费用；跨期退款可能产生负的当期毛利 |
| 商品毛利率 | 商品毛利润 / 商品净销售额 | 分子分母使用相同范围 | 净销售额 ≤ 0 时返回 NULL并说明金额；不平均 SKU/每日毛利率，也不将异常比率截断为 0—100% |
| 现金净流入 | 成功 capture 含税额 − 已完成含税现金退款 | 分别按 capture 和退款完成日期进入发生期 | 包含顾客运费与税，不等于商品净销售额、现金结算余额或企业利润 |
| 可售库存 | 各 SKU/仓截至时点累计 `on_hand_delta` − 累计 `reserved_delta` | 按 `occurred_at,event_seq` 重建状态，限定可知时间；日终余额是时点值 | 在途采购、未验收退货、不可再售退货不入可售；支付不再次扣库存；不能跨天求和 |
| 访问量 | 排除内部流量后的不同 `session_id` 数 | 按 `sessions.started_at` 当地经营日 | 不是不同访客数、点击数、浏览事件数或订单数；付费落地必须关联实际点击 |
| 会话购买转化率 | 同一有效会话集合中，经 `checkout_session_id` 形成成功支付订单的会话数 / 有效访问会话数 | 按会话开始日 cohort；截至截点的成功支付；会话结束和其订单支付有效期需足够观察 | 同一会话计一次；后续退款不抹除转化；不使用广告归因窗，不以当日订单数÷当日访问冒充会话 cohort 转化 |

### 4.1 广告归因、ROAS 与同口径费用

- **广告消耗**为 `ad_daily.spend_ex_vat_cents`，按实际投放当地日统计，不含税。应等于相同 campaign、相同当地点击日的 `ad_click.click_cost_ex_vat_cents` 之和；未落地点击仍纳入媒体费。
- **默认成交 ROAS** = 同一点击日期 cohort 归因订单的商品折后不含税金额 / 该点击 cohort 的不含税广告消耗。分子退款前、不含顾客运费；同一订单全部商品只归一个计划。分母为零则返回 NULL。含税商品成交 ROAS可另报，但须明确名称，不与默认口径混用。
- **有效归因**要求同一可连接 visitor，`click.occurred_at <= orders.ordered_at`，且 `capture - click <= 7×24小时`；成功 capture 时在合格点击中选最后一次。订单创建之后、支付之前的新点击也不能抢占该订单归因；点击可能来自此前会话。
- **自然回访与归因**分开：订单可在自然来源 checkout 会话成交，仍归给此前合格付费点击；会话转化归 checkout_session，广告成交归 attributed_click，两者不能混名或重复拿整单收入。
- **退款后 ROAS**需对原归因订单扣除截至 as_of 已完成的商品不含税退款，仍除以原点击 cohort 的费用；发货前已付取消的商品退款也应扣除。退款发生月份不直接决定广告收入归属，不将运费退款当商品退款。
- **跨期与成熟度**：支付日归因收入可以单独报告，但不能除以同一天费用冒充点击 cohort ROAS。正常点击 cohort 至少需要完整七日购买机会；经营窗口停止后的尾窗只处理既有订单，不能补足新购买机会，因此营业结束前七日点击仍受右截断影响，应单列并披露限制。
- **预算对比**应按相同长度、相同计划覆盖的日预算比较；预算增加不保证消耗或成交同比例增加。CTR=点击/曝光，CPC=消耗/点击，点击落地率=对应付费会话/点击；三个比例分别处理零分母，不平均日比率代替汇总重算。

### 4.2 收入、退款与经营贡献的边界

退款还可以按原订单成功支付月份 cohort 统计：先将每笔完成退款关联原支付与商品行，再在指定 `as_of` 观察其累计结果。退款完成发生期和原订单 cohort 应分别命名；不能用“本期现金退款 / 本期 GMV”冒充原订单最终退款率。

可另算不含税净顾客运费收入：发货确认的 `shipping_ex_vat_cents` 减已发货订单完成的 `shipping_refund_ex_vat_cents`。若计算店铺直接经营贡献，在商品毛利基础上加入净顾客运费收入，扣支付费用、承运费用及已发生退运/处理费；另扣相同期间的广告不含税消耗，方得到本包口径的店铺直接经营贡献；仍不包含固定工资、租金等费用，不是企业净利润。没有明确管理分摊时，不伪造 SKU/订单精确费用贡献。

### 4.3 投诉、客服处理与评价指标

| 指标 | 计算与事实粒度 | 时间、分母与边界 |
|---|---|---|
| 客户咨询/投诉根数量 | 只计 complaint_opened 根事件，或按其 feedback_event_id 去重；其中包含一般咨询 | 按根投诉发生日；reply/resolved 不增加新投诉数量；一个订单可以有多份不同投诉 |
| 客户咨询/投诉订单数量及比例 | 有根投诉的不同订单数；除以同一原发货 cohort 的不同已发货订单数 | 明确 as_of 及原订单/发货范围；尚未送达订单可投诉，不仅保留已送达订单；事件数不等于投诉订单数 |
| 首次客服回复时长 | 对每个根投诉取首次 complaint_replied.occurred_at 减根事件发生时间 | 根投诉 cohort，报告已回复样本及未回复数量；未回复不填0时长 |
| 投诉解决率及解决时长 | 有 complaint_resolved 的根投诉数/根投诉数；解决时间减根投诉时间 | 同一根投诉 cohort、同一 as_of；只统计已发生且已知的解决；仍开放的投诉另列，不伪造完整周期 |
| 评价数、均分和分布 | 只取 review_submitted；评分范围1—5；先明确订单级还是商品级 | 按评价发生日或同一原订单/发货 cohort 分别命名；不把客服事件、缺失评分或未评价订单混入均分 |
| 订单评价覆盖率 | 同一已送达订单 cohort 中有订单级评价的不同订单数/已送达不同订单数 | 商品级评价另列覆盖，不能将多商品评价直接加成多份订单级评价；说明观察长度与未评价数量 |

同一订单的多条投诉、回复、解决和评价直接连接订单商品、支付或退款后，会复制原业务金额。先分别计算并聚合到订单、商品行或根投诉粒度再连接；例如按投诉与无投诉分组比较销售额，应先构造互斥的不同订单集合，而不是按反馈事件行加总订单金额。订单级评分不能平均分配给每个商品后宣称得到商品真实评分。

本包的物流与反馈是观察数据。可以核对延迟发货群组的投诉和评价分布，但不能只凭低分文本认定某承运商或某种商品导致全部退款。比较正常与迟到订单时，应同时给出样本数、未送达情况、反馈覆盖和可比观察窗口。

## 5. 对账与使用边界

1. **商品与订单：** 原价金额减优惠金额等于商品折后含税额；每行含税 = 不含税 + 税；订单头等于商品行合计。
2. **订单与支付：** 每单成功 capture 至多一次；成功扣款 = 商品折后含税合计 + 顾客含税运费。支付尝试行数不是订单数。
3. **采购与库存：** 收货验收才入库，数量与采购 `accepted_qty` 一致；期初是资产余额，不在期初月份一次性计为 COGS。
4. **订单与库存：** 下单预占，超时/取消释放；发货同时减少实物在库和预占。库存必须逐事件验证 `on_hand >= reserved >= 0`。
5. **退款与验收：** 资金退款按 completed 事实，成本恢复按 inspected 事实。不能以 `status_as_of='completed'` 作为成本恢复的必要条件，也不能以资金完成自动补库。
6. **跨表连接：** 先将支付、发货、商品行和退款聚合到目标粒度再连接；订单总金额不能在每个商品行上重复求和。商品行费用也不能因多退款分摊重复累计。
7. **截点：** 事实必须在截止时点前已发生且已知。使用期末状态不能反推更早日期状态；若没有相应历史截面，应说明不可直接还原的字段。
8. **零与未知：** 不适用金额/数量可用明确定义的 0；未来完成时间及未完成退款税额为空。空时间不能填零时长；零分母比率返回 NULL。
9. **解释边界：** 退款理由是业务陈述；物流延迟与退款关联不自动证明单一因果。可观察广告归因、竞品相对价格及库存约束，但它们不等于随机实验或未成交反事实；证据不足时应停止确定性因果结论。

当问题要求按SKU细分直接经营贡献时，支付费、顾客运费收入及退款、承运费及共用售后费用按原商品行折后不含税金额比例以最大余数法分摊，欧分余数并列按order_item_id排序；分摊合计必须还原原费用。广告费归计划目标SKU，商品净销售和成本仍归实际购买SKU，报告注明这是管理分摊，不是增量因果贡献。按渠道时，订单及其退款/履约费用采用唯一广告归因渠道，无广告归因则采用checkout会话来源；媒体费仍按自身计划渠道。不可把同一广告费重复分配给目标SKU和购买SKU。

反馈查询不得改变原业务金额；任何投诉/评价分组汇总应可还原到去重订单集合，并与相同口径的原交易事实核对。

本字典定义数据结构和可核对业务口径。所有具体经营结果应由当前可见事实计算，并在回答中注明时间、金额单位、税费、退款与观察截止范围。


## 6. 本快照的可知范围

本包标识为 `asof_2026_08_31`，排他观察截点为 `2026-08-31T22:00:00Z`。只包含在截点之前已发生且已知的业务事实；后续字段分别按自己的信息可得时间处理。合同性质的支付到期、采购预计到货和承诺送达时间可以晚于截点，它们是当时已知计划，不是未来完成结果。

订单、采购、退款和履约状态已经重建为该截点状态，不能拿终态解释更早时点。未完成退款的实际退税和不含税退款金额为空；尚未验收的可再售数量和恢复成本为零；退款完成与退货验收互不替代。投诉根事件仍为历史open，当前是否解决须查看截点内的回复和解决事件。

广告日记录在次日当地午夜才上报；处于观察截止当刻或之后的上报不在本包内。因此最后一个经营日可能只有已发生点击和对应费用，而没有完整ad_daily记录。请使用元数据中的广告上报覆盖字段，不能把缺失报表当作零费用或从未来报表补齐。

运行时只提供当前快照目录；不能访问其他时点快照或开发私有重建与答案资料。本包的文件哈希只描述当前可见文件，不包含终态数据的哈希或评测结果。
