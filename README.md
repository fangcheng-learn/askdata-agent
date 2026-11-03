# AskData Agent / InsightPilot

HomeNova 数据分析助手：FastAPI / LangGraph 后端与 InsightPilot React / Next.js 工作台。支持自然语言查数、授权元数据问答、已有结果追问、经营调查和报告生成。

当前前端是 `apps/insightpilot`，后端是 `references/askdata_studio/backend`。旧 Vue 前端保留在 `references/askdata_studio/frontend`，供历史兼容使用。

## 当前行为（2026-11-03）

- 元数据请求由入口模型理解，读取授权目录后交由问答 Agent 回答，不使用关键词规则或固定字段表替代答案。
- 基于已有结果生成报告时复用原始结果及权限绑定，单独整理报告，不重开调查或重置旧调查预算。
- 独立报告内容审核目前暂停：`REPORT_CONTENT_REVIEW_ENABLED=false`。报告明确显示未经过独立内容审核；权限、引用和数据来源检查仍保留。
- 必要入口问题先等待用户回答；调查中的可选背景问题支持跳过。
- 保留当前有效判断、相关正常检查与反证；减少重复上下文，按需读取历史结果。

最新状态见 [检查点](docs/homenova_protocol_overhaul_checkpoint.md) 和 [恢复 Agent 回答与暂停审核说明](docs/homenova_agent_restore_review_pause_20261103.md)。离线验证通过不等于真实业务报告质量已验收。

## 安装

需要 Python 3.11 和 Node.js 22 或以上。以下命令从仓库根目录运行：

```bash
python3.11 -m venv references/askdata_studio/backend/.venv
references/askdata_studio/backend/.venv/bin/python -m pip install -r references/askdata_studio/backend/requirements.txt
cp references/askdata_studio/backend/.env.example references/askdata_studio/backend/.env
npm ci
npm run build
```

在后端 `.env` 中配置自己的模型服务。配置项见 `.env.example` 和 `backend/app/config.py`，不要提交密钥。公开 HomeNova 演示快照与语义定义已包含；会话数据库、模型调用记录、缓存、私有题库和参考答案不包含在仓库中。

## 启动

分别打开两个终端：

```bash
# 终端一
cd references/askdata_studio/backend
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

```bash
# 终端二，从仓库根目录运行
npm start
# 开发模式可使用 npm run dev
```

工作台：http://127.0.0.1:5174/data-tasks 。API 文档：http://127.0.0.1:8001/docs 。登录账号与权限说明见 [后端应用说明](references/askdata_studio/README.md)。

开发机服务管理脚本 `scripts/homenova_phase_e/start.py` 及桌面启动器保留原配置，其中含开发机 Node 和共享配置路径；其他机器优先使用上面的直接启动命令。首次检索构建索引可能调用 Embedding 服务并产生费用。

## 验证

```bash
cd references/askdata_studio/backend
PYTHONPATH=.:tests .venv/bin/python -m unittest tests.test_agent_owned_delivery tests.test_multiturn_evidence tests.test_investigation_report -v
```

```bash
# 从仓库根目录运行
npm run build
npm run test:web
```

完整回归与真实场景验收的已知限制记录在检查点中。本次仓库更新没有重新运行真实模型调查或重建数据库。

## 目录与来源

- `apps/insightpilot/`：当前工作台。
- `packages/contracts/`：工作台使用的共享类型。
- `references/askdata_studio/`：后端、测试、旧前端及基础演示数据。
- `data/homenova_phase_d/public/`、`data/homenova_phase_e/public_semantics/`：授权演示快照及语义。
- `scripts/homenova_phase_e/`、`scripts/homenova_harness_v1/`：启动、开发和验证脚本。
- `docs/`：设计记录及实施状态。历史文档中的本机私有日志链接不会随仓库发布。

InsightPilot 前端及共享类型基于 DataFoundry，保留其 Apache-2.0 许可；来源记录见 `apps/insightpilot/datafoundry-source-manifest.json`。本仓库保持独立历史，未导入完整 DataFoundry 项目。`SOURCE_SNAPSHOT.json` 记录本次同步范围及源文件哈希。
