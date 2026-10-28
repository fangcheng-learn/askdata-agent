# HomeNova Harness V1 开发与验收

实际应用为 `references/askdata_studio`，API 8001、前端 5174。不得启动其他副本或执行Demo数据库初始化。

2026-10-28最新实施：后端129项、E适配14项及前端构建通过；用户授权的两个不同自然问题均已运行一次且失败，**两次额度已用完，当前不得新增真实调用或全量回归。** `run_budgeted_acceptance.py`持久锁定台账并拒绝重复／第三次运行；下面旧runner例子仅保留历史用法，不是本轮继续运行指令。完整状态见`docs/homenova_harness_v1_implementation_v2_report.md`。

逐任务全过程、Skill记录和token汇总在`private/implementation_v2/process_reports/index.html`，可用`python3 scripts/homenova_harness_v1/build_latest_reports.py`离线重新生成，不调用模型／网络。未来运行的`budget.events`记录入口路由／Skill选择、实际节点Skill注入及正文指纹、工具／修复／重规划事件；原两题没有逐节点打点的部分明确注明还原依据或未知，不回填成真实事件。

查询校验简化（V3）：定义／显示名按已知来源规范化，等价运算符及大小写兼容；`query_contract.validation`保存`normalizations`、`pending_checks`和`sql_summary`。待核对项跟随结果及同会话追问，不直接当成业务通过；明确的金额／件数、聚合、声明筛选／日期冲突与权限限制保留。修复无进展时提前停止，预算不增加。离线脚本`replay_query_validation_v3.py`只检查已保存候选和本地公开CSV，不发起Agent任务；结果在`private/query_validation_v3`，不能回填或改写原两题状态。

```sh
python3 scripts/homenova_phase_e/start.py restart --record-dir data/homenova_harness_v1/private/runtime
```

沿用现有模型与密钥加载。打开 http://127.0.0.1:5174 ，使用现有登录，选择默认8月底快照。多步任务返回计划、步骤状态、结果编号、SQL、完整性、审核和预算；输入框可回复关键澄清条件。

固定验收条件见 [acceptance.md](acceptance.md)。真实运行脚本只读取自然题目和现有登录定义，不导入参考SQL/答案。每次必须使用新输出目录。

```sh
references/askdata_studio/backend/.venv/bin/python scripts/homenova_harness_v1/run_baseline.py --output data/homenova_harness_v1/private/new_baseline --workers 2
references/askdata_studio/backend/.venv/bin/python scripts/homenova_harness_v1/run_case.py --query '调查2026年7月成功支付商品GMV比6月下降的变化、主要贡献与有证据的解释。金额欧分。' --output data/homenova_harness_v1/private/new_gmv.json
```

`run_acceptance.py`记录简单查数、同会话追问、缺目标与恢复、前提不成立、数据不支持，默认新建acceptance_round1，复测使用 `--output 新目录`。`independent_check.py`是独立离线核算进程，只读公开CSV，不把核算结果传给Agent。

后端回归从 `references/askdata_studio/backend` 运行 `.venv/bin/python -m unittest discover -s tests -v`；E适配测试从根目录运行对应unittest discover。前端沿用 `npm run build`（无全局npm时可用已有Node运行vue-tsc与vite的同一构建命令）。

运行Skill由 `backend/app/skills/*/config.json` 明确注册，三份业务Skill是在开发时通过 `register_skills.py`复制，运行时不读docs。修改Skill草稿后需要明确重新发布应用副本并重启，不能假定路径即加载。

默认任务工具20次、单步额外修复2次、重规划1次、主动300秒（调查240秒，余量收尾），环境变量 `HARNESS_TOOL_LIMIT`、`HARNESS_REPAIR_LIMIT`、`HARNESS_REPLAN_LIMIT`、`HARNESS_SECONDS`、`HARNESS_INVESTIGATION_SECONDS` 可配置。前端等待360秒。此轮验收不放宽初值。

结果rows为200行展示预览，`total_rows`、`preview_truncated`、`complete`、`result_ref`描述服务端完整性。最多保存50000行，超限标记不完整。读取接口 `/api/tasks/{task_id}/results/{result_ref}?datasource_id=...&offset=...&limit=...` 每次仍校验登录、任务归属和快照；数据不足需查询完整聚合。

当前任务、检查点与结果引用在服务内存中，重启后不能恢复；此限制须在交付报告中保留，不宣称持久任务平台已完成。


## 消耗核查与恢复方式

历史运行出现过`Arrearage`，不得反复请求探测欠费。2026-10-28两题模型服务正常，失败为查询契约／口径问题；当前停止原因是本轮两次试运行额度已用完。后续只有在新的明确付费预算下安排验收，不重复整组排错。

任务预算增加`model_usage`：逐请求记录节点、步骤、模型、请求字符量、耗时、状态及服务端数值usage（输入/输出/缓存等）。不会保存提示词、密钥、鉴权头。本轮两题真实接口已经返回逐节点usage；更早无usage的记录不能据次数推算精确token。Skill逐节点事件是此后的补充，仅通过本地测试，未新增付费复验。

具体开发消耗、月度账单范围和原因见 `docs/homenova_harness_v1_consumption_audit.md`；它与评测报告均不是运行知识。
