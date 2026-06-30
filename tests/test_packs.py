from __future__ import annotations

import unittest
from pathlib import Path

from review_gatekeeper.packs import chunk_markdown, discover_packs


class StandardPackTests(unittest.TestCase):
    def test_bundled_packs_are_versioned_and_have_authoritative_sources(self) -> None:
        root = Path(__file__).resolve().parents[1]
        packs = discover_packs(root / "standard-packs")

        self.assertEqual({item.id for item in packs}, {"python", "fastapi", "typescript", "node"})
        for pack in packs:
            self.assertEqual(pack.version, "1.0.0")
            self.assertTrue(pack.content_checksum)
            self.assertTrue(pack.manifest["sources"])
            self.assertTrue(
                all(source["url"].startswith("https://") for source in pack.manifest["sources"])
            )

    def test_markdown_is_chunked_by_review_topic(self) -> None:
        chunks = chunk_markdown("# Title\nIntro\n\n## Contracts\nRules\n\n## Tests\nEvidence")

        self.assertEqual(
            chunks,
            [("Title", "Intro"), ("Contracts", "Rules"), ("Tests", "Evidence")],
        )


if __name__ == "__main__":
    unittest.main()
