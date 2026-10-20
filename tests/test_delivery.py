import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

from news_monitor import delivery


class Response:
    def __init__(self, value):
        self.body = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return self.body


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.json"
        self.env = patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "123456:private-token",
            "TELEGRAM_CHAT_IDS": "111,222",
            "TELEGRAM_CHAT_ID": "999",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_plural_recipient_setting_takes_precedence_and_deduplicates(self):
        self.assertEqual(delivery.recipients({"TELEGRAM_CHAT_IDS": "11,22;11\n33", "TELEGRAM_CHAT_ID": "99"}),
                         ["11", "22", "33"])
        self.assertEqual(delivery.recipients({"TELEGRAM_CHAT_IDS": " ", "TELEGRAM_CHAT_ID": "99"}), ["99"])
        with self.assertRaises(delivery.DeliveryError):
            delivery.recipients({})

    def test_message_uses_no_paid_broadcast_and_no_markup_interpretation(self):
        requests = []

        def opener(request, **kwargs):
            requests.append(request)
            return Response({"ok": True, "result": {"message_id": 9}})

        self.assertEqual(delivery.send_message("secret", "111", "<news> & link", opener=opener), 9)
        fields = parse_qs(requests[0].data.decode())
        self.assertEqual(fields["text"], ["<news> & link"])
        self.assertNotIn("allow_paid_broadcast", fields)
        self.assertNotIn("parse_mode", fields)

    def test_rate_limit_retries_after_server_delay(self):
        limited = HTTPError("https://api.telegram.org/botSECRET/sendMessage", 429, "limited", {},
                            io.BytesIO(b'{"parameters":{"retry_after":2}}'))
        responses = [limited, Response({"ok": True, "result": {"message_id": 7}})]

        def opener(*args, **kwargs):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(delivery.time, "sleep") as sleeper:
            self.assertEqual(delivery.send_message("secret", "111", "hello", opener=opener, sleep=sleeper), 7)
            sleeper.assert_called_once_with(2)

    def test_ambiguous_network_failure_is_not_immediately_retried_or_exposed(self):
        with patch.object(delivery, "urlopen"):
            calls = []

            def opener(*args, **kwargs):
                calls.append(1)
                raise URLError("https://api.telegram.org/botPRIVATE-TOKEN/sendMessage recipient111")

            with self.assertRaises(delivery.DeliveryError) as caught:
                delivery.send_message("PRIVATE-TOKEN", "111", "hello", opener=opener)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("PRIVATE-TOKEN", str(caught.exception))
        self.assertNotIn("recipient111", str(caught.exception))

    def test_http_error_does_not_expose_request_url_or_body(self):
        def opener(*args, **kwargs):
            raise HTTPError("https://api.telegram.org/botSECRET/sendMessage", 403, "chat 111", {},
                            io.BytesIO(b"PRIVATE-ERROR-BODY"))

        with self.assertRaises(delivery.DeliveryError) as caught:
            delivery.send_message("SECRET", "111", "hello", opener=opener)
        self.assertEqual(str(caught.exception), "Telegram request failed (HTTP 403)")

    def test_restart_only_sends_unacknowledged_chunks_and_freezes_content(self):
        first = [("111", "a"), ("111", "b"), ("222", "a"), ("222", "b")]

        def fail_after_first(token, chat, message):
            expected = first.pop(0)
            self.assertEqual((chat, message), expected)
            if message == "b":
                raise delivery.DeliveryError("safe failure")
            return 8

        with patch.object(delivery, "send_message", side_effect=fail_after_first), patch.object(delivery.time, "sleep"):
            with self.assertRaises(delivery.DeliveryError):
                delivery.deliver(["a", "b"], delivery_key="daily:2026-10-19", state_path=self.path)
        calls = []
        with patch.object(delivery, "send_message", side_effect=lambda token, chat, message: calls.append((chat, message)) or 9), patch.object(delivery.time, "sleep"):
            result = delivery.deliver(["changed", "content", "new"], delivery_key="daily:2026-10-19", state_path=self.path)
        self.assertEqual(calls, [("111", "b"), ("222", "b")])
        self.assertEqual(result["sent_messages"], 2)
        self.assertTrue(delivery.load_state(self.path)["deliveries"]["daily:2026-10-19"]["complete"])

    def test_completed_digest_can_catch_up_new_recipient_without_resending_old(self):
        with patch.object(delivery, "send_message", return_value=1), patch.object(delivery.time, "sleep"):
            delivery.deliver(["news"], delivery_key="daily:2026-10-19", state_path=self.path)
        with patch.dict(os.environ, {"TELEGRAM_CHAT_IDS": "111,222,333"}), patch.object(delivery, "send_message", return_value=1) as send, patch.object(delivery.time, "sleep"):
            result = delivery.deliver(["news"], delivery_key="daily:2026-10-19", state_path=self.path)
        self.assertEqual(send.call_args.args[1], "333")
        self.assertEqual(result["sent_messages"], 1)
        self.assertEqual(result["skipped_messages"], 2)

    def test_public_ledger_never_contains_raw_recipient_or_token(self):
        with patch.dict(os.environ, {"TELEGRAM_CHAT_IDS": "8675309123456789,-1001234567890123"}), patch.object(delivery, "send_message", return_value=1), patch.object(delivery.time, "sleep"):
            delivery.deliver(["public news"], delivery_key="daily:2026-10-19", state_path=self.path)
        saved = self.path.read_text(encoding="utf-8")
        self.assertNotIn("8675309123456789", saved)
        self.assertNotIn("-1001234567890123", saved)
        self.assertNotIn("123456:private-token", saved)

    def test_invalid_ledger_fails_before_any_send(self):
        self.path.write_text('{"version":0,"deliveries":{}}', encoding="utf-8")
        with patch.object(delivery, "send_message") as send:
            with self.assertRaises(delivery.DeliveryError):
                delivery.deliver(["news"], delivery_key="daily:2026-10-19", state_path=self.path)
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
