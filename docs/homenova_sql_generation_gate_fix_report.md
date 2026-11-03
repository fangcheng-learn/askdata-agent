# SQL生成次数前置停止修复

2026-10-29。用户要求同一逻辑SQL最多生成两次，禁止生成第三份后才拒绝执行。本次只修调用调度，未新增付费试跑，历史运行保持。

- 两次均失败后，在下一次普通ReAct调用前直接停止调查、受控收尾，不再生成第三份。新循环入口与实际模型调用入口均检查。
- 已开始批次中的其他独立查询照常分别完成，成功证据保留；第二次修复成功可继续独立取证。
- 重启、显式继续、未知或不合规重写不返还已消耗次数；仍按原状态交付，未完成不冒充完整结论。
- 修正按相同目的措辞误合并独立查询的情况；逻辑身份按实际契约、输入依赖及SQL签名判断，不只比较一句目标描述。

新增9项受控测试，检查的是实际模型调用序列及剩余未消费输出，而非只断言第三条SQL没有执行。上次两道失败题的保存输出也在独立进程中离线回放：均只请求首次生成和技术重写，未请求多余普通ReAct；这些使用替身，不算真实业务验收。

最终后端407项通过（93.831秒），E适配14项通过（0.802秒）。前端未改。原启动脚本已重启加载当前后端；健康检查不调用模型。无新增付费试跑。本次没有修复表名混用、契约格式及访客取证等业务问题，不能将次数修复等同SQL正确率提升。

[离线回放记录](../data/homenova_harness_v1/private/sql_generation_gate_fix_20261029/saved_run_offline_replay.json) · [新增控制测试](../references/askdata_studio/backend/tests/test_sql_generation_pre_dispatch_gate.py)

[全量回归](../data/homenova_harness_v1/private/sql_generation_gate_fix_20261029/backend_tests.log) · [E适配](../data/homenova_harness_v1/private/sql_generation_gate_fix_20261029/phase_e_tests.log) · [启动状态](../data/homenova_harness_v1/private/sql_generation_gate_fix_20261029/runtime/startup.json)
