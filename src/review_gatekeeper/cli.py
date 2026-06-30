from __future__ import annotations

import json
import sys
from pathlib import Path

from .database import project_root
from .models import ReviewRequest
from .repository import BundledPackRepository
from .service import ReviewService


def build_service() -> ReviewService:
    root = project_root()
    repository = BundledPackRepository(
        packs_root=root / "standard-packs",
        profiles_root=root / "profiles",
    )
    return ReviewService(repository)


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("Usage: review-gate <review_request.json>", file=sys.stderr)
        return 2

    payload = json.loads(Path(argv[0]).read_text())
    request = ReviewRequest.from_dict(payload)
    result = build_service().review(request)
    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
