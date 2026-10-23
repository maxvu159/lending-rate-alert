"""Send messages to Telegram with the Bot API, one message per alert with its own image.

Message format used by the bot: plain text with **bold**, and an optional last line `MEDIA:<png path>`.
Env: TELEGRAM_BOT_TOKEN (required to send), TELEGRAM_CHAT_ID, TELEGRAM_THREAD_ID (forum topic, optional);
chat and thread can also be set in config.json under "telegram".
Without a token, or when sending fails, the message is printed to stdout so cron mail or logs still get it.
"""
import html
import json
import os
import re
import sys
import uuid
import urllib.request
from pathlib import Path

import core

API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_MAX = 1024


def settings():
    t = core.config().get("telegram", {})
    return (os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID") or str(t.get("chat_id") or ""),
            os.environ.get("TELEGRAM_THREAD_ID") or str(t.get("thread_id") or ""))


def to_html(text):
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(text, quote=False))


def split_media(msg):
    lines = msg.rstrip().split("\n")
    if lines and lines[-1].startswith("MEDIA:"):
        return "\n".join(lines[:-1]), lines[-1][6:].strip()
    return msg, None


def _post(token, method, fields, file=None):
    url = API.format(token=token, method=method)
    if file is None:
        req = urllib.request.Request(url, data=json.dumps(fields).encode(),
                                     headers={"content-type": "application/json"})
    else:
        b = uuid.uuid4().hex
        body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                        for k, v in fields.items())
        body += (f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="{Path(file).name}"\r\n'
                 f"Content-Type: image/png\r\n\r\n").encode() + Path(file).read_bytes() + f"\r\n--{b}--\r\n".encode()
        req = urllib.request.Request(url, data=body, headers={"content-type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    if not d.get("ok"):
        raise RuntimeError(d.get("description"))


def send(msg):
    token, chat, thread = settings()
    if not (token and chat):
        return False
    text, img = split_media(msg)
    base = {"chat_id": chat, "parse_mode": "HTML"}
    if thread:
        base["message_thread_id"] = thread
    if img and Path(img).exists():
        if len(to_html(text)) <= CAPTION_MAX:
            _post(token, "sendPhoto", {**base, "caption": to_html(text)}, file=img)
            return True
        _post(token, "sendPhoto", base, file=img)
    _post(token, "sendMessage", {**base, "text": to_html(text), "disable_web_page_preview": True})
    return True


def send_all(msgs):
    """Send each message on its own; anything not sent goes to stdout."""
    left = []
    for msg in msgs:
        try:
            if send(msg):
                continue
        except Exception as e:  # noqa: BLE001
            print(f"telegram send failed: {e}", file=sys.stderr)
        left.append(msg)
    if left:
        print("\n\n".join(left))
