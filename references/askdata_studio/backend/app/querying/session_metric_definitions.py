"""Application-owned category cohort; published SKU definitions remain intact."""
from copy import deepcopy


def extend_session_metrics(original, database):
    metrics = deepcopy(original)
    if database != 'homenova_20260831' or any(m['id'] == 'category_view_session_cvr' for m in metrics):
        return metrics
    base = next((m for m in metrics if m['id'] == 'sku_view_session_cvr'), None)
    if not base:
        return metrics
    metric = deepcopy(base)
    identity = 'category_view_session_cvr'
    definition = ('按sessions.started_at的Europe/Berlin经营期间，排内部流量；'
        '分母是有product_view浏览该品类任一SKU的不同会话，分子是其中同checkout_session成功支付购买该品类任一SKU的不同会话。'
        '同会话跨SKU、重复浏览和重复支付行均去重；允许所购SKU与所浏览SKU不同，但必须同品类；'
        '分母不要求下单。不同品类的会话数不可直接相加。')
    metric.update(id=identity, label='品类浏览会话购买率', aliases=['品类浏览到购买转化率'],
                  definition=definition, formula=definition, depends_on=[], version='app-session-cohort-v1',
                  statistical_entity='category_view_session_cohort', grain='品类×访问会话cohort',
                  duplicate_risks=['同会话同品类多SKU浏览或购买只计一次；跨品类会话数不可相加。'],
                  application_definition=True)
    prefix = 'hn08_'
    metric['required_fields'] += [{'table': 'sku_catalog', 'table_id': prefix + 'sku_catalog',
                                  'field': field, 'field_id': prefix + 'sku_catalog.' + field}
                                 for field in ('sku_id', 'category_code')]
    metric['fact_tables'] = sorted(set(metric['fact_tables']) | {'sku_catalog'})
    metric['component_policies'] = [{key: deepcopy(metric[key]) for key in
        ('definition', 'required_predicates', 'time_fields', 'maturity')} | {'metric_id': identity}]
    metrics.append(metric)
    return metrics
