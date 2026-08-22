from __future__ import annotations

import logging
import unittest

from wsj_ereader import (
    EReaderCandidate,
    EReaderImage,
    WsjEReaderAdapter,
    article_from_payload,
    deduplicate_articles,
    normalise_page_label,
    source_id_for,
)


class EReaderParserTests(unittest.TestCase):
    def test_cover_url_matches_selected_issue_and_high_resolution_layer(self) -> None:
        class Frame:
            @staticmethod
            def run_js(script: str) -> str:
                return (
                    "https://wsj-bcdn.newsmemory.com/ajax-request.php?"
                    "action=loadImage&type=graph1024&issue=20260821&crc=abc"
                )

        adapter = WsjEReaderAdapter(None, logging.getLogger("test"), timeout_s=5)
        adapter.frame = Frame()
        adapter.issue_date = "2026-08-21"
        self.assertIn("issue=20260821", adapter.first_page_cover_url())

    def test_page_labels_and_source_ids_are_stable(self) -> None:
        self.assertEqual(normalise_page_label("Page A001"), "A1")
        self.assertEqual(normalise_page_label("M004A"), "M4A")
        self.assertEqual(
            source_id_for("2026-08-21", "A001", 3),
            "wsj-ereader:2026-08-21:A1:3",
        )

    def test_article_events_preserve_crossheads_and_put_images_first(self) -> None:
        candidate = EReaderCandidate(
            issue_date="2026-08-21",
            section="Main",
            page=1,
            print_page_label="A1",
            page_article_index=1,
            xml_id=3,
            title="Example headline",
        )
        payload = {
            "title": "Example headline",
            "subtitle": "Example subtitle",
            "byline": "BY REPORTER",
            "events": [
                {"type": "paragraph", "text": "Opening fragment."},
                {"type": "page_break"},
                {"type": "paragraph", "text": "Continued fragment."},
                {"type": "crosshead", "text": "Next section"},
                {"type": "paragraph", "text": "Paragraph before image."},
                {
                    "type": "image",
                    "url": "https://example.invalid/image.jpg",
                    "caption": "Original caption",
                    "credit": "Original credit",
                    "alt_text": "Original alt text",
                },
                {"type": "paragraph", "text": "Final paragraph."},
            ],
        }

        article = article_from_payload(
            candidate,
            payload,
            page_by_label={"A1": 1},
            root_candidates={("A1", 3): candidate},
        )

        self.assertEqual(article.paragraphs[0]["text"], "Opening fragment. Continued fragment.")
        self.assertEqual(article.paragraphs[1], {"role": "crosshead", "text": "Next section"})
        self.assertEqual(article.images[0].placement, "lead")
        self.assertIsNone(article.images[0].after_paragraph_index)
        self.assertEqual(article.images[0].caption, "Original caption")

    def test_continuation_points_to_root_and_longer_variant_wins(self) -> None:
        root = EReaderCandidate(
            issue_date="2026-08-21",
            section="Main",
            page=1,
            print_page_label="A1",
            page_article_index=2,
            xml_id=3,
            title="A complete headline",
        )
        continuation = EReaderCandidate(
            issue_date="2026-08-21",
            section="From Page One",
            page=10,
            print_page_label="A10",
            page_article_index=1,
            xml_id=1,
            title="A shortened headline",
        )
        root_payload = {
            "title": root.title,
            "events": [
                {"type": "paragraph", "text": "Opening paragraph."},
                {"type": "paragraph", "text": "Continuation paragraph with more detail."},
            ],
        }
        continuation_payload = {
            "events": [{"type": "paragraph", "text": "Continuation paragraph."}],
            "jump": {
                "text": "CONTINUED FROM ON PAGE A1",
                "href": (
                    "javascript:art_getJumpId('A001','20260821',"
                    "'document.pdf.0','3');"
                ),
            },
        }
        candidates = {("A1", 3): root, ("A10", 1): continuation}
        page_by_label = {"A1": 1, "A10": 10}
        root_article = article_from_payload(
            root, root_payload, page_by_label=page_by_label, root_candidates=candidates,
        )
        continuation_article = article_from_payload(
            continuation,
            continuation_payload,
            page_by_label=page_by_label,
            root_candidates=candidates,
        )

        self.assertEqual(continuation_article.source_id, root_article.source_id)
        self.assertEqual(continuation_article.page, 1)
        self.assertEqual(continuation_article.section, "Main")
        self.assertEqual(continuation_article.source_pages, [1, 10])
        result = deduplicate_articles([continuation_article, root_article])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].title, "A complete headline")
        self.assertEqual(result[0].source_pages, [1, 10])

    def test_title_fallback_dedup_keeps_images_from_shorter_variant(self) -> None:
        root = EReaderCandidate(
            issue_date="2026-08-21",
            section="Main",
            page=1,
            print_page_label="A1",
            page_article_index=3,
            xml_id=3,
            title="Hawaii Chases The Startup Wave",
        )
        continuation = EReaderCandidate(
            issue_date="2026-08-21",
            section="From Page One",
            page=10,
            print_page_label="A10",
            page_article_index=2,
            xml_id=1,
            title=root.title,
        )
        root_article = article_from_payload(
            root,
            {
                "events": [
                    {"type": "paragraph", "text": "Long opening paragraph."},
                    {"type": "paragraph", "text": "Long continuation paragraph."},
                ]
            },
            page_by_label={"A1": 1, "A10": 10},
            root_candidates={("A1", 3): root},
        )
        continuation_article = article_from_payload(
            continuation,
            {"events": [{"type": "paragraph", "text": "Short continuation."}]},
            page_by_label={"A1": 1, "A10": 10},
            root_candidates={("A1", 3): root},
        )
        continuation_article.images.append(
            EReaderImage(
                url="https://example.invalid/hawaii.jpg",
                placement="after_paragraph",
                after_paragraph_index=1,
                caption="Visitors at Waikiki Beach.",
                credit="Photographer for WSJ",
                alt_text="",
            )
        )

        result = deduplicate_articles([root_article, continuation_article])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source_pages, [1, 10])
        self.assertEqual(len(result[0].images), 1)
        self.assertEqual(result[0].images[0].url, "https://example.invalid/hawaii.jpg")
        self.assertEqual(result[0].images[0].placement, "lead")
        self.assertEqual(result[0].images[0].caption, "Visitors at Waikiki Beach.")


if __name__ == "__main__":
    unittest.main()
