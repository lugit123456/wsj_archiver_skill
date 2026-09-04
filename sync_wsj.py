#!/usr/bin/env python3
"""WSJ 报道增量抓取、翻译与按日归档。"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
from io import BytesIO
import json
import logging
import os
import random
import re
import shutil
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

from wsj_ereader import (
    EREADER_URL,
    EReaderArticle,
    EReaderImage,
    WsjEReaderAdapter,
)

# 延迟导入 requests/openai,方便无 LLM 依赖环境下跑单元测试。
# 真正调用 LLM / 推飞书时才触发。

# ---------------------------------------------------------------------------
# 常量(详见 DEVELOPMENT.md §3.2.3 / §3.2.4)
# ---------------------------------------------------------------------------

NON_ARTICLE_PATH_KEYWORDS: tuple[str, ...] = (
    "/livecoverage", "/newsletters/", "/audio/", "/video/", "/podcasts/",
)
GLOSSARY_VERSION = 2
ZH_ENGLISH_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"([A-Za-z][A-Za-z0-9]*(?:[.'’_-][A-Za-z0-9]+)*"
    r"(?:(?:[ \t]+|,\s*)[A-Za-z&][A-Za-z0-9]*(?:[.'’_-][A-Za-z0-9]+)*){0,7})"
    r"(?![A-Za-z0-9_])"
)
GLOSSARY_TYPES = {
    "person", "organization", "company", "policy_law", "event",
    "place_context", "work", "proper_concept", "acronym",
}
GENERIC_GLOSSARY_TERMS = {
    "business", "ceo", "company", "democracy", "economy", "government",
    "globalization", "inflation", "market", "president", "sovereignty", "us", "u.s",
    "u.s.", "uk", "xl", "gen",
}

PAPER_PUBLICATION_TYPE = "WSJ"
PAPER_PUBLICATION_NAME = "The Wall Street Journal"
BLANK_IMAGE_DESCRIPTION = " "


def validate_issue_date(issue_date: str) -> tuple[bool, str]:
    """校验 issue 日期合法性 + 是否周六(Economist weekly 仅周六发布,其他日期 SSR 数据是空)。

    返回 (is_valid, error_message)。合法返回 (True, "")。
    """
    try:
        d = datetime.strptime(issue_date, "%Y-%m-%d")
    except ValueError:
        return False, f"日期格式错误:{issue_date!r},期望 YYYY-MM-DD"
    if d.year < 2010 or d.year > 2100:
        return False, f"年份超出合理范围:{d.year}"
    # weekday(): Monday=0 ... Sunday=6; Saturday=5
    if d.weekday() != 5:
        weekday_name = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][d.weekday()]
        return False, (
            f"{issue_date} 是{weekday_name},不是周六。Economist weekly edition 仅周六发布,"
            f"其他日期 SSR 数据是空(返回 0 条),建议改成最近的周六。"
        )
    return True, ""

SELECTORS = {
    # 集中化，DOM 改版时只改这里
    "weekly_issue_card": "a[data-test-id='issue-card']",
    "weekly_article_link": "section.ds-volume-list a",
    "article_h1": "h1[data-test-id='article-headline'], h1.css-1tik00t", # 增加对单篇 H1 的兼容
    "article_body": "p[data-component='paragraph'], div.article-body p, section.ds-content-body p", # 精准指向段落组件
}

SUMMARY_SYSTEM_PROMPT: str = """\
你是一位专业的国际政经与科技评论员。请阅读《华尔街日报》报道的英文原文,深度总结为中文。

硬性要求:
1. 总结总字数严禁少于 300 字(中文字符计)。
2. 严禁遗漏任何核心论点和数据(数字、人物、机构名、年份)。
3. 严禁引入原文外的信息。
4. 若原文涉及争议,保留双方观点,标注来源。
5. 英文平台名、产品名、品牌名、人名、公司名和机构名保留英文原文，不音译、意译或使用中文网络俗称。

严格按以下 Markdown 格式输出(不可增删章节):

### 🌟 一句话核心主旨
(1-2 句话,精准点出文章最核心的论点)

### 🔍 核心观点与论据拆解
(分点列出,3-5 个,每点 50-80 字)

### 🤨 争议与潜在挑战
(若文章未涉及,写"原文未涉及明显争议")

### 🔮 未来趋势预判
(基于文章事实延伸,2-3 句话,不可编造新数据)
"""

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
DATABASE_JS = ROOT / "database.js"
LOGS_DIR = ROOT / "logs"

CN_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
MIN_CN_CHARS = 300

# ---------------------------------------------------------------------------
# 默认配置(全部从 .env 覆盖,默认值在这里 hardcode)
# ---------------------------------------------------------------------------
DEFAULTS = {
    "llm": {
        "provider": "openai",
        "api_key": "",
        "base_url": "",
        "model": "gpt-4o-mini",
        "max_tokens": 2048,
        "temperature": 0.4,
        "timeout_s": 60,
    },
    "feishu": {
        "webhook_url": "",
        "enabled": False,
    },
    "browser": {
        "user_data_path": str(ROOT / ".wsj-browser"),
        "headless": False,
        "cookie_path": str(ROOT / ".wsj-auth.json"),
        "manual_login_timeout_s": 900,
    },
    "crawl": {
        "since_date": "2026-08-20",
        "archive_timezone": "America/New_York",
        "max_pages": 20,
        "delay_min_s": 2,
        "delay_max_s": 4,
        "max_retries": 2,
    },
    "glossary": {
        "enabled": True,
        "model": "",
        "max_terms": 32,
        "max_candidates": 32,
        "max_input_chars": 24000,
        "max_tokens": 5000,
        "max_retries": 2,
    },
    "image_analysis": {
        "enabled": False,
        "model": "",
        "max_tokens": 2400,
        "max_retries": 2,
        "max_images": 6,
    },
    "pipeline": {
        "compile_workers": 2,
        "image_workers": 1,
        "max_pending": 4,
    },
    "ereader": {
        "timeout_s": 30,
    },
    "paths": {
        "output_root": "",
        "database_js": "",   # 空 = 用全局 DATABASE_JS(项目根/database.js)
        "index_html": "",    # 空 = 不生成自包含 index.html(只用项目根的)
        "index_template": "",  # 空 = 用项目根/index.html 当模板
        "article_md_dir": "",  # 空 = 不导出 .md;填了则每篇文章单独一个 {id}.md
    },
}

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


def setup_logger() -> logging.Logger:
    LOGS_DIR.mkdir(exist_ok=True)
    fname = f"sync_{datetime.now().strftime('%Y%m%d')}.log"
    log_path = LOGS_DIR / fname
    logger = logging.getLogger("sync_wsj")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


log = setup_logger()


def acquire_run_lock() -> Any:
    """防止定时任务重叠运行并争用浏览器 profile/database.js。"""
    lock_path = ROOT / ".wsj-sync.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("已有 WSJ 同步任务正在运行，本次退出以避免重复抓取") from exc
    return handle


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def load_config(env: dict[str, str] | None = None) -> dict[str, Any]:
    """从 DEFAULTS 起步,用 .env / 环境变量覆盖任何字段。

    所有配置都集中在 .env(详见 .env.example),不再依赖 config.json。

    环境变量约定(均可选,只有 LLM_API_KEY 是 LLM 调用所必需的):
      LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
        → llm.api_key / llm.base_url / llm.model
      FEISHU_WEBHOOK_URL → feishu.webhook_url
      DATABASE_JS_PATH / INDEX_HTML_PATH / INDEX_HTML_TEMPLATE → paths.*
      LLM_MAX_TOKENS / LLM_TEMPERATURE / LLM_TIMEOUT_S → llm.*
      BROWSER_USER_DATA_PATH / BROWSER_HEADLESS → browser.*
      CRAWL_DELAY_MIN_S / CRAWL_DELAY_MAX_S / CRAWL_MAX_RETRIES → crawl.*

    设计:DEFAULTS 提供结构化默认值,所有真实配置在 .env 里覆盖,不入库。
    env 覆盖 DEFAULTS(后写优先),便于 CI / 本地临时切换。
    """
    # 每次读 config 前确保 .env 已加载
    _load_dotenv()

    import copy
    cfg = copy.deepcopy(DEFAULTS)

    src = env if env is not None else os.environ
    overlay = {
        "llm.api_key": src.get("LLM_API_KEY", "").strip(),
        "llm.base_url": src.get("LLM_BASE_URL", "").strip(),
        "llm.model": src.get("LLM_MODEL", "").strip(),
        "llm.max_tokens": src.get("LLM_MAX_TOKENS", "").strip(),
        "llm.temperature": src.get("LLM_TEMPERATURE", "").strip(),
        "llm.timeout_s": src.get("LLM_TIMEOUT_S", "").strip(),
        "feishu.webhook_url": src.get("FEISHU_WEBHOOK_URL", "").strip(),
        "browser.user_data_path": src.get("BROWSER_USER_DATA_PATH", "").strip(),
        "browser.headless": src.get("BROWSER_HEADLESS", "").strip().lower() in ("1", "true", "yes"),
        "browser.cookie_path": src.get("WSJ_COOKIE_PATH", "").strip(),
        "browser.manual_login_timeout_s": src.get("BROWSER_MANUAL_LOGIN_TIMEOUT_S", "").strip(),
        "crawl.since_date": src.get("WSJ_SINCE_DATE", "").strip(),
        "crawl.archive_timezone": src.get("ARCHIVE_TIMEZONE", "").strip(),
        "crawl.max_pages": src.get("WSJ_MAX_PAGES", "").strip(),
        "crawl.delay_min_s": src.get("CRAWL_DELAY_MIN_S", "").strip(),
        "crawl.delay_max_s": src.get("CRAWL_DELAY_MAX_S", "").strip(),
        "crawl.max_retries": src.get("CRAWL_MAX_RETRIES", "").strip(),
        "ereader.timeout_s": src.get("WSJ_EREADER_TIMEOUT_S", "").strip(),
        "glossary.model": (
            src.get("LLM_GLOSSARY_MODEL", "").strip()
            or src.get("OPENAI_GLOSSARY_MODEL", "").strip()
        ),
        "glossary.max_terms": src.get("LLM_GLOSSARY_MAX_TERMS", "").strip(),
        "glossary.max_candidates": src.get("LLM_GLOSSARY_MAX_ZH_CANDIDATES", "").strip(),
        "glossary.max_input_chars": src.get("LLM_GLOSSARY_MAX_INPUT_CHARS", "").strip(),
        "glossary.max_tokens": src.get("LLM_GLOSSARY_MAX_TOKENS", "").strip(),
        "glossary.max_retries": src.get("LLM_GLOSSARY_MAX_RETRIES", "").strip(),
        "image_analysis.model": (
            src.get("OPENAI_VISION_MODEL", "").strip()
            or src.get("LLM_IMAGE_ANALYSIS_MODEL", "").strip()
        ),
        "image_analysis.max_tokens": src.get("LLM_IMAGE_ANALYSIS_MAX_TOKENS", "").strip(),
        "image_analysis.max_retries": src.get("LLM_IMAGE_ANALYSIS_MAX_RETRIES", "").strip(),
        "image_analysis.max_images": src.get("LLM_MAX_IMAGES_PER_ARTICLE", "").strip(),
        "pipeline.compile_workers": src.get("LLM_COMPILE_WORKERS", "").strip(),
        "pipeline.image_workers": src.get("LLM_IMAGE_WORKERS", "").strip(),
        "pipeline.max_pending": src.get("LLM_MAX_PENDING", "").strip(),
        "paths.database_js": src.get("DATABASE_JS_PATH", "").strip(),
        "paths.output_root": src.get("OUTPUT_ROOT", "").strip(),
        "paths.index_html": src.get("INDEX_HTML_PATH", "").strip(),
        "paths.index_template": src.get("INDEX_HTML_TEMPLATE", "").strip(),
        "paths.article_md_dir": src.get("ARTICLE_MD_DIR", "").strip(),
    }
    for dotted, val in overlay.items():
        if val == "" or val is False:
            continue
        section, key = dotted.split(".", 1)
        cfg[section][key] = val

    # 类型转换(LLM/爬虫调优参数都是数字);非法值静默回退到 DEFAULTS
    int_fields = {
        "llm": ["max_tokens", "timeout_s"],
        "browser": ["manual_login_timeout_s"],
        "crawl": ["max_pages", "delay_min_s", "delay_max_s", "max_retries"],
        "glossary": ["max_terms", "max_candidates", "max_input_chars", "max_tokens", "max_retries"],
        "image_analysis": ["max_tokens", "max_retries", "max_images"],
        "pipeline": ["compile_workers", "image_workers", "max_pending"],
        "ereader": ["timeout_s"],
    }
    for section, keys in int_fields.items():
        for k in keys:
            raw = cfg[section][k]
            try:
                cfg[section][k] = int(raw)
            except (ValueError, TypeError):
                cfg[section][k] = DEFAULTS[section][k]
    try:
        raw = cfg["llm"]["temperature"]
        cfg["llm"]["temperature"] = float(raw)
    except (ValueError, TypeError):
        cfg["llm"]["temperature"] = DEFAULTS["llm"]["temperature"]

    raw_glossary_enabled = src.get("LLM_GLOSSARY_ENABLED", "").strip().lower()
    if raw_glossary_enabled:
        cfg["glossary"]["enabled"] = raw_glossary_enabled in ("1", "true", "yes", "on")
    # 图片只下载和输出，不允许环境变量重新开启 LLM 图片解析。
    cfg["image_analysis"]["enabled"] = False

    paths = cfg.get("paths") or {}
    if not str(paths.get("output_root") or "").strip():
        paths["output_root"] = str(ROOT / "output_results")
    cfg["paths"] = paths

    return cfg


def _load_dotenv() -> None:
    """从项目根 .env 加载环境变量(缺失 python-dotenv 时静默跳过)。"""
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    env_path = ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


# ---------------------------------------------------------------------------
# database.js 读写
# ---------------------------------------------------------------------------


def read_database_js(path: Path | None = None) -> list[dict[str, Any]]:
    """从 database.js 读出当前数组(剥离 window.* 赋值)。

    `path` 默认惰性查找模块级 DATABASE_JS,以便 monkeypatch 测试时改路径生效。
    """
    if path is None:
        path = DATABASE_JS
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    # 抓最后一个赋值(避免注释里出现相同模式)，再交给 JSON decoder
    # 解析完整数组；正文字符串中可能合法出现 `];`。
    matches = list(re.finditer(r"window\.economist_db\s*=\s*", text))
    if not matches:
        log.warning("database.js 格式异常,按空数组处理")
        return []
    raw = text[matches[-1].end():].lstrip()
    if not raw.strip() or raw.strip() == "[]":
        return []
    data, _ = json.JSONDecoder().raw_decode(raw)
    return data


def _serialize_for_js(obj: Any) -> str:
    """用 JSON 序列化,再用 _js_escape 保护 JS 字符串。"""
    return json.dumps(obj, ensure_ascii=False, indent=2)


def write_database_js(articles: list[dict[str, Any]], path: Path | None = None) -> None:
    """原子写 database.js: 按稳定来源标识去重 → 排序 → 临时文件 + os.replace。

    `path` 默认惰性查找 DATABASE_JS,以便测试 monkeypatch 生效。
    """
    if path is None:
        path = DATABASE_JS
    path.parent.mkdir(parents=True, exist_ok=True)
    # 备份旧文件
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    def storage_key(article: dict[str, Any]) -> str:
        source_id = str(article.get("source_id") or "").strip()
        if source_id:
            return f"source:{source_id}"
        url = str(article.get("url") or "").strip()
        if url:
            return f"url:{url}"
        return f"id:{article.get('id') or id(article)}"

    # eReader 各篇共享入口 URL，新记录必须按 source_id 去重；旧记录继续按 URL。
    existing = read_database_js()
    by_source: dict[str, dict[str, Any]] = {storage_key(article): article for article in existing}
    for a in articles:
        by_source[storage_key(a)] = a
    merged = []
    for article in by_source.values():
        stored_article = dict(article)
        try:
            primary_page = int(stored_article.get("page") or 0)
        except (TypeError, ValueError):
            primary_page = 0
        stored_article["source_pages"] = [primary_page] if primary_page > 0 else []
        merged.append(stored_article)
    # 同一天按发布时间倒序，保证轮询中新抓到的报道出现在最前面。
    merged.sort(key=lambda a: str(a.get("id") or ""))
    merged.sort(key=lambda a: str(a.get("published_at_utc") or ""), reverse=True)
    merged.sort(key=lambda a: _date_key(a.get("issue_date", "")), reverse=True)
    # 原子写
    body = "window.economist_db = " + _serialize_for_js(merged) + ";\n"
    fd, tmp_path = tempfile.mkstemp(prefix="database.", suffix=".js.tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _paper_output_root(cfg: dict[str, Any]) -> Path:
    paths = cfg.get("paths") or {}
    out = str(paths.get("output_root") or "").strip()
    return Path(out) if out else (ROOT / "output_results")


def _group_articles_by_issue(articles: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for article in articles:
        issue_date = str(article.get("issue_date") or "").strip()
        if not issue_date:
            continue
        grouped.setdefault(issue_date, []).append(article)
    for issue_date, issue_articles in grouped.items():
        issue_articles.sort(key=lambda a: str(a.get("id") or ""))
        issue_articles.sort(
            key=lambda a: str(a.get("published_at_utc") or ""), reverse=True,
        )
    return dict(sorted(grouped.items(), key=lambda item: item[0], reverse=True))


def _ensure_image_insight_placeholders(
    images: list[str],
    image_insights: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Give every image a blank, truthy description for the shared legacy frontend."""
    by_path: dict[str, dict[str, Any]] = {}
    for raw in image_insights or []:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path") or "").strip()
        if not path or path in by_path:
            continue
        item = dict(raw)
        item["path"] = path
        item.setdefault("image_type", "photo")
        if not str(item.get("description") or ""):
            item["description"] = BLANK_IMAGE_DESCRIPTION
        by_path[path] = item

    output: list[dict[str, Any]] = []
    for value in images:
        path = str(value or "").strip()
        if not path:
            continue
        output.append(by_path.get(path, {
            "path": path,
            "image_type": "photo",
            "description": BLANK_IMAGE_DESCRIPTION,
        }))
    return output


