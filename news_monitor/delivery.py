"""Telegram transport and restart-safe delivery bookkeeping; standard library only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class DeliveryError(RuntimeError):
    pass


def recipients(environ=None):
    env = os.environ if environ is None else environ
    # Match the original monitor: the plural setting takes precedence.
    raw = env.get("TELEGRAM_CHAT_IDS", "").strip() or env.get("TELEGRAM_CHAT_ID", "").strip()
    chats = list(dict.fromkeys(x for x in re.split(r"[,;\s]+", raw) if x))
    if not chats:
        raise DeliveryError("Telegram recipient secret is missing")
    return chats


def send_message(token, chat_id, message, *, opener=urlopen, sleep=time.sleep):
    if not token:
        raise DeliveryError("Telegram bot token secret is missing")
    # Never include the request URL, response body, token, or recipient in errors.
    body = urlencode({"chat_id": chat_id, "text": message,
                      "link_preview_options": json.dumps({"is_disabled": True}),
                      "disable_notification": "false"}).encode()
    request = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    for attempt in range(3):
        try:
            with opener(request, timeout=25) as response:
                result = json.loads(response.read(128_000))
            if not result.get("ok"):
                raise DeliveryError("Telegram rejected the message")
            return result["result"]["message_id"]
        except HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                try:
                    delay = int(json.loads(exc.read(16_000)).get("parameters", {}).get("retry_after", 3))
                except (ValueError, TypeError, KeyError):
                    delay = 3
                if delay > 50:
                    raise DeliveryError("Telegram rate limit exceeds retry window") from None
                sleep(max(1, delay))
                continue
            raise DeliveryError(f"Telegram request failed (HTTP {exc.code})") from None
        except (URLError, TimeoutError, OSError, ValueError, KeyError):
            # Delivery can be ambiguous after a network timeout. Do not blindly resend.
            raise DeliveryError("Telegram response was unavailable or invalid") from None
    raise DeliveryError("Telegram delivery failed")


def load_state(path):
    file = Path(path)
    if not file.exists():
        return {"version": 1, "deliveries": {}}
    state = json.loads(file.read_text(encoding="utf-8"))
    if state.get("version") != 1 or not isinstance(state.get("deliveries"), dict):
        raise DeliveryError("Invalid delivery ledger; refusing to send without deduplication")
    return state


def save_state(path, state):
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(file)


def recipient_key(chat):
    # The state branch is public: never persist a raw chat ID or username.
    # Salted hashes use the private bot token; an observer cannot enumerate IDs.
    import hmac
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    return hmac.new(token.encode(), str(chat).encode(), hashlib.sha256).hexdigest()[:24]


def deliver(messages, *, delivery_key, state_path, force=False):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise DeliveryError("Telegram bot token secret is missing")
    chats = recipients()
    state = load_state(state_path)
    record = state["deliveries"].setdefault(delivery_key, {"recipients": {}, "complete": False})
    # Freeze the initial complete digest so a retry cannot change chunk boundaries
    # and send different stories under already-recorded chunk numbers.
    frozen = record.setdefault("messages", messages)
    delivered = skipped = 0
    errors = []
    for chat in chats:
        key = recipient_key(chat)
        sent = record["recipients"].setdefault(key, [])
        if force:
            sent.clear()
        for index, message in enumerate(frozen):
            if index in sent:
                skipped += 1
                continue
            try:
                if delivered:
                    time.sleep(1.1)
                send_message(token, chat, message)
                sent.append(index)
                save_state(state_path, state)
                delivered += 1
            except DeliveryError as exc:
                errors.append(str(exc))
                break
    record["complete"] = all(len(record["recipients"].get(recipient_key(chat), [])) == len(frozen)
                             for chat in chats)
    save_state(state_path, state)
    if errors:
        raise DeliveryError(f"Delivery incomplete for {len(errors)} recipient(s): {errors[0]}")
    return {"sent_messages": delivered, "skipped_messages": skipped, "recipients": len(chats)}
