"""State intervals over complete event ledgers, independent of model-written windows.

No query, generated code, business table names or expected business answers live
here. The caller supplies an authorized complete result and source cutoff.
"""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlglot
from sqlglot import exp

from .calculations import CalculationError, _number


def _fail(message, code='invalid_ledger', **details):
    raise CalculationError(message, code=code, details=details)


def _timestamp(value, zone):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            # Ambiguous/nonexistent local times require an explicit offset.
            a, b = parsed.replace(tzinfo=zone, fold=0), parsed.replace(tzinfo=zone, fold=1)
            if a.utcoffset() != b.utcoffset():
                _fail('夏令时切换处的本地时间须提供明确UTC偏移', value=str(value))
            parsed = a
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError) as exc:
        if isinstance(exc, CalculationError):
            raise
        _fail('时间字段必须是有效ISO日期或时间戳', value=str(value))


def _raw_ledger_columns(request, data):
    """A complete SQL result alone does not prove a complete event population.

    This specialized operator needs raw history for selected partitions. Keep
    ordinary SQL unrestricted by this check: only this calculation rejects a
    prefiltered/aggregated ledger and asks for its raw source instead.
    """
    try:
        trees = sqlglot.parse(data.get('sql') or '', read='duckdb')
    except sqlglot.errors.ParseError:
        _fail('无法确认台账来源；请先查询原始完整台账', code='ledger_source_not_raw')
    tree = trees[0] if len(trees) == 1 else None
    if isinstance(tree, exp.Select):
        # Normalize equivalent projection wrappers for proof only. The stored
        # SQL and executed rows stay immutable; this never rewrites a query.
        from sqlglot.optimizer.merge_subqueries import merge_subqueries
        from sqlglot.optimizer.eliminate_ctes import eliminate_ctes
        proof = tree.copy()
        graph = data.get('schema_graph') or {}
        if graph.get('tables') and graph.get('fields'):
            from sqlglot.optimizer.qualify import qualify
            names = {t['id']:t['name'] for t in graph['tables']}
            schema = {}
            for field in graph['fields']:
                table = field.get('table_name') or names.get(field.get('table_id'))
                if table and field.get('name'):
                    schema.setdefault(table,{})[field['name']] = field.get('sql_type') or field.get('data_type') or 'UNKNOWN'
            try:
                proof = qualify(proof, dialect='duckdb', schema=schema,
                    quote_identifiers=False, identify=False, validate_qualify_columns=True)
            except (sqlglot.errors.OptimizeError, sqlglot.errors.SchemaError):
                proof = tree.copy()  # Unknown lineage remains unproven below.
        tree = eliminate_ctes(merge_subqueries(proof))
    if (not isinstance(tree, exp.Select) or tree.args.get('with_')
            or any(tree.args.get(k) for k in ('distinct', 'group', 'having', 'qualify', 'limit', 'offset'))
            or tree.find(exp.TableSample)):
        _fail('state_duration需要可确认完整的原始台账，不能用分组、抽样或LIMIT后的片段；普通SQL仍可执行', code='ledger_source_not_raw')
    source = tree.args.get('from_')
    if not source or not isinstance(source.this, exp.Table):
        _fail('请直接读取原始台账表', code='ledger_source_not_raw')
    ledger = source.this
    alias = ledger.alias_or_name.casefold()
    graph = data.get('schema_graph') or {}
    table_names = {t.get('id'): t.get('name') for t in graph.get('tables', [])}
    raw_fields = {str(f.get('name') or str(f.get('sql_name') or '').rsplit('.', 1)[-1]).casefold()
                  for f in graph.get('fields', [])
                  if str(f.get('table_name') or table_names.get(f.get('table_id')) or '').casefold() == ledger.name.casefold()}
    bindings, dimension_bindings = {}, {}
    for projection in tree.expressions:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(node, exp.Star) or isinstance(node, exp.Column) and node.is_star:
            owner = node.table.casefold() if isinstance(node, exp.Column) else ''
            if tree.args.get('joins') and (owner != alias or not raw_fields):
                _fail('连接台账请明确选择原始台账列，不能用含义不明确的星号', code='ledger_source_not_raw')
            bindings.update({column: column for column in data['columns']
                             if not raw_fields or column.casefold() in raw_fields})
        elif isinstance(node, exp.Column) and (not node.table or node.table.casefold() == alias):
            if not raw_fields or node.name.casefold() in raw_fields:
                bindings[projection.alias_or_name] = node.name
        elif isinstance(node, exp.Column):
            dimension_bindings[projection.alias_or_name] = (node.table.casefold(), node.name.casefold())
        # State/time inputs must remain raw; possible dimension labels are
        # accepted only after the stable unique lookup proof below.
    needed = [request.time_field, request.order_field, request.opening_field,
              *(term.column for term in request.delta_terms)]
    missing = [name for name in needed if name not in data['columns'] or name not in bindings]
    if missing:
        _fail('台账缺少计算所需原始列；计算输入不能绑定汇总值或临时序号', code=('ledger_source_not_raw' if any(name in data['columns'] and name not in bindings for name in missing) else 'unknown_columns'), missing_columns=missing,
              available_columns=list(bindings))
    raw_partitions = [name for name in request.partition_by if name in bindings]
    partitions = {bindings[name].casefold() for name in raw_partitions}
    dimension_aliases = set()
    for join in tree.args.get('joins') or []:
        dimension = join.this
        if (not isinstance(dimension, exp.Table) or join.args.get('side') not in (None, '', 'LEFT')
                or join.args.get('kind') not in (None, '', 'INNER') or join.args.get('using')):
            _fail('台账仅能连接按分区键唯一匹配的维表', code='ledger_source_not_raw')
        definitions = [t for t in graph.get('tables', []) if str(t.get('name') or '').casefold() == dimension.name.casefold()]
        primary = {str(k).casefold() for k in definitions[0].get('primary_key', [])} if len(definitions) == 1 else set()
        dim_alias = dimension.alias_or_name.casefold()
        on = join.args.get('on')
        if not primary or on is None or dim_alias == alias or dim_alias in dimension_aliases:
            _fail('连接缺少已登记唯一键，无法证明不会复制台账事件', code='ledger_source_not_raw')
        matched = set()
        # Only conjunctions of partition-key equalities establish stable lookup
        # cardinality. No guessed uniqueness, event-time joins, or cross joins.
        for predicate in on.flatten() if isinstance(on, exp.And) else [on]:
            columns = list(predicate.find_all(exp.Column))
            if columns and all(c.table.casefold() == dim_alias for c in columns) and not predicate.find(exp.Func):
                continue  # A dimension-only filter is constant per partition.
            if not isinstance(predicate, exp.EQ) or not all(isinstance(v, exp.Column) for v in (predicate.left, predicate.right)):
                _fail('台账维表连接须为分区键等值连接', code='ledger_source_not_raw')
            left, right = predicate.left, predicate.right
            if left.table.casefold() == alias:
                left, right = right, left
            if left.table.casefold() != dim_alias or right.table.casefold() != alias or right.name.casefold() not in partitions:
                _fail('连接依赖台账事件而不是固定分区，可能截断历史', code='ledger_history_filtered')
            matched.add(left.name.casefold())
        if not primary.issubset(matched):
            _fail('连接未覆盖维表完整唯一键，可能复制事件', code='ledger_source_not_raw')
        dimension_aliases.add(dim_alias)
    # A display label can participate in grouping only after the lookup has
    # been proved unique and dependent on existing raw partition keys. State,
    # time, event order and opening inputs above remain raw ledger fields.
    labels = [name for name in request.partition_by if name not in bindings]
    known_dimension_fields = {
        (join.this.alias_or_name.casefold(), str(field.get('name') or '').casefold())
        for join in tree.args.get('joins') or []
        for field in graph.get('fields', [])
        if str(field.get('table_name') or table_names.get(field.get('table_id')) or '').casefold()
            == join.this.name.casefold()}
    missing = [name for name in labels if name not in data['columns']
               or dimension_bindings.get(name) not in known_dimension_fields]
    if missing or (labels and not raw_partitions):
        _fail('分区标签必须来自按原始分区键唯一连接的已知维表直接字段',
              code=('unknown_columns' if any(name not in data['columns'] for name in missing)
                    else 'ledger_source_not_raw'), missing_columns=missing)
    if labels:
        observed = {}
        for index, row in enumerate(data['rows']):
            key = tuple(row.get(name) for name in raw_partitions)
            values = tuple(row.get(name) for name in labels)
            if any(not isinstance(v, (str, int, float)) or isinstance(v, bool) for v in (*key, *values)):
                _fail('台账分区键及固定标签必须是非空标量', row=index)
            if key in observed and observed[key] != values:
                _fail('同一原始分区的维表标签发生变化，不能拆分事件历史',
                      code='ledger_partition_label_changed', partition=key, labels=labels)
            observed[key] = values
    where = tree.args.get('where')
    if where:
        # An upper bound at/after every requested interval end preserves all
        # history needed for those intervals. Lower bounds and event filters do
        # not: opening state would be lost. Only direct conjunctive upper bounds
        # are removed from this proof copy, never from executed/stored SQL.
        zone = ZoneInfo(request.timezone_name)
        report_end = max(_timestamp(p.end, zone) for p in request.periods)
        def prove_upper(node):
            if isinstance(node, (exp.Where, exp.Paren)):
                node.set('this', prove_upper(node.this))
            elif isinstance(node, exp.And):
                node.set('this', prove_upper(node.this)); node.set('expression', prove_upper(node.expression))
            elif isinstance(node, (exp.LT, exp.LTE)) and isinstance(node.this, exp.Column):
                column, value = node.this, node.expression
                while isinstance(value, (exp.Cast, exp.TryCast, exp.Paren)):
                    value = value.this
                if (column.name.casefold() == bindings[request.time_field].casefold()
                        and column.table.casefold() in ('', alias) and isinstance(value, exp.Literal)
                        and _timestamp(value.this, zone) >= report_end):
                    return exp.true()
            return node
        where = prove_upper(where.copy())
        partitions = {bindings[name].lower() for name in raw_partitions}
        outer_nodes = list(where.walk(prune=lambda node: isinstance(node, (exp.Select, exp.Subquery))))
        if (any(node.table.casefold() not in dimension_aliases and node.name.casefold() not in partitions
                    for node in outer_nodes if isinstance(node, exp.Column))
                or any(isinstance(node, exp.Func) and not isinstance(node, (exp.And, exp.Or)) for node in where.walk())):
            _fail('请保留所选对象的完整历史与全部正负变动；只按分区对象筛选，目标期间由state_duration切分',
                  code='ledger_history_filtered')
        # IN(SELECT partition_key FROM an entity catalog ...) selects whole
        # partitions without multiplying events. It must not depend on the
        # outer event's time/delta/order, including ambiguous unqualified names.
        protected = {bindings[name].lower() for name in
                     [request.time_field, request.order_field, request.opening_field,
                      *(term.column for term in request.delta_terms)]}
        outer_alias = source.this.alias_or_name.lower()
        for subquery in where.find_all(exp.Select):
            if any(column.name.lower() in protected or
                   (column.table.lower() == outer_alias and column.name.lower() not in partitions)
                   for column in subquery.find_all(exp.Column)):
                _fail('对象映射子查询不能按外层变动时间或状态逐行截断历史', code='ledger_history_filtered')


