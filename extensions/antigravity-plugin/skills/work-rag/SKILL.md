---
name: work-rag
description: >-
  操作本地 work-rag 个人资料库与事项核心：管理多个本地工作目录与同步面板、按需解析检索 XLSX/PDF/DOCX/MD/图片、
  执行确定性表格/电费计算、追溯历史版本证据，以及维护带乐观锁的事项、跟进日志与复盘快照。
---

# Work-RAG 工作流指南

`work-rag` 是共享的本地资料库与事项核心（Antigravity 与 ChatGPT 桌面端共用同一本地数据目录）。

## 1. 管理多目录与直写面板（优先打开 UI）

当用户需要添加/管理多个本地工作目录、预览文件差异、一键同步导入，或手动浏览资料库/事项/复盘时，直接调用 `work_rag_panel` 工具打开交互面板：

- `work_rag_panel(tab="roots")`：源目录配置、浏览文件夹、差异扫描与一键同步；
- `work_rag_panel(tab="library")`：资料库检索、版本历史、建索引与归档；
- `work_rag_panel(tab="tasks")`：事项新建、编辑、状态切换与追加跟进；
- `work_rag_panel(tab="reviews")`：查看已保存的不可变复盘快照。

> 面板内部操作直连本地库、不经过模型；同步后的资料和索引会立即可被 Antigravity 与 ChatGPT 两端共享。

## 2. 资料检索与强证据读取流程

遵循「目录定位 → 检查状态/结构 → 精确范围读取 → 保留证据」顺序：

1. **找资料**：先用 `kb_catalog(query=..., suffix=...)` 找 `source_id`，再结合 `kb_search(query=...)` 搜索已索引正文/单元格。
2. **查覆盖度**：若搜索结果为空或 `sources_without_complete_index > 0`，用 `kb_status(source_id)` 检查该版本是否已解析；未解析时用 `kb_describe(source_id)`、`kb_read_text`、`kb_read_sheet` 或 `kb_ocr` 按需补解析。
3. **分格式读取**：
   - **Excel (`.xlsx`)**：先 `kb_describe` 看工作表和维度，再 `kb_read_sheet` 按范围读取（有 `next_cursor` 时用相同参数续页）。统计优先用 `kb_calculate`（34 位 Decimal 精确计算）；电费样本表用 `kb_electricity` / `kb_electricity_total`。
   - **PDF (`.pdf`)**：先 `kb_read_text(source_id, page=N)` 读原生文本；若提示含图片或 `not_processed`，用 `kb_ocr` 识别，并对关键金额/日期/编号调用 `kb_render` 核对原页图像。
   - **DOCX / MD / TXT**：用 `kb_describe` 看段落/表格/附件列表，`kb_read_text(start=..., limit=...)` 顺序读取；栅格附件图片用 `kb_render_attachment` 查看。
4. **回溯旧证据**：持有历史 `EvidenceRef` JSON 时，用 `kb_resolve(reference=...)` 精确定位其固定快照版本。

## 3. 事项、跟进与复盘规范

- **查询事项**：使用 `task_list(view="unfinished" | "today" | "overdue" | "recent")` 或 `task_get(task_id)`。
- **写入保护**：每次写操作（`task_create`、`task_update`、`task_append_log`、`review_create`）必须生成唯一的 `request_id`（仅在超时重试同一操作时复用相同 `request_id`）。
- **乐观锁**：`task_update` 和 `task_append_log` 必须传入最新的 `expected_revision`；若遇冲突，先 `task_get` 查看最新状态再决定下一步。
- **仅追加日志**：不改写历史跟进；更正旧记录时通过 `task_append_log(..., corrects=old_log_id)` 追加更正条目。
- **保存复盘**：调用 `review_create(task_ids=[...], sections={...}, request_id=...)`，段落键限于 `background`、`process`、`result`、`improvement`，每条明确标注 `kind` 为 `user_statement`、`source_excerpt` 或 `ai_inference`。
