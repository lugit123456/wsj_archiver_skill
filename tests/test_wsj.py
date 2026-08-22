from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sync_wsj
from sync_wsj import (
    _persist_wsj_state,
    _wsj_text_cover_url,
    acquire_run_lock,
    materialize_ereader_images,
)
from wsj_ereader import EReaderImage


def read_daily_payload(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    start = text.index(" = ", text.index("\n") + 1) + 3
    return json.loads(text[start:].rstrip().removesuffix(";"))


class WsjStorageTests(unittest.TestCase):
    def test_cover_text_layer_matches_graph_resource(self) -> None:
        graph_url = (
            "https://wsj-bcdn.newsmemory.com/ajax-request.php?"
            "action=loadImage&type=graph1024&issue=20260821&crc=abc%2Fdef"
        )
        text_url = _wsj_text_cover_url(graph_url)
        self.assertIn("type=text1024", text_url)
        self.assertIn("crc=abc%2Fdef", text_url)
        self.assertEqual(_wsj_text_cover_url(graph_url.replace("graph1024", "graph")), "")

    def test_run_lock_rejects_overlapping_sync(self) -> None:
        first = acquire_run_lock()
        try:
            with self.assertRaisesRegex(RuntimeError, "已有 WSJ 同步任务"):
                acquire_run_lock()
        finally:
            first.close()

    def test_root_database_keeps_distinct_sources_with_same_ereader_url(self) -> None:
        articles = [
            {
                "id": "art_2026-08-21_001",
                "issue_date": "2026-08-21",
                "source_id": "wsj-ereader:2026-08-21:A1:1",
                "url": sync_wsj.EREADER_URL,
            },
            {
                "id": "art_2026-08-21_002",
                "issue_date": "2026-08-21",
                "source_id": "wsj-ereader:2026-08-21:A1:2",
                "url": sync_wsj.EREADER_URL,
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "database.js"
            with patch.object(sync_wsj, "DATABASE_JS", database):
                sync_wsj.write_database_js(articles)
                stored = sync_wsj.read_database_js()
        self.assertEqual(len(stored), 2)
        self.assertEqual({item["source_id"] for item in stored}, {
            "wsj-ereader:2026-08-21:A1:1",
            "wsj-ereader:2026-08-21:A1:2",
        })

    def test_ereader_fields_and_issue_pages_are_written(self) -> None:
        article = {
            "id": "art_2026-08-21_001",
            "source_id": "wsj-ereader:2026-08-21:A1:3",
            "issue_date": "2026-08-21",
            "section": "Main",
            "page": 1,
            "page_article_index": 2,
            "print_page_label": "A1",
            "print_section": "Main",
            "source_pages": [1, 10],
            "title": "English title",
            "title_zh": "中文标题",
            "subtitle": "Deck",
            "byline": "By Reporter",
            "url": sync_wsj.EREADER_URL,
            "summary_md": "中文解读",
            "content_raw": "English body",
            "content_markdown": "English body",
            "paragraphs": [{
                "para_id": "art_2026-08-21_001_p1",
                "en_text": "English body",
                "zh_text": "中文正文",
                "role": "body",
            }],
            "images": ["images/art_2026-08-21_001_01.jpg"],
            "image_placements": [{
                "path": "images/art_2026-08-21_001_01.jpg",
                "placement": "after_paragraph",
                "after_paragraph_index": 1,
                "caption": "Original caption",
                "credit": "Original credit",
                "alt_text": "Original alt",
            }],
            "image_insights": [],
            "glossary_entries": [],
            "term_annotations": [],
            "glossary_analysis_complete": True,
            "glossary_version": sync_wsj.GLOSSARY_VERSION,
            "compiled_article": True,
            "compile_status": "complete",
        }
        pages = [
            {"page": 1, "print_page_label": "A1", "print_section": "Main"},
            {"page": 10, "print_page_label": "A10", "print_section": "From Page One"},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = {key: dict(value) for key, value in sync_wsj.DEFAULTS.items()}
            config["paths"]["output_root"] = str(root / "output_results")
            config["paths"]["index_html"] = ""
            database = root / "database.js"
            with patch.object(sync_wsj, "DATABASE_JS", database):
                _persist_wsj_state(
                    config,
                    [article],
                    issue_pages={"2026-08-21": pages},
                )

            daily = root / "output_results" / "WSJ" / "2026-08-21" / "database.js"
            payload = read_daily_payload(daily)
            stored = payload["articles"][0]
            self.assertEqual(stored["source_id"], article["source_id"])
            self.assertEqual(stored["print_page_label"], "A1")
            self.assertEqual(stored["source_pages"], [1])
            self.assertEqual(stored["print_section"], "PAGE ONE")
            self.assertEqual(stored["image_placements"][0]["caption"], "Original caption")
            self.assertEqual(len(payload["pages"]), 1)
            self.assertEqual(payload["pages"][0]["pdf_page"], 1)
            self.assertEqual(payload["pages"][0]["print_page_label"], "A1")
            self.assertEqual(payload["pages"][0]["print_section"], "PAGE ONE")
            self.assertEqual(payload["pages"][0]["article_ids"], [article["id"]])
            self.assertNotIn("published_at_utc", stored)

    def test_ereader_image_metadata_follows_materialized_path(self) -> None:
        source = [
            EReaderImage(
                url="https://example.invalid/photo.jpg",
                placement="lead",
                after_paragraph_index=None,
                caption="Caption",
                credit="Credit",
                alt_text="Alt",
            )
        ]
        with patch.object(
            sync_wsj,
            "materialize_article_images",
            return_value=["images/local.jpg"],
        ):
            paths, placements = materialize_ereader_images(
                source,
                sync_wsj.DEFAULTS,
                "2026-08-21",
                "art_2026-08-21_001",
            )
        self.assertEqual(paths, ["images/local.jpg"])
        self.assertEqual(placements[0]["path"], "images/local.jpg")
        self.assertEqual(placements[0]["caption"], "Caption")


if __name__ == "__main__":
    unittest.main()
