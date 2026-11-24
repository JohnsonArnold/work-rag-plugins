# work-rag 事项扩展

适用于 DSH Web `0.2.2-rc.1`。`/work` 和会话右侧的「事项」面板通过本地 Python 核心读写事项库。录入、跟进、状态切换和草稿同步均直接执行。

## 构建与安装

需要 Node.js 22.13+。将 `DSH_PACKAGE_ROOT` 设为 DSH 安装中 `@deepseek-ai/dsh` 的包根目录。从项目根目录运行以下命令生成安装包。

```sh
export DSH_PACKAGE_ROOT="/absolute/path/to/node_modules/@deepseek-ai/dsh"
(cd extensions/dsh-work && npm ci && npm run typecheck && npm run build && npm pack)
```

在 DSH 主机或容器内进入项目根目录，确保该进程能访问 work-rag 源码、Python 运行环境和持久库。将示例数据路径替换为 DSH 进程可见且与 `work-rag` MCP 配置一致的绝对路径。

```sh
export WORK_RAG_PROJECT_ROOT="$PWD"
export WORK_RAG_DATA_DIR="/absolute/path/to/library"
dsh plugin --profile web add "$WORK_RAG_PROJECT_ROOT/extensions/dsh-work/work-rag-dsh-work-0.1.2.tgz"
```

用 Docker 时，在 DSH 服务配置中设置上述环境变量。首次安装默认将扩展状态保存在 `~/.dsh/work-rag-work`，升级时可沿用已有状态目录，也可用 `WORK_RAG_WORK_STATE_DIR` 指定。若当前 profile 已启用旧版事项扩展，升级时停用旧版并启用新版，让 `/work` 和事项侧栏只由同一扩展注册。配置在下次加载 profile 时生效。若使用其他 profile，把 `web` 换成实际名称。

现有 schema 2 库在首次打开时迁移至 schema 3。可事先用核心 `backup` 命令保存副本。核心备份包含正式事项，草稿和操作回执随扩展状态目录保留。恢复到新库后，可在「旧资料库共享草稿」复制需要的内容到当前表单。旧提交继续从操作回执读取。

## 使用

- 在 DSH 会话输入 `/work help` 查看命令。用 `/work add "检查设备" --content "记录现状"` 新建，再用回执中的完整事项 ID 查询、跟进或切换状态。
- 打开右侧面板，选择「事项」。输入后自动同步草稿。同一 DSH 实例的另一设备打开同一会话，便可接续共享稿。离线输入留在当前浏览器，重连后同步。如两端同时编辑，界面会显示两份内容供选择或合并。
- 正式提交后可以按事项 ID 查询。若结果待确认，在面板查看操作回执，或用 `/work operations` 和 `/work retry OPERATION_ID` 恢复原提交。