def _normalise_paper_article(article: dict[str, Any], index: int) -> dict[str, Any]:
    article_id = str(article.get("id") or f"art_{index:03d}")
    source_paragraphs = article.get("paragraphs") if isinstance(article.get("paragraphs"), list) else []
    normalized_paragraphs: list[dict[str, Any]] = []
    for para_index, paragraph in enumerate(source_paragraphs, start=1):
        if not isinstance(paragraph, dict):
            continue
        en_text = str(paragraph.get("en_text") or paragraph.get("en_html") or "").strip()
        zh_text = str(paragraph.get("zh_text") or "").strip()
        role = str(paragraph.get("role") or "body").strip() or "body"
        if not en_text and not zh_text:
            continue
        normalized_paragraphs.append(
            {
                "para_id": str(paragraph.get("para_id") or f"{article_id}_p{para_index}"),
                "en_text": en_text,
                "zh_text": zh_text,
                "role": role,
            }
        )

    if not normalized_paragraphs:
        normalized_paragraphs = [
            {
                "para_id": f"{article_id}_p1",
                "en_text": str(article.get("content_markdown") or article.get("content_raw") or "").strip(),
                "zh_text": "",
                "role": "body",
            }
        ]

    content_markdown = str(article.get("content_markdown") or "").strip()
    if not content_markdown:
        content_markdown = "\n\n".join(
            f"## {para['en_text']}" if para.get("role") == "crosshead" else para["en_text"]
            for para in normalized_paragraphs
            if str(para.get("en_text") or "").strip()
        )

    try:
        page = int(article.get("page") or 0)
    except (TypeError, ValueError):
        page = 0
    print_page_label = str(article.get("print_page_label") or "")
    print_section = str(article.get("print_section") or article.get("section") or "General")
    if print_page_label.upper() == "A1" and print_section.casefold() == "main":
        print_section = "PAGE ONE"

    images = [str(path) for path in (article.get("images") or []) if str(path or "").strip()]
    return {
        "id": article_id,
        "publication_type": PAPER_PUBLICATION_TYPE,
        "publication_date": str(article.get("issue_date") or ""),
        "source_pdf": PAPER_PUBLICATION_NAME,
        "page": page,
        "page_article_index": int(article.get("page_article_index") or 0) if str(article.get("page_article_index") or "").isdigit() else index,
        "print_page_label": print_page_label,
        "print_section": print_section,
        "print_page_source": "ereader",
        "source_pages": [page] if page > 0 else [],
        "source_id": str(article.get("source_id") or ""),
        "url": str(article.get("url") or ""),
        "category": str(article.get("section") or "General"),
        "title": str(article.get("title") or ""),
        "title_zh": str(article.get("title_zh") or ""),
        "subtitle": str(article.get("subtitle") or ""),
        "byline": str(article.get("byline") or ""),
        "markdown_path": f"articles/{article_id}.md",
        "summary_md": str(article.get("summary_md") or ""),
        "compiled_article": bool(article.get("compiled_article")),
        "compile_status": str(article.get("compile_status") or "pending"),
        "content_markdown": content_markdown,
        "content_raw": str(article.get("content_raw") or article.get("content_markdown") or "").strip(),
        "paragraphs": normalized_paragraphs,
        "images": images,
        "image_placements": article.get("image_placements") or [],
        "image_insights": _ensure_image_insight_placeholders(
            images, list(article.get("image_insights") or []),
        ),
        "term_annotations": article.get("term_annotations") or [],
        "glossary_analysis_complete": bool(article.get("glossary_analysis_complete")),
        "glossary_version": int(article.get("glossary_version") or 0),
    }


def _write_atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        raise


