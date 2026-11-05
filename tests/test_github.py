from __future__ import annotations

import base64
import unittest
from pathlib import Path

from review_gatekeeper.config import load_repository_config
from review_gatekeeper.github import GitHubSettings, verify_webhook_signature
from review_gatekeeper.github.processor import _decode_github_content, _select_stack
from review_gatekeeper.github.publish import _escape_markdown, _terminal_error_code
from review_gatekeeper.models import ChangedFile


class GitHubIntegrationTests(unittest.TestCase):
    def test_github_settings_rejects_non_http_api_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "GITHUB_API_URL must be an http"):
            GitHubSettings(
                app_id="1",
                private_key_path=Path("github-app.pem"),
                webhook_secret="secret",
                api_url="file:///tmp/github",
            )

    def test_webhook_signature_matches_github_test_vector(self) -> None:
        self.assertTrue(
            verify_webhook_signature(
                b"Hello, World!",
                "It's a Secret to Everybody",
                "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17",
            )
        )
        self.assertFalse(
            verify_webhook_signature(b"tampered", "secret", "sha256=not-valid")
        )

    def test_github_base64_content_accepts_wrapped_lines(self) -> None:
        encoded = base64.b64encode(b"version: 1\n").decode()
        wrapped = encoded[:5] + "\n" + encoded[5:]
        self.assertEqual(
            _decode_github_content({"encoding": "base64", "content": wrapped}),
            "version: 1\n",
        )

    def test_stack_is_selected_from_changed_paths(self) -> None:
        config = load_repository_config(
            """
version: 1
knowledge: {}
stacks:
  - paths: [backend/**]
    language: python
    framework: fastapi
    packs: [python@1.0.0, fastapi@1.0.0]
  - paths: [frontend/**]
    language: typescript
    framework: node
    packs: [typescript@1.0.0, node@1.0.0]
"""
        )
        language, framework, packs = _select_stack(
            config,
            [
                ChangedFile("backend/app.py", additions=80),
                ChangedFile("frontend/app.ts", additions=5),
            ],
        )
        self.assertEqual((language, framework), ("python", "fastapi"))
        self.assertEqual(
            packs,
            ["python@1.0.0", "fastapi@1.0.0", "typescript@1.0.0", "node@1.0.0"],
        )

    def test_terminal_error_types_have_stable_public_codes(self) -> None:
        for error_type, code in (
            ("GitHubAPIError", "github_api_error"),
            ("OperationalError", "database_error"),
            ("UniqueViolation", "database_error"),
            ("PoolTimeout", "database_error"),
            ("InsightResponseError", "model_error"),
            ("HTTPResponseError", "embedding_error"),
            ("ValueError", "internal_error"),
            ("unknown", "internal_error"),
        ):
            with self.subTest(error_type=error_type):
                self.assertEqual(
                    _terminal_error_code(f"{error_type}: diagnostic"), code
                )

    def test_markdown_preserves_plain_text(self) -> None:
        for original, escaped in (
            ("plain text", "plain text"),
            ("[label](url)", r"\[label\]\(url\)"),
            ("![image](url)", r"\!\[image\]\(url\)"),
            ("<img>", "&lt;img&gt;"),
            ("a\nb\rc", "a b c"),
            ("`code` *bold*", r"\`code\` \*bold\*"),
            (r"\[label]", r"\\\[label\]"),
        ):
            with self.subTest(original=original):
                self.assertEqual(_escape_markdown(original), escaped)


if __name__ == "__main__":
    unittest.main()
