# 低频 AICLI 模型同步

日常调用优先使用既有 Toolkit registry；只有明确更换本地 AICLI 模型、Profile 或模型 catalog 时才运行此脚本。它读取本机 JSON，并调用 AICLI `profile show --json` 取得既有配置 fingerprint；不会启动模型、GPU Broker 或后台服务。

```powershell
.\.venv\Scripts\python.exe scripts\sync_aicli_backends.py `
  --aicli-root E:\Projects\Tools\ai-cli-profile-manager
```

预览确认差异后，才写 Toolkit 的唯一 canonical registry：

```powershell
.\.venv\Scripts\python.exe scripts\sync_aicli_backends.py `
  --aicli-root E:\Projects\Tools\ai-cli-profile-manager `
  --apply
```

`--aicli-root` 可指向 AICLI source root 或已安装 module root；脚本会查找各自的 `data/providers` 与 `data/model-catalogs`。`--registry` 只用于定向 source 或隔离夹具，默认是本项目 `src/llm_backend_toolkit/default_backends.json`。`.agents` mirror 仍由其既有 owner 流程复制并回读，本脚本不写镜像。

脚本只同步四个 main Profile 与独立 review Profile 的共同模型身份、显示名、256K context 和 endpoint origin。它不会改 Toolkit 的 backend ID、路由策略、采样参数或 `num_predict`；main 的 Toolkit output 只校验不超过 Codex main Profile 的 output 能力。其他 engine 的 output 设置，以及 review direct route 的视觉/output 能力，属于各自独立合同，不会被 Codex Profile 覆盖。

模型身份变化时，必须显式提供 Toolkit direct vision 能力，避免从 Agent Profile 猜测或继承：

```powershell
.\.venv\Scripts\python.exe scripts\sync_aicli_backends.py `
  --aicli-root E:\Projects\Tools\ai-cli-profile-manager `
  --main-direct-vision true `
  --apply
```

如果同次更新 review 模型，还要按新模型的直接调用能力补充 `--review-direct-vision true` 或 `false`。

模型或已记录的 Profile/catalog binding 变化会移除旧 Agent evidence 并留下最小 `pending_reacceptance` 状态。同步不是 live 验收：实际 AICLI Agent receipt、安装回读和模型现场验证仍按既有流程完成。

对于已验收的 route，脚本通过 AICLI `profile show --json` 读取其既有 `profileFingerprint`；这个指纹已包含关联 catalog 的内容哈希。它不会另建 fingerprint 格式或证据系统。
