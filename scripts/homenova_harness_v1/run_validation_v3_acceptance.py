"""Two new trials explicitly authorized by the user after the V3 fixes.

Reuses the tested reservation/duplicate/crash/balance guards. The original
implementation_v2 ledger is never reset or changed. This round has a fixed
directory and its own two-run limit, not an arbitrary repeat/output option.
"""
from pathlib import Path
import json

import run_budgeted_acceptance as capped


if __name__ == '__main__':
    capped.DIRECTORY = Path(__file__).resolve().parents[2] / 'data/homenova_harness_v1/private/validation_v3_real'
    capped.LEDGER = capped.DIRECTORY / 'paid_runs.json'
    if json.loads(capped.LEDGER.read_text()).get('hold_for_user_discussion'):
        raise RuntimeError('首题已发现未解决问题；按用户要求先商量，保留剩余名额，禁止自动继续')
    capped.main()
