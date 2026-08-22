from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
import re
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse


EREADER_URL = "https://ereader.wsj.net/?editionStart=The+Wall+Street+Journal"
_ARTICLE_LINK_RE = re.compile(
    r"art_printArticle\(\s*(\d+)\s*,\s*(\d+)\s*\)", re.IGNORECASE,
)
_SECTION_LINK_RE = re.compile(r"ta_articleScrollUp\(\s*['\"]?(\d+)", re.IGNORECASE)
_JUMP_LINK_RE = re.compile(
    r"art_getJumpId\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]"
    r"\s*,\s*['\"][^'\"]+['\"]\s*,\s*['\"](\d+)['\"]\s*\)",
    re.IGNORECASE,
)


class EReaderError(RuntimeError):
    pass


class EReaderAccessError(EReaderError):
    pass


class EReaderIssueUnavailable(EReaderError):
    pass


@dataclass(frozen=True)
class EReaderCandidate:
    issue_date: str
    section: str
    page: int
    print_page_label: str
    page_article_index: int
    xml_id: int
    title: str


@dataclass
class EReaderImage:
    url: str
    placement: str
    after_paragraph_index: int | None
    caption: str = ""
    credit: str = ""
    alt_text: str = ""


@dataclass
class EReaderArticle:
    source_id: str
    issue_date: str
    section: str
    page: int
    print_page_label: str
    page_article_index: int
    title: str
    subtitle: str
    byline: str
    paragraphs: list[dict[str, str]]
    images: list[EReaderImage]
    source_pages: list[int] = field(default_factory=list)
    url: str = EREADER_URL

    @property
    def body(self) -> str:
        blocks: list[str] = []
        for paragraph in self.paragraphs:
            text = _clean_text(paragraph.get("text"))
            if not text:
                continue
            blocks.append(f"## {text}" if paragraph.get("role") == "crosshead" else text)
        return "\n\n".join(blocks)


_INDEX_SCRIPT = r"""
function() {
  return Array.from(document.querySelectorAll('#slideArt a')).map((link) => ({
    text: (link.textContent || '').replace(/\s+/g, ' ').trim(),
    href: link.getAttribute('href') || ''
  })).filter((item) => item.href);
}
"""


_SELECT_OPTIONS_SCRIPT = r"""
function(selector) {
  const select = document.querySelector(selector);
  if (!select) return [];
  return Array.from(select.options).map((option) => ({
    text: (option.textContent || '').replace(/\s+/g, ' ').trim(),
    value: option.value || '',
    selected: Boolean(option.selected)
  }));
}
"""


_ARTICLE_READY_SCRIPT = r"""
function() {
  const article = document.querySelector('#articletext');
  if (!article) return {ready: false, chars: 0, title: '', nodes: 0, images: 0, html_chars: 0};
  const body = article.querySelector('[class^="article_body"]');
  const title = article.querySelector('[class^="article_head"] .maintitle');
  const chars = body ? (body.textContent || '').trim().length : 0;
  return {
    ready: chars > 0,
    chars: chars,
    title: title ? (title.textContent || '').replace(/\s+/g, ' ').trim() : '',
    nodes: body ? body.querySelectorAll('p.abody, div.endart, div.imgArt').length : 0,
    images: article.querySelectorAll('div.imgArt img:not(.imgchild)').length,
    html_chars: body ? body.innerHTML.length : 0
  };
}
"""


