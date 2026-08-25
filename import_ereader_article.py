#!/usr/bin/env python3
"""Import one eReader article payload captured from an authenticated browser."""
from __future__ import annotations

import argparse
import json
import re
import sys

from sync_wsj import (
    _compile_article_task,
    _next_seq,
    _persist_wsj_state,
    load_config,
    materialize_ereader_images,
    read_database_js,
)
from wsj_ereader import EReaderCandidate, article_from_payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Issue date in YYYY-MM-DD format")
    parser.add_argument("--section", required=True)
    parser.add_argument("--page", required=True, type=int)
    parser.add_argument("--page-label", required=True)
    parser.add_argument("--article-index", required=True, type=int)
    parser.add_argument("--xml-id", required=True, type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = json.load(sys.stdin)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("Browser payload is missing the article title")

    candidate = EReaderCandidate(
        issue_date=args.date,
        section=args.section,
        page=args.page,
        print_page_label=args.page_label,
        page_article_index=args.article_index,
        xml_id=args.xml_id,
        title=title,
    )
    source = article_from_payload(
        candidate,
        payload,
        page_by_label={args.page_label: args.page},
        root_candidates={(args.page_label, args.xml_id): candidate},
    )
    existing = read_database_js()
    title_key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    duplicate = next(
        (
            article for article in existing
            if str(article.get("source_id") or "") == source.source_id
            or (
                str(article.get("issue_date") or "") == args.date
                and re.sub(
                    r"[^a-z0-9]+", " ", str(article.get("title") or "").lower()
                ).strip() == title_key
            )
        ),
        None,
    )
    if duplicate is not None:
        print(json.dumps({"status": "already_archived", "id": duplicate.get("id"), "title": title}))
        return 0

    cfg = load_config()
    if not str(cfg["llm"].get("api_key") or "").strip():
        raise RuntimeError("LLM_API_KEY is required to compile the article")
    article_id = f"art_{args.date}_{_next_seq(existing, args.date):03d}"
    images, placements = materialize_ereader_images(
        source.images,
        cfg,
        args.date,
        article_id,
    )
    metadata = {
        "issue_date": args.date,
        "section": source.section,
        "title": source.title,
        "url": source.url,
        "body": source.body,
        "article_id": article_id,
        "images": images,
        "image_placements": placements,
    }
    article = _compile_article_task(cfg, metadata)
    if article is None:
        raise RuntimeError("Article compilation produced no record")
    article.update({
        "source_id": source.source_id,
        "page": source.page,
        "page_article_index": source.page_article_index,
        "print_page_label": source.print_page_label,
        "print_section": source.section,
        "source_pages": source.source_pages,
        "subtitle": source.subtitle,
        "byline": source.byline,
        "image_placements": placements,
    })
    existing.append(article)
    _persist_wsj_state(cfg, existing)
    print(json.dumps({
        "status": "archived",
        "id": article_id,
        "title": article["title"],
        "title_zh": article["title_zh"],
        "paragraphs": len(article.get("paragraphs") or []),
        "images": len(article.get("images") or []),
        "image_insights": len(article.get("image_insights") or []),
        "compile_status": article.get("compile_status"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
