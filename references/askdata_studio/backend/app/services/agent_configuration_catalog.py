"""Explicit catalog for the active execution path; never scans docs for knowledge."""
from __future__ import annotations

from pathlib import Path
import re

from ..agent_configuration_runtime import configuration_store
from ..config import settings
from .. import prompt_defaults as defaults
from ..workflows.react_prompts import BUSINESS_REACT_SYSTEM, REVIEW_SYSTEM
from .agent_configuration import ConfigurationValidationError


def prompt_definitions() -> dict:
    rows = [
        ('preprocess', 'preprocess', '意图与任务理解', defaults.PREPROCESS_SYSTEM,
         'app/preprocessing.py', '三条初始路由；原因调查识别；目标、范围和已有信息。'),
        ('react', 'investigation', '经营调查', BUSINESS_REACT_SYSTEM,
         'app/workflows/react_prompts.py', '选择业务Skill、读取证据、决定下一轮取证。'),
        ('sql_query', 'sql', 'SQL生成与修复', defaults.SQL_QUERY_SYSTEM,
         'app/querying/sql_module.py', '首生成与失败子集修复共用此模板；修复的原SQL与实际错误另行输入。'),
        ('data_qa', 'report', '已有结果问答', defaults.DATA_QA_SYSTEM,
         'app/querying/data_qa_agent.py', '解释已选结果，并按需生成报告。'),
        ('investigation_report', 'report', '调查报告草稿', defaults.INVESTIGATION_REPORT_SYSTEM,
         'app/querying/data_qa_agent.py', '同一DataQa组件的调查收尾模式，整理多份真实证据。'),
        ('result_draft', 'report', '简单查询结果整理', defaults.RESULT_DRAFT_SYSTEM,
         'app/querying/response_generator.py', '普通查数后的简短标题和结果说明。'),
        ('review', 'review', '调查最终审核', REVIEW_SYSTEM,
         'app/workflows/react_prompts.py', '审核报告草稿、核心问题覆盖及证据；定稿直接展示。'),
        ('simple_review', 'review', '简单查数最终审核', defaults.SIMPLE_REVIEW_SYSTEM,
         'app/querying/response_generator.py', '审核普通查数的说明与真实结果。'),
        ('memory_summary', 'memory', '会话摘要', defaults.MEMORY_SUMMARY_SYSTEM,
         'app/services/short_term_memory.py', '压缩旧会话；调查进度和完整结果仍由后端单独保存。'),
    ]
    return {row[0]: dict(zip(('id', 'agent_id', 'label', 'default_text', 'source', 'description'), row))
            for row in rows}


def validate_prompt_markers(changes: dict[str, str | None]) -> None:
    definitions = prompt_definitions()
    for key, text in changes.items():
        if key not in definitions or text is None:
            continue  # The storage allowlist rejects unknown IDs.
        marker = r'\[\[(?:SKILL:[A-Za-z0-9_]+|DISPLAY_TOOLS)\]\]'
        expected = re.findall(marker, definitions[key]['default_text'])
        actual = re.findall(marker, text)
        if sorted(expected) != sorted(actual):
            raise ConfigurationValidationError(f'{key}需保留且仅保留原组装标记：' + '、'.join(expected))


