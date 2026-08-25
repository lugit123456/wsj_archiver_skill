# WSJ eReader 双语归档

本项目从 [WSJ eReader](https://ereader.wsj.net/?editionStart=The+Wall+Street+Journal)
读取报纸整期目录和 Editorial 正文，生成中文标题、逐段中英对照、中文解读、glossary，
并下载正文图片。旧的 `wsj.com` 板块列表和文章 `__NEXT_DATA__` 抓取路径已移除。

## 数据规则

- 默认读取 eReader 当前最新一期；`--date YYYY-MM-DD` 可选择日期下拉框中的历史期次。
- `issue_date` / `publication_date` 是报纸出版日期，不编造或推算每篇文章的精确发布时间。
- `page` 保存物理顺序 `1, 2, 3...`，`print_page_label` 保存 `A1`、`B3` 等印刷版号。
- `print_section` 保存 `Main`、`U.S. News` 等报纸栏目，期次顶层 `pages` 保存完整版面目录。
- `source_id` 使用 `wsj-ereader:<date>:<root-page-label>:<xml-id>`，`url` 保存 eReader 入口。
- 续文优先根据 `CONTINUED FROM` 的原始版号和 `xmlId` 映射到首页文章；同一
  `source_id` 保留正文节点更多、文本更长的版本，并合并 `source_pages`。
- 旧期次缺少跳转信息时，才按标准化后的完全相同标题去重，同样保留正文更完整的版本。
- 只保存能解析出普通正文段落的 Editorial 内容；广告、行情表、视频、音频和 Podcast 不入库。
- 图片只下载并按 eReader 原始位置输出，不调用 LLM 解析画面或翻译图片说明。原始 caption、credit 和 alt 仍保存在 `image_placements` 作为来源元数据；新增文章的 `image_insights` 保持为空，前端不显示图片说明。
- 正文翻译、中文标题和中文解读中的英文平台名、产品名、品牌名、人名、公司名和机构名保留英文原文，例如 `Google`、`Reddit`、`Instagram`、`TikTok`、`Sensor Tower`，不使用音译、意译或中文网络俗称。

eReader 的 Newsmemory `TOKEN` 是短期会话凭据，只在浏览器内存中使用，不写入日志、配置或数据库。

## 安装

```bash
cd /Users/luzhe/Desktop/code/agent_skills/wsj_archiver_skill
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

在 `.env` 填写 `LLM_API_KEY`。默认使用项目内 `.wsj-browser/` 独立 Chromium profile，
并把 Cookie 备份到 `.wsj-auth.json`（权限 `0600`）。
图片始终只下载和输出；不提供启用 LLM 图片解析或图片说明翻译的配置。

## 登录

```bash
python sync_wsj.py --login
```

在打开的 Chromium 中完成 WSJ 登录并确认 eReader 可访问，然后回到终端按 Enter。
程序会验证出版日期控件和整期文章目录，再保存 Cookie。不会重放 DataDome payload 或绕过验证。

## 使用

```bash
# 抓取、续文去重并列出最新一期尚未归档的报道；不调用 LLM、不写库
python sync_wsj.py --dry-run

# 指定出版日期
python sync_wsj.py --date 2026-08-21 --dry-run

# 完整处理最新一期，最多新增 3 篇
python sync_wsj.py --limit 3

# 完整增量处理一期
python sync_wsj.py --date 2026-08-21

# 从根 database.js 重建每日数据库和索引
python sync_wsj.py --rebuild-outputs

```

为了准确识别 A1/A10 续文，`--dry-run` 也会读取整期正文，但不会下载图片、调用 LLM 或写数据库。
`--limit` 限制的是去重后尚未归档的新增文章数。

指定日期不是通过 URL 参数实现。程序进入 eReader 后读取顶部 `#pullDownDate` 下拉框，
把 `--date 2026-08-21` 转换为选项值 `20260821` 并触发期次切换；如果下拉框没有该日期，
会列出当前可选日期并明确退出。

## 每日定时运行

定时任务建议不传 `--date`，每次处理 eReader 下拉框当前最新一期：

```bash
cd /Users/luzhe/Desktop/code/agent_skills/wsj_archiver_skill
./run_wsj_sync.sh
```

不加 `--limit` 会处理该期全部尚未归档的报道。周末、休刊日或重复执行时，即使 eReader
仍显示上一期，也会通过 `source_id` 跳过已有文章。需要补抓指定历史期次时使用：

```bash
./run_wsj_sync.sh --date 2026-08-21
```

长文逐段翻译会按最多 16 段、约 1200 个英文词拆分请求，逐块校验后按原顺序合并，
避免整篇响应过大导致连接超时。

## 增量与兼容

新记录优先按 `source_id` 跳过；对没有 `source_id` 的现有记录，使用
`issue_date + 标准化标题` 做兼容匹配。现有 `database.js`、历史期次和图片不会删除或迁移。
每篇文章编译完成后会原子更新根库、每日库和 `database_index.js`。

每日库文章字段包括：

```text
publication_date, page, page_article_index, print_page_label,
print_section, source_pages, source_id, subtitle, byline,
images, image_placements（原始位置/caption/credit/alt）, image_insights（新增文章为空）
```

期次级 `pages` 中每个版面包含 `page`、`print_page_label`、`print_section` 和 `article_ids`。

## 输出

```text
database.js
output_results/
├── database_index.js
└── WSJ/
    └── YYYY-MM-DD/
        ├── database.js
        └── images/
frontend/
├── index.html
└── assets/
```

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile sync_wsj.py wsj_ereader.py
node --check frontend/assets/app.js
```

测试覆盖 eReader 版号、稳定 `source_id`、跨页段落合并、A1/A10 续文去重、图片位置和每日库版面结构。

## 安全与版权

Cookie、浏览器 profile、数据库、图片及付费正文只应在本机用于个人阅读和研究。
不要公开部署或提交 `.env`、`.wsj-auth.json`、`.wsj-browser/`、归档数据库、图片及付费正文。
