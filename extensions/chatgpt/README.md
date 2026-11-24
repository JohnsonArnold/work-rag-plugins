# ChatGPT 桌面端 / Codex Windows 原生接入指南

ChatGPT 桌面端与 Antigravity 在同一台 Windows 主机上共用同一个 `work-rag` 本地数据目录（默认 `D:\work-rag\.work-rag-data`）。

## 配置步骤

1. 打开 ChatGPT 桌面端：**设置 → MCP servers → Add server**。
2. 填写以下配置（请按本机实际路径核对）：
   - **Name**: `work-rag`
   - **Transport**: `STDIO`
   - **Command**: `D:\work-rag\work-rag-plugins\.venv-win\Scripts\python.exe`
   - **Arguments**（每行一项）：
     ```text
     -m
     work_rag
     --data-dir
     D:\work-rag\.work-rag-data
     mcp
     ```
3. 注意：**ChatGPT 端不要添加 `--enable-app`**，这样 ChatGPT 端只暴露通用的 23 个标准 MCP 工具（`kb_*`、`task_*`、`review_*`），工具列表保持简洁干净。
4. 在 Antigravity 的 `Work-RAG` 面板里添加你的多个日常工作目录并点击同步后，ChatGPT 端立即可以检索、读取、计算这些资料并共享全部事项与复盘。