def _write_atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _build_issue_glossary(articles: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """把逐篇术语条目聚合为前端按 glossary_id 查询的期刊级字典。"""
    glossary: dict[str, dict[str, Any]] = {}
    for article in articles:
        for entry in article.get("glossary_entries") or []:
            if not isinstance(entry, dict):
                continue
            glossary_id = str(entry.get("id") or "").strip()
            if glossary_id:
                glossary[glossary_id] = dict(entry)
    return glossary


def _write_paper_issue_database(
    output_root: Path,
    issue_date: str,
    articles: list[dict[str, Any]],
    cover_image: str = "",
    pages: list[dict[str, Any]] | None = None,
) -> tuple[Path, str, dict[str, Any]]:
    issue_dir = output_root / PAPER_PUBLICATION_TYPE / issue_date
    database_path = issue_dir / "database.js"
    pdf_id = f"{PAPER_PUBLICATION_TYPE}_{issue_date}_wsj-daily"
    normalized_articles = [
        _normalise_paper_article(article, index)
        for index, article in enumerate(articles, start=1)
    ]
    normalized_articles.sort(
        key=lambda article: (
            int(article.get("page") or 10**9),
            int(article.get("page_article_index") or 10**9),
            str(article.get("id") or ""),
        )
    )
    previous_pages: list[dict[str, Any]] = []
    if database_path.exists():
        try:
            previous = database_path.read_text(encoding="utf-8")
            if not cover_image:
                matched = re.search(r'"cover_image"\s*:\s*"([^"]*)"', previous)
                cover_image = matched.group(1) if matched else ""
            payload_match = re.search(
                r"window\.paper_databases\[[^\]]+\]\s*=\s*(\{.*\})\s*;\s*$",
                previous,
                re.DOTALL,
            )
            if payload_match:
                old_payload = json.loads(payload_match.group(1))
                if isinstance(old_payload.get("pages"), list):
                    previous_pages = old_payload["pages"]
        except Exception:
            pass

    page_rows = pages if pages is not None else previous_pages
    if not page_rows:
        page_map: dict[int, dict[str, Any]] = {}
        for article in normalized_articles:
            page_number = int(article.get("page") or 0)
            if page_number <= 0:
                continue
            page_map.setdefault(
                page_number,
                {
                    "page": page_number,
                    "print_page_label": article.get("print_page_label") or "",
                    "print_section": article.get("print_section") or "",
                    "print_page_source": article.get("print_page_source") or "ereader",
                },
            )
        page_rows = [page_map[key] for key in sorted(page_map)]

    normalized_pages: list[dict[str, Any]] = []
    for raw_page in page_rows:
        if not isinstance(raw_page, dict):
            continue
        try:
            page_number = int(raw_page.get("pdf_page") or raw_page.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if page_number <= 0:
            continue
        article_ids = [
            article["id"]
            for article in normalized_articles
            if page_number == int(article.get("page") or 0)
        ]
        if not article_ids:
            continue
        print_page_label = str(raw_page.get("print_page_label") or "")
        print_section = str(raw_page.get("print_section") or "")
        if print_page_label.upper() == "A1" and print_section.casefold() == "main":
            print_section = "PAGE ONE"
        normalized_pages.append(
            {
                "pdf_page": page_number,
                "page_order": len(normalized_pages) + 1,
                "print_page_label": print_page_label,
                "print_section": print_section,
                "print_page_source": str(raw_page.get("print_page_source") or "ereader"),
                "article_ids": article_ids,
            }
        )
    normalized_pages.sort(key=lambda row: row["pdf_page"])
    payload = {
        "id": pdf_id,
        "publication_type": PAPER_PUBLICATION_TYPE,
        "publication_date": issue_date,
        "original_filename": f"The Wall Street Journal - {issue_date}",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cover_image": cover_image,
        "article_count": len(normalized_articles),
        "glossary_version": GLOSSARY_VERSION,
        "glossary": _build_issue_glossary(articles),
        "pages": normalized_pages,
        "articles": normalized_articles,
    }
    text = (
        "window.paper_databases = window.paper_databases || {};\n"
        f'window.paper_databases[{json.dumps(pdf_id, ensure_ascii=False)}] = '
        f"{json.dumps(payload, ensure_ascii=False, indent=2)};\n"
    )
    _write_atomic_text(database_path, text)
    return database_path, pdf_id, payload


def _write_paper_database_index(
    output_root: Path,
    grouped_articles: dict[str, list[dict[str, Any]]],
) -> Path:
    index_path = output_root / "database_index.js"
    items: list[dict[str, Any]] = []
    for issue_date, issue_articles in grouped_articles.items():
        pdf_id = f"{PAPER_PUBLICATION_TYPE}_{issue_date}_wsj-daily"
        cover_image = ""
        issue_database = output_root / PAPER_PUBLICATION_TYPE / issue_date / "database.js"
        try:
            previous = issue_database.read_text(encoding="utf-8")
            matched = re.search(r'"cover_image"\s*:\s*"([^"]*)"', previous)
            cover_image = matched.group(1) if matched else ""
        except Exception:
            pass
        items.append(
            {
                "id": pdf_id,
                "publication_type": PAPER_PUBLICATION_TYPE,
                "publication_date": issue_date,
                "original_filename": f"The Wall Street Journal - {issue_date}",
                "database_path": f"{PAPER_PUBLICATION_TYPE}/{issue_date}/database.js",
                "cover_image": cover_image,
                "article_count": len(issue_articles),
                "sections": sorted({str(article.get("section") or "General") for article in issue_articles}),
                "titles": [article.get("title") for article in issue_articles if article.get("title")],
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
    text = "window.paper_db_index = " + json.dumps(items, ensure_ascii=False, indent=2) + ";\n"
    _write_atomic_text(index_path, text)
    return index_path


def _sync_paper_outputs(
    cfg: dict[str, Any],
    articles: list[dict[str, Any]],
    issue_date: str | None = None,
    issue_covers: dict[str, str] | None = None,
    issue_pages: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    output_root = _paper_output_root(cfg)
    grouped = _group_articles_by_issue(articles)
    for grouped_issue_date, issue_articles in grouped.items():
        _write_paper_issue_database(
            output_root,
            grouped_issue_date,
            issue_articles,
            cover_image=(issue_covers or {}).get(grouped_issue_date, ""),
            pages=(issue_pages or {}).get(grouped_issue_date),
        )
    _write_paper_database_index(output_root, grouped)


def _date_key(s: str) -> int:
    """YYYY-MM-DD → 整数用于排序。非法字符串当作极小值。"""
    try:
        return int(datetime.strptime(s, "%Y-%m-%d").strftime("%Y%m%d"))
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# index.html 生成(把数据库内联进 HTML,产出可独立打开的自包含文件)
# ---------------------------------------------------------------------------


# 模板里这一行会被替换成内联脚本块
_INDEX_TEMPLATE_MARKER = '<script src="database.js"></script>'


def build_index_html(
    output_path: Path,
    db_path: Path,
    template_path: Path,
) -> bool:
    """生成自包含的 index.html(数据库内联,不再依赖外部 database.js)。

    工作流:
      1) 读 template_path(index.html 模板,带 <script src="database.js"></script> 占位)
      2) 读 db_path(合法 database.js)
      3) 抽出 window.economist_db = [...] 数组
      4) 把模板里的占位行替换成 <script>window.economist_db = [...] ;</script>
      5) 原子写到 output_path(临时文件 + os.replace)

    返回 True/False 表示是否成功。
    """
    try:
        template = template_path.read_text(encoding="utf-8")
    except Exception as e:
        log.error(f"[index] 读模板失败 {template_path}:{e}")
        return False

    if _INDEX_TEMPLATE_MARKER not in template:
        log.error(
            f"[index] 模板缺少占位符 {_INDEX_TEMPLATE_MARKER!r},"
            f"请确认 template_path 是新版 index.html"
        )
        return False

    try:
        db_text = db_path.read_text(encoding="utf-8")
    except Exception as e:
        log.error(f"[index] 读数据库失败 {db_path}:{e}")
        return False

    m = re.search(r"window\.economist_db\s*=\s*(\[.*\])\s*;", db_text, re.DOTALL)
    if not m:
        log.error(f"[index] 数据库格式异常,未找到 window.economist_db = [...]")
        return False
    array_str = m.group(1)

    inline = (
        "<script>\n"
        "/* 自包含:数据已内联,无外部文件依赖 */\n"
        f"window.economist_db = {array_str};\n"
        "</script>"
    )
    rendered = template.replace(_INDEX_TEMPLATE_MARKER, inline)

    # 原子写
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="index.", suffix=".html.tmp", dir=output_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(rendered)
        os.replace(tmp_path, output_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        raise

    log.info(
        f"[index] ✓ 已生成自包含 index.html → {output_path}  "
        f"({output_path.stat().st_size:,} bytes,含 {array_str.count(chr(34) + 'id' + chr(34))} 篇文章)"
    )
    return True


def _maybe_rebuild_index(cfg: dict[str, Any]) -> None:
    """如果 .env 配置了 paths.index_html / paths.index_template,重建自包含 index.html。

    任何错误都只记 log 不抛,避免影响抓取主流程。
    """
    paths_cfg = cfg.get("paths") or {}
    out_str = paths_cfg.get("index_html") or ""
    if not out_str:
        return  # 未配置 → 跳过(不影响流程)
    template_str = paths_cfg.get("index_template") or ""
    # 默认模板 = 当前项目里的 index.html(用户可以直接复用既有文件作为模板)
    template_path = Path(template_str) if template_str else (ROOT / "index.html")
    output_path = Path(out_str)

    # db 路径优先用 cfg,否则用全局 DATABASE_JS
    db_str = paths_cfg.get("database_js") or ""
    db_path = Path(db_str) if db_str else DATABASE_JS

    if not template_path.exists():
        log.warning(f"[index] 模板不存在 {template_path},跳过重建")
        return
    if not db_path.exists():
        log.warning(f"[index] 数据库不存在 {db_path},跳过重建")
        return
    try:
        template = template_path.read_text(encoding="utf-8")
    except Exception as e:
        log.warning(f"[index] 读模板失败 {template_path}:{e}")
        return

    if _INDEX_TEMPLATE_MARKER not in template:
        try:
            db_text = db_path.read_text(encoding="utf-8")
            inline = (
                "\n<script>\n"
                "/* legacy inline database for compatibility */\n"
                f"{db_text}\n"
                "</script>\n"
            )
            rendered = template.replace("</body>", f"{inline}</body>")
            if rendered == template:
                rendered = template + inline
            _write_atomic_text(output_path, rendered)
            log.info(f"[index] ✓ 已复制模板并内联旧数据库到 {output_path}")
        except Exception as e:
            log.warning(f"[index] 复制模板失败(不影响抓取):{e}")
        return

    try:
        build_index_html(output_path, db_path, template_path)
    except Exception as e:
        log.warning(f"[index] 重建失败(不影响抓取):{e}")


# ---------------------------------------------------------------------------
# 单篇文章 .md 导出(可选)
# ---------------------------------------------------------------------------


def _article_to_markdown(article: dict[str, Any]) -> str:
    """把一条 article 渲染为「干干净净的英文原文」(不要 front matter / 标题 / 摘要 / 链接等)。

    只返回 content_raw.strip(),其它字段(title / summary_md / url / section 等)全部丢弃。
    """
    return (article.get("content_raw", "") or "").strip() + "\n"


# 文件名里标题部分的最大长度(超出按 word boundary 截断)
_MAX_TITLE_LEN = 60


def _slugify_title(title: str, max_len: int = _MAX_TITLE_LEN) -> str:
    """把标题转成适合做文件名的 slug。

    - 全小写
    - 非字母数字 / 中文字符 → -
    - 连续 - 合并
    - 去掉首尾 -
    - 长度限制(超出在最近的 - 边界截断,避免切到一半单词)
    - 空标题返回空字符串(让调用方只拿 id 命名)
    """
    if not title:
        return ""
    slug = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "-", title).strip("-").lower()
    if len(slug) <= max_len:
        return slug
    # 在 max_len 之前的最后一个 - 处截断,避免切到单词中间
    truncated = slug[:max_len]
    last_dash = truncated.rfind("-")
    if last_dash > max_len // 2:
        truncated = truncated[:last_dash]
    return truncated.strip("-")


def _article_filename(article: dict[str, Any]) -> str:
    """构造文件名:`{slug-title}-{id}.md`。

    - 标题太短或为空 → 只用 `{id}.md`
    - 标题 slug 截断后为空 → 只用 `{id}.md`
    - 标题 + id 之间用 `-` 拼接,便于 grep / Tab 补全 / 跨软件引用
    """
    art_id = article.get("id", "").strip()
    if not art_id:
        raise ValueError("article 必须有 id 字段")
    slug = _slugify_title(article.get("title", "") or "")
    if not slug:
        return f"{art_id}.md"
    return f"{slug}-{art_id}.md"


def write_article_md(article: dict[str, Any], output_dir: Path) -> Path:
    """把一条 article 写到 output_dir / {issue_date} / {slug-title}-{id}.md。

    - 在 output_dir 下按 issue_date(YYYY-MM-DD)建子目录,便于按期翻阅
    - 文件名 = 标题 slug + id(便于 grep / Tab 补全 / 跨软件引用)
    - 原子写(临时文件 + os.replace)
    - 创建中间目录(如不存在)
    - 覆盖已存在(同一 article.id 反复抓取应更新,而不是留多份)
    返回最终文件路径。
    """
    art_id = article.get("id", "").strip()
    if not art_id:
        raise ValueError("article 必须有 id 字段")
    issue_date = article.get("issue_date", "").strip()
    if not issue_date:
        # 兜底:缺 issue_date 时直接放根目录,不强行猜目录名
        sub_dir = output_dir
    else:
        sub_dir = output_dir / issue_date
    sub_dir.mkdir(parents=True, exist_ok=True)

    content = _article_to_markdown(article)
    out_path = sub_dir / _article_filename(article)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f"{out_path.name}.", suffix=".tmp", dir=sub_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, out_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        raise
    return out_path


def _maybe_export_article_md(cfg: dict[str, Any], article: dict[str, Any]) -> None:
    """如果 paths.article_md_dir 已配置,把 article 写成 .md。

    任何错误都只 log 不抛,不影响抓取主流程。
    """
    paths_cfg = cfg.get("paths") or {}
    dir_str = paths_cfg.get("article_md_dir") or ""
    if not dir_str:
        return
    output_dir = Path(dir_str)
    try:
        out = write_article_md(article, output_dir)
        log.info(f"[md] 已导出 {out.name} → {out} ({out.stat().st_size:,} bytes)")
    except Exception as e:
        log.warning(f"[md] 导出失败(不影响抓取):{e}")


# ---------------------------------------------------------------------------
# 板块过滤
# ---------------------------------------------------------------------------


def is_allowed_article(url: str, section: str) -> bool:
    """保留 weeklyedition 中的所有文章，只排除确定不是文章的链接。"""
    del section  # 板块不再参与过滤，Politics、Business 等均应抓取。
    path = urlparse(url).path.lower()
    return bool(path) and not any(keyword in path for keyword in NON_ARTICLE_PATH_KEYWORDS)


# ---------------------------------------------------------------------------
# 浏览器抓取
# ---------------------------------------------------------------------------


def open_browser(user_data_path: str, headless: bool):
    """懒导入 DrissionPage,失败时给出明确指引。

    DrissionPage 4.x API:`ChromiumPage(addr_or_opts=None, ...)`。
    `ChromiumOptions()` 默认 `_address='127.0.0.1:9222'`(连已有浏览器),
    `auto_port()` 又把 `_address` 清成空串导致 `address.split(':')` 崩。
    正确做法:自己挑一个空闲端口,`set_address('127.0.0.1:<port>')` 显式设置。

    创建后立即访问 about:blank 并等待,确保浏览器 UI 渲染好(否则开窗口但 URL 框还是空的)。
    """
    try:
        from DrissionPage import ChromiumOptions, ChromiumPage  # type: ignore
    except ImportError as e:
        sys.exit(f"未安装 drissionpage: pip install -r requirements.txt ({e})")

    opts = ChromiumOptions()

    # 0. 清残留进程 + lock(脚本崩过后会留下孤儿)
    _cleanup_stale_chrome_locks(user_data_path)

    # 1. 寻找空闲端口并显式强绑地址，避开 split(':') 报错
    free_port = _find_free_port()
    opts.set_address(f"127.0.0.1:{free_port}")

    # 2. 设置用户目录
    opts.set_user_data_path(user_data_path)
    opts.headless = bool(headless)

    # 3. Mac 环境下建议加上这两个参数以增加稳定性
    opts.set_argument('--no-sandbox')
    opts.set_argument('--disable-gpu')

    page = ChromiumPage(opts)
    # 强制初始化 + 等 UI 渲染;不然后续 page.get 可能 race-condition
    # (用户报告:窗口弹出但 URL 框空)
    page.get("about:blank")
    time.sleep(1.5)
    log.info(f"浏览器已就绪  url={page.url!r}")
    return page


def _find_free_port(start: int = 9600, end: int = 59600) -> int:
    """在 [start, end] 找一个未占用的 TCP 端口。

    步进 2 是给同进程下后续可能的 tab/连接留余地。
    失败抛 RuntimeError,提示用户检查是否有其它 Chrome 实例占着。
    """
    import socket
    for port in range(start, end + 1, 2):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(
        f"未找到空闲端口 ({start}-{end}),请检查是否有残留的 Chrome 进程:ps aux | grep -i chrome"
    )


def _cleanup_stale_chrome_locks(profile_path: str | Path) -> int:
    """杀掉引用指定 profile 的残留 Chrome 进程,删除 SingletonLock 等。

    返回杀掉的进程数。多次崩溃后会留下孤儿 Chrome 实例把 profile 锁住,
    后续启动会报 BrowserConnectError。脚本启动前清一遍最稳。
    """
    import subprocess
    profile_path = str(profile_path)
    killed = 0
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"user-data-dir={profile_path}"],
            capture_output=True, text=True, timeout=5,
        )
        pids = [p.strip() for p in result.stdout.split() if p.strip().isdigit()]
        for pid in pids:
            try:
                subprocess.run(["kill", "-9", pid], timeout=3, check=False)
                killed += 1
            except Exception:
                pass
    except Exception as e:
        log.warning(f"扫 Chrome 残留进程失败:{e}")
    # 删 lock 文件(进程杀掉后这些文件就没用了)
    for fname in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = Path(profile_path) / fname
        try:
            if p.exists():
                p.unlink()
        except Exception as e:
            log.warning(f"删 lock 文件失败 {p}:{e}")
    lock_p = Path(profile_path) / "Default" / "LOCK"
    try:
        if lock_p.exists():
            lock_p.unlink()
    except Exception:
        pass
    if killed:
        log.info(f"清理了 {killed} 个残留 Chrome 进程 + lock 文件")
    return killed


def fetch_weekly_index(page, issue_date: str | None = None, debug_html_dir: Path | None = None) -> list[dict[str, str]]:
    """访问 weeklyedition 目录,返回 [{url, section, title, issue_date}, ...]。

    `issue_date` 非空时:访问 /weeklyedition/<YYYY-MM-DD> 指定期次(必须是周六)。
    `issue_date` 为空时:访问 /weeklyedition 默认页(最新期)。

    优化策略:绕过容易返回 None 的客户端 JS 执行,直接用 Python 正则解析 SSR 源码。
    """
    if issue_date:
        ok, err = validate_issue_date(issue_date)
        if not ok:
            log.error(f"[weekly] {err}")
            log.error("[weekly] 拒绝抓取(日期不是周六)。如要强行试,绕过此检查即可。")
            return []
        url = f"{WEEKLY_URL}/{issue_date}"
    else:
        url = WEEKLY_URL
    page.get(url)
    page.wait.load_start()
    time.sleep(2.0)  # 留出基础加载时间

    try:
        log.info(f"[weekly] 确认浏览器当前真实 URL: {page.url!r} | 标题: {page.title!r}")
    except Exception as e:
        log.warning(f"[weekly] 读取浏览器基本状态失败: {e}")

    # 获取完整的 HTML 源码
    html_source = page.html

    if debug_html_dir is not None:
        debug_html_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        (debug_html_dir / f"weekly_{stamp}.html").write_text(html_source, encoding="utf-8")
        log.info(f"[weekly] 已 dump 诊断 HTML 到 {debug_html_dir}/weekly_{stamp}.html")

    articles = _parse_weeklyedition_html_v2(html_source)
    cover_url = _weekly_cover_url_from_html(html_source) or _visible_weekly_cover_url(page)
    if cover_url:
        for article in articles:
            article["cover_image_url"] = cover_url
        log.info(f"[weekly] 已识别本期顶部封面图: {cover_url[:120]}")
    else:
        log.warning("[weekly] 未识别到本期顶部封面图，不影响文章抓取")
    return articles


def _weekly_cover_url_from_html(html: str) -> str:
    """从 weekly edition 的结构化 content.cover 读取页面顶部期刊封面。"""
    try:
        matched = re.search(r'<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
        if not matched:
            return ""
        payload = json.loads(matched.group(1).strip())
        cover = payload.get("props", {}).get("pageProps", {}).get("content", {}).get("cover", {})
        value = cover.get("url", "") if isinstance(cover, dict) else ""
        return str(value) if _is_article_image_url(value) else ""
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""


def _visible_weekly_cover_url(page) -> str:
    """选取 weekly edition 首屏中面积最大的 Economist 图片作为期刊封面。"""
    try:
        value = page.run_js(
            """
            (() => Array.from(document.images)
              .map(img => {
                const rect = img.getBoundingClientRect();
                return {
                  src: img.currentSrc || img.src || '',
                  top: rect.top,
                  area: Math.max(rect.width, 0) * Math.max(rect.height, 0)
                };
              })
              .filter(item => item.src && item.top > -80 && item.top < window.innerHeight * 1.5 && item.area > 12000)
              .sort((left, right) => left.top - right.top || right.area - left.area)
              .map(item => item.src)[0] || '')()
            """
        )
    except Exception as exc:
        log.warning(f"[weekly] 读取首屏封面图失败: {exc}")
        return ""
    return str(value or "") if _is_article_image_url(value) else ""


def _parse_weeklyedition_html_v2(html: str) -> list[dict[str, str]]:
    """终极解析版：通过解析 Next.js 的 __NEXT_DATA__ 结构化数据块实现 100% 稳健抓取"""
    import json
    import re
    from datetime import datetime

    out: list[dict[str, str]] = []

    try:
        # 1. 精准提取隐藏在 HTML 尾部的 Next.js 原生 JSON 数据块[cite: 5]
        next_data_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
        if not next_data_match:
            log.error("[weekly] 严重错误：在 HTML 页面中未找到 __NEXT_DATA__ 标识，请检查是否触发高强度人机验证。")
            return []

        # 2. 解析 JSON
        raw_json = json.loads(next_data_match.group(1).strip())

        # 3. 顺藤摸瓜找到周刊的组件树路径[cite: 5]
        page_props = raw_json.get("props", {}).get("pageProps", {})
        content_data = page_props.get("content", {})
        components = content_data.get("components", [])  # 所有的板块大合集[cite: 5]

        # 获取封面日期用于归档[cite: 5]
        issue_date_raw = content_data.get("issueDate", "")  # 格式如 "2026-07-11T00:00:00.000Z"[cite: 5]
        if issue_date_raw:
            issue_date = issue_date_raw.split("T")[0]
        else:
            issue_date = datetime.now().strftime("%Y-%m-%d")

        log.info(f"[weekly] 成功加载 Next.js 核心数据，检测到本期封面日期为: {issue_date}[cite: 5]")

        # 4. 遍历每个 Section 组件[cite: 5]
        for comp in components:
            if comp.get("type") != "COLLECTION":
                continue

            section_name = comp.get("name", "Unknown").strip()  # 比如 "Leaders", "Science & technology"[cite: 5]
            articles = comp.get("articles", [])  # 该板块下的文章数组[cite: 5]

            for art in articles:
                title = art.get("headline", "").strip()  # 原生标题[cite: 5]
                relative_url = art.get("url", "").strip()  # 相对或绝对路径[cite: 5]

                if not relative_url or not title:
                    continue

                # 补全绝对路径 URL
                if relative_url.startswith("/"):
                    full_url = "https://www.economist.com" + relative_url
                else:
                    full_url = relative_url

                # 5. 调用你原有的板块白名单+排除政治商业过滤函数[cite: 3]
                if not is_allowed_article(full_url, section_name):
                    continue

                out.append({
                    "url": full_url,
                    "section": section_name,
                    "title": title[:200],
                    "issue_date": issue_date,
                })

    except Exception as e:
        log.error(f"[weekly] 解析 JSON 数据流时发生异常: {e}", exc_info=True)
        return []

    log.info(f"[weekly] 核心数据流解析完毕，本期共成功捕获 {len(out)} 篇符合条件的有效文章。")
    return out


def _is_article_image_url(value: Any) -> bool:
    return _is_wsj_image_url(value)


def _collect_article_image_urls(value: Any, output: list[str], seen: set[str]) -> None:
    """从 Next.js 文章数据中提取 Economist CDN 图片 URL。"""
    if isinstance(value, str):
        if _is_article_image_url(value) and value not in seen:
            seen.add(value)
            output.append(value)
        return
    if isinstance(value, list):
        for item in value:
            _collect_article_image_urls(item, output, seen)
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_article_image_urls(item, output, seen)


def _visible_article_image_urls(page) -> list[str]:
    """只抓 Explore more 之前已渲染的正文和漫画图片。"""
    try:
        values = page.run_js(
            """
            (() => {
              const marker = Array.from(document.querySelectorAll('h1,h2,h3,h4,p,span,a,div'))
                .find(el => el.textContent.trim() === 'Explore more');
              const beforeMarker = node => !marker || Boolean(
                node.compareDocumentPosition(marker) & Node.DOCUMENT_POSITION_FOLLOWING
              );
              const imageUrls = Array.from(document.querySelectorAll('img'))
                .filter(beforeMarker).map(img => img.currentSrc || img.src);
              const backgroundUrls = Array.from(document.querySelectorAll('[style*="background-image"]'))
                .filter(beforeMarker).map(node => getComputedStyle(node).backgroundImage)
                .map(value => (value.match(/url\\(["']?(.*?)["']?\\)/) || [])[1]);
              return imageUrls.concat(backgroundUrls).filter(Boolean);
            })()
            """
        ) or []
    except Exception as exc:
        log.warning(f"图片 DOM 提取失败(不影响正文): {exc}")
        return []
    return [value for value in values if _is_article_image_url(value)]


def _parse_interactive_article_html(html_source: str) -> tuple[str, str, list[str]]:
    """Extract article content from Economist's standalone Svelte interactives."""
    try:
        from lxml import html as lxml_html  # type: ignore
    except ImportError:
        return "", "", []

    try:
        document = lxml_html.fromstring(html_source)
    except (TypeError, ValueError):
        return "", "", []

    main_nodes = document.xpath("//main")
    main = main_nodes[0] if main_nodes else document
    title_nodes = main.xpath(".//h1") or document.xpath("//h1")
    title = ""
    if title_nodes:
        title = " ".join(" ".join(title_nodes[0].itertext()).split())

    blocks: list[str] = []
    seen_blocks: set[str] = set()
    for node in main.xpath(".//body-text"):
        text = " ".join(" ".join(node.itertext()).split())
        if len(text) < 20 or text in seen_blocks:
            continue
        seen_blocks.add(text)
        blocks.append(text)

    image_urls: list[str] = []
    seen_images: set[str] = set()
    for image in main.xpath(".//img"):
        if any(
            ancestor.tag == "footer"
            or "related-content" in str(ancestor.get("class", ""))
            for ancestor in image.iterancestors()
        ):
            continue
        candidates = [
            image.get("src", ""),
            image.get("data-src", ""),
            image.get("data-original", ""),
        ]
        for srcset_name in ("srcset", "data-srcset"):
            srcset = image.get(srcset_name, "")
            candidates.extend(
                item.strip().split()[0]
                for item in srcset.split(",")
                if item.strip()
            )
        for candidate in candidates:
            resolved = urljoin("https://www.economist.com", candidate.strip())
            if _is_article_image_url(resolved) and resolved not in seen_images:
                seen_images.add(resolved)
                image_urls.append(resolved)

    return title, "\n\n".join(blocks), image_urls


def fetch_article_content(page, url: str) -> tuple[str, str, list[str]]:
    """访问单篇文章，返回 (title, content_raw, image_urls)。

    【核心修正版】：放弃不稳定的动态 DOM 抓取，全面转向提取并解析页面底部的 __NEXT_DATA__ JSON 块。
    100% 免疫前端改名、懒加载截断和动态闪烁，确保长文章全文无损恢复。
    """
    page.get(url)
    page.wait.load_start()
    is_interactive = "/interactive/" in urlparse(url).path
    time.sleep(5.0 if is_interactive else 1.5)

    html_source = page.html
    title = ""
    body_text = ""
    image_urls: list[str] = []
    seen_image_urls: set[str] = set()

    try:
        import re
        import json

        # 1. 强行用正则切出隐藏在源码底部的 Next.js 全局结构化数据块
        next_data_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html_source)
        if next_data_match:
            raw_json = json.loads(next_data_match.group(1).strip())

            # 2. 定位到文章的内容核心(props -> pageProps -> content)[cite: 6]
            content_data = raw_json.get("props", {}).get("pageProps", {}).get("content", {})

            # 3. 提取文章的标准 Headline[cite: 6]
            title = content_data.get("headline", "").strip()

            # 4. 精准提取完整的正文数组（躺在 JSON 里的 body 节点中）[cite: 6]
            body_components = content_data.get("body", [])
            # 正文图通常在 body 中；部分漫画和导语图只出现在文章根节点的 lead image。
            # 只读这些明确字段，不能再递归扫描整个 content，否则会混入 Explore more 的周刊封面。
            for key in (
                "image", "imageUrl", "image_url", "leadImage", "lead_image",
                "leadMedia", "leadComponent", "media", "imageData",
            ):
                _collect_article_image_urls(content_data.get(key), image_urls, seen_image_urls)
            _collect_article_image_urls(body_components, image_urls, seen_image_urls)
            paragraphs = []

            for node in body_components:
                # 如果是标准英文段落，一字不落地提取纯文本[cite: 6]
                if node.get("type") == "PARAGRAPH":
                    text = node.get("text", "").strip()
                    if text:
                        paragraphs.append(text)
                # 如果是文章内部的排版小标题(CROSSHEAD)，带上 Markdown 格式保留[cite: 6]
                elif node.get("type") == "CROSSHEAD":
                    text = node.get("text", "").strip()
                    if text:
                        paragraphs.append(f"\n## {text}\n")

            if paragraphs:
                body_text = "\n\n".join(paragraphs)
                log.info(
                    f"[single-url] 成功通过 __NEXT_DATA__ 通道提取全文，共 {len(paragraphs)} 个正文/标题节点[cite: 6]。")

    except Exception as e:
        log.error(f"[single-url] 通过 JSON 核心提取正文失败，正在切换至常规 DOM 兜底保底: {e}")

    if not body_text and is_interactive:
        interactive_title, interactive_body, interactive_images = _parse_interactive_article_html(html_source)
        title = title or interactive_title
        body_text = interactive_body
        for image_url in interactive_images:
            if image_url not in seen_image_urls:
                seen_image_urls.add(image_url)
                image_urls.append(image_url)
        if body_text:
            log.info(
                "[single-url] 成功通过交互页 HTML 通道提取全文，共 "
                f"{len(body_text.split(chr(10) + chr(10)))} 个正文组件。"
            )

    # =========================================================================
    # 🌟 强力兜底保底逻辑：如果上面的原生通道发生未料异常，用硬捞机制保底
    # =========================================================================
    if not body_text:
        log.info("[single-url] 触发兜底机制，正在捞取可见元素...")
        payload = page.run_js(
            """
            (() => {
              // 强兼容 2026 最新版特征的 H1 和标准 H1[cite: 6]
              const h1 = document.querySelector('h1.css-1tik00t')
                      || document.querySelector('h1[data-test-id="article-headline"]')
                      || document.querySelector('h1');

              // 强捞所有带 paragraph 标记的组件，以及普通的 p 标签[cite: 6]
              const ps = Array.from(document.querySelectorAll('p[data-component="paragraph"], div.article-body p, p'));
              return {
                title: h1 ? h1.innerText.trim() : '',
                body: ps.map(p => p.innerText.trim()).filter(Boolean).join('\\n\\n')
              };
            })()
            """
        )
        title = title or (payload or {}).get("title", "")
        body_text = (payload or {}).get("body", "")

    for image_url in _visible_article_image_urls(page):
        if image_url not in seen_image_urls:
            seen_image_urls.add(image_url)
            image_urls.append(image_url)
    return title, body_text, image_urls


def _image_extension(url: str, content_type: str) -> str:
    content_type = content_type.lower().split(";", 1)[0].strip()
    by_type = {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
        "image/webp": ".webp", "image/gif": ".gif",
    }
    if content_type in by_type:
        return by_type[content_type]
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"


def _vision_supported_image(content: bytes, content_type: str) -> tuple[bytes, str]:
    """把 AVIF 等 vision API 不接受的格式转换为 RGB JPEG。"""
    normalized_type = content_type.lower().split(";", 1)[0].strip()
    supported = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
    is_avif = b"ftypavif" in content[:32] or b"ftypavis" in content[:32]
    if normalized_type in supported and not is_avif:
        return content, normalized_type
    try:
        from PIL import Image  # type: ignore

        with Image.open(BytesIO(content)) as image:
            output = BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=90, optimize=True)
            return output.getvalue(), "image/jpeg"
    except Exception as exc:
        raise ValueError(f"无法把 {normalized_type or 'unknown image'} 转换为 JPEG: {exc}") from exc


def materialize_article_images(
    image_urls: list[str], cfg: dict[str, Any], issue_date: str, article_id: str,
) -> list[str]:
    """下载图片到期刊目录；下载失败时保留原 URL，确保页面仍可展示。"""
    if not image_urls:
        return []
    try:
        import requests
    except ImportError as exc:
        log.warning(f"未安装 requests，图片保留远程 URL: {exc}")
        return image_urls

    image_dir = _paper_output_root(cfg) / PAPER_PUBLICATION_TYPE / issue_date / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    path_by_digest: dict[str, str] = {}
    for index, image_url in enumerate(image_urls[:20], start=1):
        try:
            response = requests.get(
                image_url,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "image/webp,image/png,image/jpeg,image/*;q=0.8"},
                timeout=30,
            )
            content_type = response.headers.get("Content-Type", "")
            if response.status_code != 200 or not content_type.lower().startswith("image/"):
                raise ValueError(f"HTTP {response.status_code}, Content-Type={content_type!r}")
            image_content, content_type = _vision_supported_image(response.content, content_type)
            digest = hashlib.sha256(image_content).hexdigest()
            if digest in path_by_digest:
                paths.append(path_by_digest[digest])
                continue
            filename = f"{article_id}_{index:02d}{_image_extension(image_url, content_type)}"
            target = image_dir / filename
            _write_atomic_bytes(target, image_content)
            relative_path = f"images/{filename}"
            path_by_digest[digest] = relative_path
            paths.append(relative_path)
        except Exception as exc:
            log.warning(f"图片下载失败，保留远程 URL: {image_url[:100]} ({exc})")
            paths.append(image_url)
    return paths


def materialize_ereader_images(
    source_images: list[EReaderImage],
    cfg: dict[str, Any],
    issue_date: str,
    article_id: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    """下载 eReader 正文图，同时保留图注、credit 和原始正文位置。"""
    unique_urls: list[str] = []
    for image in source_images:
        url = str(image.url or "").strip()
        if url and url not in unique_urls:
            unique_urls.append(url)
    materialized = materialize_article_images(unique_urls, cfg, issue_date, article_id)
    path_by_url = dict(zip(unique_urls, materialized))
    placements: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int | None]] = set()
    for image in source_images:
        path = path_by_url.get(str(image.url or "").strip())
        if not path:
            continue
        placement = str(image.placement or "unlocated")
        paragraph_index = image.after_paragraph_index
        identity = (path, placement, paragraph_index)
        if identity in seen:
            continue
        seen.add(identity)
        placements.append(_normalize_image_placement_metadata({
            "path": path,
            "placement": placement,
            "after_paragraph_index": paragraph_index,
            "caption": str(image.caption or ""),
            "credit": str(image.credit or ""),
            "alt_text": str(image.alt_text or ""),
        }))
    return list(dict.fromkeys(materialized)), placements


