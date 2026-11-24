使用 `work-rag` 工具查询本地资料库和事项库时，严格遵守以下约束：

1. **证据优先、禁止编造**：引用资料库中的数据、条款或原文时，必须基于工具实际返回的内容，保留对应的 `source_id`、`version_id` 与定位信息（工作表/单元格、页码或偏移）；不得臆测未读取页面或未索引文件的内容。
2. **如实报告覆盖度与告警**：工具返回 `coverage` 为 `partial`、`not_processed` 或附带 `warnings`（例如 Excel 公式缓存新鲜度未知、PDF 含图部分覆盖、存在未完全索引来源）时，必须明确告知用户。
3. **确定性计算**：涉及 Excel 汇总、差值、均值或电费复算时，必须调用 `kb_calculate` / `kb_electricity` / `kb_electricity_total`，不要口算替代工具结果。
4. **事项与复盘边界**：更新事项或追加跟进前必须确认最新 `revision`，每次新写入使用新的 `request_id`；撰写复盘时严格区分 `user_statement`（用户陈述）、`source_excerpt`（资料摘录）和 `ai_inference`（AI 推断）。
5. **资料导入与多目录配置**：不要绕过 `work-rag` 直接篡改数据目录内的数据库或 `blobs/` 文件；引导用户通过 Work-RAG 配置面板（`work_rag_panel`）或 `work-rag root` / `source import` CLI 管理本地目录与导入文件。