def _seconds(delta):
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1000000


def calculate_state_duration(request, data):
    _raw_ledger_columns(request, data)
    try:
        zone = ZoneInfo(request.timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        _fail('时间区必须为有效IANA时区', timezone=request.timezone_name)
    if not data.get('observation_cutoff_at'):
        _fail('缺少来源的可信观测截止时间，不能将最后状态无限延伸', code='missing_observation_boundary')
    cutoff = _timestamp(data['observation_cutoff_at'], zone)
    periods = [(p.label, _timestamp(p.start, zone), _timestamp(p.end, zone)) for p in request.periods]
    for label, start, end in periods:
        if start >= end or end > cutoff:
            _fail('期间必须为有效左闭右开区间且不超过来源观测截止', code='invalid_period', period=label)
    groups = defaultdict(list)
    for index, row in enumerate(data['rows']):
        for column in [*request.partition_by, request.time_field, request.order_field, request.opening_field]:
            if row.get(column) is None:
                _fail('台账键、时间、顺序和期初标记不能缺失', row=index, column=column)
        key = tuple(row[column] for column in request.partition_by)
        if any(not isinstance(value, (str, int, float)) or isinstance(value, bool) for value in key):
            _fail('台账分区键必须是非空标量', row=index)
        order = _number(row, request.order_field, index)
        timestamp = _timestamp(row[request.time_field], zone)
        if timestamp >= cutoff:
            _fail('台账包含观测截止之后的记录', code='outside_observation', row=index)
        delta = Decimal(0)
        for term in request.delta_terms:
            value = _number(row, term.column, index)
            if value is None:
                _fail('台账变动为空时不能当作0', row=index, column=term.column)
            delta += value * term.coefficient
        groups[key].append((timestamp, order, delta, str(row[request.opening_field]) == request.opening_value))
    reserved = {'period', 'period_start', 'period_end', 'opening_state', 'ending_state', 'duration_seconds',
                'duration_hours', 'period_hours', 'time_fraction', 'episode_count', 'threshold'}
    group_map = {}
    used = set(reserved)
    for field in request.partition_by:
        name = field
        while name in used:
            name = 'group_' + name
        used.add(name)
        group_map[field] = name
    out = []
    threshold = Decimal(str(request.threshold))
    for key, events in groups.items():
        events.sort(key=lambda event: event[:2])
        if not events[0][3] or sum(event[3] for event in events) != 1:
            _fail('每个分区必须从唯一已登记的期初余额记录开始；不能从期间内变动猜期初',
                  code='missing_opening_state', partition=key, opening_field=request.opening_field,
                  requested_opening_value=request.opening_value,
                  observed_values=sorted({str(row[request.opening_field]) for row in data['rows']
                      if tuple(row[field] for field in request.partition_by)==key})[:20])
        if len({event[:2] for event in events}) != len(events):
            _fail('同一分区时间及顺序键重复，无法确定真实状态顺序', code='ambiguous_event_order', partition=key)
        intervals, state = [], Decimal(0)
        for position, (at, order, delta, opening) in enumerate(events):
            state += delta
            end = events[position + 1][0] if position + 1 < len(events) else cutoff
            if end > at:
                intervals.append((at, end, state))
        for label, start, end in periods:
            if events[0][0] > start:
                _fail('目标期间早于已知期初，不能将未知状态视作0', code='missing_opening_state', partition=key, period=label)
            duration, episodes, active = Decimal(0), 0, False
            first = last = None
            for left, right, balance in intervals:
                lo, hi = max(start, left), min(end, right)
                if lo >= hi:
                    continue
                if first is None:
                    first = balance
                last = balance
                if balance <= threshold:
                    duration += _seconds(hi - lo)
                    if not active:
                        episodes += 1
                    active = True
                else:
                    active = False
            seconds = _seconds(end - start)
            out.append({**{group_map[field]: value for field, value in zip(request.partition_by, key)},
                'period': label, 'period_start': start.isoformat(), 'period_end': end.isoformat(),
                'opening_state': float(first), 'ending_state': float(last),
                'duration_seconds': float(duration), 'duration_hours': float(duration / 3600),
                'period_hours': float(seconds / 3600), 'time_fraction': float(duration / seconds),
                'episode_count': episodes, 'threshold': request.threshold})
    diagnostics = [] if out else [{'code': 'empty_ledger', 'message': '没有台账记录；不能据此断言没有异常状态'}]
    return {'operation': request.operation, 'parameters': request.model_dump(mode='json'),
        'columns': [*group_map.values(), *[key for key in (
            'period', 'period_start', 'period_end', 'opening_state', 'ending_state', 'duration_seconds',
            'duration_hours', 'period_hours', 'time_fraction', 'episode_count', 'threshold')]],
        'rows': out, 'complete': True, 'total_rows': len(out), 'calculation_complete': not diagnostics,
        'diagnostics': diagnostics, 'group_field_map': group_map, 'metric_metadata': {},
        'column_metadata': {
            'duration_hours': {'unit': 'hours', 'additive': False, 'meaning': '每分区状态时长，跨对象相加不是日历时长或订单损失'},
            'duration_seconds': {'unit': 'seconds', 'additive': False},
            'episode_count': {'unit': 'episodes', 'meaning': '期间内连续状态区间数，不是变动记录数或被阻断订单数'},
            'time_fraction': {'unit': 'ratio', 'additive': False}},
        'interpretation': 'state_exposure_duration_not_demand_weighted_loss_or_causal_effect'}
