"""Branch coverage for retry policy; real provider flows live in tests_e2e."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import patch

import httpx

from review_gatekeeper.http import HTTPClient, HTTPResponseError, retry_after_seconds


class HTTPRetryTests(unittest.TestCase):
    def request(self, method="GET", **kwargs):
        return HTTPClient().request(
            method, "https://example.test", headers={}, timeout=3, **kwargs
        )

    def response(self, status, retry_after=None):
        return httpx.Response(
            status,
            headers={"Retry-After": retry_after} if retry_after is not None else {},
            request=httpx.Request("GET", "https://example.test"),
        )

    @patch.dict("os.environ", {"HTTP_MAX_RETRIES": "2"})
    def test_retryable_statuses_and_idempotent_methods(self):
        for method in ("GET", "HEAD", "PATCH", "POST"):
            for status in (429, 500, 502, 503, 504):
                with (
                    self.subTest(method=method, status=status),
                    patch(
                        "httpx.Client.request",
                        side_effect=[self.response(status), self.response(200)],
                    ) as send,
                    patch("review_gatekeeper.http.time.sleep") as sleep,
                ):
                    self.request(method, retry_post=method == "POST")
                    self.assertEqual(send.call_count, 2)
                    self.assertEqual(send.call_args.kwargs["timeout"], 3)
                    self.assertEqual(sleep.call_count, 1)

    @patch.dict("os.environ", {"HTTP_MAX_RETRIES": "2"})
    def test_transport_errors_retry_and_propagate_when_exhausted(self):
        for error in (httpx.ConnectError, httpx.ReadTimeout):
            with (
                self.subTest(error=error),
                patch("httpx.Client.request", side_effect=error("unavailable")) as send,
                patch("review_gatekeeper.http.time.sleep") as sleep,
            ):
                with self.assertRaises(error):
                    self.request()
                self.assertEqual(send.call_count, 3)
                self.assertEqual(sleep.call_count, 2)
                self.assertLess(
                    sleep.call_args_list[0].args[0], sleep.call_args_list[1].args[0]
                )

    def test_non_retryable_requests_and_responses(self):
        for method, status in (
            ("POST", 502),
            ("PUT", 502),
            ("GET", 302),
            ("GET", 401),
            ("GET", 501),
        ):
            with (
                self.subTest(method=method, status=status),
                patch(
                    "httpx.Client.request", return_value=self.response(status)
                ) as send,
                patch("review_gatekeeper.http.time.sleep") as sleep,
            ):
                with self.assertRaises(httpx.HTTPStatusError):
                    self.request(method)
                self.assertEqual(send.call_count, 1)
                sleep.assert_not_called()

    def test_retry_after_short_wait_and_long_delay(self):
        with (
            patch(
                "httpx.Client.request",
                side_effect=[self.response(429, "3"), self.response(200)],
            ),
            patch("review_gatekeeper.http.time.sleep") as sleep,
        ):
            self.request()
            sleep.assert_called_once_with(3)
        with (
            patch(
                "httpx.Client.request", return_value=self.response(429, "600")
            ) as send,
            patch("review_gatekeeper.http.time.sleep") as sleep,
        ):
            with self.assertRaises(httpx.HTTPStatusError):
                self.request()
            self.assertEqual(send.call_count, 1)
            sleep.assert_not_called()
        with patch("httpx.Client.request", return_value=self.response(429, "600")):
            with self.assertRaises(HTTPResponseError) as raised:
                self.request("POST", retry_post=True)
            self.assertEqual(raised.exception.retry_after_seconds, 600)

    def test_retry_after_dates_and_invalid_values(self):
        now = datetime.now(UTC)
        for value, expected in (("bad", None), ("-1", None), ("", None), ("7", 7)):
            self.assertEqual(retry_after_seconds(self.response(429, value)), expected)
        delay = retry_after_seconds(
            self.response(429, format_datetime(now + timedelta(seconds=60)))
        )
        self.assertIn(delay, (59, 60))
        self.assertEqual(
            retry_after_seconds(
                self.response(429, format_datetime(now - timedelta(seconds=60)))
            ),
            0,
        )

    @patch.dict("os.environ", {"HTTP_MAX_RETRIES": "0"})
    def test_zero_retries(self):
        with patch("httpx.Client.request", return_value=self.response(503)) as send:
            with self.assertRaises(httpx.HTTPStatusError):
                self.request()
            self.assertEqual(send.call_count, 1)
