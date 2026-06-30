from __future__ import annotations

import unittest

from review_gatekeeper.providers import normalize_github_event, normalize_gitlab_event


class ProviderNormalizationTests(unittest.TestCase):
    def test_github_repository_key_uses_immutable_repository_id(self) -> None:
        event = normalize_github_event(
            "pull_request",
            {
                "installation": {"id": 22},
                "repository": {
                    "id": 123,
                    "full_name": "acme/api",
                    "html_url": "https://github.example/acme/api",
                },
                "pull_request": {
                    "number": 7,
                    "title": "Change",
                    "head": {"sha": "abc", "ref": "feature"},
                    "base": {"ref": "main"},
                },
            },
        )

        self.assertEqual(event.repository_key, "github:https://github.example:123")
        self.assertEqual(event.installation_id, "22")
        self.assertEqual(event.head_sha, "abc")

    def test_gitlab_repository_key_uses_instance_and_project_id(self) -> None:
        event = normalize_gitlab_event(
            "Merge Request Hook",
            {
                "project": {
                    "id": 456,
                    "path_with_namespace": "acme/api",
                    "web_url": "https://gitlab.example/acme/api",
                },
                "object_attributes": {
                    "iid": 8,
                    "title": "Change",
                    "last_commit": {"id": "def"},
                },
            },
        )

        self.assertEqual(event.repository_key, "gitlab:https://gitlab.example:456")
        self.assertEqual(event.head_sha, "def")


if __name__ == "__main__":
    unittest.main()
