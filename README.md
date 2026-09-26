# LLM Backend Toolkit（模型任务工具）

1. 这是什么：让 AI 把明确的任务交给选定的本地或云端模型，并拿回可检查的结果；它不会自己决定换模型。
2. 我怎么用：请 AI 代用；自己用时先运行 `python -m venv .venv` 和 `.\.venv\Scripts\python.exe -m pip install -e .`；用 `.\.venv\Scripts\llm-backend-toolkit.exe submit --request examples/local-request.json` 提交，再用同一个程序的 `job --id <编号> --result` 查询。
3. 怎么知道它正常：`llm-backend-toolkit version` 能查安装来源，`llm-backend-toolkit preflight --request examples/local-request.json` 能预查路线；具体任务仍要看完成回执和结果。
4. 坏了怎么提醒我：没有自动提醒；任务失败会在查询结果和模型调用观察台（看任务状态的窗口）显示，出问题直接跟 AI 说。
5. 让 AI 做什么：按 [项目约定](AGENTS.md) 选模型、核对结果，云端请求须显式允许发送材料；`benchmark_only` 是基准专用路线，`codex-cli` 是 Codex 运行路线，`no fallback` 表示失败不自动换模型。