_ARTICLE_EXTRACT_SCRIPT = r"""
function() {
  const article = document.querySelector('#articletext');
  if (!article) return null;
  const head = article.querySelector('[class^="article_head"]');
  const body = article.querySelector('[class^="article_body"]');
  if (!body) return null;

  const clean = (value) => {
    if (value && typeof value.textContent === 'string') value = value.textContent;
    return String(value || '').replace(/\s+/g, ' ').trim();
  };
  const title = clean(head && head.querySelector('.maintitle'));
  const subtitle = clean(head && head.querySelector('.subtitle'));
  const byline = clean(body.querySelector('p.byline'));
  const events = [];

  // Continuation images can be appended outside article_body after the text is ready.
  const nodes = article.querySelectorAll(
    '[class^="article_body"] p.abody, [class^="article_body"] div.endart, div.imgArt'
  );
  nodes.forEach((node) => {
    if (node.matches('div.endart')) {
      events.push({type: 'page_break'});
      return;
    }
    if (node.matches('div.imgArt')) {
      const image = Array.from(node.querySelectorAll('img')).find((img) => {
        const source = img.currentSrc || img.getAttribute('src') ||
          img.getAttribute('data-src') || img.getAttribute('data-original');
        return !img.classList.contains('imgchild') && clean(source);
      });
      if (!image) return;
      const imageSource = clean(
        image.currentSrc || image.getAttribute('src') || image.getAttribute('data-src') ||
        image.getAttribute('data-original')
      );
      const captionParts = [];
      const creditParts = [];
      node.querySelectorAll('p.paragraph span').forEach((span) => {
        const text = clean(span.textContent);
        if (!text) return;
        if (span.classList.contains('Fid_12')) creditParts.push(text);
        else captionParts.push(text);
      });
      events.push({
        type: 'image',
        url: new URL(imageSource, document.baseURI).href,
        caption: clean(captionParts.join(' ')),
        credit: clean(creditParts.join(' ')),
        alt_text: clean(image.getAttribute('alt'))
      });
      return;
    }
    const text = clean(node.textContent);
    if (!text) return;
    const bold = node.querySelector('span[style*="font-weight:bold"], strong, b');
    events.push({
      type: bold && text.length <= 180 ? 'crosshead' : 'paragraph',
      text: text,
      split: node.classList.contains('split')
    });
  });

  const jump = Array.from(article.querySelectorAll('a[href*="art_getJumpId"]'))
    .map((link) => ({text: clean(link.textContent), href: link.getAttribute('href') || ''}))
    .find((item) => /CONTINUED\s+FROM/i.test(item.text));
  return {title, subtitle, byline, events, jump: jump || null};
}
"""


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalise_page_label(value: str) -> str:
    raw = _clean_text(value).upper().removeprefix("PAGE ")
    match = re.fullmatch(r"([A-Z]+)0*(\d+)([A-Z]*)", raw)
    if not match:
        return raw
    number = str(int(match.group(2))) if match.group(2) else "0"
    return f"{match.group(1)}{number}{match.group(3)}"


def source_id_for(issue_date: str, print_page_label: str, xml_id: int | str) -> str:
    return f"wsj-ereader:{issue_date}:{normalise_page_label(print_page_label)}:{int(xml_id)}"


def _date_from_value(value: str) -> str:
    return datetime.strptime(str(value), "%Y%m%d").date().isoformat()


def _wait_for(
    callback: Callable[[], Any],
    *,
    timeout_s: float,
    interval_s: float = 0.2,
    message: str,
) -> Any:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = callback()
            if value:
                return value
        except Exception as exc:
            last_error = exc
        time.sleep(interval_s)
    detail = f": {last_error}" if last_error else ""
    raise EReaderError(f"{message}{detail}")


def _find_reader_frame(container: Any, depth: int = 0) -> Any | None:
    if depth > 5:
        return None
    try:
        owns_marker = container.run_js(
            "function(){return Boolean(document.querySelector('#pullDownDate'));}"
        )
        if owns_marker:
            return container
    except Exception:
        pass
    try:
        frames = container.eles("tag:iframe", timeout=0.5) or []
    except Exception:
        return None
    for frame in frames:
        found = _find_reader_frame(frame, depth + 1)
        if found is not None:
            return found
    return None


def _safe_reader_src(value: str) -> str:
    src = str(value or "").strip()
    parsed = urlparse(src)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith("newsmemory.com"):
        raise EReaderAccessError("eReader 没有生成有效的 Newsmemory 阅读器会话")
    return src


