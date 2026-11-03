"""Business requests become fixed SQL batches; code owns the attempt ledger."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from ..runtime_budget import BudgetStop, current_budget


class QueryPreparationClarification(ValueError):
    def __init__(self, detail: dict):
        from ..querying.sql_output_protocol import normalize_clarification
        self.detail = normalize_clarification(detail)
        self.query_id = detail.get('query_id')
        super().__init__(self.detail['question'])


def _key(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def is_time_condition_refinement(previous: dict, current: dict) -> bool:
    """Making inherited time explicit is repair, not a fresh SQL allowance.

    Only an unfinished request with previously omitted time qualifies. An
    explicitly different old/new range, object filter, dependency or business
    comparison remains a different request; outer identity checks cover those.
    """
    old = previous.get('conditions') or {}
    new = current.get('conditions') or {}
    return (not previous.get('comparison_purpose') and not current.get('comparison_purpose')
            and 'time_range' not in old and isinstance(new.get('time_range'), dict)
            and bool(new['time_range']) and {k: v for k, v in new.items() if k != 'time_range'} == old)


class SqlBatchController:
    def _postprocess_query(self, task, action: dict, candidate: dict, outcome: dict) -> dict:
        """Complete a typed SQL+calculation request without a second model turn.

        Query results stay immutable and independently usable. A failed
        calculation exposes its own repair action; it never reruns good SQL.
        """
        from ..querying.sql_output_protocol import normalize_postprocess
        plan = normalize_postprocess(candidate.get('postprocess'))
        if not plan or outcome.get('status') not in {'completed','reused'}:
            return outcome
        raw = outcome.get('result') or {}
        rid = raw.get('id')
        if not rid:
            raise ValueError('查询后计算缺少真实已保存结果ID')
        source = next(e for e in task.evidence if e['id'] == rid)
        source.update(result_kind='calculation_source', pending_calculation={'status':'pending','operation':plan['operation']})
        action['completion_role'] = 'calculation_source'
        task.logs.append({'stage': 'query_calculation_handoff', 'query_id': action['id'],
                          'source_result_id': rid, 'operation': plan['operation']})
        request = {**plan, 'result_id': rid, 'type': 'calculate', 'goal': action['goal']}
        self._calculate_action(task, request, '对本步完整查询结果执行已声明的计算')
        observation = dict(task.observation)
        # A filtered/aggregated source needs SQL correction, not repeated
        # parameter edits over the same insufficient rows.
        if observation.get('error_code') in {'ledger_history_filtered','ledger_source_not_raw'}:
            source['pending_calculation'] = {k:observation.get(k) for k in ('action_id','error','error_code','details')}
            source['pending_calculation']['status']='source_repair_required'
            action.update(status='failed', error=observation.get('error'), error_kind='calculation_source')
            action['last_generation_error']={'kind':'calculation_source',
                'message':observation.get('error'),'details':observation.get('details'),
                'repair_target':'SQL原始数据范围/形态；保留本次计算目标，补齐完整历史，不能只修改postprocess参数'}
            return {**outcome,'status':'failed','error':observation.get('error'),
                'source_result_id':rid,'calculation':observation}
        # Technical parameter repairs stay with the SQL module and the same
        # saved source. Do not send an unfinished raw ledger back as a result
        # of the business measurement, or regenerate successful SQL.
        if observation.get('status') in {'failed','no_progress'} and getattr(self.workflow,'model_client',None) is not None:
            from ..querying.sql_output_protocol import StateDurationPostprocess
            from ..structured_output import using_output_schema
            budget = current_budget.get()
            repairs = budget.repair_limit if budget else 2
            for attempt in range(repairs):
                calc_id = observation.get('action_id')
                if budget and budget.repairs.get(calc_id,0) >= budget.repair_limit:
                    break
                source['pending_calculation'] = {k:observation.get(k) for k in ('action_id','error','error_code','details')}
                source['pending_calculation']['status'] = 'repairing'
                action['calculation_repair_calls'] = action.get('calculation_repair_calls',0)+1
                self._persist(task)
                system = self._sql_module().technical_system(task.state['datasource_id']) + (
                    '\n本次仅修复postprocess计算参数，按指定Schema输出参数对象，不输出SQL或queries。'
                    '原始SQL已成功，不能重新查询。依据实际列、原始行和具体错误修复；不能猜字段值或更改业务目标。')
                with using_output_schema('sql_calculation_repair',StateDurationPostprocess.model_json_schema()):
                    fixed = self._call(task,'sql_calculation_repair',system,{
                        'goal':action['goal'],'previous_parameters':plan,
                        'error':{k:observation.get(k) for k in ('action_id','error','error_code','details')},
                        'source':self._evidence_context(task,source,20)})
                try:
                    plan = normalize_postprocess(fixed)
                except ValueError as exc:
                    if budget:
                        budget.step=calc_id
                        budget.repair('calculation_parameter_format')
                    observation={**observation,'error':str(exc),'status':'failed'}
                    continue
                self._calculate_action(task,{**plan,'result_id':rid,'type':'calculate','goal':action['goal']},
                                       '修复原查询后的计算参数，复用原始结果')
                observation=dict(task.observation)
                if observation.get('status') not in {'failed','no_progress'}:
                    break
        calculated = observation.get('result')
        if observation.get('status') == 'reused' and observation.get('result_ids'):
            prior = next(e for e in task.evidence if e['id'] == observation['result_ids'][0])
            calculated = self._evidence_context(task, prior, 30)
            observation['calculation_complete'] = prior.get('calculation_complete')
        if isinstance(calculated, dict):
            saved = next(e for e in task.evidence if e['id'] == calculated['id'])
            saved['question_ids'] = list(action.get('question_ids') or [])
            saved['result_kind'] = 'calculated_result'
            if not saved.get('calculation_complete'):
                source['pending_calculation'] = {'status': 'incomplete', 'result_id': saved['id']}
                action['status'] = 'awaiting_calculation'
                return {**outcome, 'status': 'needs_calculation_repair', 'source_result_id': rid,
                        'result': self._evidence_context(task, saved, 30), 'calculation_complete': False,
                        'error': '状态计算尚不完整；保留缺口，不能把空台账或未确认状态解释为零时长'}
            calculation_action = next(a for a in task.actions if a['id'] == saved['step_id'])
            calculation_action['business_request'] = dict(action.get('business_request') or {})
            calculation_action['normalized_business_request'] = dict(action.get('normalized_business_request') or {})
            if candidate.get('execution_source') == 'registered_recipe':
                from ..querying.measurement_context import measurement_card
                saved.update(execution_source='registered_recipe', calculation_definition=candidate['recipe'],
                             interpretation_limits=list(candidate.get('interpretation_limits') or []))
                for column, metadata in (candidate.get('output_metadata') or {}).items():
                    if column not in saved['columns']:
                        continue
                    metric = task.metric_pool.get(metadata.get('metric_id'))
                    if metadata.get('kind') == 'dimension':
                        saved.setdefault('column_metadata', {})[column] = {
                            **saved.get('column_metadata', {}).get(column, {}), **metadata}
                    if metric:
                        card = measurement_card(metric, task.state.get('schema_graph') or {})
                        saved.setdefault('column_metadata', {})[column] = {
                            **saved.get('column_metadata', {}).get(column, {}), **metadata,
                            'kind': 'registered', 'measurement_id': metric['id'],
                            'measurement_version': card.get('version'),
                            'binding_basis': 'versioned_recipe_and_completed_state_duration'}
            source['pending_calculation'] = {'status':'completed','result_id':calculated['id']}
            action['status'] = 'completed'
            return {**outcome, 'source_result_id': rid, 'result': self._evidence_context(task, saved, 30),
                    'calculation_action_id': observation.get('action_id'),
                    'calculation_complete': observation.get('calculation_complete')}
        source['pending_calculation'] = {k:observation.get(k) for k in ('action_id','error','error_code','details')}
        source['pending_calculation']['status'] = 'failed'
        raw = {**raw, 'result_kind':'calculation_source', 'pending_calculation':source['pending_calculation']}
        action['status'] = 'awaiting_calculation'
        return {**outcome, 'status': 'needs_calculation_repair', 'source_result_id': rid,
                'result': raw, 'calculation': observation,
                'error': observation.get('error') or '查询已成功；计算尚未完成，复用原结果修复计算参数'}

    def _entity_values(self, task, field: dict, *, limit: int = 100) -> dict:
        """Read an acquired entity field through the same authorized MCP boundary."""
        from ..security import AccessScope
        source = task.state['datasource_id']
        scope = AccessScope.from_dict(task.state['access_scope'])
        graph = task.state.get('schema_graph') or {}
        table = next((t for t in graph.get('tables') or []
                      if t.get('id') == field.get('table_id') and t.get('name') == field.get('table_name')), None)
        known_field = any(f.get('table_id') == field.get('table_id') and f.get('name') == field.get('name')
                          for f in graph.get('fields') or [])
        if (not table or not known_field or field.get('datasource_id') != source
                or not scope.allows_table(source, str(table['id']))):
            raise PermissionError('实体值检索必须绑定当前授权数据源的真实字段')
        key = _key({'identity': self._identity(task), 'source': source, 'scenario': task.state.get('scenario'),
                    'scope': task.state['access_scope'], 'field': field['sql_name']})
        cache = task.entity_value_cache
        catalog = cache.get(key)
        quote = lambda name: '"' + str(name).replace('"', '""') + '"'
        column, table_name = quote(field['name']), quote(table['name'])
        def read(sql):
            data = self.workflow.mcp_client(task.state['access_scope']).call_tool('query_' + source, {'sql': sql})
            if not data.get('success'):
                raise RuntimeError('实体值检索失败：' + str(data.get('error') or '未知错误'))
            if not data.get('complete') or data.get('preview_truncated'):
                raise RuntimeError('实体值检索返回不完整，不能确认名称绑定')
            return [row['value'] for row in data.get('rows') or [] if row.get('value') is not None]
        if catalog is None:
            values = read(f'SELECT DISTINCT {column} AS value FROM {table_name} '
                          f'WHERE {column} IS NOT NULL ORDER BY value LIMIT {limit + 1}')
            catalog = {'values': values[:limit], 'complete': len(values) <= limit}
            cache[key] = catalog
            task.logs.append({'stage': 'entity_values_loaded', 'field': field['sql_name'],
                              'catalog_complete': catalog['complete'], 'count': len(catalog['values'])})
        requested = field.get('requested_values') or []
        missing = [value for value in requested if str(value).casefold() not in
                   {str(v).casefold() for v in catalog['values']}]
        if not catalog['complete'] and missing:
            exact_key = key + ':' + _key(missing)
            if exact_key not in cache:
                # Only values are quoted as SQL data; identifiers originate in
                # authorized metadata and the engine repeats its safety checks.
                literals = ','.join("'" + str(v).lower().replace("'", "''") + "'" for v in missing)
                exact = read(f'SELECT DISTINCT {column} AS value FROM {table_name} '
                             f'WHERE lower(CAST({column} AS VARCHAR)) IN ({literals}) LIMIT {limit + 1}')
                cache[exact_key] = {'values': list(dict.fromkeys([*catalog['values'], *exact]))[:limit * 2],
                                    'complete': False, 'exact_lookup_complete': len(exact) <= limit,
                                    'looked_up_values': requested}
            return cache[exact_key]
        return catalog

    @staticmethod
    def _inherit_query_scope(task, query: dict) -> dict:
        """Carry user scope explicitly; a purposeful comparison can override it."""
        from copy import deepcopy
        value = deepcopy(query)
        understanding = task.state.get('task_understanding') or {}
        scope = understanding.get('scope') or {}
        if task.protocol_version < 4 or not isinstance(scope, dict):
            return value
        from ..querying.query_scope import effective_conditions
        value['conditions'] = effective_conditions(value, task.state)
        concepts = {'store': 'store_id', 'category': 'category_code', 'sku': 'sku_id', 'channel': 'source_channel'}
        confirmed_scope = (task.state.get('confirmed_parameters') or {}).get('scope')
        name = confirmed_scope if isinstance(confirmed_scope, str) and confirmed_scope.strip() else scope.get('name_or_id')
        concept = concepts.get(scope.get('kind'))
        if name and concept and not value.get('comparison_purpose'):
            from ..querying.entity_resolution import resolve_dimension
            conditions = value.setdefault('conditions', {})
            inherited = resolve_dimension(concept, task.state.get('schema_graph') or {},
                access_scope=task.state['access_scope'], datasource_id=task.state['datasource_id'])
            for key, existing in conditions.items():
                resolved = resolve_dimension(key, task.state.get('schema_graph') or {},
                    access_scope=task.state['access_scope'], datasource_id=task.state['datasource_id'])
                same = (key == concept or resolved.get('concept') == scope.get('kind')
                        or inherited.get('field') and inherited.get('field') == resolved.get('field'))
                if same:
                    if existing != name:
                        raise ValueError('分析范围与用户指定对象冲突；对照查询须明确comparison_purpose')
                    break
            else:
                conditions[concept] = name
        return value

    def _sql_module(self):
        from ..querying.sql_module import SqlQueryModule
        module = getattr(self.workflow.single_database_agent, 'sql_module', None) if hasattr(self.workflow, 'single_database_agent') else None
        if module is None:
            module = SqlQueryModule(self.model, self.workflow.mcp_client,
                                    self.workflow.skills.get('database_query'), self.workflow.database_engine)
        return module

    def _prepare_sql_context(self, task, requests: list[dict]) -> dict:
        from ..querying.query_context import assemble_query_context
        from ..querying.task_measurements import measurement_memory, inherit_definitions, measurement_field_refs
        names = list(dict.fromkeys(str(v) for q in requests for v in q.get('metric_requirements', [])))
        # Exact public metric IDs do not need a probabilistic search to recover
        # their definitions. Physical dependencies still pass authorized graph
        # assembly and the existing recipe dependency loader below.
        if task.protocol_version >= 4:
            from ..homenova import registered_metric
            for name in names:
                metric = registered_metric(task.state['datasource_id'], name)
                if metric:
                    task.metric_pool[metric['id']] = metric
        inherited = measurement_memory(task, names)
        inherited_names = {str(d.get('measurement_id') or d.get('metric_id')).casefold()
                           for d in inherited['definitions'] if d.get('kind') in {'registered', 'task_local'}}
        dimensions = list(dict.fromkeys(str(v) for q in requests for v in
                         [*q.get('dimensions', []), *(q.get('conditions') or {}).keys()]))
        field_names = {str(v).casefold() for field in task.field_pool.values()
                       for v in [field.get('name'), field.get('label'), field.get('sql_name'),
                                 *(field.get('aliases') or [])] if v}
        metric_ids, missing = [], []
        for name in names:
            matching = [key for key, item in task.metric_pool.items()
                        if name.casefold() in {str(v).casefold() for v in
                           [key, item.get('label'), *(item.get('aliases') or [])] if v}]
            if len(matching) == 1:
                metric_ids.extend(matching)
            elif not matching and name.casefold() not in field_names and name.casefold() not in inherited_names:
                missing.append(name)
        # Missing business terms trigger the existing authorized retrieval,
        # never a repository scan or a fabricated physical field name.
        if task.protocol_version >= 3:
            from ..querying.entity_resolution import is_time_grain_term
            from ..querying.query_scope import TIME_CONDITIONS
            missing += [name for name in dimensions if name.casefold() not in field_names
                        and not is_time_grain_term(name) and name not in TIME_CONDITIONS]
        else:
            missing += [name for name in dimensions if name.casefold() not in field_names]
        if not task.field_pool or missing:
            retrieval_goal = '；'.join(q['goal'] for q in requests)
            if missing:
                retrieval_goal += '；补充当前未取得的业务资料：' + '、'.join(dict.fromkeys(missing))
            # Existing metric IDs are already safely pooled. Passing only those
            # IDs would make _retrieve's cache branch return before looking for
            # the new dimension/term. An explicit new goal with no cached-only
            # request forces the normal authorized search and preserves the pool.
            self._retrieve(task, {'goal': retrieval_goal, 'metric_ids': [], 'fields': []})
        graph = task.state.get('schema_graph') or {}
        entities = None
        if task.protocol_version >= 4:
            from ..querying.entity_resolution import prepare_entity_context
            entities = prepare_entity_context(requests, graph, access_scope=task.state['access_scope'],
                datasource_id=task.state['datasource_id'],
                lookup_values=lambda field, **kwargs: self._entity_values(task, field, **kwargs))
            task.logs.append({'stage': 'entity_resolution', 'resolutions': entities['resolutions'],
                              'missing_requests': entities['missing_requests']})
            if entities.get('clarification'):
                raise QueryPreparationClarification(entities['clarification'])
            requests = entities['normalized_requests']
        resolution = None
        if task.protocol_version >= 3:
            from ..querying.measurement_context import resolve_query_requirements
            resolution = resolve_query_requirements(requests, graph, list(task.metric_pool.values()))
            inherit_definitions(resolution, inherited)
            selected_fields = list(resolution['requested_fields'])
            selected_fields += [ref for d in inherited['definitions'] for ref in measurement_field_refs(d)]
            if entities:
                selected_fields += entities['requested_fields']
            if resolution.get('unresolved_requirements'):
                # Only candidates from the targeted latest retrieval travel
                # with task-local measures; never fall back to the whole pool.
                selected_fields += [task.field_pool[key].get('sql_name') or key
                                    for key in task.active_fields if key in task.field_pool]
            bundle = assemble_query_context(graph, metrics=list(task.metric_pool.values()),
                metric_ids=resolution['metric_ids'], requested_fields=list(dict.fromkeys(selected_fields)),
                database=task.state['datasource_id'], access_scope=task.state['access_scope'])
            bundle['measurement_requests'] = resolution
            bundle['measurement_groups'] = resolution.get('grouping') or []
            bundle['missing_requests'] = [*(bundle.get('missing_requests') or []),
                                          *(resolution.get('unresolved_requirements') or [])]
        else:
            bundle = assemble_query_context(graph, metrics=list(task.metric_pool.values()),
                                            database=task.state['datasource_id'], access_scope=task.state['access_scope'])
        # SQL gets task constraints once; do not assemble and then throw away
        # a second complete business evidence view just to obtain this header.
        context = {'task': {'task_id': task.state['task_id'], 'question': task.state['query'],
                           'scope_and_parameters': task.state.get('confirmed_parameters') or
                           (task.state.get('workspace') or {}).get('confirmed_parameters') or {},
                           'task_understanding': task.state.get('task_understanding') or {}}}
        dependencies = {}
        inherited_refs = [entry['result_id'] for entry in inherited['results']]
        expanded = {m.get('id') for m in bundle.get('registered_metrics') or []}
        inherited_ids = {m.get('metric_id') or m.get('measurement_id') for m in inherited['definitions'] if m.get('kind')=='registered'}
        for rid in dict.fromkeys([rid for q in requests for rid in q.get('input_result_ids', [])] + inherited_refs):
            evidence, data = self._read_evidence(task, rid)
            dependencies[rid] = self._evidence_context(task, evidence, 30,expanded_metric_names=expanded|inherited_ids)
        from ..querying.task_measurements import compact_measurement_memory
        inherited=compact_measurement_memory(inherited,expanded)
        bundle.update(database=task.state['datasource_id'], access_scope=task.state['access_scope'],
                      execution_protocol_version=task.protocol_version,
                      task=context['task'], task_context={'task_question': task.state['query'],
                          'conditions': context['task']['scope_and_parameters'],
                          'task_understanding': task.state.get('task_understanding') or {},
                          'time_context': task.state.get('time_context') or {}},
                      confirmed_parameters=task.state.get('confirmed_parameters') or {},
                      dependency_results=dependencies,
                      inherited_measurements=inherited,
                      time_context=task.state.get('time_context') or {},
                      scenario=task.state.get('scenario') or {})
        if entities:
            bundle['entity_resolution'] = entities['resolutions']
            bundle['normalized_requests'] = requests
            bundle['missing_requests'] += entities['missing_requests']
        task.logs.append({'stage': 'sql_context_assembled', 'manifest': bundle.get('manifest'),
                          'missing_requests': bundle.get('missing_requests'),
                          'conflicts': bundle.get('conflicts'), 'dependency_result_ids': list(dependencies),
                          'context_fingerprint': _key(bundle)})
        return bundle

    def _prepare_recipe_dependencies(self, task, candidate: dict) -> None:
        """Acquire the selected recipe's full output pack through existing scoped retrieval."""
        definitions = [task.metric_pool[name] for name in candidate['registered_metric_ids']]
        fields = sorted(candidate.get('required_fields') or
                        {ref for metric in definitions for ref in self._metric_field_references(metric)})
        available = {field.get('sql_name') for field in task.field_pool.values()}
        missing = sorted(set(fields) - available)
        if not missing:
            return
        self._retrieve(task, {'goal': '补齐已选固定计算的字段依赖：' + candidate['recipe']['recipe_id'],
            'metric_ids': candidate['registered_metric_ids'], 'fields': fields})
        available = {field.get('sql_name') for field in task.field_pool.values()}
        missing = sorted(set(fields) - available)
        task.logs.append({'stage': 'registered_recipe_dependencies',
            'recipe_id': candidate['recipe']['recipe_id'], 'requested_fields': fields, 'missing_fields': missing})
        if missing:
            raise ValueError('已选计算的授权字段依赖未取得：' + '、'.join(missing))

    def _repair_business_result(self, task, request: dict, reason: str) -> None:
        """Challenge successful SQL without opening a new query or new quota."""
        from copy import deepcopy
        query_id=request.get('query_id');issue=request.get('issue')
        action=next((a for a in task.actions if a['id']==query_id and a.get('action_type')=='query'),None)
        if action is None or not isinstance(issue,str) or not issue.strip():
            self._protocol_error(task,'修复必须引用本任务真实查询编号并说明具体结果问题')
            return
        if action.get('status') in {'generating','rewriting','interrupted'}:
            raise BudgetStop('原查询的模型调用结果未知，不自动重放')
        original=deepcopy(action.get('business_request'))
        if not isinstance(original,dict):
            self._protocol_error(task,'原查询没有可恢复的业务请求，不能凭编号重造目标')
            return
        observed=[]
        for rid in action.get('result_ids') or []:
            entry=next((e for e in task.evidence if e['id']==rid and e.get('check_status')!='superseded'),None)
            if entry:
                self._read_evidence(task,rid)
                observed.append(self._evidence_context(task,entry,20))
        signatures=[key for key,rid in task.completed_queries.items() if rid in (action.get('result_ids') or [])]
        affected=self._invalidate(task,action.get('result_ids') or [])
        for key in signatures:task.failed_signatures[key]=issue
        action.update(status='failed',error=issue,error_kind='result_semantics',
            repair_diagnostics={'kind':'result_semantics','issue':issue,'observed_results':observed,
                'instruction':'保持原目标和范围，核查原SQL、连接键、统计集合及口径。不要为了对齐数字修改正确数据；不能重算独立成功查询。'})
        task.logs.append({'stage':'result_challenged','query_id':query_id,'issue':issue,
            'invalidated_result_ids':sorted(affected),'sql_generations':action.get('sql_generations',0)})
        self._persist(task)
        if action.get('sql_generations',0)>=self._sql_limit(task,action):
            action['generation_closed']=True
            task.observation={'type':'query_exhausted','action_id':query_id,
                'error':'原查询总生成次数已用完；问题结果已失效，独立证据保留，不能换ID重领额度'}
            return
        self._business_query_batch(task,{'queries':[original]},reason,repair_action=action)

    def _prepare_independent_queries(self, task, bound):
        """A bad request must not discard independent members of the same batch."""
        try:
            return self._prepare_sql_context(task, [q for _, q in bound]), bound, {}
        except BudgetStop:
            raise
        except (ValueError, KeyError, TypeError, RuntimeError, OSError):
            if task.protocol_version < 4 or len(bound) < 2:
                raise
        survivors, failures = [], {}
        for action, query in bound:
            try:
                self._prepare_sql_context(task, [query])
                survivors.append((action, query))
            except BudgetStop:
                raise
            except (ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
                detail = exc.detail if isinstance(exc, QueryPreparationClarification) else None
                action.update(status='needs_method_decision' if detail else 'blocked',
                              error=str(exc), error_kind='clarification' if detail else 'preparation')
                failures[action['id']] = {'action_id': action['id'], 'status': action['status'],
                    'error': str(exc), **({'clarification': detail} if detail else {})}
        context = self._prepare_sql_context(task, [q for _, q in survivors]) if survivors else {}
        task.logs.append({'stage': 'query_preparation_isolated', 'failed_query_ids': list(failures),
                          'continuing_query_ids': [a['id'] for a, _ in survivors]})
        return context, survivors, failures

    def _business_query_batch(self, task, request: dict, reason: str, *, repair_action: dict | None = None) -> None:
        budget = current_budget.get()
        queries = request.get('queries')
        if not isinstance(queries, list) or not queries or not all(isinstance(q, dict) for q in queries):
            self._protocol_error(task, 'query_batch必须包含非空queries数组')
            return
        if task.protocol_version >= 4 and repair_action is None:
            from ..querying.query_scope import partition_registered_request
            expanded = []
            for original in queries:
                try:
                    from ..querying.query_scope import normalize_conditions
                    scoped = self._inherit_query_scope(task, {**original,
                        'conditions': normalize_conditions(original.get('conditions') or {}, strict=True)})
                    parts = partition_registered_request(scoped, task.state['datasource_id'])
                except (ValueError, TypeError):
                    parts = [original]  # Normal input handling records the precise error.
                if len(parts) > 1:
                    task.logs.append({'stage': 'query_populations_partitioned', 'original_request': original,
                                      'execution_requests': parts, 'business_rounds': 1})
                expanded.extend(parts)
            queries = expanded
        allowed = {'goal', 'metric_requirements', 'dimensions', 'required_output_fields',
                   'input_result_ids', 'comparison_purpose', 'conditions', 'question_ids', 'execution_mode'}
        valid = {e['id']: e for e in task.evidence if e.get('check_status') != 'superseded'}
        bound = []
        reused = []
        bound_keys = set()
        batch_id = f'{task.state["task_id"]}:B{1 + sum(a.get("stage") == "sql_batch_bound" for a in task.logs)}'
        for query in queries:
            if set(query) - allowed or not isinstance(query.get('goal'), str) or not query['goal'].strip():
                self._protocol_error(task, '业务取数只接受目标/指标/维度/输出/依赖，不能包含SQL、契约或自定ID')
                return
            for name in ('metric_requirements', 'dimensions', 'required_output_fields', 'input_result_ids', 'question_ids'):
                if not isinstance(query.get(name, []), list) or not all(isinstance(v, str) for v in query.get(name, [])):
                    self._protocol_error(task, name + '必须为字符串数组')
                    return
            if not isinstance(query.get('conditions', {}), dict):
                self._protocol_error(task, 'conditions必须为结构化业务条件，省略表示继承')
                return
            try:
                from ..querying.query_scope import normalize_conditions
                query = {**query, 'conditions': normalize_conditions(query.get('conditions', {}), strict=True)}
                query = self._inherit_query_scope(task, query)
                if task.protocol_version >= 4:
                    from ..querying.query_scope import complete_registered_request
                    completed = complete_registered_request(query, task.state['datasource_id'])
                    if completed != query:
                        task.logs.append({'stage': 'query_execution_details_completed',
                                          'original_request': query, 'execution_request': completed})
                    query = completed
            except ValueError as exc:
                self._protocol_error(task, str(exc))
                return
            refs = query.get('input_result_ids') or []
            if task.protocol_version >= 3 and not set(query.get('question_ids') or []) <= {
                q['id'] for q in task.progress.get('core_questions') or []}:
                self._protocol_error(task, '查询引用未知调查问题编号')
                return
            if not set(refs) <= valid.keys() or any(not valid[r].get('complete') for r in refs):
                self._protocol_error(task, '业务取数依赖不存在、已失效或不完整；不能引用同批未来结果')
                return
            # Semantic identity survives a renamed goal or model-supplied ID.
            from ..querying.task_measurements import canonical_name, canonical_conditions
            def canonical_term(value):
                return canonical_name(task, value)
            term_keys = ('metric_requirements', 'dimensions') if task.protocol_version >= 3 else (
                'metric_requirements', 'dimensions', 'required_output_fields')
            terms = {key: sorted(set(canonical_term(v) for v in query.get(key, []))) for key in term_keys}
            query_conditions = query.get('conditions') or {}
            identity = {'source': task.state['datasource_id'], 'snapshot': task.state.get('scenario'),
                        'conditions': task.state.get('confirmed_parameters') or {},
                        'understanding': task.state.get('task_understanding') or {}, 'terms': terms,
                        'inputs': [(rid, valid[rid].get('fingerprint')) for rid in sorted(refs)],
                        'query_conditions': canonical_conditions(query_conditions, canonical_term),
                        'comparison_purpose': query.get('comparison_purpose') or ''}
            if query.get('execution_mode'):
                identity['execution_mode'] = query['execution_mode']
            if query.get('execution_mode') == 'exploratory':
                identity['goal'] = query['goal'].strip().casefold()
            if not any(terms.values()):
                identity['goal'] = query['goal'].strip().casefold()
            request_key = _key(identity)
            if request_key in bound_keys:
                task.logs.append({'stage': 'sql_batch_deduplicated', 'batch_id': batch_id,
                                  'request_key': request_key, 'goal': query['goal']})
                continue
            bound_keys.add(request_key)
            existing = repair_action if repair_action is not None else next((a for a in task.actions if a.get('request_key') == request_key), None)
            if existing is None and task.protocol_version >= 4:
                from ..querying.task_measurements import covered_result_ids
                for previous in task.actions:
                    covered = covered_result_ids(task, previous, query, canonical_term)
                    if covered:
                        # Read through the same authorization boundary before
                        # reuse. Do not synthesize data from the directory.
                        for rid in covered:
                            self._read_evidence(task, rid)
                        reused.append({'action_id': previous['id'], 'status': 'reused', 'result_ids': covered})
                        task.logs.append({'stage': 'query_coverage_reused', 'goal': query['goal'],
                                          'result_ids': covered, 'request_key': request_key})
                        break
                else:
                    covered = []
                if covered:
                    continue

            inherited_parent = None
            if existing is None and task.protocol_version >= 3:
                # Re-resolve the original business terms against the now-known
                # metadata. Retrieval must not turn GMV -> its registered ID
                # into a fresh allowance on the next round.
                for previous in task.actions:
                    original_identity = previous.get('request_identity')
                    original_request = previous.get('business_request') or {}
                    if not original_identity or previous.get('action_type') != 'query':
                        continue
                    refreshed = {**original_identity, 'query_conditions': canonical_conditions(original_request.get('conditions') or {}, canonical_term), 'comparison_purpose': original_request.get('comparison_purpose') or '', 'terms': {
                        key: sorted(set(canonical_term(v) for v in original_request.get(key, [])))
                        for key in term_keys}}
                    if (task.protocol_version >= 4 and previous.get('status') in {'failed', 'no_progress', 'blocked'}
                            and is_time_condition_refinement(original_request, query)
                            and all(refreshed.get(k) == identity.get(k) for k in
                                    ('source', 'snapshot', 'conditions', 'understanding', 'inputs', 'terms'))):
                        refreshed = {**refreshed, 'query_conditions': canonical_conditions(query_conditions, canonical_term)}
                        task.logs.append({'stage': 'query_condition_refinement', 'query_id': previous['id'],
                                          'reason': '失败取数补充明确时间，继承原生成次数',
                                          'sql_generations': previous.get('sql_generations', 0)})
                    if _key(refreshed) == request_key:
                        existing = previous
                        previous.update(request_key=request_key, request_identity=refreshed)
                        break
                    # Splitting an unfinished request is technical repair,
                    # not a fresh quota for every smaller set of metrics.
                    old_terms = refreshed['terms']
                    same_basis = all(refreshed.get(k) == identity.get(k) for k in
                                     ('source', 'snapshot', 'conditions', 'understanding', 'inputs', 'query_conditions'))
                    subset = (set(terms.get('metric_requirements') or []) and
                              set(terms.get('metric_requirements') or []) < set(old_terms.get('metric_requirements') or []) and
                              terms.get('dimensions') == old_terms.get('dimensions'))
                    if same_basis and subset and previous.get('status') != 'completed':
                        inherited_parent = previous
                if existing is None and inherited_parent and inherited_parent.get('sql_generations', 0) >= self._sql_limit(task, inherited_parent):
                    existing = inherited_parent
            if existing is None:
                existing = next((a for a in task.actions if a.get('awaiting_clarification')
                    and {key: sorted(set(canonical_term(v) for v in (a.get('business_request') or {}).get(key, [])))
                         for key in terms} == terms
                    and ((a.get('business_request') or {}).get('conditions') or {}) == query_conditions), None)
                if existing:
                    existing.update(request_key=request_key, awaiting_clarification=False,
                                    request_version=existing.get('request_version', 1) + 1)
            if existing:
                if existing.get('status') in {'completed','awaiting_calculation'}:
                    current = {e['id']: e for e in task.evidence if e.get('check_status') not in {'superseded', 'invalid', 'failed'}}
                    ids = existing.get('result_ids') or []
                    if not ids or any(rid not in current or not current[rid].get('complete') for rid in ids):
                        self._protocol_error(task, '旧查询结果不完整或已失效，不能复用；需明确更正范围或补查目标')
                        return
                    for rid in ids:
                        self._read_evidence(task, rid)
                    if existing.get('completion_role') == 'calculation_source':
                        # One raw ledger can support many periods/thresholds.
                        # Only this query's successful handoff binds its result.
                        calculation_ids = [current[rid]['pending_calculation']['result_id'] for rid in ids
                            if (current[rid].get('pending_calculation') or {}).get('status') == 'completed']
                        calculation_ids = [rid for rid in calculation_ids if rid in current
                            and current[rid].get('calculation_complete') and current[rid].get('complete')]
                        if not calculation_ids:
                            reused.append({'action_id': existing['id'], 'status': 'needs_calculation_repair',
                                'source_result_ids': ids, 'error': '原始查询成功但计算未完成；读取已保存原结果，修正计算参数，不能重查SQL或当作已得到目标指标'})
                            continue
                        for rid in calculation_ids:
                            self._read_evidence(task, rid)
                        ids = calculation_ids
                    reused.append({'action_id': existing['id'], 'status': 'reused', 'result_ids': ids})
                    continue
                if existing.get('sql_generations', 0) >= self._sql_limit(task, existing):
                    if task.protocol_version < 3:
                        raise BudgetStop('同一业务取数已用完两次生成，不允许换目标措辞或批次重置')
                    existing['generation_closed'] = True
                    task.observation = {'type': 'query_exhausted', 'action_id': existing['id'],
                        'error': '该逻辑查询已耗尽生成次数，不得改名／改输出别名重开；可选择真正不同的证据或有边界交付。'}
                    self._charge_protocol_repair(task, existing, '重复请求已关闭的逻辑查询')
                    continue
                action = existing
                # An unknown in-flight attempt needs explicit user resume;
                # this path cannot automatically replay it under a new batch.
                if action.get('status') in {'interrupted', 'generating', 'rewriting'}:
                    raise BudgetStop('已有生成调用结果未知，保留账本并停止自动重放')
            else:
                action = self._new_action(task, 'query', query['goal'], refs)
                action.update(request_key=request_key, sql_generations=0, attempts=0,
                              sql_generation_limit=self._sql_limit(task), business_request=dict(query), request_version=1)
                if task.protocol_version >= 3:
                    action['request_identity'] = identity
                    if inherited_parent:
                        action.update(parent_query_id=inherited_parent['id'],
                                      sql_generations=inherited_parent.get('sql_generations', 0),
                                      attempts=inherited_parent.get('attempts', 0),
                                      sql_generation_limit=self._sql_limit(task, inherited_parent))
            if task.protocol_version >= 3:
                action['question_ids'] = query.get('question_ids') or [q['id'] for q in
                    task.progress.get('core_questions') or [] if q.get('importance') == 'core' and
                    q.get('status') in {'open', 'blocked'}]
            action['batch_id'] = batch_id
            bound.append((action, {**query, 'goal': action['goal'], 'query_id': action['id']}))
        if not bound:
            task.observation = {'type': 'query_batch', 'queries': reused} if reused else task.observation
            if task.protocol_version >= 3:
                self._persist(task)
                return
            raise BudgetStop('本批查询均已有有效结果，停止重复取证')
        if repair_action is not None:
            if len(bound)!=1 or bound[0][0] is not repair_action or not repair_action.get('sql_generations'):
                raise ValueError('结果修复只能绑定原查询及原生成账本')
        elif budget:
            budget.query_round()
        task.logs.append({'stage': 'sql_batch_bound', 'batch_id': batch_id,
                          'query_ids': [a['id'] for a, _ in bound]})
        self._persist(task)
        try:
            context, bound, preparation_failures = self._prepare_independent_queries(task, bound)
        except QueryPreparationClarification as exc:
            if task.protocol_version >= 4:
                affected = []
                for action, _ in bound:
                    if not exc.query_id or action['id'] == exc.query_id:
                        action.update(status='needs_method_decision', awaiting_clarification=True,
                                      error=str(exc), error_kind='clarification')
                        affected.append({'query_id': action['id'], 'clarification': exc.detail})
                task.observation = {'type':'query_method_decision','method_clarifications':affected,
                    'instruction':'主Agent判断缺失的是用户必要条件还是自己选择的技术方法；只有前者才向用户澄清。'}
                self._persist(task)
                return
            task.clarification = exc.detail
            task.final_status = 'waiting_clarification'
            for action, _ in bound:
                if not exc.query_id or action['id'] == exc.query_id:
                    action.update(status='waiting_clarification', awaiting_clarification=True,
                                  error=str(exc), error_kind='clarification')
            task.observation = {'type': 'query_preparation_clarification', 'clarification': exc.detail}
            self._persist(task)
            return
        except BudgetStop:
            raise
        except (ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
            for action, _ in bound:
                action.update(status='blocked', error=str(exc), error_kind='preparation')
            task.observation = {'type': 'query_preparation_error', 'batch_id': batch_id, 'error': str(exc),
                                'query_ids': [a['id'] for a, _ in bound]}
            self._persist(task)
            return
        module = None  # Fixed calculations and preparation failures need no SQL model.
        if task.protocol_version >= 4:
            normalized = {q['query_id']: q for q in context.get('normalized_requests') or []}
            bound = [(action, normalized.get(action['id'], query)) for action, query in bound]
            for action, query in bound:
                action['normalized_business_request'] = query
                action['entity_resolutions'] = [r for r in context.get('entity_resolution') or []
                                               if r.get('query_id') == action['id']]
        if task.protocol_version >= 3:
            by_query = (context.get('measurement_requests') or {}).get('by_query') or {}
            for action, _ in bound:
                action['measurement_request'] = by_query.get(action['id']) or {}
        outcomes = dict(preparation_failures)
        pending = []
        # Compile only an untouched request covered by a versioned recipe.
        # It passes the very same validation/MCP boundary as generated SQL.
        # A compiled candidate consumes one of the three SQL candidates, but
        # never a model call. Unsupported requests remain intact for the coder.
        from ..querying.compiled_metrics import compile_paid_goods, RecipeNotCovered, METRICS
        from ..querying.compiled_inventory import compile_inventory_duration, inventory_duration_requested
        from ..querying.compiled_sessions import compile_sessions, SessionPopulationMismatch, check_session_population
        from ..querying.compiled_bundle import compile_bundle
        from ..homenova import registered_metric
        for action, query in bound:
            inventory_requested = inventory_duration_requested(query, task.state['datasource_id'])
            dependency_retry = action.get('error_kind') == 'recipe_dependencies'
            if dependency_retry:
                previous_candidate = action['candidates'][-1]
                fields = set(previous_candidate.get('required_fields') or
                             {ref for name in previous_candidate['registered_metric_ids']
                              for ref in self._metric_field_references(task.metric_pool[name])})
                available = {field.get('sql_name') for field in task.field_pool.values()}
                if fields - available:
                    outcomes[action['id']] = {'action_id': action['id'], 'status': 'blocked',
                        'error': action['error'], 'sql_generations': action['sql_generations'],
                        'repair_instruction': '原固定计算的授权字段缺口仍未解决；先补齐元数据，不能改由模型生成SQL。'}
                    continue
            try:
                # Statistical meaning is independent of retry ownership. A
                # split request inheriting an earlier generation must not skip
                # the same registered population check as its parent.
                if task.protocol_version >= 4 and task.state['datasource_id'] == 'homenova_20260831':
                    identities = {(registered_metric(task.state['datasource_id'], name) or {}).get('id', name)
                                  for name in query.get('metric_requirements') or []}
                    check_session_population(identities, query.get('dimensions') or [], query.get('conditions') or {})
                    from ..querying.query_scope import TIME_CONDITIONS
                    if (query.get('execution_mode') == 'registered_metrics'
                            and 'month' in (query.get('dimensions') or [])
                            and not set(query.get('conditions') or {}) & TIME_CONDITIONS
                            and not context.get('requested_time_expressions')):
                        action.update(status='needs_method_decision', error_kind='request_shape',
                            error='逐月统计缺少明确期间；请补齐conditions.months或time_range。不能用模型SQL猜测月份。')
                        outcomes[action['id']] = {'action_id': action['id'], 'status': 'needs_method_decision',
                            'error': action['error'], 'sql_generations': action.get('sql_generations', 0)}
                        continue
                if (task.protocol_version < 4 or action.get('sql_generations', 0) and action.get('execution_source') == 'model_sql'
                        or query.get('execution_mode') != 'registered_metrics' and not inventory_requested):
                    pending.append((action, query))
                    continue
                if inventory_requested:
                    candidate = compile_inventory_duration(query, context)
                else:
                    try:
                        candidate = compile_paid_goods(query, context)
                    except RecipeNotCovered as paid_error:
                        try:
                            candidate = compile_sessions(query, context)
                        except SessionPopulationMismatch:
                            raise
                        except RecipeNotCovered as session_error:
                            try:
                                candidate = compile_bundle(query, context)
                            except SessionPopulationMismatch:
                                raise
                            except RecipeNotCovered as bundle_error:
                                raise RecipeNotCovered(str(paid_error) + '; ' + str(session_error)
                                                       + '; ' + str(bundle_error)) from bundle_error
            except SessionPopulationMismatch as exc:
                action['recipe_selection'] = {'status': 'population_mismatch', 'reason': str(exc)}
                action.update(status='needs_method_decision', error=str(exc), error_kind='metric_scope')
                outcomes[action['id']] = {'action_id': action['id'], 'status': 'needs_method_decision',
                    'error': str(exc), 'alternative_metric_ids': list(exc.alternative_metric_ids),
                    'sql_generations': action.get('sql_generations', 0)}
                continue
            except RecipeNotCovered as exc:
                action['recipe_selection'] = {'status': 'not_covered', 'reason': str(exc)}
                if dependency_retry:
                    action.update(status='needs_method_decision', error=str(exc), error_kind='recipe_dependencies')
                    outcomes[action['id']] = {'action_id': action['id'], 'status': 'needs_method_decision',
                        'error': str(exc), 'sql_generations': action.get('sql_generations', 0)}
                    continue
                if inventory_requested and task.state['datasource_id'] == 'homenova_20260831':
                    # This known operator owns state intervals. A mixed request
                    # must be separated, not delegated to new free-form interval SQL.
                    error = ('库存时长需单独查询项，明确月份及SKU粒度；可筛品类并保留仓库分区。'
                             '其他会话、价格或机制指标放同批独立项；不改变原业务目标。' + str(exc))
                    action.update(status='needs_method_decision', error=error, error_kind='request_shape')
                    outcomes[action['id']] = {'action_id': action['id'], 'status': 'needs_method_decision',
                        'error': error, 'sql_generations': action.get('sql_generations', 0)}
                    continue
                pending.append((action, query))
                continue
            generation = action.get('sql_generations', 0) + 1
            if generation > self._sql_limit(task, action):
                outcomes[action['id']] = {'action_id': action['id'], 'status': 'failed',
                    'error': '原查询生成次数已耗尽，不能为算子修复重置'}
                continue
            if budget and generation > 1:
                budget.step = action['id']
                budget.repair('修复已登记计算，继承原SQL生成次数')
            action.update(sql_generations=generation, attempts=action.get('attempts', 0) + 1,
                          compiled_generations=action.get('compiled_generations', 0) + 1,
                          model_generations=action.get('model_generations', 0), status='generating', execution_source='registered_recipe',
                          recipe_selection={'status': 'compiled', 'recipe': candidate['recipe']},
                          query_contract=candidate['query_contract'])
            parent_id, seen_parents = action.get('parent_query_id'), set()
            while parent_id and parent_id not in seen_parents:
                seen_parents.add(parent_id)
                parent = next((a for a in task.actions if a['id'] == parent_id), None)
                if not parent:
                    raise ValueError('查询修复父项不存在，不能创建独立额度')
                parent.update(sql_generations=max(parent.get('sql_generations', 0), generation),
                              attempts=max(parent.get('attempts', 0), action['attempts']))
                parent_id = parent.get('parent_query_id')
            metric_ids = candidate.get('registered_metric_ids') or list(METRICS)
            for identity in metric_ids:
                task.metric_pool[identity] = registered_metric(task.state['datasource_id'], identity)
            task.logs.append({'stage': 'registered_metric_definitions_loaded',
                              'query_id': action['id'], 'metric_ids': metric_ids})
            action['recipe_output_metadata'] = candidate.get('output_metadata') or {}
            action['interpretation_limits'] = list(candidate.get('interpretation_limits') or [])
            action.setdefault('candidates', []).append({'generation': generation, **candidate})
            self._persist(task)
            try:
                self._prepare_recipe_dependencies(task, candidate)
            except BudgetStop:
                raise
            except (ValueError, OSError, RuntimeError) as exc:
                action.update(status='blocked', error=str(exc), error_kind='recipe_dependencies')
                outcomes[action['id']] = {'action_id': action['id'], 'status': 'blocked',
                    'error': str(exc), 'sql_generations': generation}
                self._persist(task)
                continue
            outcome = self._query(task, {**query, **candidate, 'repair_action_id': action['id'],
                'input_result_ids': action['input_result_ids']}, reason,
                allowed_inputs=set(valid), generation_reserved=True)
            outcome = self._postprocess_query(task, action, candidate, outcome)
            outcomes[action['id']] = outcome
            task.logs.append({'stage': 'registered_recipe_executed', 'query_id': action['id'],
                'recipe_id': candidate['recipe']['recipe_id'], 'status': outcome.get('status'),
                'model_calls': 0})
            # Failed compiled SQL is an implementation/coverage defect, not a
            # license to silently invent a new definition. Expose the failure;
            # explicit repair_query retains the original allowance/semantics.
        pending_clarification = None
        method_clarifications = []
        for attempt in range(1, self._sql_limit(task) + 1):
            if not pending:
                break
            eligible = [(a, q) for a, q in pending if a.get('sql_generations', 0) < self._sql_limit(task, a)]
            if not eligible:
                break
            for action, _ in eligible:
                if budget:
                    budget.step = action['id']
                    if action.get('sql_generations'):
                        budget.repair('失败子集技术重写，不增加业务取证轮')
                action.update(sql_generations=action.get('sql_generations', 0) + 1,
                              model_generations=action.get('model_generations', action.get('sql_generations', 0)) + 1,
                              attempts=action.get('attempts', 0) + 1,
                              status='rewriting' if action.get('sql_generations', 0) else 'generating')
                parent_id = action.get('parent_query_id')
                seen_parents = set()
                while parent_id and parent_id not in seen_parents:
                    seen_parents.add(parent_id)
                    parent = next((a for a in task.actions if a['id'] == parent_id), None)
                    if not parent:
                        raise ValueError('查询修复父项不存在，不能创建独立额度')
                    parent.update(sql_generations=max(parent.get('sql_generations', 0), action['sql_generations']),
                                  attempts=max(parent.get('attempts', 0), action['attempts']))
                    parent_id = parent.get('parent_query_id')
            # Reservation committed before every remote call, including malformed
            # output, timeout and crashes. The module has no hidden retry loop.
            self._persist(task)
            repair = ({a['id']: {'sql': a.get('sql'), 'query_contract': a.get('query_contract'),
                                  'error': a.get('error'), 'error_kind': a.get('error_kind'),
                                  'diagnostics': a.get('repair_diagnostics') or a.get('last_generation_error'),
                                  'sql_generations': a['sql_generations']} for a, _ in eligible}
                      if attempt > 1 or any(a['sql_generations'] > 1 for a, _ in eligible) else None)
            try:
                module = module or self._sql_module()
                # A retry must shrink its context as well as its executable query list.
                if {a['id'] for a, _ in eligible} != {q.get('query_id') for q in context.get('normalized_requests', [])}:
                    context = self._prepare_sql_context(task, [q for _, q in eligible])
                generated = module.generate_batch([q for _, q in eligible], context,
                    model_call=lambda node, system, payload: self._call(task, node, system, payload), repair=repair)
            except BudgetStop as exc:
                for action, _ in eligible:
                    action.update(status='interrupted', error=str(exc), error_kind='budget')
                self._persist(task)
                raise
            except (RuntimeError, OSError) as exc:
                for action, _ in eligible:
                    not_sent = getattr(exc, 'request_sent', None) is False
                    action.update(status='failed' if not_sent else 'interrupted', error=str(exc),
                                  error_kind='model_not_sent' if not_sent else 'model_service',
                                  generation_delivery='not_sent' if not_sent else 'unknown')
                    outcomes[action['id']] = {'action_id': action['id'], 'status': action['status'],
                        'error_kind': action['error_kind'], 'error': str(exc),
                        'sql_generations': action['sql_generations'], 'repair_available': False}
                self._persist(task)
                if task.protocol_version < 4:
                    raise BudgetStop('SQL模型调用中断或服务故障，次数已保存；不把服务异常当SQL错误重写') from exc
                # Unknown provider output must never be replayed automatically.
                # Independent compiled results survive and other branches can
                # continue if the shared budget still allows a decision.
                task.logs.append({'stage': 'query_branch_interrupted', 'error': str(exc),
                                  'query_ids': [a['id'] for a, _ in eligible]})
                break
            candidates, errors = generated.get('candidates') or {}, generated.get('errors') or {}
            pending = []
            for action, query in eligible:
                candidate = candidates.get(action['id'])
                if candidate:
                    action['error_kind'] = None
                    action['execution_source'] = 'model_sql'
                    action['query_contract'] = candidate.get('query_contract')
                    action.setdefault('candidates', []).append({'generation': action['sql_generations'], **candidate})
                    outcome = self._query(task, {**query, **candidate, 'repair_action_id': action['id'],
                                               'input_result_ids': action['input_result_ids']}, reason,
                                          allowed_inputs=set(valid), generation_reserved=True)
                    outcome = self._postprocess_query(task, action, candidate, outcome)
                else:
                    error = errors.get(action['id']) or {'kind': 'format', 'message': 'SQL模块未返回绑定查询'}
                    action.update(status='failed', error=str(error.get('message') or error),
                                  error_kind=error.get('kind', 'format'))
                    action['last_generation_error'] = error
                    outcome = {'action_id': action['id'], 'status': 'failed', 'error': action['error'],
                               'sql_generations': action['sql_generations'], 'sql_generation_limit': self._sql_limit(task, action)}
                    if error.get('kind') == 'clarification':
                        from ..querying.sql_output_protocol import normalize_clarification
                        detail = error.get('clarification') or {}
                        detail = normalize_clarification({
                            'parameter': detail.get('parameter') or 'other',
                            'question': detail.get('question') or action['error'],
                            'reason': detail.get('reason') or '缺失条件会影响本次取数',
                            'options': detail.get('options') or []})
                        if task.protocol_version >= 4:
                            # The SQL module proposes an ambiguity; the business
                            # agent owns whether it is a user requirement or its
                            # own method choice. Do not automatically ask users
                            # to design an agent-selected drilldown denominator.
                            method_clarifications.append({'query_id': action['id'], 'clarification': detail})
                            action.update(status='needs_method_decision', awaiting_clarification=True)
                            outcome['status'] = 'needs_method_decision'
                        else:
                            pending_clarification = detail
                            action.update(status='waiting_clarification', awaiting_clarification=True)
                            outcome['status'] = 'waiting_clarification'
                outcomes[action['id']] = outcome
                retry_statuses = {'failed', 'no_progress'} if task.protocol_version >= 3 else {'failed'}
                if outcome.get('status') in retry_statuses and action.get('error_kind') not in {'storage', 'permission', 'capability', 'model_service', 'clarification'}:
                    pending.append((action, query))
            gaps = [(a, q) for a, q in pending if a.get('error_kind') == 'missing_context']
            if gaps and attempt == 1:
                before = _key({'schema': context.get('schema_graph'), 'metrics': context.get('registered_metrics')})
                try:
                    self._retrieve(task, {'goal': '；'.join(q['goal'] for _, q in gaps) +
                        '；SQL模块明确缺少的技术资料：' + json.dumps(
                            [a.get('last_generation_error') for a, _ in gaps], ensure_ascii=False),
                        'fields': [], 'metric_ids': []})
                    replacement_context = self._prepare_sql_context(task, [q for _, q in pending])
                    changed = before != _key({'schema': replacement_context.get('schema_graph'),
                                              'metrics': replacement_context.get('registered_metrics')})
                    if changed:
                        context = replacement_context
                except BudgetStop:
                    raise
                except (ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
                    changed = False
                    task.logs.append({'stage': 'sql_context_gap', 'error': str(exc), 'success': False})
                if not changed:
                    for action, _ in gaps:
                        action.update(status='blocked', error_kind='missing_context')
                        outcomes[action['id']].update(status='blocked', repair_available=False,
                            error='补查未取得新的必要资料，未对相同资料重复调用SQL模型：' + action['error'])
                    pending = [(a, q) for a, q in pending if a.get('error_kind') != 'missing_context']
            self._persist(task)
        task.observation = {'type': 'query_batch', 'batch_id': batch_id, 'queries': [*reused, *outcomes.values()]}
        if method_clarifications:
            task.observation.update(type='query_method_decision', method_clarifications=method_clarifications,
                instruction='判断歧义属于用户目标还是自行选择的调查方法。内部方法由主Agent依据登记口径选择并说明；只有真正影响用户目标且无法确定的条件才clarify用户。')
        if task.protocol_version >= 3:
            for action, _ in bound:
                if action.get('status') != 'completed':
                    action['generation_closed'] = action.get('sql_generations', 0) >= self._sql_limit(task, action)
                    for question in task.progress.get('core_questions') or []:
                        if question['id'] in action.get('question_ids', []) and question.get('status') in {'open', 'blocked'}:
                            question.update(status='blocked', reason=action.get('error') or '必要取证未完成',
                                            blocked_query_id=action['id'])
        if pending_clarification:
            task.clarification = pending_clarification
            task.final_status = 'waiting_clarification'
        task.logs.append({'stage': 'sql_batch_completed', **task.observation})
        self._persist(task)
