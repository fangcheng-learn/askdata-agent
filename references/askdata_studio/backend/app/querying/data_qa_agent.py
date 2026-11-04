from __future__ import annotations

from ..agent_configuration_runtime import effective_prompt, effective_skill
from ..prompt_defaults import INVESTIGATION_REPORT_SYSTEM, DATA_QA_SYSTEM

import json
import hashlib
import math
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any, Callable

from pydantic import ValidationError

from ..errors import PipelineStageError
from ..runtime_budget import BudgetStop
from ..runtime_budget import current_budget
from ..mcp_runtime import LocalMcpClient
from ..model_client import ModelClient
from ..models import AnalysisReport, ReportDataSource, VisualizationSpec
from ..skills import SkillDefinition
from .analysis_evidence import model_analysis_context
from .analysis_output import analysis_schema
from ..structured_output import using_output_schema
from contextlib import nullcontext


@dataclass
class DataQaResult:
    action: str
    answer: str
    report: AnalysisReport | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence_gaps: list[str] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


class ReportReferenceError(ValueError):
    """A local binding defect; never silently infer another task's reference."""


class ReportRevisionError(ValueError):
    """An invalid edit must be fixed before reviewing the resulting report."""


class DataQaAgent:
    """分析已有数据，并按需生成Markdown报告和图表配置。"""

    def __init__(
        self,
        model_client: ModelClient,
        mcp_client_factory: Callable[[dict[str, Any]], LocalMcpClient],
        skill: SkillDefinition,
    ) -> None:
        self.model_client = model_client
        self.mcp_client_factory = mcp_client_factory
        self.skill = skill

    def draft_investigation(
        self,
        query: str,
        context: dict[str, Any],
        access_scope: dict[str, Any],
        *,
        call_model: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> DataQaResult:
        """Compose the investigation draft in the existing draft call, without routing.

        The Controller has already read the evidence through authorized result
        references. Models choose presentation, never supply chart data or run SQL.
        """
        evidence = context.get("evidence") or []
        sources = self._investigation_sources(evidence, access_scope)
        budget = current_budget.get()
        instructions = effective_skill(self.skill.name, getattr(self.skill, 'default_instructions', '') or self.skill.instructions)
        if budget:
            budget.model_node = "draft"
            budget.skill(self.skill.name, instructions, "draft")
        system = effective_prompt('investigation_report', INVESTIGATION_REPORT_SYSTEM).replace('[[SKILL:data_qa]]', instructions)
        # Investigator-written summaries are not a second source of truth. In
        # particular, its self-assessed "supported" explanations can still be
        # wrong. The report role gets actual results and open questions, while
        # the original notes remain available to the investigator and reviewer.
        progress = context.get("progress") or {}
        report_context = {key: context[key] for key in (
            "task", "semantics", "data_capabilities", "delivery_policy", "unsuccessful_attempts",
            "unsuccessful_attempts_policy", "investigation_checks", "previous_report",
            "source_tasks", "prior_reports", "prior_user_messages", "user_feedback",
            "report_reference_closure", "trust_boundary", "inherited_evidence_gaps",
            "source_gaps", "data_gaps") if key in context}
        report_context["progress"] = {
            "core_questions": [{"question": question["question"]}
                for question in progress.get("core_questions") or [] if question.get("question")],
            "unresolved_questions": progress.get("unresolved_questions") or []}
        report_context['investigation_claims'] = {
            category: [{**{k: v for k, v in note.items() if k != 'status'},
                        'investigator_assessment': note.get('status', 'unassessed')}
                       for note in progress.get(category, []) if note.get('status') not in
                       {'retracted', 'invalidated', 'superseded', 'removed'}]
            for category in ('facts', 'hypotheses', 'explanations')}
        report_context['claims_policy'] = ('调查判断不是已认证事实，investigator_assessment仅标识调查中的支持／反驳／未知判断，须与真实结果核对。相关正常检查、反证及未查明事项也必须交代；'
            '不要从头另编归因故事，也不要直接照抄调查解释。')
        from .report_reading import compact_reading_view, reading_view
        # Incomplete previews already appear in evidence. Repeating their rows
        # as formatted numbers adds no comparison and crowds out full results.
        report_context['numeric_reading_view'] = compact_reading_view(reading_view(
            [source for source in sources if source.complete]))
        report_context["evidence"] = [{**entry, "query_contract": {
            key: value for key, value in (entry.get("query_contract") or {}).items() if key != "validation"
        }} for entry in evidence]
        for source, projected in zip(evidence, report_context["evidence"]):
            checks = ((source.get("query_contract") or {}).get("validation") or {}).get("pending_checks")
            if checks:
                projected["query_contract"]["pending_checks"] = checks
        # Only an active revision carries review instructions. Old feedback and
        # result-memory summaries can contain superseded, unsupported assertions.
        if (context.get("last_observation") or {}).get("type") == "delivery_repair":
            report_context["revision_feedback"] = context.get("feedback")
        from ..workflows.investigation_output import InvestigationReport
        payload = {
            "mode": "investigation_report",
            "audience": "运营负责人；需要当前判断、重点变化、解释和行动优先级，而非执行与审计明细",
            "query": query,
            "available_data_sources": [source.model_dump(mode="json", exclude={"rows"}) for source in sources],
            "output_protocol": {"report_schema": InvestigationReport.model_json_schema(),
                                "visualization_schema": VisualizationSpec.model_json_schema(),
                                "valid_citations": [f"[{key}]" for key in self._citation_aliases(sources)],
                                "citation_bindings": self._citation_aliases(sources),
                                "presentation_guidance": {
                                    "comparison": "先写指标上升/下降X%（前值 → 后值）；数值取numeric_reading_view的change_rate、base、current，不从旧稿抄变化率。",
                                    "table": "主表使用前期、本期、变化幅度；比率不再另列百分点差，金额与数量按需补差额；单位写列标题或指标名。",
                                    "emphasis": "加粗关键完整方向短语，如 **上升8.6%**；关键绝对值可加粗。不要生成HTML颜色，前端上升绿、下降红。",
                                    "coverage": "先查看已有比较中的上升、下降和稳定项，再选择重要发现；若存在会抵消总体下降的上升项，必须一并解释其作用，不能只摘录负向指标。",
                                    "prior_report": "旧稿仅帮助理解用户所指，不是本次格式和事实模板。用户要求改表达时重新写受影响段落与表格，按当前证据纠正旧稿，不仅给旧文加粗。",
                                },
                                "instructions": "按report_schema先组织overview、analysis_points和action_items；观点选择表或图，解释紧随其后，不重复数字。比较优先变化率加前后值，强调完整方向短语；比率主表不默认另列百分点差，单位写表头或指标名。修订时按本次要求重新组织受影响的段落和表格，旧稿不是格式模板。evidence_ids使用citation_bindings中的ID键，不加方括号，程序绑定完整ID；多任务重名时用完整ID。正文可用[R1]短引用。图表只能绑定真实字段，不抄写数据；没有合适图表时使用表格或文字。省略source_version由服务端绑定。"},
            "context": model_analysis_context(report_context),
        }
        previous = context.get('previous_report') or {}
        revision = bool(report_context.get('revision_feedback') and previous.get('markdown'))
        if revision:
            payload['mode'] = 'report_revision'
            payload['output_protocol']['revision_instructions'] = (
                '本次只返回接口要求的revise_report和markdown_edits，不重写整份报告。'
                '每个before必须原样引用previous_report.markdown中唯一匹配的片段，after是修订文字，可为空表示删除。'
                '各片段不得重叠，保留所有未受影响内容；检查标题、摘要、建议中相同错误，不能只改一处。'
                '已有数值正确时只降低或删除无证据断言，保留关联、正常项、反证和必要未决事项。'
                '不得照抄审核心算的数字；以真实结果和numeric_reading_view为准。'
                'title、visualizations、evidence_gaps无需变化时返回null。至少有一项实质修改。')
            system += '\n本次为已有报告局部修订，输出协议以report_revision及本次JSON Schema为准。'
        for attempt in range(2):
            response = (call_model(system, payload) if call_model else
                        self.model_client.chat_json(system, json.dumps(payload, ensure_ascii=False)))
            try:
                candidate = self._apply_report_revision(previous, response) if revision else response
                from .report_composition import compose_report
                candidate = compose_report(candidate)
                built = self._build_investigation_report(candidate, evidence, access_scope)
                return self._repair_investigation_charts(candidate, built, evidence, access_scope, call_model)
            except (ReportReferenceError, ReportRevisionError) as exc:
                if attempt:
                    raise
                # Binding repair is local, bounded, and carries the actual previous draft.
                payload = {**payload, 'reference_repair': {
                    'error': str(exc), 'previous_output': response,
                    'instruction': ('修正刚才的局部修订格式、匹配位置或引用，仍返回revise_report；所有before仍以previous_report原稿为准。'
                                    if revision else '只修正明确无效的引用和相应图表来源，保留正文、数字及正确内容；不能猜配来源，依据提供的证据核对。')}}
        raise AssertionError('unreachable')

    def _repair_investigation_charts(self, candidate, built, evidence, access_scope, call_model):
        """One bounded presentation-only repair. Never block an otherwise usable report."""
        failures = [item for item in built.diagnostics if item.get('stage') == 'chart_validation'
                    and item.get('chart_index', 0) > 0]
        if not failures:
            return built
        from ..workflows.investigation_output import ReportChartRepair
        raw = candidate.get('report') if isinstance(candidate.get('report'), dict) else candidate
        aliases = self._citation_aliases(built.report.data_sources)
        failed_ids = {aliases.get(item.get('source_result_id'), item.get('source_result_id')) for item in failures}
        payload = {'mode': 'chart_repair', 'failed_charts': failures,
                   'available_data_sources': [{**s.model_dump(mode='json', exclude={'rows'}),
                       'row_preview': s.rows[:20] if s.result_id in failed_ids else [],
                       'preview_truncated': len(s.rows)>20} for s in built.report.data_sources],
                   'output_protocol': ReportChartRepair.model_json_schema()}
        system = ('只修复报告中失败的图表配置，返回repairs数组，每项chart_index与visualization。'
                  '不改正文、不生成数值、不调用SQL。使用真实来源、字段、期间和粒度。'
                  'grouped_bar必须指定series_field；若类别已经唯一且只有一个系列，改用bar。'
                  '多期分类比较用实际期间列作series_field。不得把不同月份混成同一类别。'
                  '只处理failed_charts中的序号；确实没有适用数据时visualization=null。')
        try:
            response = call_model(system, payload) if call_model else self.model_client.chat_json(system, json.dumps(payload, ensure_ascii=False))
            repair = ReportChartRepair.model_validate(response)
            allowed = {f['chart_index'] for f in failures}
            updates = {item.chart_index: item.visualization for item in repair.repairs}
            if len(updates) != len(repair.repairs) or not set(updates) <= allowed:
                raise ValueError('图表修复只能引用失败图表的唯一序号')
            charts = [{**updates[i].model_dump(mode='json'), 'section_heading': chart.get('section_heading')} if i in updates else chart
                      for i, chart in enumerate(raw.get('visualizations') or [], 1)
                      if i not in updates or updates[i] is not None]
            fixed = self._build_investigation_report({**raw, 'visualizations': charts}, evidence, access_scope)
            fixed.diagnostics = [*built.diagnostics, {'stage': 'chart_repair', 'attempts': 1,
                'updated_indices': sorted(updates), 'remaining_invalid': len(fixed.diagnostics)}, *fixed.diagnostics]
            return fixed
        except (ValueError, RuntimeError, TimeoutError, OSError) as exc:
            built.diagnostics.append({'stage': 'chart_repair', 'attempts': 1, 'status': 'unavailable',
                                      'error': str(exc)[:500]})
            return built

    @staticmethod
    def _apply_report_revision(previous: dict, response: dict) -> dict:
        """Apply Agent-authored edits atomically against the original draft."""
        from ..workflows.investigation_output import ReportRevision
        try:
            patch = ReportRevision.model_validate(response)
        except ValidationError as exc:
            raise ReportRevisionError(f'修订格式错误：{exc}') from exc
        original = previous['markdown']
        ranges = []
        for edit in patch.markdown_edits:
            if original.count(edit.before) != 1:
                raise ReportRevisionError('修订before必须在原稿中唯一匹配；请复制足够上下文，不能猜测原句')
            start = original.index(edit.before)
            ranges.append((start, start + len(edit.before), edit.after))
        ranges.sort()
        if any(a[1] > b[0] for a, b in zip(ranges, ranges[1:])):
            raise ReportRevisionError('修订片段重叠，请合并为一处修改')
        candidate = {**previous}
        markdown = original
        for start, end, replacement in reversed(ranges):
            markdown = markdown[:start] + replacement + markdown[end:]
        if not markdown.strip():
            raise ReportRevisionError('不能删除全部报告；保留可独立成立的可靠发现')
        candidate['markdown'] = markdown
        for key in ('title', 'visualizations', 'evidence_gaps'):
            value = getattr(patch, key)
            if value is not None:
                candidate[key] = ([v.model_dump(mode='json') for v in value]
                                  if key == 'visualizations' else value)
        if all(candidate.get(key) == previous.get(key) for key in ('markdown', 'title', 'visualizations')):
            raise ReportRevisionError('报告没有实质修改；请针对审核指出的错误修改具体片段')
        return candidate

    @staticmethod
    def _citation_aliases(sources):
        aliases = {s.result_id: s.result_id for s in sources}
        for source in sources:
            short = source.result_id.rsplit(':', 1)[-1]
            if sum(s.result_id.rsplit(':', 1)[-1] == short for s in sources) == 1:
                aliases[short] = source.result_id
        return aliases

    def _build_investigation_report(self, response: Any, evidence: list[dict],
                                    access_scope: dict[str, Any]) -> DataQaResult:
        """Use the same evidence/version/chart checks for initial and follow-up reports."""
        sources = self._investigation_sources(evidence, access_scope)
        if not isinstance(response, dict):
            raise ValueError("调查报告草稿必须是JSON对象")
        raw = response.get("report") if isinstance(response.get("report"), dict) else response
        markdown = str(raw.get("markdown") or raw.get("draft") or "").strip()
        if not markdown:
            raise ValueError("调查报告草稿缺少markdown")
        aliases = self._citation_aliases(sources)
        def bind(match):
            rid, version = match.group(1), match.group(2)
            return '[' + aliases.get(rid, rid) + ('@' + version if version else '') + ']'
        markdown = re.sub(r"\[([A-Za-z0-9_.:-]*R\d+)(?:@([^\]\s]+))?\]", bind, markdown)
        title = str(raw.get("title") or "经营调查报告").strip()
        charts = raw.get("visualizations") or []
        report = AnalysisReport(title=title, markdown=markdown, data_sources=sources)
        warnings: list[str] = []
        diagnostics: list[dict[str, Any]] = []
        if not isinstance(charts, list):
            diagnostics.append(self._chart_diagnostic(0, charts, ValueError('visualizations必须为数组')))
            charts = []
            warnings.append("图表规格不是数组，已省略图表。")
        for index, chart in enumerate(charts, start=1):
            try:
                candidate_spec = dict(chart) if isinstance(chart, dict) else chart
                if isinstance(candidate_spec, dict):
                    ref = candidate_spec.get('source_result_id')
                    candidate_spec['source_result_id'] = aliases.get(ref, ref)
                    exact_source = next((item for item in sources if item.result_id == candidate_spec.get('source_result_id')), None)
                    if exact_source and ('source_version' not in candidate_spec or candidate_spec['source_version'] is None):
                        candidate_spec['source_version'] = exact_source.version
                spec = VisualizationSpec.model_validate(candidate_spec)
                source = next((item for item in sources if item.result_id == spec.source_result_id), None)
                if source and not source.complete:
                    warnings.append(f"{source.result_id}仅提供部分结果，已省略图表：{spec.title}")
                    continue
                candidate = report.model_copy(deep=True)
                candidate.visualizations = [spec]
                checked = self.validate_investigation_report(candidate, evidence, access_scope)
                report.visualizations.append(checked.visualizations[0])
            except (ValueError, TypeError) as exc:
                # A chart is optional presentation. Preserve the usable text
                # and other valid charts, without extra model/repair requests.
                diagnostic = self._chart_diagnostic(index, chart, exc)
                diagnostics.append(diagnostic)
                error = diagnostic['errors'][0]
                location = '.'.join(str(part) for part in error['loc'])
                reason = (location + '：' if location else '') + error['msg']
                warnings.append(f"第{index}张图表校验未通过，已省略：{reason}")
        gaps = raw.get("evidence_gaps") or []
        if not isinstance(gaps, list):
            raise ValueError("evidence_gaps必须是数组")
        report.warnings = list(dict.fromkeys(warnings))
        report = self.validate_investigation_report(report, evidence, access_scope)
        return DataQaResult(action="report", answer=str(raw.get("answer") or title),
                            report=report, warnings=list(report.warnings),
                            evidence_gaps=[str(item) for item in gaps if item], diagnostics=diagnostics)

    @staticmethod
    def _chart_diagnostic(index: int, candidate: Any, error: Exception) -> dict[str, Any]:
        """Retain bounded provider output and actionable errors in private audit."""
        raw = json.dumps(candidate, ensure_ascii=False, default=str)
        errors = ([{'loc': tuple(str(part)[:256] if isinstance(part, str) else part for part in item.get('loc', ())[:12]),
                    'type': str(item.get('type') or '')[:100], 'msg': str(item.get('msg') or '')[:500]}
                   for item in error.errors(include_url=False, include_input=False)[:12]]
                  if isinstance(error, ValidationError) else
                  [{'loc': [], 'type': type(error).__name__, 'msg': str(error)[:500]}])
        return {'stage': 'chart_validation', 'chart_index': index,
                'source_result_id': str(candidate.get('source_result_id'))[:256] if isinstance(candidate, dict) and candidate.get('source_result_id') is not None else None,
                'candidate_json': raw[:4096], 'candidate_truncated': len(raw) > 4096, 'errors': errors}

    @staticmethod
    def _investigation_sources(evidence: list[dict[str, Any]], access_scope: dict[str, Any]) -> list[ReportDataSource]:
        sources: list[ReportDataSource] = []
        seen: set[str] = set()
        for item in evidence:
            if not isinstance(item, dict):
                raise ValueError("调查证据必须是对象")
            rid = str(item.get("id") or item.get("result_id") or "")
            if not rid or rid in seen:
                raise ValueError("调查结果编号缺失或重复")
            seen.add(rid)
            if item.get("check_status") in {"superseded", "failed", "blocked", "invalid"}:
                raise ValueError(f"报告不能引用失效证据：{rid}")
            authorized = access_scope.get('authorized_analysis_sources')
            binding = authorized.get(rid) if isinstance(authorized, dict) else None
            if isinstance(authorized, dict) and not isinstance(binding, dict):
                raise PermissionError(f'报告结果不属于本轮授权证据：{rid}')
            for key in ("user_id", "task_id", "datasource_id"):
                if binding is not None:
                    if binding.get(key) is None or item.get(key) != binding[key]:
                        raise PermissionError(f'报告原证据{key}与授权绑定不一致：{rid}')
                    if key != 'task_id' and access_scope.get(key) is not None and item.get(key) != access_scope[key]:
                        raise PermissionError(f'报告证据{key}超出当前范围：{rid}')
                    continue
                expected = access_scope.get(key)
                actual = item.get(key)
                if expected is not None and actual is not None and str(actual) != str(expected):
                    raise PermissionError(f"报告证据{key}超出授权范围：{rid}")
            expected_snapshot = access_scope.get("snapshot") or access_scope.get("scenario")
            actual_snapshot = item.get("snapshot") or item.get("scenario")
            if binding is not None and ((actual_snapshot or {}) != (binding.get('snapshot') or {})
                                        or (actual_snapshot or {}) != (expected_snapshot or {})):
                raise PermissionError(f'报告原证据快照与授权绑定不一致：{rid}')
            if expected_snapshot and actual_snapshot and expected_snapshot != actual_snapshot:
                raise PermissionError(f"报告证据快照超出授权范围：{rid}")
            rows = list(item.get("rows") or [])
            columns = [str(column) for column in item.get("columns") or []]
            if any(not isinstance(row, dict) or any(key not in columns for key in row) for row in rows):
                raise ValueError(f"报告证据列与数据不一致：{rid}")
            total = item.get("total_rows")
            stored = int(item.get("stored_rows", len(rows)))
            complete = (bool(item.get("complete")) and not item.get("display_truncated")
                        and len(rows) >= stored and (total is None or len(rows) >= int(total)))
            version = str(item.get("version") or item.get("result_version") or item.get("fingerprint") or "")
            if not version:
                identity = {key: item.get(key) for key in ("id", "result_id", "task_id", "datasource_id", "sql", "query_contract", "columns", "rows")}
                version = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
            if binding is not None and version != binding.get('version'):
                raise PermissionError(f'报告原证据版本与授权绑定不一致：{rid}')
            metadata = item.get("column_metadata") or {}
            units = dict(item.get("units") or {})
            currencies = dict(item.get("currencies") or {})
            precisions = dict(item.get("precisions") or {})
            if isinstance(metadata, dict):
                for column, definition in metadata.items():
                    if column not in columns or not isinstance(definition, dict):
                        continue
                    if definition.get("unit"):
                        units[column] = str(definition["unit"])
                    if definition.get("currency"):
                        currencies[column] = str(definition["currency"])
                    if definition.get("precision") is not None:
                        precisions[column] = int(definition["precision"])
            sources.append(ReportDataSource(
                result_id=rid, version=version, task_id=str(item.get("task_id") or access_scope.get("task_id") or ""),
                datasource_id=str(item.get("datasource_id") or access_scope.get("datasource_id") or ""),
                title=str(item.get("goal") or item.get("title") or rid), columns=columns, rows=rows,
                complete=complete, provided_rows=len(rows), total_rows=int(total) if total is not None else None,
                units={str(k): str(v) for k, v in units.items() if k in columns},
                currencies={str(k): str(v) for k, v in currencies.items() if k in columns},
                precisions={str(k): int(v) for k, v in precisions.items() if k in columns},
            ))
        return sources

    @classmethod
    def validate_investigation_report(
        cls, report: AnalysisReport | dict[str, Any], evidence: list[dict[str, Any]], access_scope: dict[str, Any],
    ) -> AnalysisReport:
        """Check facts code can prove; business/text claims remain the final reviewer's job."""
        output = AnalysisReport.model_validate(report).model_copy(deep=True)
        if not output.title.strip() or not output.markdown.strip():
            raise ValueError("调查报告缺少标题或正文")
        sources = {item.result_id: item for item in cls._investigation_sources(evidence, access_scope)}
        supplied = {item.result_id: item for item in output.data_sources}
        if len(supplied) != len(output.data_sources) or set(supplied) != set(sources):
            raise ValueError("报告数据源与授权调查证据不一致")
        for rid, source in sources.items():
            if supplied[rid].model_dump(mode="json") != source.model_dump(mode="json"):
                raise ValueError(f"报告证据版本或数据发生变化：{rid}")
        for rid, version in re.findall(r"\[([A-Za-z0-9_.:-]*R\d+)(?:@([^\]\s]+))?\]", output.markdown):
            if rid not in sources:
                raise ReportReferenceError(f"报告引用了不存在的结果：{rid}；只允许明确提供的引用，不猜配任务前缀")
            if version and version != sources[rid].version:
                raise ReportReferenceError(f"报告引用了过期结果版本：{rid}")
        for spec in output.visualizations:
            source = sources.get(str(spec.source_result_id or ""))
            if source is None or spec.source_version != source.version:
                raise ValueError("调查图表必须绑定可用的结果编号及准确版本")
            if spec.source_task_id and source.task_id and spec.source_task_id != source.task_id:
                raise ValueError("调查图表任务与结果来源不一致")
            spec.source_task_id = source.task_id
            if not source.complete:
                raise ValueError(f"图表不能把结果预览当完整数据：{source.result_id}")
            if spec.category_field not in source.columns or spec.value_field not in source.columns:
                raise ValueError("图表引用了不存在的结果字段")
            if spec.series_field and spec.series_field not in source.columns:
                raise ValueError("图表系列引用了不存在的结果字段")
            numeric: list[float] = []
            for row in source.rows:
                if row.get(spec.category_field) is None or (spec.series_field and row.get(spec.series_field) is None):
                    raise ValueError("图表类别或系列字段缺失")
                value = row.get(spec.value_field)
                if isinstance(value, bool):
                    raise ValueError("图表数值列不能使用布尔值")
                try:
                    number = float(value)
                except (ValueError, TypeError):
                    raise ValueError("图表数值字段包含非数值或空值") from None
                if not math.isfinite(number):
                    raise ValueError("图表数值字段包含非有限值")
                numeric.append(number)
            if spec.type == "grouped_bar" and not spec.series_field:
                raise ValueError("分组柱状图需要实际series_field")
            if spec.type in {"grouped_bar", "line", "area"} or (spec.type == "bar" and spec.series_field):
                pairs = [(str(row[spec.category_field]), str(row.get(spec.series_field, ""))) for row in source.rows]
                if len(set(pairs)) != len(pairs):
                    raise ValueError("图表类别与系列不唯一，请指定区分期间或组别的series_field；不自动求和或平均")
                if len({pair[1] for pair in pairs}) > 12:
                    raise ValueError("图表系列过多，请选择更合适的已有汇总结果")
            if spec.type in {"line", "area"} and spec.order == "value_desc":
                spec.order = "category_asc"
            if spec.type == "scatter":
                if not spec.x_field or spec.x_field not in source.columns:
                    raise ValueError("散点图需要实际数值列x_field")
                for row in source.rows:
                    value = row.get(spec.x_field)
                    if isinstance(value, bool) or value is None or not math.isfinite(float(value)):
                        raise ValueError("散点图横轴包含非数值或非有限值")
            if spec.type in {"pie", "donut"} and (any(value < 0 for value in numeric) or len(numeric) > spec.max_items):
                raise ValueError("饼图不能隐藏负值或截取部分分类后表达总体占比")
            for attribute, mapping in (("unit", source.units), ("currency", source.currencies)):
                expected = mapping.get(spec.value_field)
                # Values always come from the frozen source. Bind its raw unit;
                # display conversion is deterministic and never model-specified.
                setattr(spec, attribute, expected)
            if spec.value_field in source.precisions:
                spec.precision = source.precisions[spec.value_field]
        return output

    def _report_from_existing(self, query: str, contexts: dict, access_scope: dict) -> DataQaResult:
        from .report_continuation import continuation_context
        from ..workflows.investigation_output import output_schema
        lineage = continuation_context(query, contexts, str(access_scope.get('task_id') or ''))
        # The current query is already at payload.query. Historical feedback is
        # transmitted once, alongside the selected prior report and real rows.
        previous_reports = [{k: v for k, v in report.items() if k != 'continuation_context'}
                            for report in contexts.get('prior_reports') or []]
        context = {**contexts, 'task': {'original_questions': lineage['original_questions']},
                   'prior_reports': previous_reports,
                   'user_feedback': {'source': 'user', 'database_verified': False,
                       'history': lineage['user_messages'][:-1],
                       'current_request_location': 'payload.query'}}
        context.pop('prior_user_messages', None)
        columns = {item['id']: item.get('columns') or [] for item in contexts['evidence']}

        def generate(system, payload):
            node = 'chart_repair' if payload.get('mode') == 'chart_repair' else 'draft'
            with using_output_schema('existing_evidence_report', output_schema(node, result_columns=columns)):
                return self.model_client.chat_json(system, json.dumps(payload, ensure_ascii=False))

        try:
            result = self.draft_investigation(query, context, access_scope,
                                             call_model=generate)
            # Old investigative questions may be resolved by the user's new
            # background. Only actual unreadable/missing sources carry over
            # mechanically; the Agent reassesses the old business gaps.
            inherited = [str(item) for item in contexts.get('data_gaps') or []]
            result.evidence_gaps = list(dict.fromkeys([*inherited, *result.evidence_gaps]))
            result.warnings = list(dict.fromkeys([*result.warnings, *contexts.get('source_gaps', [])]))
            if result.report:
                result.report.continuation_context = lineage
                result.report.warnings = list(dict.fromkeys([*result.report.warnings, *inherited]))
            return result
        except (BudgetStop, PipelineStageError):
            raise
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            raise PipelineStageError('qa_answer', str(exc)) from exc

    def run(
        self,
        query: str,
        contexts: dict[str, Any],
        access_scope: dict[str, Any],
    ) -> DataQaResult:
        budget = current_budget.get()
        if budget:
            budget.model_node = "draft" if budget.phase == "closing" else "data_qa"
        metadata = contexts.get('mode') == 'metadata'
        managed = isinstance(contexts.get('evidence'), list)
        if managed and contexts.get('analysis_mode') == 'report':
            return self._report_from_existing(query, contexts, access_scope)
        client = None if managed or metadata else self.mcp_client_factory(access_scope)
        tools = [] if managed or metadata else [
            tool for tool in client.list_tools()
            if self._tool_allowed(str(tool.get("name") or ""))
        ]
        source_catalog = self._source_catalog(contexts)
        instructions = effective_skill(self.skill.name, getattr(self.skill, 'default_instructions', '') or self.skill.instructions)
        system = (effective_prompt('data_qa', DATA_QA_SYSTEM)
                  .replace('[[DISPLAY_TOOLS]]', json.dumps(tools, ensure_ascii=False))
                  .replace('[[SKILL:data_qa]]', instructions))
        if metadata:
            system += ('\n当前是元数据问答。authorized_metadata是按当前数据源及用户权限读取的实际目录，'
                       '不是经营查询结果。根据原问题选择计数、列举、解释或总结，直接回答所问，'
                       '不要把完整字段目录机械贴出，不需要经营期间或调查。'
                       '只用目录内的真实表和字段，不猜实际业务数据。返回action=answer、answer和空tool_calls。')
        if managed:
            system += ('\n本轮使用服务端按原任务授权读取的多份真实证据。只解释或整理已提供数据，不运行SQL或重新调查。'
                       '不同R即使task_id相同也必须分别使用；方括号引用完整result_id，版本可用[result_id@version]。'
                       'complete=false或display_truncated=true是预览，不能计算全集或把缺失当零。'
                       '普通追问仍输出action=answer；用户要求报告、补充报告或图表时输出action=report，'
                       '使用title、markdown、visualizations数组、evidence_gaps数组，不输出tool_calls。'
                       'visualizations只使用实际VisualizationSpec，数据由服务端绑定，不抄数据；'
                       'source_result_id必须是可用完整R编号，source_version可省略，不能以相同task_id混用R。'
                       '原调查状态始终保留在source_tasks；整理原报告不能冒充补齐调查。'
                       '若本次只要求解释已知指标，按本次问题判断是否回答完整，不把原任务其他未解问题算成本次缺口；'
                       '本次问题仍缺数据时必须写入evidence_gaps。'
                       '用户要求新增期间、范围或原证据没有的数时，明确尚需取证，不能猜数。')
        if managed and contexts.get('analysis_mode') == 'fact':
            system += ('\n本轮仅回答数值事实：说明适用范围、基期和本期数值、所问差额/比例及真实R引用即可。'
                       '不要生成原因、机制、建议或SKU归因段落；这些不是当前请求。')
        if contexts.get("business_methods"):
            system += "\n当前选中的业务Skill规则（必须遵守）：\n" + str(contexts["business_methods"])
        if budget:
            budget.skill(self.skill.name, instructions, budget.model_node)
            methods = contexts.get('business_methods') or '{}'
            if isinstance(methods, str):
                try:
                    methods = json.loads(methods)
                except (ValueError, TypeError):
                    methods = {}
            if isinstance(methods, dict):
                for name, instructions in methods.items():
                    if isinstance(instructions, str):
                        budget.skill(name, instructions, budget.model_node)
        user = json.dumps(
            {
                "query": query,
                "available_data_sources": list(source_catalog.values()),
                "context": model_analysis_context(contexts) if managed else contexts,
            },
            ensure_ascii=False,
        )
        try:
            schema = analysis_schema() if managed else None
            if managed and contexts.get('analysis_mode') == 'report':
                from ..workflows.investigation_output import output_schema
                schema = output_schema('draft', result_columns={
                    rid: item['columns'] for rid, item in source_catalog.items()})
            with using_output_schema('existing_evidence_analysis', schema) if managed else nullcontext():
                payload = self.model_client.chat_json(system, user)
            if managed:
                evidence = contexts['evidence']
                if str(payload.get('action') or '') == 'report':
                    # Old configured prompts may still emit display tool specs.
                    # Adapt those specs locally; do not invoke a second model or MCP.
                    adapted = dict(payload)
                    adapter_diagnostics = []
                    if 'visualizations' not in adapted and adapted.get('tool_calls'):
                        charts = []
                        for index, call in enumerate(adapted['tool_calls'], start=1):
                            try:
                                if not isinstance(call, dict):
                                    raise ValueError('图表规格必须是对象')
                                arguments = dict(call.get('arguments') or {})
                                name = call.get('name')
                                if name not in {'build_bar_chart', 'build_pie_chart'}:
                                    raise ValueError('已有证据报告只接受图表规格，不执行工具')
                                self._validate_chart_source(arguments, source_catalog)
                                if not arguments.get('source_result_id'):
                                    matches = [item for item in source_catalog.values() if item['task_id'] == arguments.get('source_task_id')]
                                    if len(matches) != 1:
                                        raise ValueError('同任务多个结果必须指定source_result_id')
                                    arguments['source_result_id'] = matches[0]['result_id']
                                charts.append({'type': 'bar' if name == 'build_bar_chart' else 'pie', **arguments})
                            except (ValueError, TypeError) as exc:
                                adapter_diagnostics.append(self._chart_diagnostic(index, call, exc))
                        adapted['visualizations'] = charts
                    result = self._build_investigation_report(adapted, evidence, access_scope)
                    for diagnostic in adapter_diagnostics:
                        result.diagnostics.append(diagnostic)
                        result.warnings.append('已省略无法绑定唯一结果的旧版图表规格：' + diagnostic['errors'][0]['msg'])
                    result.report.warnings = list(dict.fromkeys([*result.report.warnings, *result.warnings]))
                else:
                    result = self._execute(payload, client, source_catalog)
                    sources = self._investigation_sources(evidence, access_scope)
                    self.validate_investigation_report(AnalysisReport(title='已有结果追问', markdown=result.answer,
                                                                     data_sources=sources), evidence, access_scope)
                if result.action == 'answer':
                    own_gaps = payload.get('evidence_gaps') or []
                    if not isinstance(own_gaps, list):
                        raise ValueError('evidence_gaps必须是数组')
                    result.evidence_gaps = [str(item) for item in own_gaps if item]
                inherited = [str(item) for item in contexts.get(
                    'inherited_evidence_gaps' if result.report else 'data_gaps') or []]
                result.warnings = list(dict.fromkeys([*result.warnings, *contexts.get('source_gaps', [])]))
                result.evidence_gaps = list(dict.fromkeys([*inherited, *result.evidence_gaps]))
                if result.report and inherited:
                    result.report.warnings = list(dict.fromkeys([*result.report.warnings, *inherited]))
                return result
            if metadata and payload.get('action') != 'answer':
                raise ValueError('元数据问答应返回Agent回答，不创建经营报告或调用展示工具')
            return self._execute(payload, client, source_catalog)
        except (BudgetStop, PipelineStageError):
            raise
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            raise PipelineStageError("qa_answer", str(exc)) from exc

    def _execute(
        self,
        payload: dict[str, Any],
        client: LocalMcpClient,
        source_catalog: dict[str, dict[str, Any]],
    ) -> DataQaResult:
        action = str(payload.get("action") or "")
        if action not in self.skill.output_actions:
            raise ValueError(f"问答Skill不支持输出动作：{action or '空'}")

        answer = str(payload.get("answer") or "").strip()
        raw_calls = payload.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ValueError("tool_calls必须是数组")

        if action == "answer":
            if raw_calls:
                raise ValueError("普通回答不能调用前端展示工具")
            if not answer:
                raise ValueError("普通回答缺少answer")
            return DataQaResult(action="answer", answer=answer)

        if len(raw_calls) > self.skill.max_tool_calls:
            raise ValueError(f"报告最多调用{self.skill.max_tool_calls}个展示工具")

        title = str(payload.get("title") or "").strip()
        markdown = str(payload.get("markdown") or "").strip()
        if not title or not markdown:
            raise ValueError("分析报告缺少title或markdown")

        traces: list[dict[str, Any]] = []
        visualizations: list[VisualizationSpec] = []
        for index, raw_call in enumerate(raw_calls, start=1):
            if not isinstance(raw_call, dict):
                raise ValueError("展示工具调用必须是对象")
            name = str(raw_call.get("name") or "")
            arguments = raw_call.get("arguments") or {}
            if not self._tool_allowed(name) or not isinstance(arguments, dict):
                raise ValueError(f"展示工具调用不合法：{name or '空'}")
            self._validate_chart_source(arguments, source_catalog)
            result = client.call_tool(name, arguments)
            visualization = VisualizationSpec.model_validate(result)
            visualizations.append(visualization)
            traces.append({
                "call_index": index,
                "tool": name,
                "arguments": arguments,
                "result": visualization.model_dump(mode="json"),
            })

        report = AnalysisReport(
            title=title,
            markdown=markdown,
            visualizations=visualizations,
        )
        return DataQaResult(
            action="report",
            answer=answer or title,
            report=report,
            tool_calls=traces,
        )

    def _tool_allowed(self, name: str) -> bool:
        return any(fnmatch(name, pattern) for pattern in self.skill.allowed_tools)

    @staticmethod
    def _source_catalog(contexts: dict[str, Any]) -> dict[str, dict[str, Any]]:
        catalog: dict[str, dict[str, Any]] = {}
        for key in ("evidence", "recent_result", "selected_tables"):
            raw = contexts.get(key) or ""
            if not raw:
                continue
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                continue
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if not isinstance(item, dict) or not item.get("task_id"):
                    continue
                nested = item.get('evidence')
                if isinstance(nested, list) and nested:
                    nested_catalog = DataQaAgent._source_catalog({'evidence': nested})
                    catalog.update(nested_catalog)
                    continue
                task_id = str(item["task_id"])
                rid = str(item.get('id') or item.get('result_id') or task_id)
                catalog[rid] = {
                    "task_id": task_id,
                    "result_id": rid, "version": item.get('version') or item.get('fingerprint'),
                    "complete": bool(item.get('complete')) and not item.get('display_truncated'),
                    "title": str(item.get("title") or "查询结果"),
                    "query": str(item.get("query") or ""),
                    "columns": [str(column) for column in item.get("columns") or []],
                    "row_count": int(item.get("row_count") or len(item.get("rows") or [])),
                }
        return catalog

    @staticmethod
    def _validate_chart_source(
        arguments: dict[str, Any],
        source_catalog: dict[str, dict[str, Any]],
    ) -> None:
        task_id = str(arguments.get("source_task_id") or "")
        rid = str(arguments.get('source_result_id') or '')
        source = source_catalog.get(rid) if rid else None
        if not rid:
            matches = [item for item in source_catalog.values() if item['task_id'] == task_id]
            source = matches[0] if len(matches) == 1 else None
        if not source:
            raise ValueError(f"图表来源不可用或同任务多个结果需指定source_result_id：{rid or task_id or '空'}")
        if task_id and task_id != source['task_id']:
            raise ValueError('图表任务与结果编号不一致')
        columns = set(source["columns"])
        category = str(arguments.get("category_field") or "")
        value = str(arguments.get("value_field") or "")
        missing = [field for field in (category, value) if field not in columns]
        if missing:
            raise ValueError(f"图表字段不在查询结果中：{', '.join(missing)}")