def _normalise_events(events: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[EReaderImage]]:
    paragraphs: list[dict[str, str]] = []
    images: list[EReaderImage] = []
    join_next = False
    seen_images: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        if event_type == "page_break":
            join_next = True
            continue
        if event_type == "image":
            url = str(event.get("url") or "").strip()
            if not url or url in seen_images:
                continue
            seen_images.add(url)
            images.append(
                EReaderImage(
                    url=url,
                    placement="lead",
                    after_paragraph_index=None,
                    caption=_clean_text(event.get("caption")),
                    credit=_clean_text(event.get("credit")),
                    alt_text=_clean_text(event.get("alt_text")),
                )
            )
            continue
        if event_type not in {"paragraph", "crosshead"}:
            continue
        text = _clean_text(event.get("text"))
        if not text:
            continue
        role = "crosshead" if event_type == "crosshead" else "body"
        should_join = bool(event.get("split") or join_next)
        if should_join and role == "body" and paragraphs and paragraphs[-1]["role"] == "body":
            paragraphs[-1]["text"] = _clean_text(f"{paragraphs[-1]['text']} {text}")
        else:
            paragraphs.append({"role": role, "text": text})
        join_next = False
    return paragraphs, images


def article_from_payload(
    candidate: EReaderCandidate,
    payload: dict[str, Any],
    *,
    page_by_label: dict[str, int],
    root_candidates: dict[tuple[str, int], EReaderCandidate],
) -> EReaderArticle:
    paragraphs, images = _normalise_events(list(payload.get("events") or []))
    root_label = candidate.print_page_label
    root_xml_id = candidate.xml_id
    jump = payload.get("jump") if isinstance(payload.get("jump"), dict) else None
    if jump:
        match = _JUMP_LINK_RE.search(str(jump.get("href") or ""))
        if match:
            root_label = normalise_page_label(match.group(1))
            root_xml_id = int(match.group(3))

    root_candidate = root_candidates.get((root_label, root_xml_id))
    page = page_by_label.get(root_label, candidate.page)
    section = root_candidate.section if root_candidate else candidate.section
    page_article_index = (
        root_candidate.page_article_index if root_candidate else candidate.page_article_index
    )
    title = _clean_text(payload.get("title")) or candidate.title
    return EReaderArticle(
        source_id=source_id_for(candidate.issue_date, root_label, root_xml_id),
        issue_date=candidate.issue_date,
        section=section,
        page=page,
        print_page_label=root_label,
        page_article_index=page_article_index,
        title=title,
        subtitle=_clean_text(payload.get("subtitle")),
        byline=_clean_text(payload.get("byline")),
        paragraphs=paragraphs,
        images=images,
        source_pages=sorted({candidate.page, page}),
    )


def _article_score(article: EReaderArticle) -> tuple[int, int, int]:
    body_chars = sum(len(str(item.get("text") or "")) for item in article.paragraphs)
    return body_chars, len(article.paragraphs), len(article.title)


def _merge_variant_metadata(
    best: EReaderArticle,
    variants: list[EReaderArticle],
) -> EReaderArticle:
    best.source_pages = sorted({page for item in variants for page in item.source_pages})
    seen_urls = {image.url for image in best.images}
    for variant in variants:
        if variant is best:
            continue
        for image in variant.images:
            if image.url in seen_urls:
                continue
            seen_urls.add(image.url)
            best.images.append(
                EReaderImage(
                    url=image.url,
                    placement="lead",
                    after_paragraph_index=None,
                    caption=image.caption,
                    credit=image.credit,
                    alt_text=image.alt_text,
                )
            )
    return best


def deduplicate_articles(articles: list[EReaderArticle]) -> list[EReaderArticle]:
    by_source: dict[str, list[EReaderArticle]] = {}
    for article in articles:
        by_source.setdefault(article.source_id, []).append(article)

    selected: list[EReaderArticle] = []
    for variants in by_source.values():
        selected.append(_merge_variant_metadata(max(variants, key=_article_score), variants))

    # 部分旧版期次没有 page-jump 链接。仅对标题完全一致的版本做第二层去重。
    by_title: dict[str, list[EReaderArticle]] = {}
    for article in selected:
        key = re.sub(r"[^a-z0-9]+", " ", article.title.lower()).strip()
        by_title.setdefault(key or article.source_id, []).append(article)
    output: list[EReaderArticle] = []
    for variants in by_title.values():
        output.append(_merge_variant_metadata(max(variants, key=_article_score), variants))
    output.sort(key=lambda item: (item.page, item.page_article_index, item.title))
    return output


