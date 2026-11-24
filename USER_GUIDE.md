# work-rag 0.4.0 使用手册

work-rag 用于管理个人资料和事项。你先手动选定文件并导入，随后通过命令行或本地 MCP 查资料、读原文、做表格计算、跟进事项和保存复盘。每次导入、查询和备份都使用同一个本地数据目录。

## 1. 启动

### Linux 与 WSL

在项目根目录，用 Python 3.11 或更新版本创建虚拟环境。数据目录需要位于支持 SQLite 文件锁、硬链接和 fsync 的本地持久文件系统，并且系统应提供 `Asia/Shanghai` 时区数据。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
. .venv/bin/activate
rag() { work-rag "$@"; }
rag --version
```

需要本地 OCR 时，将安装命令改为 `.venv/bin/python -m pip install '.[ocr]'`。下文的 `rag` 是当前 shell 的简写，普通安装可直接使用 `work-rag`。

## 2. 用临时库试一遍

在项目根目录运行以下命令。下面在临时目录新建 Markdown 文件并导入。

```bash
export TRIAL_ROOT="$(mktemp -d)"
export WORK_RAG_DATA_DIR="$TRIAL_ROOT/library"

printf '# 设备说明\n每周检查一次。\n' > "$TRIAL_ROOT/example.md"
rag init
rag source import "$TRIAL_ROOT/example.md" --title '设备说明'
rag source catalog --query 设备说明
```

导入结果中，`data.source.source_id` 是资料编号，`data.version.version_id` 是本次版本编号。从目录查询结果也可以取得 `data[0].source_id`。把返回的实际值填入下面的变量，再执行读取。

```bash
SOURCE_ID='在此填入 src_ 开头的资料编号'

rag kb describe "$SOURCE_ID"
rag kb read-text "$SOURCE_ID"
rag kb search 检查
```

正式使用时，把 `WORK_RAG_DATA_DIR` 改为你选定的绝对持久路径，运行 `rag init`，并在后续终端和 MCP 配置中使用该路径。

## 3. 导入、更新和查找资料

| 你要做什么 | 命令或做法 |
|---|---|
| 导入文件 | 先把文件放到运行 work-rag 的主机可读的位置，再运行 `rag source import '/绝对路径/文件.xlsx'`。含本地相对附件的 Markdown 应连同附件目录一并放好。 |
| 更新原文件 | 在同一路径重新执行导入。相同内容复用原版本，变化的内容产生新版本。换路径后添加 `--source-id "$SOURCE_ID"`，继续沿用原资料编号。 |
| 防止基于旧版更新 | 导入时同时传入 `--source-id "$SOURCE_ID" --expected-current-version "$VERSION_ID"`。遇到冲突，先查看当前版本。 |
| 按标题或文件名查找 | `rag source catalog --query 电费 --suffix .xlsx`。也可只用其中一个筛选条件。 |
| 查看历史 | `rag source versions "$SOURCE_ID"`。支持版本参数的读取命令可带 `--version-id "$VERSION_ID"`。 |
| 改标题或归档 | `rag source edit "$SOURCE_ID" --title '新标题'`；用 `--archived` 归档，用 `--no-archived` 恢复显示。目录默认隐藏已归档资料，可用 `--include-archived` 查看。 |

### 读内容和做计算

- **Excel** 使用 `kb describe` 查看工作表和范围，使用 `kb read-sheet` 查看单元格、公式和缓存值。响应有 `next_cursor` 时，用同一资料、工作表和范围加上 `--cursor "$CURSOR"` 继续读取。通用统计支持 `sum`、`difference`、`count`、`average`，例如 `rag kb calculate "$SOURCE_ID" '记录' B3:B4 difference`。公式缓存可能来自旧计算，留意结果中的值来源和警告。
- **电费样本** 使用 `kb electricity` 按读数和倍率复算一行。可用 `rag kb electricity-total "$SOURCE_ID" '早+晚' 4 5 --category day` 汇总同表、同类别且日期互不重叠的行。日期存在歧义时先确认业务口径。
- **PDF** 使用 `rag kb read-text "$PDF_ID" --page 1` 读取原生文本。扫描页可用 `rag kb ocr "$PDF_ID" --page 1` 识别。关键日期、金额和编号可通过 MCP 的 `kb_render` 查看原页后核对。CLI 的 `kb render` 在 JSON 中返回 PNG 编码。
- **DOCX、Markdown、UTF-8 文本** 使用 `kb describe` 查看结构，使用 `kb read-text` 读取内容。结果有 `data.next_start` 时，可用 `--start` 继续，并传入本次的版本编号。DOCX 或 Markdown 的栅格附件可用 `kb render-attachment` 查看。

正文搜索取决于版本的已处理范围。新导入资料先用 `source catalog` 找编号，再读取相应工作表、文本或 PDF 页，扫描页按需执行 OCR。用 `rag kb status "$SOURCE_ID"` 检查解析和索引，然后用 `rag kb search '关键词'` 查询。需要保留出处时，保存结果 `evidence` 数组中的完整对象，可用 `rag kb resolve "$EVIDENCE_JSON"` 回读其固定版本。

## 4. 记录事项和复盘

```bash
rag task create --title '检查设备' --content '核查设备状态' --tag 设备
rag task list --view unfinished
```

从创建结果的 `data.task_id` 取得事项编号。新事项初始修订号为 1；后续每次修改或追加日志前，可从 `task get` 的 `data.revision` 取得最新值。

```bash
TASK_ID='在此填入 task_ 开头的事项编号'

