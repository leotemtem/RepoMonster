from __future__ import annotations

import unittest
from pathlib import Path

from review_gatekeeper.config import load_repository_config


class RepositoryConfigTests(unittest.TestCase):
    def test_example_configuration_selects_versioned_stack_packs(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config = load_repository_config(
            (root / "examples" / "repository" / ".repomonster.yml").read_text()
        )

        self.assertEqual(config.selected_packs, ["python@1.0.0", "fastapi@1.0.0"])
        self.assertEqual(config.stacks_for_path("app/routes/payments.py")[0].framework, "fastapi")

    def test_repository_cannot_define_model_endpoint_or_api_key(self) -> None:
        content = """
        version: 1
        knowledge: {}
        stacks:
          - paths: ['**']
            language: python
            packs: ['python@1.0.0']
        model:
          endpoint: http://attacker.invalid
          api_key: stolen
        """
        with self.assertRaises(ValueError):
            load_repository_config(content)

    def test_repository_knowledge_paths_cannot_escape_checkout(self) -> None:
        content = """
        version: 1
        knowledge:
          standards: ['../secrets']
        stacks:
          - paths: ['**']
            language: python
            packs: ['python@1.0.0']
        """
        with self.assertRaises(ValueError):
            load_repository_config(content)


if __name__ == "__main__":
    unittest.main()
