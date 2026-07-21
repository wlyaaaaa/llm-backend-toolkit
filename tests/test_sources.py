import tempfile
import unittest
from pathlib import Path

from llm_backend_toolkit.sources import SourceLoader


class SourceLoaderTests(unittest.TestCase):
    def test_selects_relevant_chunks_without_returning_the_whole_file(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "notes.md"
            source.write_text(
                "# General\nalpha filler\n" + "unrelated text\n" * 40
                + "# Billing\nArrearage means the cloud account cannot call the model.\n"
                + "Keep fallback decisions with the top model.\n"
                + "tail filler\n" * 40,
                encoding="utf-8",
            )
            loader = SourceLoader(chunk_chars=300)

            result = loader.load(
                [{"id": "design", "path": str(source), "top_k": 2}],
                query="How should Arrearage billing fallback be decided by the top model?",
            )

            self.assertIn("Arrearage", result.inputs[0]["excerpt"])
            self.assertLess(sum(len(item["excerpt"]) for item in result.inputs), len(source.read_text(encoding="utf-8")))
            self.assertEqual("design", result.receipt[0]["id"])
            self.assertTrue(result.receipt[0]["sha256"])
            self.assertTrue(result.receipt[0]["selected_ranges"])

    def test_chinese_query_uses_character_ngrams_for_relevance(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "cn.txt"
            source.write_text(
                "普通说明\n" * 30
                + "上下文压缩必须保留用户目标和安全边界。\n"
                + "普通结尾\n" * 30,
                encoding="utf-8",
            )

            result = SourceLoader(chunk_chars=180).load(
                [{"id": "cn", "path": str(source), "top_k": 1}],
                query="上下文压缩保留什么边界",
            )

            self.assertIn("上下文压缩", result.inputs[0]["excerpt"])

    def test_duplicate_source_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "a.txt"
            source.write_text("x", encoding="utf-8")
            loader = SourceLoader()

            with self.assertRaises(ValueError):
                loader.load(
                    [{"id": "same", "path": str(source)}, {"id": "same", "path": str(source)}],
                    query="x",
                )


if __name__ == "__main__":
    unittest.main()
