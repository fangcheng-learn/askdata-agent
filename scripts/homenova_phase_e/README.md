# HomeNova E 阶段：AskData V2 接入与实际验收

本阶段使用 references/askdata_studio：后端8001、前端5174。根目录DataFoundry和运营版8002/5173不是本次接入对象。

## 启动和界面

从项目根目录执行：

```bash
python3 scripts/homenova_phase_e/build_semantics.py
references/askdata_studio/backend/.venv/bin/python scripts/homenova_phase_e/build_indices.py
python3 scripts/homenova_phase_e/start.py start --model qwen3.7-plus
```

打开 http://127.0.0.1:5174/，使用现有账号登录。顶部“数据源”默认选择HomeNova的8月底快照，可切换10月尾窗末，也可使用原数据源。切换会清空当前会话与参考结果。界面显示场景分析日期、Europe/Berlin时区及新订单覆盖末日。

修改后端或刷新语义发布包后使用 start.py restart，统一刷新Schema与缓存；它只重启已确认的V2后端，不停止运营版。原登录机制保留，服务重启会使已有令牌失效，需要重新登录。

脚本优先沿用现有模型配置，需要共享配置时复用现有loader，仅取DASHSCOPE_API_KEY/QWEN_MODEL并以内存环境传给后端；不写入脚本、前端或记录。backend/requirements.txt已补运行所需pytz。

## 公开资料和实际检索

data/homenova_phase_e/public_semantics/<snapshot>/各含schema、field_documents、metrics、glossary、capabilities、dataset_metadata、datasource、manifest八个JSON，15表207字段27关联40指标。来源只包含D中性字典和当前公开元数据/字段。

公开JSON是语义源，不是向量缓存。HomeNovaSchemaIndex继承AskData原有Embedding、BM25、dense、RRF、Rerank，只覆盖公开文档来源、签名和独立存储位置。之后按当前任务补齐指标的状态、时间、字段和连接依赖，术语与能力边界也进入正常模型上下文。

运行索引与长期记忆位于 references/askdata_studio/backend/data/homenova_runtime/<datasource_id>/，原schema_store和原业务数据不覆盖。每轮按所选数据源收紧Schema、索引、MCP工具、会话键和结果引用；省略选择的旧请求不开放HomeNova。当前DuckDB只物化固定十五表并关闭外部访问，拒绝文件/网络函数、其他数据库和机器时钟。日期从metadata读取，10月观察截面不代表9月仍有新订单。

运行时不扫描仓库、docs或private目录；题库、手册、参数、标准答案及新评测记录都不在检索和工具路径内。

## 验收复现

```bash
references/askdata_studio/backend/.venv/bin/python scripts/homenova_phase_e/probe_runtime.py
references/askdata_studio/backend/.venv/bin/python scripts/homenova_phase_e/run_agent_tests.py --output data/homenova_phase_e/private/agent_runs/new_round
references/askdata_studio/backend/.venv/bin/python scripts/homenova_phase_e/evaluate.py --run-dir data/homenova_phase_e/private/agent_runs/new_round
python3 scripts/homenova_phase_e/preserve.py after
```

输出目录必须为空，不覆盖前轮。runner只给Agent自然问题、授权和公开信息，不读取标准SQL/数字；evaluate是独立进程，才读取D私有基线。与界面相同使用登录后的POST /api/query。

十题共11次请求，Q09两个快照分别使用相同问题和全新会话，比较发生在模型外部。HTTP200、completed、SQL成功和业务正确分开记录；布局不同或口径复杂的结果还需人工审阅，不能只凭关键词判通过。外部模型故障与业务失败分开，未取得SQL不能判指标正确。

E私有记录统一保存于 data/homenova_phase_e/private。实际结果、修正和限制见 docs/homenova_phase_e_report.md。没有开发完整Harness。

单独复现已验证的基础相对日期查数可使用 run_smoke.py --output 一个新的私有JSON文件名；该程序只发自然问题，不读取答案。报告由 build_report.py 根据真实日志和独立审阅生成，不能在缺少审阅时冒充业务通过。