def deduplicate_article_image_metadata(
    article: dict[str, Any], cfg: dict[str, Any],
) -> None:
    """Collapse local image paths with identical bytes while keeping one metadata record."""
    issue_date = str(article.get("issue_date") or "").strip()
    issue_root = _paper_output_root(cfg) / PAPER_PUBLICATION_TYPE / issue_date
    canonical_by_digest: dict[str, str] = {}
    canonical_by_path: dict[str, str] = {}
    for value in article.get("images") or []:
        path = str(value or "").strip()
        canonical = path
        local_path = issue_root / path if path.startswith("images/") else None
        if local_path is not None and local_path.is_file():
            digest = hashlib.sha256(local_path.read_bytes()).hexdigest()
            canonical = canonical_by_digest.setdefault(digest, path)
        canonical_by_path[path] = canonical
    article["images"] = list(dict.fromkeys(
        canonical_by_path.get(str(path or ""), str(path or ""))
        for path in article.get("images") or []
        if str(path or "").strip()
    ))

    deduplicated: list[dict[str, Any]] = []
    by_identity: dict[tuple[str, str, int | None], dict[str, Any]] = {}
    for raw_item in article.get("image_placements") or []:
        if not isinstance(raw_item, dict):
            continue
        item = _normalize_image_placement_metadata(raw_item)
        path = str(item.get("path") or "")
        item["path"] = canonical_by_path.get(path, path)
        identity = (
            str(item.get("path") or ""),
            str(item.get("placement") or "unlocated"),
            item.get("after_paragraph_index"),
        )
        existing = by_identity.get(identity)
        if existing is None:
            by_identity[identity] = item
            deduplicated.append(item)
            continue
        for key in ("caption", "credit", "alt_text"):
            if not str(existing.get(key) or "").strip() and str(item.get(key) or "").strip():
                existing[key] = item[key]
    article["image_placements"] = deduplicated

    deduplicated_insights: list[dict[str, Any]] = []
    insight_by_path: dict[str, dict[str, Any]] = {}
    for raw_item in article.get("image_insights") or []:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        path = canonical_by_path.get(path, path)
        item["path"] = path
        existing = insight_by_path.get(path)
        if existing is None:
            insight_by_path[path] = item
            deduplicated_insights.append(item)
        elif not str(existing.get("description") or "").strip() and str(item.get("description") or "").strip():
            existing["description"] = item["description"]
    article["image_insights"] = deduplicated_insights


def _looks_like_image_credit(text: str) -> bool:
    value = str(text or "").strip()
    if not value or len(value) > 100 or re.search(r"[.!?]", value):
        return False
    letters = [char for char in value if char.isalpha()]
    return bool(letters) and value == value.upper()


