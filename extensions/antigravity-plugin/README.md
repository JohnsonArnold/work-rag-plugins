# Work-RAG Antigravity Plugin

将本地 `work-rag` 核心打包为 Antigravity 插件，包含：

- **MCP Server + MCP App 面板**：在右侧边栏（`+` → `Work-RAG`）和左侧导航栏打开交互界面，可视化添加多个本地工作目录、预览差异、一键同步导入、建索引、管理资料与事项。
- **Skill (`skills/work-rag/SKILL.md`)**：指导 Agent 按证据优先流程调用工具。
- **Rule (`rules/AGENTS.md`)**：防幻觉、强证据引用与公式缓存告警约束。

## 安装或更新（Windows）

在项目根目录运行（无需修改 PowerShell 执行策略）：

```powershell
.\.venv-win\Scripts\python.exe tools\install_antigravity_plugin.py --data-dir 'D:\work-rag\.work-rag-data'
```

安装完成后插件位于 `~\.gemini\config\plugins\work-rag\`，Antigravity 会自动发现。若你同时使用 ChatGPT 桌面端，请将 ChatGPT 的 `work-rag` MCP 参数中的 `--data-dir` 设为同一路径（不加 `--enable-app`），即可共享全部目录同步结果与事项库。
