from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scribe.render import ScribeError
from scribe.server import DEFAULTS, App, make_server


class FakePrinter(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok":true,"name":"scribe"}'
        self._send(200, body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.server.posts.append(self.rfile.read(length))
        self._send(200, b"ok")

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


class ApiTests(unittest.TestCase):
    def test_print_posts_wire_and_dedupes(self):
        printer = ThreadingHTTPServer(("127.0.0.1", 0), FakePrinter)
        printer.posts = []
        thread = threading.Thread(target=printer.serve_forever, daemon=True)
        thread.start()
        config = dict(DEFAULTS)
        config["printer_url"] = "http://127.0.0.1:{}".format(printer.server_address[1])
        config["min_interval_seconds"] = 0
        config["styles_file"] = "missing-styles.json"
        app = App(config)
        try:
            payload = app.print_job({"template": "note", "style": "note", "body": "Hello house", "id": "note-1"})
            self.assertTrue(payload["ok"])
            self.assertEqual(len(printer.posts), 1)
            self.assertIn(b"Hello house", printer.posts[0])
            self.assertTrue(printer.posts[0].startswith(b"drape 1\n"))
            again = app.print_job({"template": "note", "style": "note", "body": "other", "id": "note-1"})
            self.assertTrue(again["duplicate"])
            self.assertEqual(len(printer.posts), 1)
            self.assertIn("Hello house", again["preview"])
        finally:
            printer.shutdown()
            printer.server_close()

    def test_rate_limit_and_copies(self):
        config = dict(DEFAULTS)
        config["min_interval_seconds"] = 10
        config["styles_file"] = "missing-styles.json"
        app = App(config, poster=lambda *args: None, clock=lambda: 1000.0)
        app.print_job({"template": "note", "body": "one"})
        with self.assertRaises(ScribeError) as limited:
            app.print_job({"template": "note", "body": "two"})
        self.assertEqual(limited.exception.status, 429)
        with self.assertRaises(ScribeError):
            app.print_job({"template": "note", "body": "x", "copies": 3})

    def test_http_preview_print_and_token(self):
        printer = ThreadingHTTPServer(("127.0.0.1", 0), FakePrinter)
        printer.posts = []
        pthread = threading.Thread(target=printer.serve_forever, daemon=True)
        pthread.start()
        config = dict(DEFAULTS)
        config["printer_url"] = "http://127.0.0.1:{}".format(printer.server_address[1])
        config["token"] = "secret"
        config["min_interval_seconds"] = 0
        config["styles_file"] = "missing-styles.json"
        httpd = make_server(config, host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:{}".format(httpd.server_address[1])
        try:
            with self.assertRaises(HTTPError) as denied:
                urlopen(Request(base + "/v1/print", data=b"{}", method="POST"), timeout=5)
            self.assertEqual(denied.exception.code, 401)

            preview = _post(base + "/v1/preview", {"template": "message", "style": "poster", "title": "Dinner", "body": "Tacos"})
            self.assertTrue(preview["dry_run"])
            self.assertIn("Dinner", preview["preview"])
            self.assertIn("text|2|", preview["wire"])
            self.assertEqual(printer.posts, [])

            printed = _post(base + "/v1/print", {"template": "note", "body": "On my way", "id": "way-1"})
            self.assertTrue(printed["ok"])
            self.assertIn(b"On my way", printer.posts[0])

            page = urlopen(base + "/", timeout=5).read().decode("utf-8")
            self.assertIn("Print", page)
            self.assertIn("name=\"style\"", page)

            health = json.loads(urlopen(base + "/health", timeout=5).read().decode("utf-8"))
            self.assertTrue(health["ok"])
            self.assertTrue(health["printer"]["reachable"])
            self.assertTrue(health["token_required"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            printer.shutdown()
            printer.server_close()


def _post(url, payload):
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-Scribe-Token": "secret"},
    )
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
