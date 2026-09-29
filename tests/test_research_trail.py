import tempfile
import unittest
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from research_trail import Store, home_page, paper_link, rank_papers, validate_paper


def paper(**overrides):
    values = {
        "title": "Transparent ranking for literature review",
        "source_text": "A study of transparent keyword ranking.",
        "notes": "Compare this with the baseline.",
        "doi": "10.1000/example",
        "url": "",
        "source": "Journal",
        "access_level": "open",
        "license": "CC BY",
    }
    values.update(overrides)
    return values


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "app.db"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_question_papers_and_feedback_survive_reopen(self):
        self.store.save_question("How does transparent ranking support review?")
        paper_id = self.store.add_paper(paper())
        self.store.save_feedback(paper_id, "useful")
        self.store.close()

        self.store = Store(self.path)
        saved = self.store.paper(paper_id)
        self.assertEqual(self.store.question(), "How does transparent ranking support review?")
        self.assertEqual(saved["title"], paper()["title"])
        self.assertEqual(saved["feedback"], "useful")

    def test_blank_metadata_is_stored_as_explicit_unknown(self):
        paper_id = self.store.add_paper(paper(source="", access_level=" ", license=""))
        saved = self.store.paper(paper_id)
        self.assertEqual((saved["source"], saved["access_level"], saved["license"]), ("unknown",) * 3)

    def test_source_text_and_personal_notes_are_stored_separately(self):
        paper_id = self.store.add_paper(paper(source_text="Exact paper excerpt.", notes="My interpretation."))
        saved = self.store.paper(paper_id)
        self.assertEqual(saved["source_text"], "Exact paper excerpt.")
        self.assertEqual(saved["notes"], "My interpretation.")

    def test_legacy_combined_text_is_migrated_to_source_text(self):
        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.execute("DROP TABLE papers")
        connection.execute(
            """CREATE TABLE papers (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, abstract_or_notes TEXT NOT NULL,
                doi TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'unknown', access_level TEXT NOT NULL DEFAULT 'unknown',
                license TEXT NOT NULL DEFAULT 'unknown', feedback TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO papers VALUES (1, 'Legacy', 'Original abstract', '10.1000/legacy', '', 'Journal', 'open', 'CC BY', 'useful', '2026-01-01')"
        )
        connection.commit()
        connection.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.paper(1)["source_text"], "Original abstract")
        self.assertEqual(self.store.paper(1)["notes"], "")
        self.assertEqual(self.store.paper(1)["source_text_reviewed"], 0)
        self.assertEqual(rank_papers("Original abstract", self.store.papers())[0].score, 0)
        self.assertIn("Source text needs review", home_page(self.store))

        self.store.update_paper(1, paper(title="Legacy", source_text="Original abstract"))
        self.assertEqual(self.store.paper(1)["source_text_reviewed"], 1)
        self.assertEqual(rank_papers("Original abstract", self.store.papers())[0].score, 2)

    def test_shared_connection_serializes_concurrent_requests(self):
        def add(index):
            paper_id = self.store.add_paper(paper(title=f"Paper {index}", doi=f"10.1000/{index}"))
            self.store.save_feedback(paper_id, "useful")
            return paper_id

        with ThreadPoolExecutor(max_workers=8) as pool:
            paper_ids = list(pool.map(add, range(40)))

        self.assertEqual(len(set(paper_ids)), 40)
        self.assertEqual(len(self.store.papers()), 40)
        self.assertTrue(all(row["feedback"] == "useful" for row in self.store.papers()))

    def test_papers_can_be_added_one_at_a_time_and_edited_or_removed(self):
        first = self.store.add_paper(paper(title="First"))
        self.assertEqual(len(self.store.papers()), 1)
        self.store.update_paper(first, paper(title="Revised"))
        self.assertEqual(self.store.paper(first)["title"], "Revised")
        self.store.delete_paper(first)
        self.assertEqual(self.store.papers(), [])

    def test_feedback_can_be_changed_and_cleared(self):
        paper_id = self.store.add_paper(paper())
        for value in ("useful", "not useful", ""):
            self.store.save_feedback(paper_id, value)
            self.assertEqual(self.store.paper(paper_id)["feedback"], value)
        with self.assertRaises(ValueError):
            self.store.save_feedback(paper_id, "maybe")


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_title_matches_outweigh_source_text_and_reasons_name_exact_fields(self):
        source_id = self.store.add_paper(paper(title="Unrelated", source_text="Networks networks", doi="10.1/source"))
        title_id = self.store.add_paper(paper(title="Networks", source_text="Unrelated", doi="10.1/title"))
        ranked = rank_papers("Networks", self.store.papers())
        self.assertEqual([item.paper["id"] for item in ranked], [title_id, source_id])
        self.assertEqual(ranked[0].score, 2)
        self.assertEqual(ranked[0].matches_by_field, {"Title": ("networks",), "Source text": ()})
        self.assertEqual(ranked[0].excerpts_by_field["Title"], "Networks")

    def test_personal_notes_do_not_affect_ranking(self):
        self.store.add_paper(paper(title="Unrelated", source_text="No overlap", notes="networks networks"))
        ranked = rank_papers("Networks", self.store.papers())
        self.assertEqual(ranked[0].score, 0)
        self.assertEqual(ranked[0].matched_terms, ())

    def test_source_excerpt_is_an_exact_short_slice_around_match(self):
        source = "A long introduction before the exact NETWORK term and a conclusion after it."
        self.store.add_paper(paper(title="Unrelated", source_text=source))
        ranked = rank_papers("network", self.store.papers())
        excerpt = ranked[0].excerpts_by_field["Source text"]
        self.assertIn("NETWORK", excerpt)
        self.assertIn(excerpt.strip("…"), source)

    def test_ranking_ties_are_broken_by_creation_id(self):
        first = self.store.add_paper(paper(title="Methods", doi="10.1/first"))
        second = self.store.add_paper(paper(title="Methods", doi="10.1/second"))
        ranked = rank_papers("Methods", self.store.papers())
        self.assertEqual([item.paper["id"] for item in ranked], [first, second])

    def test_stop_words_do_not_affect_score(self):
        self.store.add_paper(paper(title="The method", source_text="A review"))
        ranked = rank_papers("What is the method in a review?", self.store.papers())
        self.assertEqual(ranked[0].matched_terms, ("method", "review"))
        self.assertEqual(ranked[0].score, 3)


class ValidationTests(unittest.TestCase):
    def test_requires_text_and_a_link(self):
        errors = validate_paper(paper(title="", source_text="", doi="", url=""))
        self.assertEqual(errors, ["Title is required.", "Abstract or source excerpt is required.", "Enter a DOI or URL."])

    def test_rejects_invalid_urls_and_dois(self):
        self.assertIn("URL must start", validate_paper(paper(doi="", url="file:///paper"))[0])
        self.assertIn("DOI must look", validate_paper(paper(doi="not-a-doi"))[0])

    def test_constructs_doi_resolver_and_url_fallback(self):
        self.assertEqual(paper_link("doi: 10.1000/example", ""), "https://doi.org/10.1000/example")
        self.assertEqual(paper_link("https://doi.org/10.1000/example", ""), "https://doi.org/10.1000/example")
        self.assertEqual(paper_link("", "https://example.test/paper"), "https://example.test/paper")


if __name__ == "__main__":
    unittest.main()
