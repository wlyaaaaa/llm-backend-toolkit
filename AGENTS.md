# 模型任务工具的项目约定

- 本库供顶层 AI 显式调用，不自行决定任务或模型。默认路线只属于本库登记表，不是全局模型默认，也不能用本地模型替换已选原生子代理。
- 模型、地址、平台和 AICLI 配置从现有登记表解析；省略选择时走登记的本地默认。机器字段保持稳定，面向人的界面用 `display_name` 或实际模型名，不展示内部别名；失败不自动换模型。
- 模型变更按 [AICLI 同步说明](docs/aicli-backend-sync.md) 更新，不再建立另一份长期配置来源。模型或配置指纹变化后，旧的真实验证结果失效。
- 日常 AI 入口为异步 `submit` 与 `job`；同步 `invoke` 仅保留为底层接口。上下文压缩须在回执可见，默认不输出推理内容，以结果核对代替持续监视过程。
- `data_factory` 按登记的精确模型与配置执行；云端推荐在对应指纹真实验证前仍标未验证。`benchmark_only` 只供基准测试，不作为日常模型路线。
- 原生多模态、LocalOCR 和 ChineseASR 保持可独立选择；本机 GPU 必须经现有 LocalGpuBroker，不直连内部 Ollama 后端。
- 发送到云端的正文、来源摘录和媒体均要求请求里明确 `privacy.cloud_allowed=true`。自动测试用模拟云协议，不发真实云调用。
- JSON 请求、结果、任务与错误接口保持兼容；源代码不依赖别的应用的私人配置。真实提示、结果、媒体、转录、任务状态与日志不入库。
- 初次安装：`python -m venv .venv`，再用 `.\.venv\Scripts\python.exe -m pip install -e .`；用生成的 `llm-backend-toolkit.exe submit --request examples/local-request.json` 提交，`job --id <编号> --result` 取回结果。
- `version` 查安装来源，`preflight --request <请求文件>` 检查路线；单测用 `python -m unittest discover -s tests -v`。命令运行通过不能代替真实任务结果核对。
- 根 README 的注释保留现有测试直接读取的路线说明；跨库合同和基准输入在 `docs/`、`schemas/`、`benchmarks/`，历史报告只描述当时结论，不作为当前模型验收。
