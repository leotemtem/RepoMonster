"""Bounded HTTP retries shared by provider adapters."""

from __future__ import annotations

import math
import os
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

RETRY_STATUSES = {429, 500, 502, 503, 504}
BACKOFF_LIMIT_SECONDS = 5.0


def retry_after_seconds(response: httpx.Response) -> int | None:
    value = response.headers.get("Retry-After", "").strip()
    if value.isdigit():
        return int(value)
    try:
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return max(0, math.ceil((deadline - datetime.now(UTC)).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return None


class HTTPResponseError(httpx.HTTPStatusError):
    """Keep server retry timing available to the durable job queue."""

    def __init__(self, cause: httpx.HTTPStatusError) -> None:
        super().__init__(str(cause), request=cause.request, response=cause.response)
        self.retry_after_seconds = retry_after_seconds(cause.response)


class HTTPClient:
    def __init__(self) -> None:
        self.max_retries = int(os.getenv("HTTP_MAX_RETRIES", "2"))
        if self.max_retries < 0:
            raise ValueError("HTTP_MAX_RETRIES must be non-negative")

    def request(
        self,
        method: str,
        url: str,
        *,
        timeout: float,
        headers: dict[str, str],
        payload: Any = None,
        retry_post: bool = False,
    ) -> httpx.Response:
        method = method.upper()
        retries = (
            self.max_retries
            if method in {"GET", "HEAD", "PATCH"} or (method == "POST" and retry_post)
            else 0
        )
        with httpx.Client(follow_redirects=False) as client:
            for attempt in range(retries + 1):
                delay = min(BACKOFF_LIMIT_SECONDS, 0.5 * 2 ** min(attempt, 10))
                # Retry jitter does not need cryptographic randomness.
                delay = random.uniform(delay / 2, delay)  # nosec B311
                try:
                    response = client.request(
                        method, url, headers=headers, json=payload, timeout=timeout
                    )
                except (httpx.NetworkError, httpx.TimeoutException):
                    if attempt == retries:
                        raise
                else:
                    retry_after = retry_after_seconds(response)
                    if (
                        response.status_code not in RETRY_STATUSES
                        or attempt == retries
                        or (
                            retry_after is not None
                            and retry_after > BACKOFF_LIMIT_SECONDS
                        )
                    ):
                        try:
                            response.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            raise HTTPResponseError(exc) from exc
                        return response
                    delay = max(delay, retry_after or 0)
                time.sleep(delay)
        raise AssertionError("unreachable")
