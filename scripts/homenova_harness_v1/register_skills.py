"""Explicit development-time copy; runtime never scans docs or the repository."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for name, description in {
    'business_diagnosis': '经营变化核实、贡献分析与解释证据审查',
    'goal_analysis': '目标进度与缺口；参数缺失时澄清',
    'inventory_risk_check': '库存事实与有参数的条件式风险分析',
}.items():
    target = ROOT / 'references/askdata_studio/backend/app/skills' / name
    target.mkdir(parents=True, exist_ok=True)
    text = (ROOT / 'docs/agent_skills' / name / 'SKILL.md').read_text()
    text = text.replace('（待接入初稿）', '').replace('应用级业务 Skill 初稿，待 Harness 接入。', '应用级业务 Skill，由 Harness 按需注入。')
    (target/'SKILL.md').write_text(text)
    (target/'config.json').write_text(json.dumps({'name': name, 'description': description,
        'allowed_tools': [], 'max_tool_calls': 0, 'output_actions': []},ensure_ascii=False,indent=2)+'\n')
