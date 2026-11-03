"""Explicit application-owned definitions; never learned from query success.

The source public artifacts are immutable. These additions use their captured
order population and quantity field, and keep the original definitions intact.
"""
from copy import deepcopy


def extend_metrics(original):
    metrics = deepcopy(original)
    by_id = {m['id']: m for m in metrics}
    base = by_id.get('paid_goods_gmv_gross')
    if not base or 'paid_order_count' not in by_id:
        return metrics
    specs = (
        ('paid_goods_quantity', '成功支付商品件数', ['成交商品件数', '支付商品销量'],
         '同成功支付商品GMV的订单集合，SUM(order_items.quantity)；按payments.captured_at统计，退款不倒扣历史件数。',
         'units', [], ['order_items.quantity'], 'goods_units'),
        ('units_per_paid_order', '每支付订单商品件数', ['每单件数'],
         '相同范围和支付订单集合的paid_goods_quantity/paid_order_count，零分母为NULL。',
         'units_per_order', ['paid_goods_quantity', 'paid_order_count'], ['order_items.quantity', 'payments.order_id'], 'goods_units_per_order'),
        ('paid_unit_price_gross', '每件商品平均实付金额', ['平均每件实付金额', '成交商品均价'],
         '相同范围和支付订单集合的paid_goods_gmv_gross/paid_goods_quantity，零分母为NULL；这是成交结构加权均价，不代表标价或因果。',
         'EUR_cents_gross_per_unit', ['paid_goods_gmv_gross', 'paid_goods_quantity'], ['order_items.line_paid_gross_cents', 'order_items.quantity'], 'monetary_amount_per_goods_unit'),
    )
    common_fields = {'order_items.order_id', 'order_items.sku_id', 'order_items.recorded_at',
                     'payments.order_id', 'payments.status', 'payments.captured_at', 'payments.capture_recorded_at'}
    for identity, label, aliases, definition, unit, dependencies, sources, entity in specs:
        if identity in by_id:
            continue  # Never replace a published source definition.
        metric = {key: deepcopy(base[key]) for key in ('time_fields', 'required_predicates',
                  'maturity', 'as_of_policy', 'join_dependencies') if key in base}
        metric.update(id=identity, label=label, aliases=aliases, definition=definition,
            formula=definition, unit=unit, depends_on=dependencies, version='app-paid-goods-v1',
            statistical_entity=entity, grain='当前指定商品范围×Europe/Berlin经营期间',
            tax_policy='gross' if identity == 'paid_unit_price_gross' else 'not_applicable',
            shipping_policy='excluded', refund_policy='before_refunds',
            duplicate_risks=['每单仅一个成功capture；订单跨品类/SKU不可相加，比率不可直接相加或平均。'],
            fact_tables=['order_items', 'payments'],
            required_fields=[deepcopy(f) for f in base['required_fields']
                if f"{f['table']}.{f['field']}" in common_fields | set(sources)],
            component_policies=[])
        metric['component_policies'] = [{key: deepcopy(metric[key]) for key in
            ('definition', 'required_predicates', 'time_fields', 'maturity')} | {'metric_id': identity}]
        metrics.append(metric)
    return metrics
