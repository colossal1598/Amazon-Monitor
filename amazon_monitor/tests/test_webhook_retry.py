"""_post_wa bounded-retry behavior (production 2026-07-15: wa-server 500s lost alerts)."""

import unittest
from unittest import mock

import webhook_sender


def _resp(status: int) -> mock.Mock:
    response = mock.Mock()
    if status >= 400:
        response.raise_for_status.side_effect = Exception(f"{status} Server Error")
    else:
        response.raise_for_status.return_value = None
    return response


class TestPostWaRetries(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "wa_api_url": "http://localhost:3001/send",
            "wa_send_attempts": 3,
            "wa_send_retry_backoff_seconds": 0.5,
        }
        self.payload = {"to": "x@c.us", "message": "hi"}

    def test_succeeds_on_retry_after_transient_500(self) -> None:
        with mock.patch.object(webhook_sender.time, "sleep") as sleep, mock.patch.object(
            webhook_sender.requests, "post", side_effect=[_resp(500), _resp(200)]
        ) as post:
            self.assertTrue(webhook_sender._post_wa(self.config, self.payload))
        self.assertEqual(post.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_gives_up_after_max_attempts(self) -> None:
        with mock.patch.object(webhook_sender.time, "sleep"), mock.patch.object(
            webhook_sender.requests, "post", side_effect=[_resp(500)] * 3
        ) as post:
            self.assertFalse(webhook_sender._post_wa(self.config, self.payload))
        self.assertEqual(post.call_count, 3)

    def test_single_attempt_when_configured(self) -> None:
        self.config["wa_send_attempts"] = 1
        with mock.patch.object(webhook_sender.time, "sleep") as sleep, mock.patch.object(
            webhook_sender.requests, "post", side_effect=[_resp(500)]
        ) as post:
            self.assertFalse(webhook_sender._post_wa(self.config, self.payload))
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_no_url_returns_false_without_posting(self) -> None:
        with mock.patch.object(webhook_sender.requests, "post") as post:
            self.assertFalse(webhook_sender._post_wa({}, self.payload))
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