def _normalize_image_placement_metadata(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    caption = str(normalized.get("caption") or "").strip()
    credit = str(normalized.get("credit") or "").strip()
    prefixed_credit = re.match(
        r"^([A-Z][A-Z .'/’-]{2,60})\s+((?:From|At|Above|Below|Left|Right)\b[\s\S]+)$",
        caption,
    )
    if prefixed_credit:
        prefix, caption = prefixed_credit.groups()
        credit = " | ".join(part for part in (credit, prefix.strip()) if part)
    elif _looks_like_image_credit(caption):
        credit = " | ".join(part for part in (credit, caption) if part)
        caption = ""
    normalized["caption"] = caption
    normalized["credit"] = credit
    if not caption and str(normalized.get("caption_zh") or "").strip() in {
        "无图片说明", "暂无图片说明", "图片无说明",
    }:
        normalized["caption_zh"] = ""
    return normalized


def _migrate_image_description_fields(
    placements: list[dict[str, Any]],
    image_insights: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Move legacy placement descriptions into the shared image_insights schema."""
    cleaned_placements: list[dict[str, Any]] = []
    insights: list[dict[str, Any]] = []
    insight_by_path: dict[str, dict[str, Any]] = {}
    for raw in image_insights or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        item["path"] = path
        insights.append(item)
        insight_by_path.setdefault(path, item)

    for raw in placements:
        if not isinstance(raw, dict):
            continue
        item = _normalize_image_placement_metadata(raw)
        path = str(item.get("path") or "").strip()
        legacy_description = str(
            item.pop("description_zh", "") or item.pop("caption_zh", "") or ""
        ).strip()
        item.pop("caption_zh", None)
        cleaned_placements.append(item)
        if not path or not legacy_description:
            continue
        insight = insight_by_path.get(path)
        if insight is None:
            insight = {"path": path, "image_type": "photo", "description": legacy_description}
            insights.append(insight)
            insight_by_path[path] = insight
        else:
            insight["description"] = legacy_description
            insight.setdefault("image_type", "photo")
    return cleaned_placements, insights


def _set_image_insight_description(
    image_insights: list[dict[str, Any]], path: str, description: str,
) -> None:
    for insight in image_insights:
        if str(insight.get("path") or "") == path:
            insight["description"] = description
            insight.setdefault("image_type", "photo")
            return
    image_insights.append({
        "path": path,
        "image_type": "photo",
        "description": description,
    })


def translate_image_descriptions(
    client: Any,
    cfg: dict[str, Any],
    title: str,
    placements: list[dict[str, Any]],
    image_insights: list[dict[str, Any]] | None,
    log_: logging.Logger,
) -> list[dict[str, Any]]:
    """Translate source captions into image_insights descriptions."""
    translated, insights = _migrate_image_description_fields(placements, image_insights)
    described_paths = {
        str(item.get("path") or "")
        for item in insights
        if str(item.get("description") or "").strip()
    }
    pending: list[tuple[int, str]] = []
    for index, item in enumerate(translated, start=1):
        path = str(item.get("path") or "").strip()
        source = str(item.get("caption") or "").strip()
        if not source:
            alt_text = str(item.get("alt_text") or "").strip()
            if not re.match(r"^(?:https?://|www\.)", alt_text, re.IGNORECASE):
                source = alt_text
        if not path or path in described_paths or not source:
            continue
        if count_cn_chars(source):
            _set_image_insight_description(insights, path, source)
            described_paths.add(path)
            continue
        pending.append((index, source))
    if not pending:
        return insights

    prompt_items = "\n".join(
        f'{index}. {json.dumps(source, ensure_ascii=False)}'
        for index, source in pending
    )
    prompt = f"""请把下面《华尔街日报》报道的图片说明翻译成自然、简洁的中文，只返回严格 JSON。

要求：
1. 忠实保留人物、地点、机构和画面信息，不增加原文没有的事实。
2. 原文可能来自报纸 OCR；可以修正明显的重复字母、重复标点和断词，但不得据此补写内容。
3. 保持输入 index 不变，每条输入必须返回一条非空译文。
4. 只翻译图片说明，不处理摄影署名。

文章标题：{title}
图片说明：
{prompt_items}

Return JSON in this shape:
{{"captions":[{{"index":1,"description":"中文图片说明"}}]}}
"""
    llm = cfg["llm"]
    max_retries = int(cfg["crawl"].get("max_retries", 2))
    last_error: Exception | None = None
    expected = {index for index, _ in pending}
    completed: dict[int, str] = {}
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=llm.get("model", "gpt-4o-mini"),
                messages=[
                    {
                        "role": "system",
                        "content": "只返回 JSON。你是严谨的英中新闻图片编辑。",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max(1024, len(pending) * 300),
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            payload = _extract_json_payload(response.choices[0].message.content or "")
            raw_captions = payload.get("captions")
            if not isinstance(raw_captions, list):
                raise LLMOutputValidationError("图片说明翻译结果不是 captions 数组")
            by_index: dict[int, str] = {}
            for item in raw_captions:
                if not isinstance(item, dict):
                    continue
                try:
                    index = int(item.get("index") or 0)
                except (TypeError, ValueError):
                    continue
                description = str(
                    item.get("description") or item.get("caption_zh") or ""
                ).strip()
                if index in expected and description and count_cn_chars(description):
                    by_index[index] = description
            completed.update(by_index)
            missing = expected - set(completed)
            if missing and attempt < max_retries:
                raise LLMOutputValidationError(
                    "图片说明中文翻译缺失: " + ", ".join(map(str, sorted(missing)))
                )
            for index, description in completed.items():
                path = str(translated[index - 1].get("path") or "").strip()
                if path:
                    _set_image_insight_description(insights, path, description)
            if missing:
                log_.warning(
                    "部分图片说明没有可用中文翻译，保留对应原文: %s",
                    ", ".join(map(str, sorted(missing))),
                )
            return insights
        except Exception as exc:
            last_error = exc
            log_.warning(f"图片说明翻译失败 (attempt {attempt + 1}): {exc}")
            if not _should_retry_llm_error(exc, attempt, max_retries):
                break
            time.sleep(1.0)
    for index, description in completed.items():
        path = str(translated[index - 1].get("path") or "").strip()
        if path:
            _set_image_insight_description(insights, path, description)
    log_.error(f"图片说明翻译最终失败，已保留成功项和其余英文原文: {title} ({last_error})")
    return insights


def _wsj_text_cover_url(graph_url: str) -> str:
    """Return the high-resolution transparent text layer matching graph1024."""
    parsed = urlparse(str(graph_url or "").strip())
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if query.get("type") != "graph1024":
        return ""
    query["type"] = "text1024"
    return urlunparse(parsed._replace(query=urlencode(query)))


def _cover_jpeg_bytes(graph_content: bytes, text_content: bytes = b"") -> bytes:
    """Normalize a cover, compositing eReader's transparent text layer when present."""
    from PIL import Image  # type: ignore

    with Image.open(BytesIO(graph_content)) as graph:
        base = graph.convert("RGBA")
        if text_content:
            with Image.open(BytesIO(text_content)) as text:
                overlay = text.convert("RGBA")
                if overlay.size != base.size:
                    overlay = overlay.resize(base.size, Image.Resampling.LANCZOS)
                base = Image.alpha_composite(base, overlay)
        output = BytesIO()
        base.convert("RGB").save(output, format="JPEG", quality=90, optimize=True)
        return output.getvalue()


def materialize_issue_cover(
    cover_url: str,
    cfg: dict[str, Any],
    issue_date: str,
    page: Any | None = None,
) -> str:
    """Download the complete first newspaper page as ``cover.jpg``."""
    parsed = urlparse(str(cover_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    try:
        import requests

        text_url = _wsj_text_cover_url(cover_url)
        session = requests.Session()
        if page is not None:
            for cookie in page.cookies(all_domains=True, all_info=True) or []:
                session.cookies.set(
                    str(cookie.get("name") or ""),
                    str(cookie.get("value") or ""),
                    domain=str(cookie.get("domain") or "") or None,
                    path=str(cookie.get("path") or "/"),
                )

        def download(image_url: str) -> bytes:
            response = session.get(
                image_url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": str(getattr(page, "url", "") or EREADER_URL),
                    "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                },
                timeout=45,
            )
            content_type = response.headers.get("Content-Type", "")
            if response.status_code != 200 or not content_type.lower().startswith("image/"):
                raise ValueError(f"HTTP {response.status_code}, Content-Type={content_type!r}")
            return response.content

        graph_content = download(cover_url)
        text_content = download(text_url) if text_url else b""
        target = _paper_output_root(cfg) / PAPER_PUBLICATION_TYPE / issue_date / "cover.jpg"
        _write_atomic_bytes(target, _cover_jpeg_bytes(graph_content, text_content))
        return target.name
    except Exception as exc:
        log.warning(f"[ereader] 封面图下载失败，不影响文章抓取: {exc}")
        return ""


def analyze_article_images(
    client: Any,
    cfg: dict[str, Any],
    issue_date: str,
    title: str,
    images: list[str],
    log_: logging.Logger,
) -> list[dict[str, Any]]:
    """用 .env LLM 对正文图片/图表做 50-80 字中文简析。"""
    settings = cfg["image_analysis"]
    if not settings.get("enabled") or not images:
        return []
    output_root = _paper_output_root(cfg)
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": (
            "请逐张分析下面文章中的图片或图表。每张只写一段 50-80 个中文字符的简短说明，"
            "说明画面/图表展示的内容及其与文章的关系；不要编造图片中看不出的数字或事实。"
            "返回严格 JSON，不要 Markdown。格式："
            '{"images":[{"index":1,"image_type":"photo|chart|cartoon|illustration",'
            '"description":"50-80字中文简析"}]}\n文章标题：' + title
        ),
    }]
    usable_images = images[: max(1, int(settings["max_images"]))]
    for image_path in usable_images:
        if image_path.startswith("images/"):
            local_path = output_root / PAPER_PUBLICATION_TYPE / issue_date / image_path
            try:
                import base64
                import mimetypes
                mime = mimetypes.guess_type(local_path.name)[0] or "image/jpeg"
                data = base64.b64encode(local_path.read_bytes()).decode("ascii")
                image_url = f"data:{mime};base64,{data}"
            except Exception as exc:
                log_.warning(f"读取本地图片失败，跳过解析 {image_path}: {exc}")
                continue
        elif _is_article_image_url(image_path):
            image_url = image_path
        else:
            continue
        content.append({"type": "image_url", "image_url": {"url": image_url, "detail": "low"}})

    if len(content) == 1:
        return []
    model = settings.get("model") or cfg["llm"].get("model", "gpt-4o-mini")
    for attempt in range(int(settings["max_retries"]) + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是严谨的图片与数据图表编辑，只返回 JSON。"},
                    {"role": "user", "content": content},
                ],
                max_tokens=int(settings["max_tokens"]),
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            raw = _extract_json_payload(response.choices[0].message.content or "")
            raw_items = (
                raw.get("images")
                or raw.get("image_insights")
                or raw.get("analyses")
                or raw.get("items")
                or []
            )
            if not isinstance(raw_items, list):
                raise ValueError("图片解析结果不是 images 数组")
            insights: list[dict[str, Any]] = []
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                try:
                    index = int(item.get("index") or item.get("image_index") or item.get("image_number") or 0)
                except (TypeError, ValueError):
                    continue
                if index == 0 and len(usable_images) == 1:
                    index = 1
                description = str(
                    item.get("description") or item.get("analysis") or item.get("caption") or ""
                ).strip()
                if 35 <= count_cn_chars(description) < 50:
                    description += "，帮助读者把握文章所讨论的背景与变化。"
                if not (1 <= index <= len(usable_images)) or not (50 <= count_cn_chars(description) <= 80):
                    continue
                image_type = str(item.get("image_type") or "illustration").strip().lower()
                if image_type not in {"photo", "chart", "cartoon", "illustration"}:
                    image_type = "illustration"
                insights.append({
                    "path": usable_images[index - 1],
                    "image_type": image_type,
                    "description": description,
                })
            if insights:
                return insights
            raise ValueError(
                "图片解析结果没有合格的 50-80 字说明: "
                + json.dumps(raw, ensure_ascii=False)[:500]
            )
        except Exception as exc:
            log_.warning(f"图片解析失败 (attempt {attempt + 1}): {exc}")
            if not _should_retry_llm_error(exc, attempt, int(settings["max_retries"])):
                break
    return []


def _parse_article_html(html: str) -> tuple[str, str]:
    """从文章页 HTML 中抽 (title, content_raw)。

    - title: 第一个 <h1> 的纯文本
    - content_raw: 所有 <p data-component="paragraph"> 段落拼接(双换行分隔),
      每段去掉内嵌 HTML(<span>/<small>/<a>)只留文本
    """
    # 标题
    title = ""
    m = ARTICLE_H1_RE.search(html)
    if m:
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    # 正文段落
    paragraphs: list[str] = []
    for m in ARTICLE_P_RE.finditer(html):
        # 去内嵌 tag(drop caps 之类)
        text = re.sub(r"<[^>]+>", "", m.group(1))
        # 解码 HTML 实体
        text = (text.replace("&amp;", "&")
                    .replace("&lt;", "<")
                    .replace("&gt;", ">")
                    .replace("&quot;", '"')
                    .replace("&#39;", "'")
                    .replace("&nbsp;", " "))
        text = re.sub(r"\s+", " ", text).strip()
        if text and len(text) > 10:  # 跳过太短的(可能是 drop cap 残段)
            paragraphs.append(text)
    body = "\n\n".join(paragraphs)
    return title, body


# ---------------------------------------------------------------------------
# LLM 总结
# ---------------------------------------------------------------------------


def _normalize_base_url(url: str) -> str:
    """OpenAI SDK 会在 base_url 后自动拼 /chat/completions。

    如果用户配的是完整 endpoint(包含 /chat/completions),SDK 会拼成双段导致 404。
    这里自动剥离尾部 /chat/completions(以及可能的尾斜杠)。
    """
    u = url.rstrip("/")
    suffix = "/chat/completions"
    if u.endswith(suffix):
        u = u[: -len(suffix)].rstrip("/")
    return u


def make_llm_client(cfg: dict[str, Any]):
    """懒导入 OpenAI,避免无 LLM 依赖时测试失败。"""
    from openai import OpenAI  # type: ignore
    llm = cfg["llm"]
    kwargs: dict[str, Any] = {
        "api_key": llm["api_key"],
        "timeout": llm.get("timeout_s", 60),
        # 由本项目按错误类型显式重试，避免 SDK 默认重试叠加并放大超时时间。
        "max_retries": 0,
    }
    base = llm.get("base_url")
    if base:
        kwargs["base_url"] = _normalize_base_url(base)
    return OpenAI(**kwargs)


def _strip_cot(text: str) -> str:
    """剥掉 LLM 返回里的英文 Chain of Thought,只留从「🌟 一句话核心主旨」开始的中文 Markdown。

    标记位置策略(从最具体到最宽松):
      1. 精确匹配「🌟 一句话核心主旨」(含 emoji)→ 从该位置开始
      2. 退而求其次匹配「一句话核心主旨」(无 emoji,某些模型会剥掉)
      3. 都没有 → 返回原文(后续 MIN_CN_CHARS 校验会失败,自然触发重试/丢弃)

    用 rfind(取最后出现的位置)是因为模型偶尔会在 CoT 里复述一遍 marker,
    取最后一次出现的版本能避开「假」marker,得到真正的最终回答。
    """
    for marker in ("🌟 一句话核心主旨", "一句话核心主旨"):
        idx = text.rfind(marker)
        if idx < 0:
            continue
        # 若 marker 前 10 字符内出现 ### 标题前缀,把它一起带上,保持 Markdown 完整
        prefix_start = max(0, idx - 10)
        prefix = text[prefix_start:idx]
        if "###" in prefix:
            start = prefix_start + prefix.rfind("###")
        else:
            start = idx
        return text[start:].strip()
    return text  # 不裁剪,让下游兜底


def summarize(
        client: Any,
        cfg: dict[str, Any],
        title: str,
        body: str,
        log_: logging.Logger,
) -> str:
    """调 LLM 生成中文摘要。

    【无情中文字段提取版】：
    1. 不再盲目进行从头到尾的粗暴切片。
    2. 采用精确块提取技术，分别去捞 4 个核心中文大类下最纯净的中文文本。
    3. 彻底过滤、原地蒸发任何大模型吐出来的中英混合碎碎念与字数统计。
    """
    llm = cfg["llm"]
    user_prompt = f"标题:{title}\n\n原文:\n{body[:12000]}"
    last_err: Exception | None = None

    for attempt in range(int(cfg["crawl"].get("max_retries", 2)) + 1):
        try:
            resp = client.chat.completions.create(
                model=llm.get("model", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=2048,
                temperature=0.3,
            )
            raw_text = (resp.choices[0].message.content or "").strip()

            # =========================================================================
            # 🎯 核心修正：精准打流，分块硬捞中文内容，彻底干掉任何英文和复读干扰
            # =========================================================================
            # 分别提取 4 个核心块的内容（通过寻找标志位之间的中文段落）
            block_1 = re.search(r'🌟\s*一句话核心主旨[\s\S]*?(?=🔍\s*核心观点|🤨\s*争议|🔮\s*未来|$)', raw_text)
            block_2 = re.search(r'🔍\s*核心观点与论据拆解[\s\S]*?(?=🤨\s*争议|🔮\s*未来|$)', raw_text)
            block_3 = re.search(r'🤨\s*争议与潜在挑战[\s\S]*?(?=🔮\s*未来|$)', raw_text)
            block_4 = re.search(r'🔮\s*未来趋势预判[\s\S]*?$', raw_text)

            # 安全组装 Markdown 字符串
            final_blocks = []
            if block_1: final_blocks.append(
                "### 🌟 一句话核心主旨\n" + block_1.group(0).split("一句话核心主旨")[-1].strip())
            if block_2: final_blocks.append(
                "### 🔍 核心观点与论据拆解\n" + block_2.group(0).split("核心观点与论据拆解")[-1].strip())
            if block_3: final_blocks.append(
                "### 🤨 争议与潜在挑战\n" + block_3.group(0).split("争议与潜在挑战")[-1].strip())
            if block_4:
                # 未来趋势可能带有多余英文统计，清洗掉英文字符开头的内容
                b4_text = block_4.group(0).split("未来趋势预判")[-1].strip()
                # 寻找可能存在的英文统计小句，平滑切除
                b4_text = re.split(r'\n(?:Let|Wait|I\s|Re-reading)', b4_text)[0].strip()
                final_blocks.append("### 🔮 未来趋势预判\n" + b4_text)

            # 如果捞取出了哪怕一两个干净的中文块，用它们作为完美输出
            if len(final_blocks) >= 2:
                final_chinese_summary = "\n\n".join(final_blocks)
            else:
                final_chinese_summary = raw_text  # 兜底保底

            # =========================================================================
            # 🎯 尾部残句平滑切除防乱码
            # =========================================================================
            if final_chinese_summary and final_chinese_summary[-1] not in ['。', '？', '！', '」', '】', '`']:
                last_period = final_chinese_summary.rfind('。')
                if last_period > 0:
                    final_chinese_summary = final_chinese_summary[:last_period + 1]

            # 最终中文字数统计
            cn_count = count_cn_chars(final_chinese_summary)
            if cn_count >= MIN_CN_CHARS:
                return final_chinese_summary

            if attempt >= 1:
                last_err = ValueError(f"摘要纯净字数不足({cn_count}<{MIN_CN_CHARS})")
                break
            log_.warning(f"摘要纯净字数仍未达标({cn_count}<{MIN_CN_CHARS})，再重试一次")

        except Exception as e:
            last_err = e
            log_.warning(f"LLM 连线调用失败 (attempt {attempt + 1}): {e}")
            if not _should_retry_llm_error(e, attempt, int(cfg["crawl"].get("max_retries", 2))):
                break

        time.sleep(1.0)

    log_.error(f"摘要最终失败，丢弃: {title} ({last_err})")
    return ""


def count_cn_chars(s: str) -> int:
    return len(CN_CHAR_RE.findall(s))


class LLMOutputValidationError(ValueError):
    """LLM 返回了可解析但不符合文章输出约束的内容。"""


def _split_article_paragraphs(body: str) -> list[dict[str, str]]:
    """把文章正文拆成可翻译段落,保留 crosshead / 普通段落的角色信息。"""
    paragraphs: list[dict[str, str]] = []
    for part in re.split(r"\n\s*\n", str(body or "").strip()):
        text = part.strip()
        if not text:
            continue
        role = "body"
        if text.startswith("## "):
            role = "crosshead"
            text = text[3:].strip()
        elif text.startswith("### "):
            role = "crosshead"
            text = text[4:].strip()
        paragraphs.append({"role": role, "en_text": text})
    return paragraphs


def _format_source_content_markdown(paragraphs: list[dict[str, str]]) -> str:
    """把源正文整理成稳定的英文 Markdown。"""
    rendered: list[str] = []
    for paragraph in paragraphs:
        text = str(paragraph.get("en_text") or "").strip()
        if not text:
            continue
        if paragraph.get("role") == "crosshead":
            rendered.append(f"## {text}")
        else:
            rendered.append(text)
    return "\n\n".join(rendered).strip()


def _extract_json_payload(text: str) -> dict[str, Any]:
    """从 LLM 响应里抠出 JSON object。"""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.IGNORECASE).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("LLM JSON 不是 object")
    return data


def _should_retry_llm_error(exc: Exception, attempt: int, max_retries: int) -> bool:
    """只重试短暂故障；422、认证和参数错误不会因重复请求而恢复。"""
    if attempt >= max_retries:
        return False
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code in {408, 409, 425, 429} or status_code >= 500
    if isinstance(exc, json.JSONDecodeError):
        return attempt == 0
    if isinstance(exc, LLMOutputValidationError):
        return True
    message = str(exc).lower()
    if "connection" in message or "timed out" in message or "timeout" in message:
        return True
    return False


def _normalise_compiled_paragraphs(
    source_paragraphs: list[dict[str, str]],
    raw_paragraphs: Any,
    article_id: str,
) -> list[dict[str, str]]:
    """把 LLM 翻译结果对齐回原始段落。"""
    translated: list[dict[str, str]] = []
    raw_items = raw_paragraphs if isinstance(raw_paragraphs, list) else []
    for index, source in enumerate(source_paragraphs, start=1):
        raw_item = raw_items[index - 1] if index - 1 < len(raw_items) else {}
        if not isinstance(raw_item, dict):
            raw_item = {}
        zh_text = str(raw_item.get("zh_text") or raw_item.get("translation") or "").strip()
        role = str(source.get("role") or "body").strip() or "body"
        translated.append(
            {
                "para_id": str(raw_item.get("para_id") or f"{article_id}_p{index}"),
                "en_text": str(source.get("en_text") or "").strip(),
                "zh_text": zh_text,
                "role": role,
            }
        )
    return translated


def _glossary_id(term: str, term_type: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")[:80]
    return f"{term_type}-{slug or 'term'}"


def _clean_glossary_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _is_valuable_proper_term(term: str, term_type: str = "proper_concept") -> bool:
    normalized = _clean_glossary_text(term)
    lowered = normalized.casefold()
    compact = re.sub(r"[^a-z0-9]+", "", lowered)
    if (
        len(normalized) < 2
        or lowered in GENERIC_GLOSSARY_TERMS
        or compact in {"us", "uk"}
        or re.match(r"^(?:of|in|to|and|the)\s+", lowered)
        or not re.search(r"[A-Za-z]", normalized)
    ):
        return False
    if term_type == "acronym":
        return bool(re.fullmatch(r"[A-Z][A-Z0-9.-]{1,15}", normalized))
    if re.fullmatch(r"[a-z][a-z-]*", normalized):
        return False
    return bool(re.search(r"[A-Z]", normalized) or re.search(r"\d", normalized) or "." in normalized)


def extract_zh_english_candidates(
    paragraphs: list[dict[str, str]], max_candidates: int = 32,
) -> list[dict[str, Any]]:
    """确定性提取中文译文中仍可见的英文专名，供 LLM 逐项审查。"""
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for paragraph_index, paragraph in enumerate(paragraphs, start=1):
        zh_text = str(paragraph.get("zh_text") or "")
        for match in ZH_ENGLISH_CANDIDATE_RE.finditer(zh_text):
            surface = _clean_glossary_text(match.group(1)).strip(" ,.;:!?()[]{}")
            if not _is_valuable_proper_term(surface):
                continue
            key = surface.casefold()
            if key in seen:
                continue
            seen.add(key)
            candidates.append({
                "candidate_id": f"ZH{len(candidates) + 1}",
                "paragraph_index": paragraph_index,
                "text_field": "zh_text",
                "surface": surface,
            })
            if len(candidates) >= max(1, max_candidates):
                return candidates
    return candidates


def _glossary_prompt(
    title: str,
    paragraphs: list[dict[str, str]],
    max_terms: int,
    candidates: list[dict[str, Any]] | None = None,
    *,
    only_candidates: bool = False,
    max_input_chars: int = 24000,
) -> str:
    rendered = []
    used_chars = 0
    for index, paragraph in enumerate(paragraphs, start=1):
        lines = []
        for field, marker in (("en_text", "EN"), ("zh_text", "ZH")):
            text = str(paragraph.get(field) or "").strip()
            if not text or used_chars >= max_input_chars:
                continue
            remaining = max_input_chars - used_chars
            text = text[:remaining]
            used_chars += len(text)
            lines.append(f"[P{index}.{marker}] {text}")
        if lines:
            rendered.append("\n".join(lines))
        if used_chars >= max_input_chars:
            break

    candidate_lines = "\n".join(
        f'- [{item["candidate_id"]}] P{item["paragraph_index"]}.ZH: {item["surface"]}'
        for item in (candidates or [])
    )
    if only_candidates:
        selection_rule = """This is a coverage-repair request. Return entries only for the listed candidates. Every listed item has already passed a proper-name detector: include each one unless it is unmistakably ordinary vocabulary. Do not omit a person or other named entity merely because it is famous or obvious."""
    else:
        selection_rule = """Review every listed candidate individually. Include every actual named person, organization, company, law or policy, event, place, work, publication, project, mechanism, or acronym. A transliterated Chinese name followed by its English spelling in parentheses is always a high-priority person/name and must be included. The candidate list is a coverage floor, not the full set: also add other useful English proper terms visible in the Chinese column."""

    return f"""You are a senior English-Chinese translator and global political-economic background editor. Analyze the bilingual article and return at most {max_terms} English-language proper terms that need contextual explanation for a Chinese reader.

Only annotate an exact English substring that remains visible in [P<number>.ZH]. Never create an annotation in the English column. Never annotate ordinary English vocabulary, generic abstract concepts, common roles, or terms such as democracy, inflation, President, US, CEO and similar common words.

{selection_rule}

Candidates extracted from the Chinese column:
{candidate_lines or "(none)"}

Allowed types only: person, organization, company, policy_law, event, place_context, work, proper_concept, acronym.
For every selected term:
- term must be the canonical English name and term_zh its conventional Chinese name.
- For an extracted candidate, term must copy that candidate's English surface exactly apart from letter case. Put expansions or aliases in description_zh, never substitute a different canonical name.
- description_zh must be an objective Chinese introduction of roughly 100-200 Chinese characters. State both who/what it is and its role or relevant background in this article. Do not invent facts.
- occurrences may contain only the first useful occurrence in the Chinese column.
- paragraph_index must use the [P<number>.ZH] marker, text_field must be zh_text, and surface must copy the exact visible English substring.

Return strict JSON only. Do not reproduce the article outside the exact surface field.

Article title: {title}

{chr(10).join(rendered)}

Return JSON ONLY:
{{
  "terms": [
    {{
      "term": "Jerome Powell",
      "term_zh": "杰罗姆·鲍威尔",
      "type": "person",
      "description_zh": "100-200字中文介绍",
      "occurrences": [
        {{"paragraph_index": 3, "text_field": "zh_text", "surface": "Jerome Powell", "occurrence": 1}}
      ]
    }}
  ]
}}"""


def _has_term_boundaries(text: str, start: int, length: int) -> bool:
    before = text[start - 1] if start > 0 else ""
    after_index = start + length
    after = text[after_index] if after_index < len(text) else ""
    first = text[start] if start < len(text) else ""
    last = text[after_index - 1] if after_index > 0 else ""
    word_char = re.compile(r"[A-Za-z0-9_]")
    return not (
        (first and before and word_char.fullmatch(first) and word_char.fullmatch(before))
        or (last and after and word_char.fullmatch(last) and word_char.fullmatch(after))
    )


def _actual_occurrence_surface(text: str, surface: str, occurrence: int = 1) -> str:
    if not text or not surface:
        return ""
    lowered_text = text.casefold()
    lowered_surface = surface.casefold()
    start = 0
    found = -1
    for _ in range(max(occurrence, 1)):
        found = lowered_text.find(lowered_surface, start)
        while found >= 0 and not _has_term_boundaries(text, found, len(surface)):
            found = lowered_text.find(lowered_surface, found + 1)
        if found < 0:
            return ""
        start = found + len(surface)
    return text[found:found + len(surface)]


def _find_glossary_occurrence(
    paragraphs: list[dict[str, str]], surface: str, preferred_index: int = 0, occurrence: int = 1,
) -> tuple[int, str] | None:
    indexes = []
    if 1 <= preferred_index <= len(paragraphs):
        indexes.append(preferred_index)
    indexes.extend(index for index in range(1, len(paragraphs) + 1) if index not in indexes)
    for paragraph_index in indexes:
        zh_text = str(paragraphs[paragraph_index - 1].get("zh_text") or "")
        actual = _actual_occurrence_surface(zh_text, surface, occurrence)
        if actual:
            return paragraph_index, actual
    return None


def _apply_glossary_terms(
    article: dict[str, Any], raw_terms: Any, complete: bool, max_terms: int | None = None,
) -> dict[str, Any]:
    """校验模型返回并转成前端使用的 glossary 和段落定位结构。"""
    paragraphs = article.get("paragraphs") or []
    entries: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    max_terms = max(1, int(max_terms or DEFAULTS["glossary"]["max_terms"]))
    if isinstance(raw_terms, dict):
        raw_terms = raw_terms.get("terms", raw_terms.get("glossary", raw_terms.get("items", [])))
    if not isinstance(raw_terms, list):
        raw_terms = []
    for raw in raw_terms:
        if len(entries) >= max_terms:
            break
        if not isinstance(raw, dict):
            continue
        term = _clean_glossary_text(raw.get("term") or raw.get("canonical_term") or raw.get("name"))
        term_type = str(raw.get("type") or "proper_concept").strip().lower()
        if term_type not in GLOSSARY_TYPES:
            term_type = "proper_concept"
        description = _clean_glossary_text(
            raw.get("description_zh") or raw.get("explanation_zh") or raw.get("description")
        )
        if not _is_valuable_proper_term(term, term_type) or len(description) < 60:
            continue
        glossary_id = _glossary_id(term, term_type)
        if glossary_id in seen_ids:
            continue
        valid_occurrences = []
        raw_occurrences = raw.get("occurrences") or raw.get("locations") or []
        if isinstance(raw_occurrences, dict):
            raw_occurrences = [raw_occurrences]
        for occurrence in raw_occurrences:
            if not isinstance(occurrence, dict):
                continue
            try:
                paragraph_index = int(occurrence.get("paragraph_index") or 0)
                ordinal = max(int(occurrence.get("occurrence") or 1), 1)
            except (ValueError, TypeError):
                continue
            surface = _clean_glossary_text(occurrence.get("surface") or term)
            if surface.casefold() != term.casefold():
                continue
            found = _find_glossary_occurrence(paragraphs, surface, paragraph_index, ordinal)
            if found:
                paragraph_index, actual_surface = found
                valid_occurrences.append({
                    "glossary_id": glossary_id, "paragraph_index": paragraph_index,
                    "text_field": "zh_text", "surface": actual_surface, "occurrence": ordinal,
                })
        if not valid_occurrences:
            found = _find_glossary_occurrence(paragraphs, term)
            if found:
                paragraph_index, actual_surface = found
                valid_occurrences.append({
                    "glossary_id": glossary_id, "paragraph_index": paragraph_index,
                    "text_field": "zh_text", "surface": actual_surface, "occurrence": 1,
                })
        if not valid_occurrences:
            continue
        seen_ids.add(glossary_id)
        entries.append({
            "id": glossary_id, "term": term,
            "term_zh": _clean_glossary_text(raw.get("term_zh") or raw.get("name_zh")),
            "type": term_type, "description_zh": description[:200].rstrip(), "version": GLOSSARY_VERSION,
        })
        annotations.extend(valid_occurrences[:1])
    article["glossary_entries"] = entries
    article["term_annotations"] = annotations
    article["glossary_analysis_complete"] = complete
    article["glossary_version"] = GLOSSARY_VERSION if complete else 0
    return article


def _covered_candidate_keys(
    annotations: list[dict[str, Any]], entries: list[dict[str, Any]],
) -> set[tuple[int, str]]:
    entry_terms = {
        str(item.get("id") or ""): str(item.get("term") or "").casefold()
        for item in entries
        if isinstance(item, dict)
    }
    return {
        (int(item.get("paragraph_index") or 0), str(item.get("surface") or "").casefold())
        for item in annotations
        if isinstance(item, dict)
        and entry_terms.get(str(item.get("glossary_id") or ""))
        == str(item.get("surface") or "").casefold()
    }


def _missing_glossary_candidates(
    candidates: list[dict[str, Any]], annotations: list[dict[str, Any]], entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    covered = _covered_candidate_keys(annotations, entries)
    return [
        candidate for candidate in candidates
        if (candidate["paragraph_index"], candidate["surface"].casefold()) not in covered
    ]


def _merge_glossary_articles(
    article: dict[str, Any], extra: dict[str, Any], max_terms: int,
) -> dict[str, Any]:
    entries: dict[str, dict[str, Any]] = {}
    # 漏项补充结果优先，避免首次响应占满 max_terms 后再次挤掉高置信候选。
    for entry in (extra.get("glossary_entries") or []) + (article.get("glossary_entries") or []):
        if isinstance(entry, dict) and entry.get("id"):
            entries[str(entry["id"])] = entry
    annotations = []
    seen = set()
    for item in (extra.get("term_annotations") or []) + (article.get("term_annotations") or []):
        if not isinstance(item, dict):
            continue
        key = (
            item.get("glossary_id"), item.get("paragraph_index"),
            str(item.get("surface") or "").casefold(), item.get("occurrence"),
        )
        if key in seen:
            continue
        seen.add(key)
        annotations.append(item)
    ordered_ids = list(entries)[:max_terms]
    allowed_ids = set(ordered_ids)
    article["glossary_entries"] = [entries[item] for item in ordered_ids]
    article["term_annotations"] = [
        item for item in annotations if item.get("glossary_id") in allowed_ids
    ]
    return article


def _order_glossary_by_candidates(
    article: dict[str, Any], candidates: list[dict[str, Any]],
) -> None:
    candidate_order = {
        (item["paragraph_index"], item["surface"].casefold()): index
        for index, item in enumerate(candidates)
    }
    entry_priority: dict[str, int] = {}
    for annotation in article.get("term_annotations") or []:
        key = (
            int(annotation.get("paragraph_index") or 0),
            str(annotation.get("surface") or "").casefold(),
        )
        if key in candidate_order:
            entry_priority[str(annotation.get("glossary_id") or "")] = candidate_order[key]
    entries = article.get("glossary_entries") or []
    entries.sort(key=lambda item: entry_priority.get(str(item.get("id") or ""), len(candidates)))
    entry_order = {str(item.get("id") or ""): index for index, item in enumerate(entries)}
    annotations = article.get("term_annotations") or []
    annotations.sort(key=lambda item: entry_order.get(str(item.get("glossary_id") or ""), len(entries)))


def enrich_article_glossary(
    client: Any, cfg: dict[str, Any], article: dict[str, Any], log_: logging.Logger,
) -> dict[str, Any]:
    """按 auto-paper-md-converter 的 glossary schema 为文章添加可定位术语。"""
    glossary_cfg = cfg["glossary"]
    paragraphs = article.get("paragraphs") or []
    if not glossary_cfg.get("enabled") or not any(p.get("zh_text") for p in paragraphs):
        article["glossary_entries"] = []
        article["term_annotations"] = []
        article["glossary_analysis_complete"] = False
        article["glossary_version"] = 0
        return article

    previous_entries = list(article.get("glossary_entries") or [])
    previous_annotations = list(article.get("term_annotations") or [])
    max_terms = max(1, int(glossary_cfg["max_terms"]))
    candidates = extract_zh_english_candidates(
        paragraphs, max_candidates=max(1, int(glossary_cfg.get("max_candidates", 32)))
    )
    model = glossary_cfg.get("model") or cfg["llm"].get("model", "gpt-4o-mini")

    def request_terms(prompt: str, label: str) -> tuple[Any, bool]:
        for attempt in range(int(glossary_cfg["max_retries"]) + 1):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "Return JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=int(glossary_cfg["max_tokens"]),
                    temperature=0.2,
                    response_format={"type": "json_object"},
                )
                return _extract_json_payload(response.choices[0].message.content or ""), True
            except Exception as exc:
                log_.warning(f"{label}失败 (attempt {attempt + 1}): {exc}")
                if not _should_retry_llm_error(exc, attempt, int(glossary_cfg["max_retries"])):
                    break
        return [], False

    prompt = _glossary_prompt(
        str(article.get("title") or "Untitled"), paragraphs, max_terms, candidates,
        max_input_chars=max(2000, int(glossary_cfg.get("max_input_chars", 24000))),
    )
    raw_terms, succeeded = request_terms(prompt, "关键词解析")
    if not succeeded:
        article["glossary_entries"] = previous_entries
        article["term_annotations"] = previous_annotations
        article["glossary_analysis_complete"] = False
        article["glossary_version"] = 0
        return article
    _apply_glossary_terms(article, raw_terms, complete=False, max_terms=max_terms)

    missing = _missing_glossary_candidates(
        candidates,
        article.get("term_annotations") or [],
        article.get("glossary_entries") or [],
    )
    if succeeded and missing:
        repair_limit = min(max_terms, len(missing))
        repair_prompt = _glossary_prompt(
            str(article.get("title") or "Untitled"), paragraphs, repair_limit, missing,
            only_candidates=True,
            max_input_chars=max(2000, int(glossary_cfg.get("max_input_chars", 24000))),
        )
        repair_terms, repair_succeeded = request_terms(repair_prompt, "关键词漏项补充解析")
        if repair_succeeded:
            extra = {"paragraphs": paragraphs}
            _apply_glossary_terms(extra, repair_terms, complete=False, max_terms=repair_limit)
            _merge_glossary_articles(article, extra, max_terms)

    _order_glossary_by_candidates(article, candidates)
    missing = _missing_glossary_candidates(
        candidates,
        article.get("term_annotations") or [],
        article.get("glossary_entries") or [],
    )
    article["glossary_analysis_complete"] = bool(succeeded and not missing)
    article["glossary_version"] = GLOSSARY_VERSION if article["glossary_analysis_complete"] else 0
    if missing:
        log_.warning(
            "关键词解析仍遗漏中文栏候选: "
            + ", ".join(str(item["surface"]) for item in missing[:10])
        )
    return article


def _render_article_prompt_paragraphs(paragraphs: list[dict[str, str]]) -> str:
    rendered = []
    for index, paragraph in enumerate(paragraphs, start=1):
        role = paragraph.get("role") or "body"
        text = str(paragraph.get("en_text") or "").strip()
        if text:
            rendered.append(f"{index}. [{role}] {text}")
    return "\n".join(rendered)


def _translation_prompt(
    title: str,
    section: str,
    paragraphs: list[dict[str, str]],
) -> str:
    return f"""你是一名资深英中新闻编辑。请逐段翻译下面的《华尔街日报》报道，只返回严格 JSON。

翻译要求：
1. 忠实保留原意、事实、数字、立场和语气，不增添原文之外的信息。
2. 使用自然、清楚的现代中文，不逐词照搬英文语序。主动拆开过长的英文句子，补足中文所需的主语和逻辑连接，使每句话都能独立读懂。
3. 根据上下文意译习语、隐喻和抽象表达，避免“降低杠杆”“使其过时”一类脱离中文语境的机械直译。
4. 保持段落顺序和数量完全一致，每个输入段落必须有且只有一个对应译文，不得合并、拆分或遗漏。
5. crosshead 译成简短自然的中文小标题；body 译成正文。
6. 英文平台名、产品名、品牌名、人名、公司名和机构名一律保留原文，不音译、意译或改写成中文，也不要采用中文网络俗称。例如必须写 Google、Reddit、Instagram、TikTok、Sensor Tower，不能写“谷歌”“红迪”“照片墙”“抖音海外版”“传感器塔”。普通地名、政策、法律和事件可自然翻译；确有必要时首次出现可在中文译名后括注英文原文。普通类别词和泛称不是专名，应自然翻译成中文。
7. Mr、Mrs、Ms、Dr 等英文称谓通常省略，只保留英文人名；只有称谓本身影响语义时才保留称谓。
8. 不写摘要、评论、说明或 Markdown 代码块。

Title: {title}
Section: {section}

Source paragraphs:
{_render_article_prompt_paragraphs(paragraphs)}

Return JSON in this shape:
{{
  "paragraphs": [
    {{
      "zh_text": "自然、完整的中文译文",
      "role": "body"
    }}
  ]
}}
"""


def _source_word_count(paragraphs: list[dict[str, str]]) -> int:
    return sum(
        len(re.findall(r"\b[\w’'-]+\b", str(paragraph.get("en_text") or "")))
        for paragraph in paragraphs
    )


def _translation_chunks(
    paragraphs: list[dict[str, str]],
    *,
    max_paragraphs: int = 16,
    max_words: int = 1200,
) -> list[list[dict[str, str]]]:
    chunks: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    current_words = 0
    for paragraph in paragraphs:
        paragraph_words = _source_word_count([paragraph])
        if current and (
            len(current) >= max_paragraphs
            or current_words + paragraph_words > max_words
        ):
            chunks.append(current)
            current = []
            current_words = 0
        current.append(paragraph)
        current_words += paragraph_words
    if current:
        chunks.append(current)
    return chunks


def _summary_length_bounds(paragraphs: list[dict[str, str]]) -> tuple[int, int]:
    """按英文正文体量给中文解读留出空间，避免短文灌水、长文硬压缩。"""
    source_words = _source_word_count(paragraphs)
    if source_words <= 900:
        return 420, 650
    if source_words <= 1800:
        return 520, 800
    return 620, 1000


def _summary_prompt(
    title: str,
    section: str,
    paragraphs: list[dict[str, str]],
    min_cn_chars: int,
    max_cn_chars: int,
) -> str:
    return f"""你是一名面向中文读者的资深国际政经编辑。请基于下面的《华尔街日报》英文原文，重新撰写中文标题和中文解读。只返回严格 JSON。

这不是逐段翻译，也不是把原文每段压缩后依次拼接。你需要先理解文章真正要回答的问题，再按中文读者最容易理解的顺序重组材料。

中文解读要求：
1. 开头直接讲清文章的核心判断及其现实背景；随后解释关键原因和证据；最后交代作者主张、局限或影响。主次分明，不追求覆盖所有细枝末节。
2. 写成 3-5 个自然段，不使用小标题、项目符号或编号。每段只承担一个主要作用，段落之间要有自然的因果、转折或递进关系。
3. 使用像中文原创评论稿一样顺畅、克制的表达。明确句子主语，优先使用短句和中等长度句；多数句子控制在 20-45 个汉字，超过 70 个汉字时应主动拆句。
4. 避免欧化句式、连续堆叠分号、名词串联和生硬直译。根据语境把 leverage、workaround、make obsolete 等表达转写成中文读者能直接理解的具体意思，不照搬英文词形。
5. 关键数字和事实只选择真正支撑核心判断的内容。不得为了凑字数罗列信息、重复结论或加入原文没有的背景知识。
6. 对争议性判断明确归属于文章、相关国家或相关人物，不把观点写成未经限定的事实。
7. summary_md 使用 {min_cn_chars}-{max_cn_chars} 个汉字。文章较长时已经放宽上限，应利用额外篇幅讲清逻辑，而不是让句子变得更长。返回前自行检查，但不要输出字数。
8. title_zh 应简洁、自然、准确，避免逐词翻译造成歧义。政治和外交语境中的 deal 通常译为“协议”或“安排”，不要写成“交易”“谈一笔交易”等商业化表达。
9. 英文平台名、产品名、品牌名、人名、公司名和机构名一律保留原文，不音译、意译或使用中文网络俗称。例如必须写 Google、Reddit、Instagram、TikTok、Sensor Tower，不能写“谷歌”“红迪”“照片墙”“抖音海外版”“传感器塔”。此规则同时适用于 title_zh 和 summary_md。

Title: {title}
Section: {section}

Source paragraphs:
{_render_article_prompt_paragraphs(paragraphs)}

Return JSON in this shape:
{{
  "title_zh": "自然准确的中文标题",
  "summary_md": "分成3-5个自然段的连贯中文解读"
}}
"""


def _validate_summary_style(summary: str) -> None:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", summary) if part.strip()]
    if not 2 <= len(paragraphs) <= 6:
        raise LLMOutputValidationError(
            f"中文解读段落不合格({len(paragraphs)}，要求 2-6 个自然段)"
        )
    if any(re.match(r"^(?:#{1,6}\s|[-*+]\s|\d+[.、)]\s*)", part) for part in paragraphs):
        raise LLMOutputValidationError("中文解读不得使用标题、项目符号或编号")
    sentences = [
        sentence.strip()
        for sentence in re.split(r"[。！？!?]+", summary)
        if sentence.strip()
    ]
    longest = max((count_cn_chars(sentence) for sentence in sentences), default=0)
    if longest > 90:
        raise LLMOutputValidationError(
            f"中文解读存在过长句子({longest} 个汉字，单句不得超过 90 个汉字)"
        )


def _request_article_translation(
    client: Any,
    cfg: dict[str, Any],
    *,
    title: str,
    section: str,
    source_paragraphs: list[dict[str, str]],
    article_id: str,
    log_: logging.Logger,
) -> list[dict[str, str]]:
    chunks = _translation_chunks(source_paragraphs)
    if len(chunks) > 1:
        log_.info(
            "长文逐段翻译拆分为 %d 块（%d 段）: %s",
            len(chunks),
            len(source_paragraphs),
            title,
        )
        combined: list[dict[str, str]] = []
        for chunk in chunks:
            combined.extend(
                _request_article_translation(
                    client,
                    cfg,
                    title=title,
                    section=section,
                    source_paragraphs=chunk,
                    article_id=article_id,
                    log_=log_,
                )
            )
        for index, paragraph in enumerate(combined, start=1):
            paragraph["para_id"] = f"{article_id}_p{index}"
        return combined

    llm = cfg["llm"]
    prompt = _translation_prompt(title, section, source_paragraphs)
    max_retries = int(cfg["crawl"].get("max_retries", 2))
    source_words = _source_word_count(source_paragraphs)
    translation_token_floor = 8192 if source_words > 1800 else 4096
    max_tokens = max(int(llm.get("max_tokens", 2048)), translation_token_floor)
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=llm.get("model", "gpt-4o-mini"),
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "只返回 JSON。忠实翻译全文，但必须使用自然、清楚的现代中文，"
                            "不得逐词照搬英文句法。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
                temperature=min(float(llm.get("temperature", 0.4)), 0.2),
                response_format={"type": "json_object"},
            )
            payload = _extract_json_payload(response.choices[0].message.content or "")
            raw_paragraphs = payload.get("paragraphs")
            if not isinstance(raw_paragraphs, list) or len(raw_paragraphs) != len(source_paragraphs):
                actual = len(raw_paragraphs) if isinstance(raw_paragraphs, list) else 0
                raise LLMOutputValidationError(
                    f"段落翻译数量不合格({actual}，要求 {len(source_paragraphs)})"
                )
            translated = _normalise_compiled_paragraphs(
                source_paragraphs, raw_paragraphs, article_id,
            )
            missing = [
                index
                for index, paragraph in enumerate(translated, start=1)
                if not str(paragraph.get("zh_text") or "").strip()
            ]
            if missing:
                raise LLMOutputValidationError(
                    "段落翻译存在空译文: " + ", ".join(map(str, missing[:10]))
                )
            return translated
        except Exception as exc:
            last_error = exc
            log_.warning(f"逐段翻译失败 (attempt {attempt + 1}): {exc}")
            if not _should_retry_llm_error(exc, attempt, max_retries):
                break
            time.sleep(1.0)

    log_.warning("逐段翻译数组模式失败，改用编号映射模式重试: %s", last_error)
    return _request_article_translation_numbered(
        client,
        cfg,
        title=title,
        section=section,
        source_paragraphs=source_paragraphs,
        article_id=article_id,
        log_=log_,
    )


def _translation_numbered_prompt(
    title: str,
    section: str,
    paragraphs: list[dict[str, str]],
) -> str:
    return f"""请把下面《The Wall Street Journal》文章逐段译成自然中文，只返回严格 JSON object。

要求：
1. 必须返回 translations 对象，键为段落编号字符串 "1"、"2"，一直到 "{len(paragraphs)}"。
2. 每个编号必须对应一个完整中文译文，不得空缺，不得合并或省略。
3. 人名、公司名、机构名、品牌、平台、App、网站、产品和出版物名称必须保留英文原文，不音译、不意译。例如必须写 Google、Reddit、Instagram、TikTok、Berkshire Hathaway、Brown-Forman、Ryanair、Federal Reserve，不得写“谷歌”“红迪”“照片墙”“抖音海外版”。
4. 国家、地区、政策、法律和普通概念可按中文习惯翻译；标题、行动名称、书名等专名第一次出现可保留英文或用中文后加英文括注。
5. 不输出解释、摘要或 Markdown 代码块。

Title: {title}
Section: {section}

Source paragraphs:
{_render_article_prompt_paragraphs(paragraphs)}

Return JSON in this shape:
{{
  "translations": {{
    "1": "第一段中文译文",
    "2": "第二段中文译文"
  }}
}}
"""


def _normalise_numbered_translations(
    source_paragraphs: list[dict[str, str]],
    raw_translations: Any,
    article_id: str,
) -> list[dict[str, str]]:
    if isinstance(raw_translations, list):
        translations = {str(index): value for index, value in enumerate(raw_translations, start=1)}
    elif isinstance(raw_translations, dict):
        translations = raw_translations
    else:
        raise LLMOutputValidationError("translations 不是 object")

    translated: list[dict[str, str]] = []
    missing: list[int] = []
    for index, source in enumerate(source_paragraphs, start=1):
        raw_value = translations.get(str(index))
        if isinstance(raw_value, dict):
            zh_text = str(raw_value.get("zh_text") or raw_value.get("translation") or "").strip()
        else:
            zh_text = str(raw_value or "").strip()
        if not zh_text:
            missing.append(index)
        translated.append(
            {
                "para_id": f"{article_id}_p{index}",
                "en_text": str(source.get("en_text") or "").strip(),
                "zh_text": zh_text,
                "role": str(source.get("role") or "body").strip() or "body",
            }
        )
    if missing:
        raise LLMOutputValidationError(
            "编号翻译缺少译文段落: " + ", ".join(map(str, missing[:10]))
        )
    return translated


def _request_article_translation_numbered(
    client: Any,
    cfg: dict[str, Any],
    *,
    title: str,
    section: str,
    source_paragraphs: list[dict[str, str]],
    article_id: str,
    log_: logging.Logger,
) -> list[dict[str, str]]:
    chunks = _translation_chunks(source_paragraphs)
    if len(chunks) > 1:
        combined: list[dict[str, str]] = []
        for chunk in chunks:
            combined.extend(
                _request_article_translation_numbered(
                    client,
                    cfg,
                    title=title,
                    section=section,
                    source_paragraphs=chunk,
                    article_id=article_id,
                    log_=log_,
                )
            )
        for index, paragraph in enumerate(combined, start=1):
            paragraph["para_id"] = f"{article_id}_p{index}"
        return combined

    llm = cfg["llm"]
    prompt = _translation_numbered_prompt(title, section, source_paragraphs)
    max_retries = int(cfg["crawl"].get("max_retries", 2))
    source_words = _source_word_count(source_paragraphs)
    translation_token_floor = 8192 if source_words > 1800 else 4096
    max_tokens = max(int(llm.get("max_tokens", 2048)), translation_token_floor)
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=llm.get("model", "gpt-4o-mini"),
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "只返回 JSON。逐段翻译，编号必须完整。英文人名、公司名、"
                            "机构名、品牌、平台、App、网站、产品和出版物名称必须保留英文原文。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            payload = _extract_json_payload(response.choices[0].message.content or "")
            return _normalise_numbered_translations(
                source_paragraphs,
                payload.get("translations"),
                article_id,
            )
        except Exception as exc:
            last_error = exc
            log_.warning(f"编号映射翻译失败 (attempt {attempt + 1}): {exc}")
            if not _should_retry_llm_error(exc, attempt, max_retries):
                break
            time.sleep(1.0)

    raise last_error or RuntimeError("编号映射翻译失败")


def _request_article_summary(
    client: Any,
    cfg: dict[str, Any],
    *,
    title: str,
    section: str,
    source_paragraphs: list[dict[str, str]],
    log_: logging.Logger,
) -> tuple[str, str]:
    llm = cfg["llm"]
    min_cn_chars, max_cn_chars = _summary_length_bounds(source_paragraphs)
    prompt = _summary_prompt(
        title, section, source_paragraphs, min_cn_chars, max_cn_chars,
    )
    max_retries = int(cfg["crawl"].get("max_retries", 2))
    max_tokens = max(int(llm.get("max_tokens", 2048)), 2048, max_cn_chars * 3)
    temperature = min(max(float(llm.get("temperature", 0.4)), 0.3), 0.6)
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=llm.get("model", "gpt-4o-mini"),
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "只返回 JSON。你是在为中文读者撰写原创编辑稿，不是逐段翻译或"
                            "英文摘要的直译；表达必须自然、清楚、符合中文阅读习惯。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
            payload = _extract_json_payload(response.choices[0].message.content or "")
            title_zh = str(payload.get("title_zh") or "").strip()
            summary_md = str(payload.get("summary_md") or "").strip()
            if not title_zh or not summary_md:
                raise LLMOutputValidationError("中文解读缺少 title_zh 或 summary_md")
            summary_cn_chars = count_cn_chars(summary_md)
            if not min_cn_chars <= summary_cn_chars <= max_cn_chars:
                raise LLMOutputValidationError(
                    f"中文解读字数不合格({summary_cn_chars}，要求 "
                    f"{min_cn_chars}-{max_cn_chars} 个汉字)"
                )
            _validate_summary_style(summary_md)
            return title_zh, summary_md
        except Exception as exc:
            last_error = exc
            log_.warning(f"中文解读失败 (attempt {attempt + 1}): {exc}")
            if not _should_retry_llm_error(exc, attempt, max_retries):
                break
            time.sleep(1.0)

    raise last_error or RuntimeError("中文解读失败")


def compile_article_record(
    client: Any,
    cfg: dict[str, Any],
    *,
    issue_date: str,
    section: str,
    title: str,
    url: str,
    body: str,
    article_id: str,
    log_: logging.Logger,
    images: list[str] | None = None,
) -> dict[str, Any] | None:
    """把抓到的正文编译成 Economist 前端需要的结构化 article。"""
    source_paragraphs = _split_article_paragraphs(body)
    if not source_paragraphs:
        if not images:
            return None
        article = {
            "id": article_id,
            "issue_date": issue_date,
            "section": section,
            "title": title,
            "title_zh": "",
            "url": url,
            "summary_md": "",
            "content_raw": "",
            "content_markdown": "",
            "paragraphs": [],
            "images": images,
            "image_insights": _ensure_image_insight_placeholders(images),
            "glossary_entries": [],
            "term_annotations": [],
            "glossary_analysis_complete": False,
            "glossary_version": 0,
            "compiled_article": False,
            "compile_status": "image_only",
        }
        return article

    translation_error: Exception | None = None
    summary_error: Exception | None = None

    try:
        compiled_paragraphs = _request_article_translation(
            client,
            cfg,
            title=title,
            section=section,
            source_paragraphs=source_paragraphs,
            article_id=article_id,
            log_=log_,
        )
    except Exception as exc:
        translation_error = exc
        log_.error(f"逐段翻译最终失败: {title} ({exc})")
        compiled_paragraphs = [
            {
                "para_id": f"{article_id}_p{index}",
                "en_text": str(paragraph.get("en_text") or ""),
                "zh_text": "",
                "role": str(paragraph.get("role") or "body"),
            }
            for index, paragraph in enumerate(source_paragraphs, start=1)
        ]

    try:
        title_zh, summary_md = _request_article_summary(
            client,
            cfg,
            title=title,
            section=section,
            source_paragraphs=source_paragraphs,
            log_=log_,
        )
    except Exception as exc:
        summary_error = exc
        log_.error(f"中文解读最终失败，启用兼容摘要兜底: {title} ({exc})")
        title_zh = ""
        summary_md = summarize(client, cfg, title, body, log_)

    compile_complete = translation_error is None and summary_error is None
    article = {
        "id": article_id,
        "issue_date": issue_date,
        "section": section,
        "title": title,
        "title_zh": title_zh,
        "url": url,
        "summary_md": summary_md,
        "content_raw": _format_source_content_markdown(source_paragraphs),
        "content_markdown": _format_source_content_markdown(source_paragraphs),
        "paragraphs": compiled_paragraphs,
        "images": images or [],
        "image_insights": _ensure_image_insight_placeholders(images or []),
        "compiled_article": compile_complete,
        "compile_status": "complete" if compile_complete else "fallback",
    }
    return enrich_article_glossary(client, cfg, article, log_)


# ---------------------------------------------------------------------------
# 飞书推送
# ---------------------------------------------------------------------------


def build_feishu_card(articles: list[dict[str, Any]], issue_date: str) -> dict[str, Any]:
    lines = "\n".join(
        f"- **[{a.get('section', 'General')}]** {a.get('title_zh') or a.get('title')}"
        for a in articles
    )
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"📰 经济学人周报更新 · {issue_date}",
                }
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": lines},
                },
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "请双击本地 index.html 查看深度总结",
                        }
                    ],
                },
            ],
        },
    }