rag task get "$TASK_ID"
rag task append-log "$TASK_ID" --content '完成初查' --expected-revision 1 --request-id followup-1
rag task logs "$TASK_ID"
rag task update "$TASK_ID" --expected-revision 2 --status completed
rag review create "$TASK_ID" --sections '{"process":[{"kind":"user_statement","text":"完成初查"}]}'
rag review list
```

- 其他常用视图为 `today`（今日到期）、`overdue`（逾期）和 `recent`（最近新增）。可用 `--query` 查标题或内容。截止日期支持 `2026-11-21` 或带时区的 `2026-11-21T18:00:00+08:00`，系统按北京时间计算。
- 追加日志时，给每次新操作使用不同的 `--request-id`。遇到超时，可用相同 ID 和相同参数重试。修订号冲突时先查看最新事项，再决定是否提交一条新操作。更正日志时用 `--corrects` 指定原日志编号。
- 复盘可选择一个或多个事项。可用段落键是 `background`、`process`、`result` 和 `improvement`，条目类型支持 `user_statement`、`source_excerpt` 和 `ai_inference`。用 `rag review get "$REVIEW_ID"` 回看保存时的事项和日志。

## 5. 使用本地 MCP

在支持 stdio 的客户端配置启动命令和参数。配置中填写绝对路径，并使用与 CLI 相同的持久数据目录。

| 环境 | 启动命令 | 参数列表 |
|---|---|---|
| Windows 原生 | 虚拟环境中 `Scripts\python.exe` 的绝对路径 | `-m`、`work_rag`、`--data-dir`、数据目录绝对路径、`mcp` |
| 当前工作区 | `/bin/sh` | `tools/run_mcp.sh` 的绝对路径、资料库绝对路径 |
| 已安装的 Linux 环境 | 虚拟环境中 `bin/work-rag` 的绝对路径 | `--data-dir`、数据目录绝对路径、`mcp` |

<a id="windows-mcp"></a>

### Windows 原生配置

以项目放在 `D:\work-rag`、资料库放在 `D:\work-rag\.work-rag-data` 为例。在 PowerShell 用 Python 3.11 或更新版本安装：

```powershell
Set-Location 'D:\work-rag'
py -3 -m venv .venv-win
& '.\.venv-win\Scripts\python.exe' -m pip install .
& '.\.venv-win\Scripts\python.exe' -m work_rag --data-dir 'D:\work-rag\.work-rag-data' init
```

在 ChatGPT 桌面端打开 **设置 → MCP servers → Add server**。名称填 `work-rag`，传输选 `STDIO`，命令填 `D:\work-rag\.venv-win\Scripts\python.exe`。环境变量和工作目录可留空。把下面各行依次添加为参数：

```text
-m
work_rag
--data-dir
D:\work-rag\.work-rag-data
mcp
```

保存并重启后，在 Codex 中用 `/mcp` 检查，再调用 `kb_catalog`。首次使用可初始化空库。迁入旧库时先按第 6 节恢复到新目录，再把参数中的路径改为恢复目录。WSL 也可以按 Linux 方式安装。 [OpenAI 桌面端 MCP 配置](https://learn.chatgpt.com/docs/extend/mcp#configure-in-the-chatgpt-desktop-app)

### Linux 与 DSH

在 DSH 的“账号菜单 → 设置 → MCP”中添加 `work-rag`。传输选 `stdio`，按上表填写命令和两个参数，工具调用超时可设 `120000` 毫秒。把示例路径替换成 DSH 进程实际可访问的绝对路径。[mcpServers 配置示例](tools/dsh_mcp.json)

[启动脚本](tools/run_mcp.sh)自行切换到源码目录。解释器依次使用环境变量指定的绝对可执行路径或项目 `.venv/bin/python`。新配置可用 `WORK_RAG_PYTHON` 指定解释器。数据目录优先使用脚本的第一个参数，然后读取数据目录环境变量。新配置可用 `WORK_RAG_DATA_DIR`。没有显式配置时，沿用已有的默认库，新建库使用项目下的 `.work-rag-data`。脚本只接收数据目录的绝对路径，stdio 的 stdout 专用于协议。

第 2 节的临时库和正式库独立。正式导入时，将 `WORK_RAG_DATA_DIR` 设为 MCP 配置的资料库绝对路径，再通过 CLI 导入选定文件。

接入后，先调用 `kb_catalog`、`task_list` 和 `review_list` 确认正常响应。已导入电费资料时，再调用 `kb_read_sheet` 读取指定工作表和范围，核对版本和单元格。DSH 中工具名带 `mcp__work-rag__` 前缀。新文件与文件更新继续通过 CLI 手动导入。

## 6. 备份、恢复和排查

备份、恢复的目标目录应使用尚不存在的目录，其父目录必须已存在。在第 2 节的临时库中可直接演练。

```bash
rag backup "$TRIAL_ROOT/backup"
rag --data-dir "$TRIAL_ROOT/restored" restore "$TRIAL_ROOT/backup"
rag --data-dir "$TRIAL_ROOT/restored" status
rag --data-dir "$TRIAL_ROOT/restored" source catalog --query 设备说明
```

恢复后先检查 `sqlite_integrity` 为 `ok`，并确认无缺失或损坏快照。再抽查资料版本、历史证据及事项，确认无误后可把 CLI 和 MCP 指向恢复目录。

| 遇到的情况 | 先检查什么 |
|---|---|
| 插件提示 `Connection closed` / `No module named work_rag` | 使用第 5 节的启动脚本，检查 DSH 进程能否访问脚本、解释器和资料库的绝对路径。 |
| CLI 能看到资料，MCP 查不到 | 比对启动脚本的数据目录参数（或已安装入口的 `--data-dir`）与 CLI 的数据目录；检查是否仍在第 2 节临时库。 |
| 目录能找到文件，全文搜索无结果 | 用 `kb status` 确认当前版本及相关范围是否已处理。 |
| Excel 翻页或搜索游标失效 | 使用相同查询条件从第一页重新查询；搜索结果集变化时会要求重新开始。 |
| OCR 启动失败 | 检查该运行环境是否安装 OCR 扩展，以及错误提示列出的 OpenCV 或系统依赖。 |
| 更新事项返回冲突 | 运行 `task get` 查看最新修订号，结合当前内容重新提交。 |

## 7. 在 DSH 直接录入事项

按 [事项扩展安装说明](extensions/dsh-work/README.md)安装，使用与 MCP 相同的资料库。会话中可输入 `/work help`、`/work add "事项标题" --content "记录内容"` 或 `/work list unfinished`。跟进、完成或重开时，使用回执中的完整事项 ID。

会话右侧打开「事项」可新建、编辑、跟进和切换状态。输入会在同一 DSH 实例的同一会话间同步；看到「已同步」后，另一设备重开或刷新该会话即可接续。浏览器可在离线时保留输入，联网后补传；同步冲突时可比较并选择保留哪一份。提交结果待确认时，在「操作回执」恢复原提交。
