import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bot"))
os.environ["RATE_BOT_CONFIG"] = str(ROOT / "config.example.json")

import core  # noqa: E402
import markets  # noqa: E402
import notify  # noqa: E402


class Encoding(unittest.TestCase):
    def test_selector(self):
        self.assertEqual(core.selector("balanceOf(address)"), "70a08231")
        self.assertEqual(core.selector("getReserveData(address)"), "35ea6a75")

    def test_address_keeps_leading_zero_bytes(self):
        raw = int("0e7e4c186535e57a06d902738e79347625463480", 16)
        self.assertEqual(core.to_address(raw), "0x0e7e4c186535e57a06d902738e79347625463480")
        with self.assertRaises(ValueError):
            core.to_address(1 << 160)

    def test_annualise(self):
        self.assertAlmostEqual(core.annualise(1.001, 1.0, 7 * 86400), 0.1 * 365 / 7, places=6)


class Curves(unittest.TestCase):
    def test_aave(self):
        f = lambda u: markets.aave_rate_at(u, 0.8, 0.0, 1.0, 30.0)  # noqa: E731
        self.assertAlmostEqual(f(0.4), 0.5)
        self.assertAlmostEqual(f(0.8), 1.0)
        self.assertAlmostEqual(f(0.9), 16.0)

    def test_morpho_shape(self):
        self.assertAlmostEqual(markets.morpho_curve(0.9), 1.0)
        self.assertAlmostEqual(markets.morpho_curve(0.0), 0.25)
        self.assertAlmostEqual(markets.morpho_curve(1.0), 4.0)

    def test_euler_kink(self):
        k = int(0.9 * 2 ** 32)
        self.assertAlmostEqual(markets.euler_rate_at(0.0, 0, 1, 1, k), 0.0)
        self.assertGreater(markets.euler_rate_at(0.95, 0, 10 ** 9, 10 ** 11, k),
                           markets.euler_rate_at(0.85, 0, 10 ** 9, 10 ** 11, k))


class Telegram(unittest.TestCase):
    def test_html(self):
        self.assertEqual(notify.to_html("**A < B** ok"), "<b>A &lt; B</b> ok")

    def test_send_photo_and_text(self):
        got = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                got.append((self.path, self.headers["Content-Type"], body))
                out = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        notify.API = f"http://127.0.0.1:{srv.server_port}/bot{{token}}/{{method}}"
        os.environ.update(TELEGRAM_BOT_TOKEN="T", TELEGRAM_CHAT_ID="-100", TELEGRAM_THREAD_ID="7")
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            f.write(b"\x89PNG fake")
            f.flush()
            self.assertTrue(notify.send(f"**Title**\nline\nMEDIA:{f.name}"))
            self.assertTrue(notify.send("plain"))
        srv.shutdown()
        self.assertTrue(got[0][0].endswith("/sendPhoto"))
        self.assertIn(b'name="caption"\r\n\r\n<b>Title</b>\nline', got[0][2])
        self.assertIn(b'name="message_thread_id"\r\n\r\n7', got[0][2])
        self.assertIn(b"\x89PNG fake", got[0][2])
        self.assertEqual(json.loads(got[1][2])["text"], "plain")


if __name__ == "__main__":
    unittest.main()
