# work-rag

完整操作步骤见 [work-rag 使用手册](USER_GUIDE.md)。

个人资料库与事项核心。用户手动导入文件，CLI 和本地 MCP 使用同一个数据目录。资料保留原件和历史版本，支持按需解析、检索、范围读取与计算。事项支持跟进日志和手选复盘。

## 安装与验证

需要 Python 3.11+ 和本地持久数据目录。安装时会按平台选择依赖，Windows 自动安装北京时间所需的 `tzdata`。运行时自动选用 Windows 或 POSIX 的文件锁和快照发布方式。Windows 可直接使用原生 Python。[Windows 安装与 MCP 参数](USER_GUIDE.md#windows-mcp)

Linux 和 WSL 使用支持 SQLite 文件锁、硬链接及 fsync 的本地文件系统，并使用系统时区数据。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -q
. .venv/bin/activate
```

可用 `.venv/bin/python -m pip install -e '.[ocr]'` 安装本地 OCR。

## 手动导入与读取

先将选定文件放到核心主机可读的位置。为该实例固定 `WORK_RAG_DATA_DIR` 或每次传入相同的 `--data-dir`。下面在临时目录里试用。继续使用已有资料库时，将变量设为原目录的绝对路径。

```sh
TRIAL_ROOT="$(mktemp -d)"
export WORK_RAG_DATA_DIR="$TRIAL_ROOT/library"
printf '# 设备说明\n每周检查一次。\n' > "$TRIAL_ROOT/example.md"
work-rag init
work-rag source import "$TRIAL_ROOT/example.md" --title '设备说明'
work-rag source catalog --query 设备说明
```

把 `SOURCE_ID` 替换为导入返回的来源编号。重复导入同一路径和相同内容会复用现有版本。文件换了路径时，可传入 `--source-id "$SOURCE_ID"`；也可为外部来源明确绑定 `--origin-key`。CLI 支持 `--expected-current-version` 进行条件更新。

```sh
work-rag source versions "$SOURCE_ID"
work-rag kb describe "$SOURCE_ID"
work-rag kb read-text "$SOURCE_ID"
work-rag kb search 检查
```

- `source catalog` 按标题或文件名查找，可按扩展名筛选。全文检索覆盖已产生索引的 XLSX 单元格和可检索的文本行。对未处理资料，先用目录定位，再用 `kb describe`、`kb read-text` 或 `kb ocr` 补处理。`kb status` 报告当前版本的解析、索引和失败范围。
- Excel 读取保留单元格地址、值、格式、合并区、公式和现有缓存。带游标翻页固定在首次选择的版本和产物。统计会标注公式缓存来源；缓存新鲜度未知。电费计算可针对受支持的工作表按读数和倍率复算，汇总前需核实计量区间。
- PDF 可用 `kb read-text "$SOURCE_ID" --page 1` 读取文字，`kb render` 渲染原页，`kb ocr` 识别指定页或矩形。带图片的文字页报告部分覆盖。旋转 PDF 支持整页渲染，其矩形裁剪暂缓。OCR 结果带渲染像素坐标和置信信息，关键字段可对照原页。
- DOCX 支持正文、常规表格、页眉页脚和图片关联。特殊结构按报告处理。UTF-8 文本与 Markdown 支持按偏移读取，Markdown 中可解析的本地内联相对附件随版本保留。Markdown 和 DOCX 的栅格图片可用 `kb render-attachment` 查看。
- 版本和证据返回 JSON。通过 `kb resolve '<证据对象 JSON>'` 可读取旧引用，资料更新、改名或归档后仍可定位。

## 事项与复盘

```sh
work-rag task create --title '检查设备' --content '记录当前状态' --due 2026-11-21
work-rag task list --view unfinished
work-rag task append-log "$TASK_ID" --content '完成初查' --expected-revision 1 --request-id followup-1
work-rag task logs "$TASK_ID"
work-rag review create "$TASK_ID" --sections '{"process":[{"kind":"user_statement","text":"完成初查"}]}'
```

将 `TASK_ID` 替换为创建返回的事项编号。截止日期按北京时间解释，带时刻的值须提供显式时区。追加日志推动事项修订，更正以新日志关联被更正记录。复盘固定已选事项及创建时的日志，后续更新会保留旧复盘。复盘条目可区分用户陈述、资料摘录和 AI 推断。

## 本地 MCP 与备份

已安装的客户端可通过解释器环境内 `work-rag` 的绝对路径，以 stdio 启动 `work-rag --data-dir /绝对路径/资料库 mcp`。源码工作区可用 [独立启动入口](tools/run_mcp.sh)，命令为 `/bin/sh`，参数依次是 `tools/run_mcp.sh` 的绝对路径和资料库绝对路径。两者均以客户端进程内可见的路径为准。[MCP 接入说明](USER_GUIDE.md#5-使用本地-mcp)

MCP 提供资料查询、读取、计算、图像、证据、事项和复盘工具。资料导入由用户从 CLI 发起，CLI 必须指向相同数据目录。

```sh
work-rag backup /已有父目录/新备份目录
work-rag --data-dir /已有父目录/新恢复目录 restore /已有父目录/新备份目录
work-rag --data-dir /已有父目录/新恢复目录 status
```

备份保留一致的数据库快照和引用原件，包括附件、历史证据及复盘。恢复使用新目录。完整性检查会逐个核对引用快照。

## DSH 事项入口

在 DSH Web `0.2.2-rc.1` 可安装 [事项扩展](extensions/dsh-work/README.md)。`/work` 和会话右侧「事项」直接调用本地核心，与 MCP 共用事项库。同一 DSH 实例的不同设备可以接续同一会话的草稿。
