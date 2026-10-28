"""Hand-written read-only SQL probes: local validator and CSV DuckDB only.

No model, Agent endpoint, retrieval service, private answer, or network is used.
Rejected candidates are never executed. Legal accepted equivalents are compared
with one another; negative probes only exercise validation.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / 'data/homenova_harness_v1/private/sql_probe_v5'
DATABASE = 'homenova_20260831'
sys.path.insert(0, str(ROOT / 'references/askdata_studio/backend'))


def deny_network(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'subprocess.Popen'}:
        raise RuntimeError('SQL probes forbid network and external processes')


sys.addaudithook(deny_network)

from app.homenova import schema_payload
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.query_contract import QueryContractValidator
from app.security import AccessController


START = '2026-06-01T00:00:00+02:00'
END = '2026-08-01T00:00:00+02:00'
JOIN = 'FROM payments p JOIN order_items oi ON p.order_id=oi.order_id'
WHERE = f"p.status='captured' AND p.captured_at>='{START}' AND p.captured_at<'{END}'"


def gmv():
    return dict(name='paid_goods_gmv_gross', source_fields=['order_items.line_paid_gross_cents'], aggregation='sum')


def quantity():
    return dict(name='quantity', source_fields=['order_items.quantity'], aggregation='sum')


def contract(*, metrics=None, dimensions=None, grain='overall', tables=None, filters=None, period=True):
    return dict(original_question='核对成功支付商品交易指标', data_source=DATABASE,
                tables=tables or ['payments', 'order_items'], metrics=metrics or [gmv()],
                dimensions=dimensions or [], filters=filters if filters is not None else [dict(field='payments.status', operator='=', value='captured')],
                time_range=dict(field='payments.captured_at', start=START, end=END) if period else None,
                grain=grain, assumptions=[], open_questions=[])


def probes():
    result = []

    def add(name, sql, spec=None, group=None, negative=False, reason=''):
        result.append(dict(name=name, sql=sql, contract=spec or contract(), equivalence_group=group,
                           expected='rejected' if negative else 'accepted', intent=reason))

    direct = f'SELECT SUM(oi.line_paid_gross_cents) AS total {JOIN} WHERE {WHERE}'
    add('total_direct', direct, group='total')
    add('total_reworded_chinese_alias', direct.replace('AS total', 'AS "金额合计"'),
        contract(metrics=[dict(name='商品GMV', definition='同义描述', source_fields=['order_items.line_paid_gross_cents'], aggregation='sum')]),
        group='total_value', reason='注册指标同义名称与显示列名不影响数值')
    add('total_cte_renamed_measure', f'WITH base AS (SELECT oi.line_paid_gross_cents AS v {JOIN} WHERE {WHERE}) SELECT SUM(v) AS total FROM base', group='total')
    add('total_cte_column_alias', f'WITH base(v) AS (SELECT oi.line_paid_gross_cents {JOIN} WHERE {WHERE}) SELECT SUM(v) AS total FROM base', group='total')
    add('total_cte_star_transmission', f'WITH base AS (SELECT oi.line_paid_gross_cents AS v {JOIN} WHERE {WHERE}), carry AS (SELECT * FROM base) SELECT SUM(v) AS total FROM carry', group='total')
    add('total_nested_cte_star_transmission', f'WITH base AS (SELECT oi.line_paid_gross_cents AS v {JOIN} WHERE {WHERE}), carry AS (SELECT * FROM (SELECT * FROM base) b) SELECT SUM(v) AS total FROM carry', group='total')
    add('total_double_aggregate', f'WITH base AS (SELECT oi.sku_id, SUM(oi.line_paid_gross_cents) AS v {JOIN} WHERE {WHERE} GROUP BY oi.sku_id) SELECT SUM(v) AS total FROM base', group='total')
    add('total_uppercase_identifiers', direct.upper().replace("'CAPTURED'", "'captured'"), group='total')
    add('total_typed_timestamp', direct.replace(f"'{START}'", f"TIMESTAMPTZ '{START}'").replace(f"'{END}'", f"TIMESTAMPTZ '{END}'"), group='total')
    add('total_reversed_time_comparison', f"SELECT SUM(oi.line_paid_gross_cents) AS total {JOIN} WHERE p.status='captured' AND '{START}'<=p.captured_at AND '{END}'>p.captured_at", group='total', reason='反向书写的同一半开时间边界')
    add('total_single_value_in_filter', direct.replace("p.status='captured'", "p.status IN ('captured')"), group='total', reason='单值IN与等号等价，契约仍声明等号')
    add('total_negated_status_inequality', direct.replace("p.status='captured'", "NOT (p.status <> 'captured')"), group='total', reason='NOT不等于与等号等价')
    add('total_date_cast_inclusive_end', f"SELECT SUM(oi.line_paid_gross_cents) AS total {JOIN} WHERE p.status='captured' AND CAST(p.captured_at AS DATE) BETWEEN DATE '2026-06-01' AND DATE '2026-07-31'", group='total', reason='柏林经营日CAST日期的闭区间与原始时间半开区间等价')

    monthly = contract(dimensions=['payments.captured_at'], grain='month')
    add('monthly_group_by', f"SELECT strftime(p.captured_at, '%Y-%m') AS period, SUM(oi.line_paid_gross_cents) AS amount {JOIN} WHERE {WHERE} GROUP BY 1 ORDER BY 1", monthly, group='monthly')
    add('monthly_window_distinct', f"SELECT DISTINCT strftime(p.captured_at, '%Y-%m') AS period, SUM(oi.line_paid_gross_cents) OVER (PARTITION BY strftime(p.captured_at, '%Y-%m')) AS amount {JOIN} WHERE {WHERE} ORDER BY 1", monthly, group='monthly', reason='窗口聚合加DISTINCT，结果仍每月一行')
    add('monthly_union_aggregates',
        f"SELECT '2026-06' AS period, SUM(oi.line_paid_gross_cents) AS amount {JOIN} WHERE {WHERE} AND p.captured_at<'2026-07-01T00:00:00+02:00' UNION ALL SELECT '2026-07' AS period, SUM(oi.line_paid_gross_cents) AS amount {JOIN} WHERE {WHERE} AND p.captured_at>='2026-07-01T00:00:00+02:00' ORDER BY 1",
        monthly, group='monthly', reason='每期独立聚合后UNION ALL，合法每月一行而不需要GROUP BY')

    pivot = f"SELECT SUM(CASE WHEN p.captured_at<'2026-07-01T00:00:00+02:00' THEN oi.line_paid_gross_cents ELSE 0 END) AS june, SUM(CASE WHEN p.captured_at>='2026-07-01T00:00:00+02:00' THEN oi.line_paid_gross_cents ELSE 0 END) AS july {JOIN} WHERE {WHERE}"
    add('pivot_case_sum', pivot, group='pivot')
    add('pivot_filter_sum', f"SELECT SUM(oi.line_paid_gross_cents) FILTER (WHERE p.captured_at<'2026-07-01T00:00:00+02:00') AS june, SUM(oi.line_paid_gross_cents) FILTER (WHERE p.captured_at>='2026-07-01T00:00:00+02:00') AS july {JOIN} WHERE {WHERE}", group='pivot')
    add('pivot_if_sum', f"SELECT SUM(IF(p.captured_at<'2026-07-01T00:00:00+02:00', oi.line_paid_gross_cents, 0)) AS june, SUM(IF(p.captured_at>='2026-07-01T00:00:00+02:00', oi.line_paid_gross_cents, 0)) AS july {JOIN} WHERE {WHERE}", group='pivot')

    count_spec = contract(metrics=[dict(name='paid_order_count', source_fields=['payments.order_id'], aggregation='count_distinct')], tables=['payments'])
    count_where = WHERE.replace('p.', '')
    add('count_direct_distinct', f'SELECT COUNT(DISTINCT order_id) AS n FROM payments WHERE {count_where}', count_spec, group='count')
    add('count_pre_deduplicate_sum', f'WITH paid AS (SELECT DISTINCT order_id FROM payments WHERE {count_where}) SELECT SUM(1) AS n FROM paid', count_spec, group='count')
    add('count_pre_deduplicate_count_star', f'WITH paid AS (SELECT DISTINCT order_id FROM payments WHERE {count_where}) SELECT COUNT(*) AS n FROM paid', count_spec, group='count')

    sku_spec = contract(metrics=[gmv(), quantity()], dimensions=['order_items.sku_id'], grain='dimension')
    sku = f'SELECT oi.sku_id, SUM(oi.line_paid_gross_cents) AS amount, SUM(oi.quantity) AS qty {JOIN} WHERE {WHERE} GROUP BY oi.sku_id'
    add('sku_direct_group', sku+' ORDER BY 1', sku_spec, group='sku')
    add('sku_positive_having', sku+' HAVING SUM(oi.quantity)>0 ORDER BY 1', sku_spec, group='sku', reason='数量非空正整数，所有成功支付SKU组总件数为正')
    add('sku_post_aggregate_filter', f'WITH totals AS ({sku}) SELECT sku_id, amount, qty FROM totals WHERE qty>0 ORDER BY 1', sku_spec, group='sku')
    add('sku_multi_cte_and_column_aliases', f'WITH totals(k, amount, qty) AS ({sku}), carried AS (SELECT * FROM totals) SELECT k AS sku_id, amount, qty FROM carried ORDER BY 1', sku_spec, group='sku')

    add('negative_wrong_amount_source', direct.replace('oi.line_paid_gross_cents', 'oi.quantity'), contract(metrics=[dict(name='paid_goods_gmv_gross', source_fields=['order_items.quantity'], aggregation='sum')]), negative=True)
    add('negative_wrong_aggregation', direct.replace('SUM(', 'AVG('), negative=True)
    add('negative_missing_payment_filter', direct.replace("p.status='captured' AND ", ''), negative=True)
    add('negative_wrong_start_hour', direct.replace('2026-06-01T00:00:00+02:00', '2026-06-01T01:00:00+02:00'), negative=True)
    add('negative_cross_snapshot', direct.replace('FROM payments', 'FROM homenova_20261015.payments'), negative=True)
    add('negative_write', 'DELETE FROM payments', count_spec, negative=True)
    add('negative_disjunctive_payment_scope', direct.replace("p.status='captured'", "(p.status='captured' OR p.status='failed')"), negative=True,
        reason='契约只纳入成功支付，SQL显式扩大为成功或失败，不能因包含captured子条件就通过')
    add('negative_time_lower_bound_or_true', direct.replace(f"p.captured_at>='{START}'", f"(p.captured_at>='{START}' OR 1=1)"), negative=True,
        reason='时间下界出现在永真OR分支中，不能证明落实了下界')
    add('negative_inclusive_end_for_exclusive_contract', direct.replace(f"p.captured_at<'{END}'", f"p.captured_at<='{END}'"), negative=True,
        reason='声明半开结束时间但SQL包含结束瞬间，不是同一边界')
    return result


def normalized_rows(rows, values_only=False):
    def normalize(value):
        return round(value, 8) if isinstance(value, float) else value
    if values_only:
        return sorted(json.dumps([normalize(v) for v in row.values()], ensure_ascii=False, default=str) for row in rows)
    return sorted(json.dumps({k.casefold(): normalize(v) for k,v in row.items()}, sort_keys=True, ensure_ascii=False, default=str) for row in rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DIRECTORY/'probes.json')
    args = parser.parse_args()
    output = args.output.resolve()
    if DIRECTORY.resolve() not in output.parents:
        parser.error('Output must stay in the new sql_probe_v5 development directory')
    if output.exists():
        parser.error('Use a fresh output filename; never overwrite probe history')
    engine = DuckDbEngine(); validator = QueryContractValidator(engine)
    graph = schema_payload(DATABASE); scope = AccessController().scoped('demo_admin', DATABASE)
    source_paths = [ROOT/'references/askdata_studio/backend/app/querying/query_contract.py', ROOT/'references/askdata_studio/backend/app/querying/duckdb_engine.py']
    fingerprint = lambda: {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    before = fingerprint(); cases = []
    for probe in probes():
        started = time.monotonic(); record = copy.deepcopy(probe)
        try:
            validated = validator.validate(probe['contract'], probe['sql'], probe['contract']['original_question'], DATABASE, graph, scope.public())
        except (ValueError, KeyError, TypeError) as exc:
            record.update(validation_passed=False, validation_error=str(exc), executed=False,
                          status='rejected_as_expected' if probe['expected']=='rejected' else 'unexpected_rejection')
            # Compile only to distinguish a contract rejection from illegal SQL.
            # Never fetch or execute data after the full validator rejected it.
            record['engine_preflight_error'] = engine.preflight(DATABASE, probe['sql'], scope)
        else:
            record.update(validation_passed=True, validated_contract=validated,
                          pending_checks=validated['validation']['pending_checks'], status='accepted')
            if probe['expected']=='rejected':
                record.update(status='unexpected_acceptance', executed=False)
            else:
                execution = engine.execute(DATABASE, probe['sql'], scope)
                record.update(executed=True, execution=asdict(execution))
                if not execution.success:
                    record['status']='execution_failed'
        record['elapsed_seconds']=round(time.monotonic()-started,4)
        cases.append(record)
    groups = {}
    for case in cases:
        group = case.get('equivalence_group')
        if group and case.get('execution',{}).get('success'):
            groups.setdefault(group,[]).append(case)
    comparisons=[]
    for group, members in groups.items():
        baseline=members[0]
        for candidate in members[1:]:
            comparisons.append(dict(group=group, baseline=baseline['name'], candidate=candidate['name'],
                                    equal=normalized_rows(baseline['execution']['rows'])==normalized_rows(candidate['execution']['rows'])))
    # Different display label, same scalar value.
    direct = next(c for c in cases if c['name']=='total_direct')
    display = next(c for c in cases if c['name']=='total_reworded_chinese_alias')
    if direct.get('execution',{}).get('success') and display.get('execution',{}).get('success'):
        comparisons.append(dict(group='total_value',baseline=direct['name'],candidate=display['name'],
                                equal=normalized_rows(direct['execution']['rows'],True)==normalized_rows(display['execution']['rows'],True)))
    after=fingerprint()
    summary={status:sum(c['status']==status for c in cases) for status in sorted({c['status'] for c in cases})}
    payload=dict(mode='hand-written local SQL probes, not live Agent acceptance',datasource_id=DATABASE,
                 created_at_utc=datetime.now(timezone.utc).isoformat(),new_paid_calls=0,live_model_calls=0,http_calls=0,
                 embedding_calls=0,rerank_calls=0,network_guard_installed=True,source_before=before,source_after=after,
                 source_changed_during_probe=before!=after,summary=summary,probe_count=len(cases),cases=cases,equivalence_comparisons=comparisons,
                 limits=['Rejected candidates never executed; engine preflight is compilation only',
                         'Equivalent accepted variants compared to one another, no private answers used',
                         'Legal SQL passing does not prove every requested business interpretation'])
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as handle:
        json.dump(payload,handle,ensure_ascii=False,indent=2,allow_nan=False);handle.write('\n')
    print(json.dumps(dict(output=str(output),probe_count=len(cases),summary=summary,
                          equal_comparisons=sum(c['equal'] for c in comparisons),comparison_count=len(comparisons),
                          new_paid_calls=0,source_changed_during_probe=before!=after),ensure_ascii=False))


if __name__=='__main__':main()
