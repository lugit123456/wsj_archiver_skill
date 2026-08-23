---
name: wsj-archiver
description: 从 WSJ eReader 预览抓取整期 Editorial 报道，处理续页去重，翻译并生成中文解读和 glossary，按出版日期输出双语归档。
---

# WSJ eReader Archiver

## 规则

- 数据源固定为 `https://ereader.wsj.net/?editionStart=The+Wall+Street+Journal`。
- 默认抓最新一期；指定日期时使用 `python sync_wsj.py --date YYYY-MM-DD`。
- 日期只表示报纸出版日期，不生成每篇文章的发布时间。
- `page` 是物理顺序，`print_page_label` 是 `A1`、`B3` 等印刷版号，`print_section` 是报纸栏目。
- 使用 `wsj-ereader:<date>:<root-page-label>:<xml-id>` 作为稳定 `source_id`。
- 通过 `CONTINUED FROM` 的原始版号和 `xmlId` 合并续页；同一文章保留正文更完整的版本并记录全部 `source_pages`。
- 仅在缺少 page-jump 信息时，按标准化后的完全相同标题做第二层去重。
- 只归档能解析出普通正文的 Editorial 内容，排除广告、行情表、视频、音频和 Podcast。
- 图片原始 caption、credit 和 alt 保存在 `image_placements`，中文说明统一保存到同路径 `image_insights[].description`；前端以紧凑响应式网格显示在中英对照栏顶部。
- 保留既有翻译、中文解读、glossary、原子写库和前端；不得删除或迁移旧归档。
- Newsmemory `TOKEN` 属于短期敏感会话信息，禁止写入日志、配置和数据库。
- 不要提交或公开 `.env`、Cookie、浏览器 profile、付费正文、图片和生成数据库。

完整命令、字段和验证步骤见 `README.md`。