def configuration_catalog(config=None, *, snapshot=None) -> dict:
    config = config or settings
    snapshot = snapshot if snapshot is not None else configuration_store().snapshot()
    skill_root = Path(__file__).resolve().parents[1] / 'skills'
    # Use explicit registry metadata, but read default bodies directly so a
    # task ContextVar can never disguise the default or latest admin view.
    from ..skills import SkillRegistry
    registry = SkillRegistry()
    labels = {'database_query': '数据库查询技术', 'data_qa': '结果分析与报告',
              'business_diagnosis': '经营异常诊断', 'goal_analysis': '经营目标分析',
              'inventory_risk_check': '库存风险筛查'}
    skills = []
    for metadata in registry.list():
        name = metadata['name']
        default = (skill_root / name / 'SKILL.md').read_text(encoding='utf-8').strip()
        technical = name in {'database_query', 'data_qa'}
        skills.append({'id': name, 'label': labels.get(name, name),
            'description': metadata['description'], 'default_text': default,
            'effective_text': snapshot['skills'].get(name, default),
            'source': f'app/skills/{name}/SKILL.md', 'overridden': name in snapshot['skills'],
            'editable': True, 'allowed_tools': metadata['allowed_tools'],
            'used_by': ['sql'] if name == 'database_query' else ['report'] if name == 'data_qa' else ['investigation'],
            'loading_rule': ('SQL模块自动提供，生成与修复共用。' if name == 'database_query' else
                             'DataQa组件自动提供，已有结果问答和报告模式共用。' if technical else
                             '由经营ReAct按需从白名单选择，Controller读取正文；可先核实数据，业务Skill不是执行SQL的前提，只注入本任务已加载的方法。')})
    bodies = {item['id']: item['effective_text'] for item in skills}
    prompts = []
    for entry in prompt_definitions().values():
        text = snapshot['prompts'].get(entry['id'], entry['default_text'])
        assembled = text
        for name, body in bodies.items():
            assembled = assembled.replace('[[SKILL:' + name + ']]', body)
        prompts.append({**entry, 'effective_text': text, 'overridden': entry['id'] in snapshot['prompts'],
            'editable': True, 'required_placeholders': re.findall(r'\[\[[A-Z_:a-z]+\]\]', entry['default_text']),
            'assembled_text': assembled,
            'assembly_notes': ['此处是固定系统模板；用户问题、Schema、指标口径、真实结果及错误按当前任务重新组装。',
                '业务Skill选择后才追加正文；预览不代表所有Skill都会加载。',
                '[[DISPLAY_TOOLS]]由当前用户授权的展示工具列表替换。' if '[[DISPLAY_TOOLS]]' in text else
                '代码输出Schema和任务约束由运行时提供。']})
    main_model = config.llm_model
    agents = [
        {'id': 'preprocess', 'label': 'Flash入口', 'description': '理解问题并选择初始路径，不选择Skill。',
         'model': config.route_model.strip() or main_model, 'node_names': ['preprocess'],
         'prompt_ids': ['preprocess'], 'skill_ids': [],
         'context_inputs': ['本轮问题', '相关近期会话', '机器当前日期与数据覆盖说明'],
         'loading_rule': '路由到简单查数、已有结果问答或经营调查；不制定调查步骤。'},
        {'id': 'investigation', 'label': '经营ReAct', 'description': '看一步结果，决定下一步查什么；不直接写SQL。',
         'model': main_model, 'node_names': ['react'], 'prompt_ids': ['react'],
         'skill_ids': ['business_diagnosis', 'goal_analysis', 'inventory_risk_check'],
         'context_inputs': ['目标与范围', '白名单Skill目录／已加载正文', '指标定义与能力', '调查进度', '相关真实证据', '剩余预算'],
         'loading_rule': 'Agent自主决定是否及何时读取业务Skill。复杂调查优先读取适用方法，也允许先核实数据；Controller按白名单加载，未加载业务Skill不会单独阻止SQL执行。'},
        {'id': 'sql', 'label': 'SQL查询模块', 'description': '准备查询资料、生成SQL、修复失败项并执行真实查询。',
         'model': main_model, 'node_names': ['query_coder', 'sql_repair', 'sql_calculation_repair'], 'prompt_ids': ['sql_query'],
         'skill_ids': ['database_query'], 'context_inputs': ['单步取数目标', '授权Schema及关联', '登记指标口径', '实际依赖结果', '真实错误及已用次数'],
         'loading_rule': '查询技术Skill自动注入；修复使用同一模板及失败子集，不重复整个问答流程。'},
        {'id': 'report', 'label': '结果问答与报告', 'description': '解释已有结果，或将调查证据组织成报告草稿。',
         'model': main_model, 'node_names': ['data_qa', 'draft', 'result_draft'],
         'prompt_ids': ['data_qa', 'investigation_report', 'result_draft'], 'skill_ids': ['data_qa'],
         'context_inputs': ['用户问题', '实际结果与来源版本', '指标单位', '已核查解释及缺口', '已加载业务方法'],
         'loading_rule': 'DataQa两模式共用报告Skill；简单查数另用简短结果整理模板。'},
        {'id': 'review', 'label': '最终审核', 'description': '独立审核草稿和证据，审核定稿直接展示。',
         'model': main_model, 'node_names': ['delivery_review', 'simple_delivery_review'],
         'prompt_ids': ['review', 'simple_review'], 'skill_ids': [],
         'context_inputs': ['原始目标', '报告草稿', '真实证据／必要SQL', '核心问题与竞争解释', '预算和停止原因'],
         'loading_rule': '正常路径生成草稿后调用；没有默认逐步过程Reviewer。'},
        {'id': 'memory', 'label': '会话摘要', 'description': '按既有触发条件整理较早会话，保留已确认条件。',
         'model': main_model, 'node_names': ['short_term_summary'], 'prompt_ids': ['memory_summary'], 'skill_ids': [],
         'context_inputs': ['上一版摘要', '待合并的旧轮次'],
         'loading_rule': '由会话服务按阈值调度，独立于调查结果存储。'},
    ]
    return {**{key: snapshot.get(key) for key in ('revision', 'updated_at', 'updated_by')},
        'agents': agents, 'prompts': prompts, 'skills': skills,
        'policies': [
            {'id': 'scope', 'label': '配置范围', 'description': '管理员保存应用级覆盖；不修改源码默认正文。模型名称沿用服务启动配置，此页不设置密钥。'},
            {'id': 'task_version', 'label': '任务配置版本', 'description': '新任务绑定保存时的配置。进行中任务及澄清恢复沿用原版本；恢复默认不需要重启。'},
            {'id': 'controller', 'label': '代码执行边界', 'description':
                f'权限、快照、只读查询和依赖由代码管理。当前初值：{config.harness_react_round_limit}轮业务取证，'
                f'每条SQL总共{config.harness_sql_generation_limit}次生成，工具{config.harness_tool_limit}次，'
                f'主动处理{config.harness_seconds:g}秒／调查{config.harness_investigation_seconds:g}秒；'
                '旧任务保留原账本，不能通过改提示词关闭这些限制。'},
            {'id': 'assembly', 'label': '动态上下文', 'description': '模板、Skill正文和当前任务数据分别组装。配置页不展示任何用户查询结果或私有评测资料。'},
        ]}