def push_feishu(cfg: dict[str, Any], card: dict[str, Any], log_: logging.Logger) -> None:
    """懒导入 requests,避免无网络依赖时测试失败。"""
    import requests  # type: ignore
    fs = cfg.get("feishu", {})
    if not fs.get("enabled", True):
        log_.info("飞书推送 disabled,跳过")
        return
    url = fs.get("webhook_url", "")
    if not url or "REPLACE-ME" in url:
        log_.warning("webhook 未配置,跳过推送")
        return
    try:
        r = requests.post(url, json=card, timeout=15)
        if r.status_code != 200:
            log_.error(f"飞书推送失败: {r.status_code} {r.text[:200]}")
        else:
            log_.info("飞书推送成功")
    except Exception as e:
        log_.error(f"飞书推送异常(不阻塞主流程): {e}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _next_seq(articles: list[dict[str, Any]], issue_date: str) -> int:
    used = [int(re.findall(r"(\d+)", a.get("id", "0"))[-1])
            for a in articles if a.get("id", "").startswith(f"art_{issue_date}_")]
    return (max(used) + 1) if used else 1


def _compile_article_task(cfg: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    """在线程中建立独立 LLM client，避免共享 HTTP client 的并发状态。"""
    client = make_llm_client(cfg)
    article = compile_article_record(
        client,
        cfg,
        issue_date=payload["issue_date"],
        section=payload["section"],
        title=payload["title"],
        url=payload["url"],
        body=payload["body"],
        article_id=payload["article_id"],
        log_=log,
        images=payload["images"],
    )
    placements = payload.get("image_placements") or []
    if article is not None and placements:
        cleaned_placements, _ = _migrate_image_description_fields(
            placements, list(article.get("image_insights") or []),
        )
        article["image_placements"] = cleaned_placements
        article["image_insights"] = _ensure_image_insight_placeholders(
            list(article.get("images") or payload.get("images") or []),
        )
    return article


def _analyze_article_images_task(cfg: dict[str, Any], issue_date: str, title: str, images: list[str]) -> list[dict[str, Any]]:
    return analyze_article_images(make_llm_client(cfg), cfg, issue_date, title, images, log)


def process_issue(
    cfg: dict[str, Any],
    issue_date: str,
    *,
    dry_run: bool = False,
    limit: int = 0,
    rewrite_id: str | None = None,
    no_feishu: bool = False,
    debug_html_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """抓一个 issue。浏览器串行，正文编译和图片解析由独立 LLM 队列并发完成。"""
    browser_cfg = cfg["browser"]
    delay_min = float(cfg["crawl"].get("delay_min_s", 5))
    delay_max = float(cfg["crawl"].get("delay_max_s", 10))
    pipeline = cfg["pipeline"]
    compile_workers = max(1, int(pipeline.get("compile_workers", 2)))
    image_workers = max(1, int(pipeline.get("image_workers", 1)))
    max_pending = max(compile_workers, int(pipeline.get("max_pending", compile_workers * 2)))

    log.info(f"启动浏览器 (user_data={browser_cfg['user_data_path']})")
    page = open_browser(browser_cfg["user_data_path"], bool(browser_cfg.get("headless", False)))

    try:
        log.info("抓取 weeklyedition 目录…")
        index = fetch_weekly_index(page, issue_date=issue_date, debug_html_dir=debug_html_dir)
        log.info(f"目录共 {len(index)} 条链接")
        # 板块过滤
        candidates = [a for a in index if is_allowed_article(a["url"], a["section"])]
        log.info(f"非文章链接过滤后剩 {len(candidates)} 条")
        if dry_run:
            dry_candidates = candidates[:limit] if limit > 0 else candidates
            for c in dry_candidates:
                print(f"[DRY] {c['issue_date']}  {c['section']:25s}  {c['title']}  {c['url']}")
            return []

        cover_url = next((str(item.get("cover_image_url") or "") for item in candidates if item.get("cover_image_url")), "")
        cover_image = materialize_issue_cover(cover_url, cfg, issue_date)

        existing = read_database_js()
        existing_by_url = {a["url"]: a for a in existing}
        if rewrite_id:
            existing = [a for a in existing if a.get("id") != rewrite_id]
            existing_by_url = {a["url"]: a for a in existing}

        new_articles: list[dict[str, Any]] = []
        seq = _next_seq(existing, issue_date)
        log.info(
            f"开始逐篇抓取(已存在 {len(existing_by_url)} 篇,本 issue 可抓取 {len(candidates)} 条"
            f"{f', 本 run 最多新增 {limit} 篇' if limit > 0 else ''}; "
            f"正文 LLM {compile_workers} 路, 图片 LLM {image_workers} 路)"
        )

        with ThreadPoolExecutor(max_workers=compile_workers, thread_name_prefix="econ-compile") as compile_pool, \
                ThreadPoolExecutor(max_workers=image_workers, thread_name_prefix="econ-image") as image_pool:
            pending_compile: dict[Future, dict[str, Any]] = {}
            pending_images: dict[Future, dict[str, Any]] = {}

            def persist_article(article: dict[str, Any]) -> None:
                existing.append(article)
                existing_by_url[article["url"]] = article
                new_articles.append(article)
                try:
                    write_database_js(existing)
                    log.info(
                        f"✓ 已收录并落盘: {article['id']} - {article['title'][:60]}  "
                        f"(本 run 第 {len(new_articles)} 篇 / 累计 {len(existing)} 篇)"
                    )
                    _maybe_export_article_md(cfg, article)
                    if cfg["image_analysis"].get("enabled") and article.get("images"):
                        future = image_pool.submit(
                            _analyze_article_images_task,
                            cfg,
                            issue_date,
                            str(article.get("title") or ""),
                            list(article["images"]),
                        )
                        pending_images[future] = article
                except Exception as exc:
                    log.error(f"写盘失败 {article.get('url')}:{exc},该篇未持久化")
                    existing.pop()
                    existing_by_url.pop(article.get("url"), None)
                    new_articles.pop()

            def drain_compiled(block: bool) -> None:
                if not pending_compile:
                    return
                done, _ = wait(
                    pending_compile,
                    timeout=None if block else 0,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    payload = pending_compile.pop(future)
                    try:
                        article = future.result()
                    except Exception as exc:
                        log.warning(f"结构化编译任务失败 {payload['url']}: {exc}")
                        continue
                    if article:
                        persist_article(article)

            def drain_images(block: bool) -> None:
                if not pending_images:
                    return
                done, _ = wait(
                    pending_images,
                    timeout=None if block else 0,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    article = pending_images.pop(future)
                    try:
                        article["image_insights"] = future.result()
                        write_database_js(existing)
                        log.info(
                            f"[images] 已解析 {article['id']}: "
                            f"{len(article.get('image_insights') or [])} 条图片说明"
                        )
                    except Exception as exc:
                        log.warning(f"图片解析任务失败 {article.get('url')}: {exc}")

            for cand in candidates:
                drain_compiled(block=False)
                drain_images(block=False)
                while limit > 0 and len(new_articles) + len(pending_compile) >= limit:
                    drain_compiled(block=True)
                    if len(new_articles) >= limit:
                        break
                if limit > 0 and len(new_articles) >= limit:
                    log.info(f"已达到本 run 限制 {limit} 篇,停止继续抓取")
                    break
                while len(pending_compile) >= max_pending:
                    drain_compiled(block=True)

                url = cand["url"]
                if url in existing_by_url:
                    log.info(f"⏭ 已存在,跳过: {cand['title'][:50]}  ({url[:60]}…)")
                    continue
                log.info(f"抓取正文: {cand['title'][:60]}")
                try:
                    title, body, image_urls = fetch_article_content(page, url)
                except Exception as exc:
                    log.warning(f"抓取失败 {url}: {exc}")
                    time.sleep(random.uniform(delay_min, delay_max))
                    continue
                if not body.strip() and not image_urls:
                    log.warning(f"未提取到正文或图片，丢弃: {url}")
                    time.sleep(random.uniform(delay_min, delay_max))
                    continue

                title = title or cand["title"]
                article_id = f"art_{issue_date}_{seq:03d}"
                seq += 1
                payload = {
                    "issue_date": issue_date,
                    "section": cand["section"],
                    "title": title,
                    "url": url,
                    "body": body,
                    "article_id": article_id,
                    "images": materialize_article_images(image_urls, cfg, issue_date, article_id),
                }
                pending_compile[compile_pool.submit(_compile_article_task, cfg, payload)] = payload
                time.sleep(random.uniform(delay_min, delay_max))

            while pending_compile:
                drain_compiled(block=True)
                drain_images(block=False)
            while pending_images:
                drain_images(block=True)

        try:
            _sync_paper_outputs(cfg, existing, issue_date=issue_date, issue_covers={issue_date: cover_image})
            _maybe_rebuild_index(cfg)
        except Exception as sync_exc:
            log.warning(f"paper 输出同步失败(不影响旧 database.js): {sync_exc}")

        log.info(f"=== 本 run 完成,新增 {len(new_articles)} 篇,database.js 累计 {len(existing)} 篇 ===")
        if new_articles and not no_feishu:
            push_feishu(cfg, build_feishu_card(new_articles, issue_date), log)
        elif not new_articles:
            log.info("本 run 无新增文章,不发飞书(避免空推)")
        return new_articles
    finally:
        try:
            page.close()
        except Exception:
            pass


def refresh_article_images(
    cfg: dict[str, Any], issue_date: str, article_ids: set[str], no_feishu: bool = True,
) -> list[dict[str, Any]]:
    """只刷新指定文章的正文图片和图片解析，不重新抓取或翻译正文。"""
    existing = read_database_js()
    targets = [
        article for article in existing
        if article.get("issue_date") == issue_date
        and (not article_ids or article.get("id") in article_ids)
    ]
    if not targets:
        log.warning(f"没有找到待刷新图片的文章: issue={issue_date}, ids={sorted(article_ids)}")
        return []

    page = open_browser(cfg["browser"]["user_data_path"], bool(cfg["browser"].get("headless", False)))
    client = make_llm_client(cfg)
    refreshed: list[dict[str, Any]] = []
    try:
        for article in targets:
            url = str(article.get("url") or "")
            try:
                _, _, image_urls = fetch_article_content(page, url)
                article["images"] = materialize_article_images(
                    image_urls, cfg, issue_date, str(article.get("id") or "article")
                )
                article["image_insights"] = analyze_article_images(
                    client, cfg, issue_date, str(article.get("title") or ""), article["images"], log
                )
                refreshed.append(article)
                log.info(
                    f"[images] 已刷新 {article.get('id')}: "
                    f"{len(article['images'])} 张图片, {len(article['image_insights'])} 条解析"
                )
            except Exception as exc:
                log.warning(f"[images] 刷新失败 {url}: {exc}")
        write_database_js(existing)
        _sync_paper_outputs(cfg, existing, issue_date=issue_date)
        _maybe_rebuild_index(cfg)
        if refreshed and not no_feishu:
            push_feishu(cfg, build_feishu_card(refreshed, issue_date), log)
        return refreshed
    finally:
        try:
            page.close()
        except Exception:
            pass


def refresh_article_glossary(
    cfg: dict[str, Any], issue_date: str, article_ids: set[str],
) -> list[dict[str, Any]]:
    """按当前 glossary 规则回填已有中文译文，不重新抓取或翻译正文。"""
    existing = read_database_js()
    targets = [
        article for article in existing
        if article.get("issue_date") == issue_date
        and (not article_ids or article.get("id") in article_ids)
        and any(
            isinstance(paragraph, dict) and str(paragraph.get("zh_text") or "").strip()
            for paragraph in (article.get("paragraphs") or [])
        )
    ]
    if not targets:
        log.warning(f"没有找到含中文译文的待刷新文章: issue={issue_date}, ids={sorted(article_ids)}")
        return []

    client = make_llm_client(cfg)
    refreshed: list[dict[str, Any]] = []
    for article in targets:
        try:
            enrich_article_glossary(client, cfg, article, log)
            refreshed.append(article)
            write_database_js(existing)
            log.info(
                f"[glossary] 已刷新 {article.get('id')}: "
                f"{len(article.get('glossary_entries') or [])} 个关键词, "
                f"complete={article.get('glossary_analysis_complete')}"
            )
        except Exception as exc:
            article["glossary_analysis_complete"] = False
            article["glossary_version"] = 0
            log.warning(f"[glossary] 刷新失败 {article.get('id')}: {exc}")

    if refreshed:
        write_database_js(existing)
        _sync_paper_outputs(cfg, existing, issue_date=issue_date)
        _maybe_rebuild_index(cfg)
    return refreshed


def _article_source_paragraphs(article: dict[str, Any]) -> list[dict[str, str]]:
    paragraphs: list[dict[str, str]] = []
    for paragraph in article.get("paragraphs") or []:
        if not isinstance(paragraph, dict):
            continue
        en_text = str(paragraph.get("en_text") or paragraph.get("text") or "").strip()
        if en_text:
            paragraphs.append(
                {
                    "role": str(paragraph.get("role") or "body").strip() or "body",
                    "en_text": en_text,
                }
            )
    if paragraphs:
        return paragraphs
    return _split_article_paragraphs(
        str(article.get("content_raw") or article.get("content_markdown") or "")
    )


def _article_quality_issues(article: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if not str(article.get("title_zh") or "").strip():
        issues.append("title_zh")
    if not str(article.get("summary_md") or "").strip():
        issues.append("summary_md")
    missing = [
        str(index)
        for index, paragraph in enumerate(article.get("paragraphs") or [], start=1)
        if isinstance(paragraph, dict)
        and str(paragraph.get("en_text") or "").strip()
        and not str(paragraph.get("zh_text") or "").strip()
    ]
    if missing:
        issues.append("zh_text:" + ",".join(missing[:20]))
    return issues


def _request_title_translation(
    client: Any,
    cfg: dict[str, Any],
    *,
    title: str,
    section: str,
    log_: logging.Logger,
) -> str:
    llm = cfg["llm"]
    prompt = f"""请把下面《The Wall Street Journal》文章标题译成自然、准确的中文标题，只返回严格 JSON。

要求：
1. 标题简洁清楚，不写解释。
2. 人名、公司名、机构名、品牌、平台、App、网站、产品和出版物名称保留英文原文，不音译、不意译。
3. 国家、地区、政策、法律和普通概念可按中文习惯翻译。

Title: {title}
Section: {section}

Return JSON:
{{"title_zh": "中文标题"}}
"""
    last_error: Exception | None = None
    max_retries = int(cfg["crawl"].get("max_retries", 2))
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=llm.get("model", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": "只返回 JSON。英文专名必须保留英文原文。"},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=512,
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            payload = _extract_json_payload(response.choices[0].message.content or "")
            title_zh = str(payload.get("title_zh") or "").strip()
            if not title_zh:
                raise LLMOutputValidationError("title_zh 为空")
            return title_zh
        except Exception as exc:
            last_error = exc
            log_.warning("标题补译失败 (attempt %d): %s", attempt + 1, exc)
            if not _should_retry_llm_error(exc, attempt, max_retries):
                break
            time.sleep(1.0)
    raise last_error or RuntimeError("标题补译失败")


def _select_repair_dates(articles: list[dict[str, Any]], issue_date: str | None) -> set[str]:
    if issue_date:
        return {issue_date}
    dates = sorted(
        {str(article.get("issue_date") or "") for article in articles if article.get("issue_date")},
        key=_date_key,
    )
    return {dates[-1]} if dates else set()


def repair_missing_translations(
    cfg: dict[str, Any],
    issue_date: str | None = None,
    article_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """发布前质量门禁：自动补齐中文标题、摘要和段落译文；仍有空白则失败。"""
    articles = read_database_js()
    selected_dates = _select_repair_dates(articles, issue_date)
    selected_ids = article_ids or set()
    if not selected_dates:
        log.info("[repair] 数据库为空，跳过中文完整性检查")
        return []

    targets = [
        article for article in articles
        if str(article.get("issue_date") or "") in selected_dates
        and (not selected_ids or str(article.get("id") or "") in selected_ids)
        and _article_quality_issues(article)
    ]
    if not targets:
        log.info("[repair] 中文完整性检查通过: dates=%s", ",".join(sorted(selected_dates)))
        return []
    if not str(cfg["llm"].get("api_key") or "").strip():
        raise RuntimeError("[repair] 发现中文空白但 LLM_API_KEY 未配置")

    client = make_llm_client(cfg)
    repaired: list[dict[str, Any]] = []
    for article in targets:
        article_id = str(article.get("id") or "")
        source_paragraphs = _article_source_paragraphs(article)
        before = _article_quality_issues(article)
        log.info("[repair] 修复 %s: %s", article_id, ";".join(before))

        if any(issue.startswith("zh_text:") for issue in before):
            article["paragraphs"] = _request_article_translation(
                client,
                cfg,
                title=str(article.get("title") or ""),
                section=str(article.get("section") or ""),
                source_paragraphs=source_paragraphs,
                article_id=article_id,
                log_=log,
            )

        if not str(article.get("summary_md") or "").strip() and source_paragraphs:
            try:
                title_zh, summary_md = _request_article_summary(
                    client,
                    cfg,
                    title=str(article.get("title") or ""),
                    section=str(article.get("section") or ""),
                    source_paragraphs=source_paragraphs,
                    log_=log,
                )
                article["title_zh"] = article.get("title_zh") or title_zh
                article["summary_md"] = summary_md
            except Exception as exc:
                log.warning("[repair] 中文解读补写失败，使用兼容摘要: %s", exc)
                article["summary_md"] = summarize(
                    client,
                    cfg,
                    str(article.get("title") or ""),
                    _format_source_content_markdown(source_paragraphs),
                    log,
                )

        if not str(article.get("title_zh") or "").strip():
            article["title_zh"] = _request_title_translation(
                client,
                cfg,
                title=str(article.get("title") or ""),
                section=str(article.get("section") or ""),
                log_=log,
            )

        if any(paragraph.get("zh_text") for paragraph in article.get("paragraphs") or []):
            article["glossary_entries"] = []
            article["term_annotations"] = []
            article["glossary_analysis_complete"] = False
            article["glossary_version"] = 0
            enrich_article_glossary(client, cfg, article, log)

        remaining = _article_quality_issues(article)
        article["compiled_article"] = not remaining
        article["compile_status"] = "complete" if not remaining else "fallback"
        write_database_js(articles)
        _sync_paper_outputs(cfg, read_database_js(), issue_date=str(article.get("issue_date") or ""))
        repaired.append(article)
        if remaining:
            raise RuntimeError(f"[repair] {article_id} 仍有中文空白: {remaining}")
        log.info("[repair] 已修复 %s", article_id)

    final_articles = read_database_js()
    remaining_targets = [
        article for article in final_articles
        if str(article.get("issue_date") or "") in selected_dates
        and (not selected_ids or str(article.get("id") or "") in selected_ids)
        and _article_quality_issues(article)
    ]
    if remaining_targets:
        details = {
            str(article.get("id") or ""): _article_quality_issues(article)
            for article in remaining_targets
        }
        raise RuntimeError(f"[repair] 中文完整性检查未通过: {details}")
    _sync_paper_outputs(cfg, final_articles)
    _maybe_rebuild_index(cfg)
    log.info("[repair] 中文完整性检查通过，修复 %d 篇", len(repaired))
    return repaired


def refresh_image_captions(
    cfg: dict[str, Any], article_ids: set[str],
) -> list[dict[str, Any]]:
    """补译已有图片说明到 image_insights，不重抓图片或重译正文。"""
    existing = read_database_js()
    targets: list[dict[str, Any]] = []
    for article in existing:
        if article_ids and str(article.get("id") or "") not in article_ids:
            continue
        placements = list(article.get("image_placements") or [])
        _, insights = _migrate_image_description_fields(
            placements, list(article.get("image_insights") or []),
        )
        described_paths = {
            str(item.get("path") or "")
            for item in insights
            if str(item.get("description") or "").strip()
        }
        has_legacy_fields = any(
            isinstance(item, dict)
            and ("caption_zh" in item or "description_zh" in item)
            for item in placements
        )
        has_missing_description = any(
            isinstance(item, dict)
            and str(item.get("path") or "").strip() not in described_paths
            and bool(str(item.get("caption") or item.get("alt_text") or "").strip())
            for item in placements
        )
        if has_legacy_fields or has_missing_description:
            targets.append(article)
    if not targets:
        log.info("没有需要补译中文图片说明的文章")
        return []
    client: Any = None
    refreshed: list[dict[str, Any]] = []
    for article in targets:
        deduplicate_article_image_metadata(article, cfg)
        placements, insights = _migrate_image_description_fields(
            list(article.get("image_placements") or []),
            list(article.get("image_insights") or []),
        )
        described_paths = {
            str(item.get("path") or "")
            for item in insights
            if str(item.get("description") or "").strip()
        }
        needs_llm = any(
            str(item.get("path") or "").strip() not in described_paths
            and bool(str(item.get("caption") or item.get("alt_text") or "").strip())
            and not count_cn_chars(str(item.get("caption") or item.get("alt_text") or ""))
            for item in placements
        )
        if needs_llm and client is None:
            if not str(cfg["llm"].get("api_key") or "").strip():
                raise RuntimeError("LLM_API_KEY 未配置，无法翻译图片说明")
            client = make_llm_client(cfg)
        article["image_placements"] = placements
        article["image_insights"] = translate_image_descriptions(
            client,
            cfg,
            str(article.get("title") or ""),
            placements,
            insights,
            log,
        )
        refreshed.append(article)
        log.info("已补译中文图片说明: %s - %s", article.get("id"), article.get("title"))
    _persist_wsj_state(cfg, existing)
    return refreshed


# ---------------------------------------------------------------------------
# Cookie 导入(把别处已登录的 cookie 灌到本机 Chrome profile)
# ---------------------------------------------------------------------------


def parse_cookie_file(path: Path) -> list[dict[str, Any]]:
    """从 JSON 数组或 Netscape cookies.txt 解析 cookie 列表。

    支持的 JSON 字段(浏览器扩展导出常见):
      name, value, domain, path, expirationDate(秒级 float),
      httpOnly, secure, sameSite, hostOnly, session

    Netscape 格式列序(domain, FLAG, path, secure, expires, name, value)。
    """
    text = path.read_text(encoding="utf-8")
    s = text.strip()
    if s.startswith("[") or s.startswith("{"):
        data = json.loads(s)
        if isinstance(data, dict):
            data = [data]
        return list(data)
    # Netscape cookies.txt
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _, path_, secure, expires, name, value = parts[:7]
        entry: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path_,
            "secure": secure.upper() == "TRUE",
        }
        if expires.lstrip("-").isdigit():
            entry["expirationDate"] = float(expires)
        out.append(entry)
    return out


def _cookie_for_drissionpage(c: dict[str, Any]) -> dict[str, Any]:
    """把浏览器扩展导出的字段映射成 DrissionPage set.cookies 接受的格式。"""
    payload: dict[str, Any] = {
        "name": c["name"],
        "value": c["value"],
        "domain": c.get("domain") or ".wsj.com",
        "path": c.get("path") or "/",
    }
    exp = c.get("expirationDate") or c.get("expires") or c.get("expiry")
    if isinstance(exp, (int, float)) and exp > 0:
        payload["expires"] = int(exp)
    if c.get("secure"):
        payload["secure"] = True
    if c.get("httpOnly"):
        payload["httpOnly"] = True
    return payload


def save_wsj_cookies(page: Any, cookie_path: Path) -> None:
    """把当前浏览器 Cookie 原子保存为仅当前用户可读的 JSON。"""
    rows = page.cookies(all_domains=True, all_info=True) or []
    if not isinstance(rows, list) or not rows:
        return
    cookie_path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic_text(cookie_path, json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    os.chmod(cookie_path, 0o600)


def load_wsj_cookies(page: Any, cookie_path: Path) -> int:
    if not cookie_path.exists():
        return 0
    rows = parse_cookie_file(cookie_path)
    payload = [
        _cookie_for_drissionpage(row)
        for row in rows
        if isinstance(row, dict) and row.get("name") and row.get("value") is not None
    ]
    if not payload:
        return 0
    page.get("https://www.wsj.com/")
    time.sleep(1.0)
    page.set.cookies(payload)
    log.info("已从本地加载 %d 个 WSJ Cookie", len(payload))
    return len(payload)


def _persist_wsj_state(
    cfg: dict[str, Any],
    articles: list[dict[str, Any]],
    *,
    issue_covers: dict[str, str] | None = None,
    issue_pages: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    write_database_js(articles)
    _sync_paper_outputs(
        cfg,
        articles,
        issue_covers=issue_covers,
        issue_pages=issue_pages,
    )
    _maybe_rebuild_index(cfg)


def regroup_archives_for_timezone(cfg: dict[str, Any], timezone_name: str) -> int:
    """按保留的 UTC 时间重新分日，并复制跨日文章的本地图片。"""
    ZoneInfo(timezone_name)
    articles = read_database_js()
    output_root = _paper_output_root(cfg)
    changed = 0
    for article in articles:
        published_at_utc = str(article.get("published_at_utc") or "").strip()
        if not published_at_utc:
            continue
        normalized_utc, published_local, new_date = _archive_timestamp(
            published_at_utc, timezone_name,
        )
        old_date = str(article.get("issue_date") or "")
        if old_date and old_date != new_date:
            for image_path in article.get("images") or []:
                relative = str(image_path or "")
                if not relative.startswith("images/"):
                    continue
                source = output_root / PAPER_PUBLICATION_TYPE / old_date / relative
                destination = output_root / PAPER_PUBLICATION_TYPE / new_date / relative
                if source.exists() and not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
            changed += 1
        article["issue_date"] = new_date
        article["published_at_utc"] = normalized_utc
        article["published_at_local"] = published_local
        article["archive_timezone"] = timezone_name
    cfg["crawl"]["archive_timezone"] = timezone_name
    _persist_wsj_state(cfg, articles)
    return changed


def process_wsj(
    cfg: dict[str, Any],
    *,
    issue_date: str | None = None,
    dry_run: bool = False,
    limit: int = 0,
) -> list[dict[str, Any]]:
    browser_cfg = cfg["browser"]
    page = open_browser(browser_cfg["user_data_path"], bool(browser_cfg.get("headless", False)))
    cookie_path = Path(browser_cfg["cookie_path"]).expanduser()
    try:
        load_wsj_cookies(page, cookie_path)
        adapter = WsjEReaderAdapter(
            page,
            log,
            timeout_s=float(cfg.get("ereader", {}).get("timeout_s", 30)),
        )
        selected_date = adapter.connect(issue_date)
        cover_url = adapter.first_page_cover_url()
        fetched_articles, pages = adapter.fetch_issue()
        existing = read_database_js()
        existing_source_ids = {
            str(article.get("source_id") or "").strip()
            for article in existing
            if str(article.get("source_id") or "").strip()
        }
        existing_title_keys = {
            (
                str(article.get("issue_date") or ""),
                re.sub(r"[^a-z0-9]+", " ", str(article.get("title") or "").lower()).strip(),
            )
            for article in existing
            if str(article.get("issue_date") or "") and str(article.get("title") or "").strip()
        }

        def already_archived(article: EReaderArticle) -> bool:
            title_key = re.sub(r"[^a-z0-9]+", " ", article.title.lower()).strip()
            return (
                article.source_id in existing_source_ids
                or (article.issue_date, title_key) in existing_title_keys
            )

        pending_articles = [article for article in fetched_articles if not already_archived(article)]
        if limit > 0:
            pending_articles = pending_articles[:limit]
        log.info(
            "eReader %s 共 %d 篇正常报道，其中 %d 篇已归档，本次待处理 %d",
            selected_date,
            len(fetched_articles),
            sum(1 for article in fetched_articles if already_archived(article)),
            len(pending_articles),
        )
        if dry_run:
            for article in pending_articles:
                print(
                    f"[DRY] {article.issue_date}  {article.print_page_label:5s}  "
                    f"{article.section:18s}  {article.title}  {article.source_id}"
                )
            return []
        cover_image = materialize_issue_cover(cover_url, cfg, selected_date, page=page)
        issue_covers = {selected_date: cover_image} if cover_image else None
        issue_pages = {selected_date: pages}
        if not pending_articles:
            _sync_paper_outputs(
                cfg,
                existing,
                issue_covers=issue_covers,
                issue_pages=issue_pages,
            )
            _maybe_rebuild_index(cfg)
            return []
        if not str(cfg["llm"].get("api_key") or "").strip():
            raise RuntimeError("LLM_API_KEY 未配置，无法执行翻译、中文解读和 glossary")

        compile_workers = max(1, int(cfg["pipeline"].get("compile_workers", 2)))
        max_pending = max(compile_workers, int(cfg["pipeline"].get("max_pending", 4)))
        new_articles: list[dict[str, Any]] = []
        next_sequence = _next_seq(existing, selected_date)

        with ThreadPoolExecutor(max_workers=compile_workers, thread_name_prefix="wsj-compile") as compile_pool:
            pending_compile: dict[Future, dict[str, Any]] = {}

            def persist(article: dict[str, Any]) -> None:
                existing.append(article)
                existing_source_ids.add(str(article.get("source_id") or ""))
                new_articles.append(article)
                _persist_wsj_state(
                    cfg,
                    existing,
                    issue_covers=issue_covers,
                    issue_pages=issue_pages,
                )
                log.info("已收录并更新根库、每日库和索引: %s - %s", article["id"], article["title"][:80])

            def drain_compiled(block: bool) -> None:
                if not pending_compile:
                    return
                done, _ = wait(
                    pending_compile,
                    timeout=None if block else 0,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    metadata = pending_compile.pop(future)
                    try:
                        article = future.result()
                    except Exception as exc:
                        log.error("翻译/编译失败 %s: %s", metadata["url"], exc)
                        continue
                    if article:
                        compiled_placements = article.get("image_placements")
                        article.update({
                            "source_id": metadata["source_id"],
                            "page": metadata["page"],
                            "page_article_index": metadata["page_article_index"],
                            "print_page_label": metadata["print_page_label"],
                            "print_section": metadata["print_section"],
                            "source_pages": metadata["source_pages"],
                            "subtitle": metadata["subtitle"],
                            "byline": metadata["byline"],
                            "image_placements": (
                                compiled_placements
                                if isinstance(compiled_placements, list)
                                else metadata["image_placements"]
                            ),
                        })
                        persist(article)

            for source_article in pending_articles:
                drain_compiled(block=False)
                while len(pending_compile) >= max_pending:
                    drain_compiled(block=True)
                article_id = f"art_{selected_date}_{next_sequence:03d}"
                next_sequence += 1
                images, image_placements = materialize_ereader_images(
                    source_article.images,
                    cfg,
                    selected_date,
                    article_id,
                )
                metadata = {
                    "issue_date": selected_date,
                    "section": source_article.section,
                    "title": source_article.title,
                    "url": source_article.url,
                    "body": source_article.body,
                    "article_id": article_id,
                    "images": images,
                    "source_id": source_article.source_id,
                    "page": source_article.page,
                    "page_article_index": source_article.page_article_index,
                    "print_page_label": source_article.print_page_label,
                    "print_section": source_article.section,
                    "source_pages": source_article.source_pages,
                    "subtitle": source_article.subtitle,
                    "byline": source_article.byline,
                    "image_placements": image_placements,
                }
                pending_compile[compile_pool.submit(_compile_article_task, cfg, metadata)] = metadata
                time.sleep(random.uniform(
                    float(cfg["crawl"].get("delay_min_s", 2)),
                    float(cfg["crawl"].get("delay_max_s", 4)),
                ))
            while pending_compile:
                drain_compiled(block=True)
        save_wsj_cookies(page, cookie_path)
        return new_articles
    finally:
        try:
            page.close()
        except Exception:
            pass


def login_wsj(cfg: dict[str, Any], issue_date: str | None = None) -> bool:
    browser_cfg = cfg["browser"]
    page = open_browser(browser_cfg["user_data_path"], False)
    cookie_path = Path(browser_cfg["cookie_path"]).expanduser()
    try:
        load_wsj_cookies(page, cookie_path)
        page.get(EREADER_URL)
        log.info("请在 Chromium 中完成 WSJ 登录，并确认 eReader 预览能够正常打开。")
        if sys.stdin.isatty():
            input("完成后按 Enter 保存 Cookie 并验证 eReader 日期与目录：")
        adapter = WsjEReaderAdapter(
            page,
            log,
            timeout_s=float(cfg.get("ereader", {}).get("timeout_s", 30)),
        )
        selected_date = adapter.connect(issue_date)
        candidates = adapter.discover_candidates()
        if not candidates:
            raise RuntimeError("eReader 没有返回可归档的报道目录")
        save_wsj_cookies(page, cookie_path)
        log.info(
            "eReader 登录验证成功：%s，共 %d 个目录条目；Cookie 已保存到 %s",
            selected_date,
            len(candidates),
            cookie_path,
        )
        return True
    finally:
        try:
            page.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="从 WSJ eReader 预览抓取整期报道并生成双语归档")
    p.add_argument("--date", default="", help="指定出版日期 YYYY-MM-DD；留空抓 eReader 最新一期")
    p.add_argument("--dry-run", action="store_true", help="抓取并去重，但不下载图片、不调用 LLM、不写库")
    p.add_argument("--limit", type=int, default=0, help="限制本次最多新增文章数(0=不限制)")
    p.add_argument("--login", action="store_true", help="打开独立 Chromium，人工登录并验证 eReader")
    p.add_argument("--refresh-glossary", action="store_true",
                   help="只按当前规则重新解析现有文章的中文关键词，不重抓或重译正文")
    p.add_argument("--repair-missing-translations", action="store_true",
                   help="发布前检查并自动补齐指定期次的空中文标题、摘要和段落译文")
    p.add_argument("--article-ids", default="",
                   help="配合刷新命令，逗号分隔 article id；留空表示全部")
    p.add_argument("--kill-stale", action="store_true",
                   help="启动前杀掉残留 Chrome 进程 + 删 lock 文件(默认也会自动做一次)")
    p.add_argument("--rebuild-outputs", action="store_true",
                   help="根据根 database.js 重建每日 database.js 和 database_index.js")
    return p.parse_args()


def main() -> int:
    global DATABASE_JS
    run_lock = acquire_run_lock()
    args = parse_args()
    cfg = load_config()
    configured_database = str(cfg.get("paths", {}).get("database_js") or "").strip()
    if configured_database:
        DATABASE_JS = Path(configured_database).expanduser()
    issue_date = str(args.date or "").strip() or None
    if issue_date:
        datetime.strptime(issue_date, "%Y-%m-%d")
    if args.login:
        return 0 if login_wsj(cfg, issue_date) else 1
    if args.rebuild_outputs:
        _sync_paper_outputs(cfg, read_database_js())
        return 0
    if args.kill_stale:
        _cleanup_stale_chrome_locks(cfg["browser"]["user_data_path"])
    if args.refresh_glossary:
        article_ids = {item.strip() for item in args.article_ids.split(",") if item.strip()}
        articles = read_database_js()
        selected_dates = sorted({str(item.get("issue_date") or "") for item in articles})
        for issue_date in selected_dates:
            refresh_article_glossary(cfg, issue_date, article_ids)
        return 0
    if args.repair_missing_translations:
        article_ids = {item.strip() for item in args.article_ids.split(",") if item.strip()}
        repair_missing_translations(cfg, issue_date, article_ids)
        return 0
    process_wsj(
        cfg,
        issue_date=issue_date,
        dry_run=args.dry_run,
        limit=max(0, args.limit),
    )
    run_lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
