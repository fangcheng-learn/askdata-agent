# Agent提示词与Skill配置页

2026-10-29。只修改references/askdata_studio，继续使用后端8001／前端5174。新增应用模型API调用0、真实Agent试跑0。

## 已实现

管理员登录后点击主界面顶部「Agent配置」，或访问 http://127.0.0.1:5174/?view=agent-config 。

- 六个职责：Flash入口、经营ReAct、SQL模块、结果问答与报告、最终审核、会话摘要。展示实际模型、输入材料、节点和Skill加载方式。模型名称只读，沿用原启动配置。
- 九份实际系统模板、五份已注册Skill正文。支持全文查看、搜索、复制、编辑、保存、恢复默认、默认来源和组装预览。Skill注册不代表每次任务都会加载；动态用户上下文、Schema、口径和结果不冒充固定提示词。
- 覆盖内容独立保存在backend/data/agent_configuration/overrides.json；不改默认提示词或Skill源文件，不写入密钥、业务数据。保存有修订冲突检测、原子落盘与明确错误。仅管理员可读写，不能指定任意文件或修改工具权限。
- 模板及实际运行共用默认定义，配置进入真正的提示词组装。SQL首次与修复共用模板，报告两个模式分别可编辑，普通查数的整理／审核也覆盖。必需Skill及授权展示工具标记保留，动态资料仍由代码组装。
- 新任务绑定配置版本；后续保存不影响进行中任务或澄清恢复。调查检查点持久化配置快照。旧任务未记录配置时使用内置默认。摘要排队时固定提示词。恢复默认不会残留组件启动时缓存的覆盖。

## 验证

固定范围见新记录目录acceptance.md。配置API、存储、实际组装、缓存恢复、任务冻结、重启恢复和旧任务兼容均用临时配置目录及受控模型输出验证，不调用真实模型。外部网络在后端全量回归进程中禁用。

首轮577项中1个旧预算测试失败：它绕过构造函数的fixture缺少新增的harness与configuration_snapshots字段。补齐fixture后对应专项10项通过；未放宽业务断言或权限。初次失败日志保留，最终完整回归577项通过，142.693秒。

前端类型检查和生产构建通过，E阶段14项回归通过。Work设置禁用了浏览器访问，实际页面视觉与点击验收未完成；没有用构建或HTTP200冒充界面已验收。

## 边界与遗留

本次让配置可见、可编辑，不改变既定分工：Flash不选Skill，经营ReAct自主选择并请求加载，Controller执行白名单读取。最新真实任务4fab6710cf46中，缺Skill被误送SQL重写的调度缺陷**本轮尚未修复**，也未重新运行该任务。不能据配置页交付声称经营调查已恢复。

记录目录：data/homenova_harness_v1/private/agent_configuration_20261029/，不覆盖历史E阶段或其他评测。


## 运行与验收记录

服务已通过原启动脚本重启，8001／5174健康；主模型qwen3.7-plus、路由qwen3.7-flash沿用原配置。线上接口确认管理员200、非管理员403、未登录401；读取到6个职责、9份模板、5份Skill，当前仍为默认版本0。保存／恢复在临时配置中验证，未改线上正文。134份CSV与历史任务载荷、预算保持不变。

```sh
references/askdata_studio/backend/.venv/bin/python scripts/homenova_phase_e/start.py restart --record-dir data/homenova_harness_v1/private/agent_configuration_20261029/runtime
```

- [验收清单](../data/homenova_harness_v1/private/agent_configuration_20261029/acceptance.md)、[最终验证收据](../data/homenova_harness_v1/private/agent_configuration_20261029/verification.json)。
- [最终577项回归](../data/homenova_harness_v1/private/agent_configuration_20261029/backend_tests.log)、[初次失败](../data/homenova_harness_v1/private/agent_configuration_20261029/backend_tests_initial.log)、[API及实际组装测试](../data/homenova_harness_v1/private/agent_configuration_20261029/targeted_final.log)。
- [前端构建](../data/homenova_harness_v1/private/agent_configuration_20261029/frontend_build.log)、[线上接口确认](../data/homenova_harness_v1/private/agent_configuration_20261029/runtime/api_smoke_receipt.json)、[健康状态](../data/homenova_harness_v1/private/agent_configuration_20261029/runtime/health.json)。