class WsjEReaderAdapter:
    def __init__(self, page: Any, logger: logging.Logger, timeout_s: float = 30) -> None:
        self.page = page
        self.log = logger
        self.timeout_s = max(5.0, float(timeout_s))
        self.frame: Any | None = None
        self.issue_date = ""
        self.pages: list[dict[str, Any]] = []
        self.section_starts: list[tuple[int, str]] = []

    def connect(self, issue_date: str | None = None) -> str:
        self.page.get(EREADER_URL)
        try:
            self.page.wait.doc_loaded(timeout=min(self.timeout_s, 15), raise_err=False)
        except Exception:
            pass

        def reader_src() -> str:
            frame = self.page.ele("css:iframe#eeditionFrame", timeout=1)
            return _safe_reader_src(frame.attr("src")) if frame else ""

        try:
            src = _wait_for(
                reader_src,
                timeout_s=self.timeout_s,
                message="等待 eReader 登录会话超时",
            )
        except EReaderError as exc:
            raise EReaderAccessError(str(exc)) from exc

        # 直接进入短期授权的 Newsmemory 页面，避免多层跨域 iframe 连接不稳定。
        self.page.get(src)
        try:
            self.page.wait.doc_loaded(timeout=min(self.timeout_s, 15), raise_err=False)
        except Exception:
            pass
        self.frame = _wait_for(
            lambda: _find_reader_frame(self.page),
            timeout_s=self.timeout_s,
            message="阅读器已打开，但没有加载出版日期控件",
        )
        self.issue_date = self.select_issue(issue_date)
        self.pages = self._read_pages()
        return self.issue_date

    def available_issues(self) -> list[str]:
        if self.frame is None:
            return []
        rows = self.frame.run_js(_SELECT_OPTIONS_SCRIPT, "#pullDownDate") or []
        return [_date_from_value(row["value"]) for row in rows if row.get("value")]

    def select_issue(self, issue_date: str | None) -> str:
        if self.frame is None:
            raise EReaderError("阅读器尚未连接")
        try:
            options = _wait_for(
                lambda: self.frame.run_js(_SELECT_OPTIONS_SCRIPT, "#pullDownDate") or [],
                timeout_s=self.timeout_s,
                message="等待 eReader 出版日期列表超时",
            )
        except EReaderError as exc:
            raise EReaderAccessError(str(exc)) from exc
        selected = next((row for row in options if row.get("selected")), options[0])
        if not issue_date:
            return _date_from_value(str(selected["value"]))
        datetime.strptime(issue_date, "%Y-%m-%d")
        raw = issue_date.replace("-", "")
        if raw not in {str(row.get("value") or "") for row in options}:
            available = ", ".join(self.available_issues())
            raise EReaderIssueUnavailable(
                f"eReader 当前日期列表没有 {issue_date}；可选日期: {available}"
            )
        if str(selected.get("value")) != raw:
            select = self.frame.ele("css:#pullDownDate", timeout=2)
            if not select:
                raise EReaderError("找不到日期选择控件")
            select.select.by_value(raw, timeout=3)

            def changed() -> bool:
                state = self.frame.run_js(
                    "function(v){const s=document.querySelector('#pullDownDate');"
                    "const p=document.querySelector('#pullDownPage');"
                    "return Boolean(s && s.value===v && p && p.options.length);}",
                    raw,
                )
                return bool(state)

            _wait_for(changed, timeout_s=self.timeout_s, message=f"切换到 {issue_date} 超时")
            time.sleep(1.0)
        return issue_date

    def _read_pages(self) -> list[dict[str, Any]]:
        if self.frame is None:
            return []
        rows = self.frame.run_js(_SELECT_OPTIONS_SCRIPT, "#pullDownPage") or []
        pages: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            label = normalise_page_label(str(row.get("text") or ""))
            if label:
                pages.append({"page": index, "print_page_label": label})
        return pages

    def first_page_cover_url(self) -> str:
        """Return the high-resolution graphical image for the issue's first page."""
        if self.frame is None:
            raise EReaderError("阅读器尚未连接")

        def image_url() -> str:
            value = str(self.frame.run_js(
                """
                const image = document.querySelector('#imggraph_0');
                return image && image.complete && image.naturalWidth
                  ? (image.currentSrc || image.src || '')
                  : '';
                """
            ) or "")
            query = parse_qs(urlparse(value).query)
            expected_issue = self.issue_date.replace("-", "")
            if query.get("issue", [""])[0] != expected_issue:
                return ""
            if query.get("type", [""])[0] != "graph1024":
                return ""
            return value

        try:
            return str(_wait_for(
                image_url,
                timeout_s=self.timeout_s,
                message="eReader 第一版高清图片加载超时",
            ))
        except EReaderError as exc:
            self.log.warning("未能读取 eReader 第一版封面: %s", exc)
            return ""

    def _open_main_index(self) -> list[dict[str, str]]:
        if self.frame is None:
            raise EReaderError("阅读器尚未连接")
        self.frame.run_js(
            "function(){if(typeof hybrid_openTextualMode!=='function') return false;"
            "hybrid_openTextualMode(); return true;}"
        )

        def section_links() -> list[dict[str, str]]:
            links = self.frame.run_js(_INDEX_SCRIPT) or []
            return [row for row in links if "ta_articleScrollUp" in str(row.get("href") or "")]

        return _wait_for(
            section_links,
            timeout_s=self.timeout_s,
            message="整期栏目索引加载超时",
        )

    def discover_candidates(self) -> list[EReaderCandidate]:
        section_rows = self._open_main_index()
        page_labels = {int(row["page"]): str(row["print_page_label"]) for row in self.pages}
        candidates: list[EReaderCandidate] = []
        seen: set[tuple[int, int]] = set()
        self.section_starts = []
        for row in section_rows:
            match = _SECTION_LINK_RE.search(str(row.get("href") or ""))
            if not match:
                continue
            start_page = int(match.group(1))
            start_label = page_labels.get(start_page, "")
            section = _clean_text(row.get("text"))
            if start_label and section.upper().endswith(start_label.upper()):
                section = section[: -len(start_label)].strip()
            section = section or "General"
            self.section_starts.append((start_page, section))
            opened = False
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    self.frame.run_js(
                        "function(pageId){ta_articleScrollUp(String(pageId)); return true;}",
                        start_page,
                    )
                    opened = True
                    break
                except Exception as exc:
                    last_error = exc
                    time.sleep(0.5 * (attempt + 1))
            if not opened:
                self.log.warning("栏目目录切换失败，跳过 %s: %s", section, last_error)
                continue

            def article_links() -> list[dict[str, str]]:
                links = self.frame.run_js(_INDEX_SCRIPT) or []
                return [row for row in links if "art_printArticle" in str(row.get("href") or "")]

            try:
                links = _wait_for(
                    article_links,
                    timeout_s=min(self.timeout_s, 8.0),
                    message=f"栏目 {section} 的文章索引加载超时",
                )
            except EReaderError:
                self.log.info("跳过没有普通 Editorial 报道的栏目: %s", section)
                continue
            page_counts: dict[int, int] = {}
            for link in links:
                article_match = _ARTICLE_LINK_RE.search(str(link.get("href") or ""))
                if not article_match:
                    continue
                page_id, xml_id = (int(article_match.group(1)), int(article_match.group(2)))
                if (page_id, xml_id) in seen:
                    continue
                title = _clean_text(str(link.get("text") or "").lstrip("•"))
                page_label = page_labels.get(page_id, "")
                if not title or not page_label:
                    continue
                seen.add((page_id, xml_id))
                page_counts[page_id] = page_counts.get(page_id, 0) + 1
                candidates.append(
                    EReaderCandidate(
                        issue_date=self.issue_date,
                        section=section,
                        page=page_id,
                        print_page_label=page_label,
                        page_article_index=page_counts[page_id],
                        xml_id=xml_id,
                        title=title,
                    )
                )
        candidates.sort(key=lambda item: (item.page, item.page_article_index, item.title))
        return candidates

    def extract_article(
        self,
        candidate: EReaderCandidate,
        *,
        root_candidates: dict[tuple[str, int], EReaderCandidate],
        page_by_label: dict[str, int],
    ) -> EReaderArticle:
        if self.frame is None:
            raise EReaderError("阅读器尚未连接")
        previous = self.frame.run_js(_ARTICLE_READY_SCRIPT) or {}
        previous_signature = (
            _clean_text(previous.get("title")),
            int(previous.get("chars") or 0),
        )
        def article_changed() -> bool:
            state = self.frame.run_js(_ARTICLE_READY_SCRIPT) or {}
            signature = (_clean_text(state.get("title")), int(state.get("chars") or 0))
            return bool(state.get("ready")) and (
                not previous.get("ready") or signature != previous_signature
            )

        last_error: EReaderError | None = None
        for attempt in range(2):
            self.frame.run_js(
                "function(pageId, xmlId){art_printArticle(pageId, xmlId); return true;}",
                candidate.page,
                candidate.xml_id,
            )
            try:
                _wait_for(
                    article_changed,
                    timeout_s=self.timeout_s,
                    message=f"正文加载超时: {candidate.print_page_label} {candidate.title}",
                )
                last_error = None
                break
            except EReaderError as exc:
                last_error = exc
                state = self.frame.run_js(_ARTICLE_READY_SCRIPT) or {}
                current_title = _clean_text(state.get("title"))
                current_chars = int(state.get("chars") or 0)
                if current_title and current_title != previous_signature[0] and current_chars == 0:
                    raise EReaderError(
                        f"目录条目没有可提取正文: {candidate.print_page_label} {candidate.title}"
                    ) from exc
                if attempt == 0:
                    self.log.info(
                        "正文切换未生效，重试一次: %s %s",
                        candidate.print_page_label,
                        candidate.title,
                    )
                    time.sleep(0.5)
        if last_error is not None:
            raise last_error
        self._wait_for_article_dom_stable()
        payload = self.frame.run_js(_ARTICLE_EXTRACT_SCRIPT) or {}
        article = article_from_payload(
            candidate,
            payload,
            page_by_label=page_by_label,
            root_candidates=root_candidates,
        )
        if not article.title or not article.paragraphs:
            raise EReaderError(
                f"不是可归档的普通报道: {candidate.print_page_label} {candidate.title}"
            )
        return article

    def _wait_for_article_dom_stable(self) -> None:
        """等待 eReader 异步补齐续页和图片节点。"""
        if self.frame is None:
            return
        started = time.monotonic()
        deadline = started + min(self.timeout_s, 6.0)
        stable_since = started
        last_signature: tuple[Any, ...] | None = None
        while time.monotonic() < deadline:
            state = self.frame.run_js(_ARTICLE_READY_SCRIPT) or {}
            signature = (
                _clean_text(state.get("title")),
                int(state.get("chars") or 0),
                int(state.get("nodes") or 0),
                int(state.get("images") or 0),
                int(state.get("html_chars") or 0),
            )
            now = time.monotonic()
            if signature != last_signature:
                last_signature = signature
                stable_since = now
            if (
                state.get("ready")
                and now - started >= 1.5
                and now - stable_since >= 0.5
            ):
                return
            time.sleep(0.1)

    def fetch_issue(self, *, limit: int = 0) -> tuple[list[EReaderArticle], list[dict[str, Any]]]:
        candidates = self.discover_candidates()
        root_candidates = {
            (candidate.print_page_label, candidate.xml_id): candidate for candidate in candidates
        }
        page_by_label = {
            str(row["print_page_label"]): int(row["page"]) for row in self.pages
        }
        variants: list[EReaderArticle] = []
        for candidate in candidates:
            try:
                variants.append(
                    self.extract_article(
                        candidate,
                        root_candidates=root_candidates,
                        page_by_label=page_by_label,
                    )
                )
            except EReaderError as exc:
                self.log.warning("跳过 %s %s: %s", candidate.print_page_label, candidate.title, exc)
            if limit > 0 and len(deduplicate_articles(variants)) >= limit:
                break
        articles = deduplicate_articles(variants)
        if limit > 0:
            articles = articles[:limit]

        ordered_sections = sorted(set(self.section_starts))

        def section_for_page(page: int) -> str:
            section = ""
            for start_page, name in ordered_sections:
                if start_page > page:
                    break
                section = name
            return section

        pages = [
            {
                **row,
                "print_section": section_for_page(int(row["page"])),
            }
            for row in self.pages
        ]
        return articles, pages
