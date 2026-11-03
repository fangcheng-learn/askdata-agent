# 本地 Qwen 词元估计资产

用途：单次模型输入的离线长度估计，不是模型权重，不是业务知识，也不是闭源 `qwen3.7` 的精确计费器。运行期不下载文件，不调用远端分词服务。

来源：官方 [QwenLM/Qwen-Agent](https://github.com/QwenLM/Qwen-Agent)，固定提交 `31a4d36d123688581a9e9744427272b33ce940e0`。资产和上游参考源码原样保留，Apache-2.0 全文见 `LICENSE.qwen-agent`，归属说明见 `NOTICE`。

| 文件 | SHA256 |
|---|---|
| `qwen.tiktoken` | `b2b1b8dfb5cc5f024bafc373121c6aba3f66f9a5a0269e243470a1de16a33186` |
| `tokenization_qwen.py.upstream` | `f6fdddd23643224ddb6e97b57605c205dd85d6135523fc5dc9ab18b0bde94d7e` |
| `LICENSE.qwen-agent` | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |

精确下载地址、大小和校验值见 `asset_manifest.json`，命令行校验列表见 `SHA256SUMS`。上游词表地址为该固定提交下的 `qwen_agent/utils/qwen.tiktoken`，正则 `PAT_STR` 来自同目录 `tokenization_qwen.py`。参考源码只用于溯源，运行期不导入它，也不安装 Qwen-Agent 包。

`app/token_counting.py` 使用本地 `tiktoken.Encoding` 和这份词表：文本做 NFC 规范化，以普通文本编码特殊标记字面量；本地计数增加25%余量，再增加64个普通输入开销，含messages时至少128个开销（每条消息至少64）。返回信息明确 `exact=false`。

缺少 `tiktoken`、缺词表或SHA不一致时，明确返回 `utf8_byte_upper_bound_fallback` 和失败类型，使用 UTF-8 字节保守上界，不取消限制。词表懒加载且进程内缓存，修复依赖／资产后需重启进程或清空加载缓存。

单次输入估计和任务累计费用预占分开：这里只替代把字节数直接当单次token数的判断。累计用量、输出上限、收尾预留和供应商实际usage仍由原预算层管理，不能因使用本地词表而放宽任务限额。
