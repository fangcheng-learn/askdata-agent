# AskData Agent

AskData Studio 与 HomeNova Harness V1 的独立源码快照。后端采用 FastAPI / LangGraph，前端采用 Vue 3，支持自然语言查数、已有结果问答和多步骤经营调查。

本仓库保留现有相对目录，供 HomeNova 数据路径、测试和脚本使用。它记录的是当前实现；方案文档中的规划不代表全部已经实现或验证，具体状态见设计文档和实施报告。

## 仓库内容

- `references/askdata_studio/`：实际应用、测试、依赖清单及已有演示 CSV 数据。
- `docs/ecommerce_agent_design_decisions.md`：产品与设计基线。
- `docs/agent_skills/`：业务 Skill 文档；运行时使用应用内明确注册的 Skill。
- `scripts/homenova_harness_v1/`：Harness 开发、校验及报告脚本。
- `scripts/homenova_phase_e/`：HomeNova 适配、语义构建和启动脚本。
- `data/homenova_phase_d/public/`：两份 HomeNova 公开快照。
- `data/homenova_phase_e/public_semantics/`：对应的公开指标、Schema 和能力说明。

未包含 DataFoundry、运营版副本、密钥、依赖环境、向量缓存、会话记忆、数据库文件、私有题库、参考答案或原始运行日志。历史报告中指向本机私有文件的链接不随本仓库发布；依赖这些历史文件的离线回放脚本需要单独提供文件。

## 安装与启动

需要 Python 3.11、Node.js 20.19 或更高版本，以及现有模型服务配置。以下命令从本仓库根目录执行：

```bash
python3.11 -m venv references/askdata_studio/backend/.venv
references/askdata_studio/backend/.venv/bin/python -m pip install -r references/askdata_studio/backend/requirements.txt
cp references/askdata_studio/backend/.env.example references/askdata_studio/backend/.env
npm --prefix references/askdata_studio/frontend ci
```

在后端 `.env` 中填写自己的 API Key 和模型设置。现有配置项见 `.env.example` 和 `backend/app/config.py`。密钥保留在本机。

分别打开两个终端：

```bash
# 终端一
cd references/askdata_studio/backend
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

```bash
# 终端二（从仓库根目录执行）
npm --prefix references/askdata_studio/frontend run dev
```

界面：http://127.0.0.1:5174/ 。API 文档：http://127.0.0.1:8001/docs 。内置演示账号见应用 README。

原有 `scripts/homenova_phase_e/start.py` 与桌面启动脚本也保留，但含开发机 Node / 共享配置路径；在其他机器上优先使用上面的直接启动命令。首次检索需要用配置的 Embedding 服务构建本地索引，可能产生模型费用。

## 本地验证

```bash
cd references/askdata_studio/backend
.venv/bin/python -m unittest discover -s tests -v
```

```bash
# 从仓库根目录执行
npm --prefix references/askdata_studio/frontend run build
```

真实模型验收和索引构建会产生费用，需要按自己的预算单独安排。本次上传没有启动模型验收、重新生成数据或重建数据库。

## 快照来源

从本机 `dataagent` 工作区按上述范围复制，独立初始化 Git，未导入 DataFoundry 的 Git 历史，也未修改原工作区的远程仓库或提交状态。
