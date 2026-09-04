from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sync_wsj
from sync_wsj import (
    DEFAULTS,
    _compile_article_task,
    _request_article_translation,
    _summary_length_bounds,
    compile_article_record,
    repair_missing_translations,
)


def _config(*, max_retries: int = 0) -> dict[str, object]:
    config = {key: dict(value) for key, value in DEFAULTS.items()}
    config["crawl"]["max_retries"] = max_retries  # type: ignore[index]
    config["glossary"]["enabled"] = False  # type: ignore[index]
    return config


def _natural_summary() -> str:
    sentence = "文章围绕核心问题展开分析，并用关键事实解释相关风险以及政策选择。"
    paragraph = sentence * 5
    return "\n\n".join([paragraph, paragraph, paragraph])


class _FakeCompletions:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = payloads
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.requests.append(kwargs)
        payload = self.payloads[len(self.requests) - 1]
        message = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _client(payloads: list[dict[str, object]]) -> tuple[SimpleNamespace, _FakeCompletions]:
    completions = _FakeCompletions(payloads)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


class ArticleCompileTests(unittest.TestCase):
    def test_translation_and_summary_use_separate_requests_without_schema_changes(self) -> None:
        summary = _natural_summary()
        client, completions = _client([
            {
                "paragraphs": [
                    {"zh_text": "第一段自然译文。", "role": "crosshead"},
                    {"zh_text": "第二段自然译文。", "role": "body"},
                ],
            },
            {"title_zh": "自然中文标题", "summary_md": summary},
        ])

        article = compile_article_record(
            client,
            _config(),
            issue_date="2026-08-08",
            section="Leaders",
            title="A test article",
            url="https://example.com/article",
            body="First source paragraph.\n\nSecond source paragraph.",
            article_id="art_test_001",
            log_=logging.getLogger("test"),
            images=["images/example.jpg"],
        )

        self.assertIsNotNone(article)
        assert article is not None
        self.assertEqual(len(completions.requests), 2)
        translation_prompt = completions.requests[0]["messages"][1]["content"]  # type: ignore[index]
        summary_prompt = completions.requests[1]["messages"][1]["content"]  # type: ignore[index]
        self.assertIn("不写摘要", translation_prompt)
        self.assertIn("不是专名", translation_prompt)
        self.assertIn("只保留英文人名", translation_prompt)
        for name in ("Google", "Reddit", "Instagram", "TikTok", "Sensor Tower"):
            self.assertIn(name, translation_prompt)
            self.assertIn(name, summary_prompt)
        self.assertIn("不能写“谷歌”“红迪”“照片墙”“抖音海外版”“传感器塔”", translation_prompt)
        self.assertIn("此规则同时适用于 title_zh 和 summary_md", summary_prompt)
        self.assertNotIn('"summary_md"', translation_prompt)
        self.assertIn("这不是逐段翻译", summary_prompt)
        self.assertIn("政治和外交语境", summary_prompt)
        self.assertNotIn('"paragraphs":', summary_prompt)
        self.assertEqual(article["title_zh"], "自然中文标题")
        self.assertEqual(article["summary_md"], summary)
        self.assertEqual([item["role"] for item in article["paragraphs"]], ["body", "body"])
        self.assertTrue(article["compiled_article"])
        self.assertEqual(article["compile_status"], "complete")
        self.assertEqual(
            set(article),
            {
                "id", "issue_date", "section", "title", "title_zh", "url",
                "summary_md", "content_raw", "content_markdown", "paragraphs",
                "images", "image_insights", "compiled_article", "compile_status",
                "glossary_entries", "term_annotations", "glossary_analysis_complete",
                "glossary_version",
            },
        )

    def test_compile_task_keeps_images_without_generating_descriptions(self) -> None:
        article = {
            "id": "art_test_images",
            "image_insights": [],
        }
        payload = {
            "issue_date": "2026-08-08",
            "section": "Main",
            "title": "Image test",
            "url": "https://example.com/images",
            "body": "Source paragraph.",
            "article_id": "art_test_images",
            "images": ["images/example.jpg"],
            "image_placements": [{
                "path": "images/example.jpg",
                "placement": "lead",
                "caption": "Original caption",
                "credit": "PHOTO CREDIT",
                "alt_text": "Original alt text",
            }],
        }

        with patch("sync_wsj.make_llm_client", return_value=object()), \
                patch("sync_wsj.compile_article_record", return_value=article), \
                patch("sync_wsj.translate_image_descriptions") as translate_images:
            result = _compile_article_task(_config(), payload)

        self.assertIs(result, article)
        self.assertEqual(result["image_insights"], [{
            "path": "images/example.jpg",
            "image_type": "photo",
            "description": " ",
        }])
        self.assertEqual(result["image_placements"][0]["caption"], "Original caption")
        translate_images.assert_not_called()

    def test_translation_validation_retries_without_repeating_summary(self) -> None:
        client, completions = _client([
            {"paragraphs": []},
            {"paragraphs": [{"zh_text": "完整译文。", "role": "body"}]},
            {"title_zh": "中文标题", "summary_md": _natural_summary()},
        ])

        with patch("sync_wsj.time.sleep", return_value=None):
            article = compile_article_record(
                client,
                _config(max_retries=1),
                issue_date="2026-08-08",
                section="Leaders",
                title="Retry translation",
                url="https://example.com/retry",
                body="Only one source paragraph.",
                article_id="art_test_002",
                log_=logging.getLogger("test"),
            )

        self.assertIsNotNone(article)
        self.assertEqual(len(completions.requests), 3)
        self.assertIn("忠实翻译全文", completions.requests[0]["messages"][0]["content"])  # type: ignore[index]
        self.assertIn("忠实翻译全文", completions.requests[1]["messages"][0]["content"])  # type: ignore[index]
        self.assertIn("原创编辑稿", completions.requests[2]["messages"][0]["content"])  # type: ignore[index]

    def test_translation_falls_back_to_numbered_mapping_after_count_mismatch(self) -> None:
        client, completions = _client([
            {"paragraphs": [{"zh_text": "被合并的译文。", "role": "body"}]},
            {"translations": {"1": "第一段译文。", "2": "第二段译文。"}},
        ])

        translated = _request_article_translation(
            client,
            _config(),
            title="Fallback translation",
            section="Main",
            source_paragraphs=[
                {"role": "body", "en_text": "First source paragraph."},
                {"role": "body", "en_text": "Second source paragraph."},
            ],
            article_id="art_fallback",
            log_=logging.getLogger("test"),
        )

        self.assertEqual(len(completions.requests), 2)
        fallback_prompt = completions.requests[1]["messages"][1]["content"]  # type: ignore[index]
        self.assertIn("translations 对象", fallback_prompt)
        self.assertEqual(
            [paragraph["para_id"] for paragraph in translated],
            ["art_fallback_p1", "art_fallback_p2"],
        )
        self.assertEqual(
            [paragraph["zh_text"] for paragraph in translated],
            ["第一段译文。", "第二段译文。"],
        )

    def test_repair_missing_translations_refills_blanks_before_publish(self) -> None:
        cfg = _config()
        cfg["llm"]["api_key"] = "test-key"  # type: ignore[index]
        article = {
            "id": "art_2026-09-03_001",
            "source_id": "wsj-ereader:2026-09-03:A1:1",
            "issue_date": "2026-09-03",
            "section": "Main",
            "title": "A test article",
            "title_zh": "",
            "url": sync_wsj.EREADER_URL,
            "summary_md": "",
            "content_raw": "First source paragraph.\n\nSecond source paragraph.",
            "content_markdown": "First source paragraph.\n\nSecond source paragraph.",
            "paragraphs": [
                {
                    "para_id": "art_2026-09-03_001_p1",
                    "en_text": "First source paragraph.",
                    "zh_text": "",
                    "role": "body",
                },
                {
                    "para_id": "art_2026-09-03_001_p2",
                    "en_text": "Second source paragraph.",
                    "zh_text": "第二段旧译文。",
                    "role": "body",
                },
            ],
            "images": [],
            "image_insights": [],
            "compiled_article": False,
            "compile_status": "fallback",
            "glossary_entries": [],
            "term_annotations": [],
            "glossary_analysis_complete": False,
            "glossary_version": 0,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "database.js"
            with patch.object(sync_wsj, "DATABASE_JS", database):
                sync_wsj.write_database_js([article])
                with (
                    patch("sync_wsj.make_llm_client", return_value=object()),
                    patch("sync_wsj._request_article_translation", return_value=[
                        {
                            "para_id": "art_2026-09-03_001_p1",
                            "en_text": "First source paragraph.",
                            "zh_text": "第一段新译文。",
                            "role": "body",
                        },
                        {
                            "para_id": "art_2026-09-03_001_p2",
                            "en_text": "Second source paragraph.",
                            "zh_text": "第二段新译文。",
                            "role": "body",
                        },
                    ]) as translate,
                    patch("sync_wsj._request_article_summary", return_value=("中文标题", _natural_summary())),
                    patch("sync_wsj.enrich_article_glossary", side_effect=lambda _client, _cfg, item, _log: item),
                    patch("sync_wsj._sync_paper_outputs") as sync_outputs,
                    patch("sync_wsj._maybe_rebuild_index") as rebuild_index,
                ):
                    repaired = repair_missing_translations(cfg, "2026-09-03")
                stored = sync_wsj.read_database_js()

        self.assertEqual([item["id"] for item in repaired], ["art_2026-09-03_001"])
        self.assertEqual(stored[0]["title_zh"], "中文标题")
        self.assertEqual(stored[0]["summary_md"], _natural_summary())
        self.assertEqual([item["zh_text"] for item in stored[0]["paragraphs"]], [
            "第一段新译文。",
            "第二段新译文。",
        ])
        self.assertTrue(stored[0]["compiled_article"])
        self.assertEqual(stored[0]["compile_status"], "complete")
        translate.assert_called_once()
        self.assertTrue(sync_outputs.called)
        rebuild_index.assert_called_once()

    def test_long_translation_is_chunked_and_paragraph_ids_are_resequenced(self) -> None:
        body_parts = [f"Source paragraph {index}." for index in range(1, 18)]
        first_chunk = [
            {"zh_text": f"第{index}段译文。", "role": "body"}
            for index in range(1, 17)
        ]
        second_chunk = [{"zh_text": "第17段译文。", "role": "body"}]
        client, completions = _client([
            {"paragraphs": first_chunk},
            {"paragraphs": second_chunk},
            {"title_zh": "长文标题", "summary_md": _natural_summary()},
        ])

        article = compile_article_record(
            client,
            _config(),
            issue_date="2026-08-08",
            section="Main",
            title="A long article",
            url="https://example.com/long",
            body="\n\n".join(body_parts),
            article_id="art_test_long",
            log_=logging.getLogger("test"),
        )

        self.assertIsNotNone(article)
        assert article is not None
        self.assertEqual(len(completions.requests), 3)
        self.assertEqual(len(article["paragraphs"]), 17)
        self.assertEqual(
            [paragraph["para_id"] for paragraph in article["paragraphs"]],
            [f"art_test_long_p{index}" for index in range(1, 18)],
        )
        self.assertEqual(article["compile_status"], "complete")

    def test_summary_length_expands_with_source_size(self) -> None:
        self.assertEqual(
            _summary_length_bounds([{"en_text": "word " * 900}]),
            (420, 650),
        )
        self.assertEqual(
            _summary_length_bounds([{"en_text": "word " * 901}]),
            (520, 800),
        )
        self.assertEqual(
            _summary_length_bounds([{"en_text": "word " * 1801}]),
            (620, 1000),
        )


if __name__ == "__main__":
    unittest.main()
