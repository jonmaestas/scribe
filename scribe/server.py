"""HTTP API agents and the family page use to print a slip."""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
import traceback
from collections import OrderedDict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from scribe.render import (
    TEMPLATES,
    ScribeError,
    load_styles,
    parse_wire,
    public_style,
    render_job,
)
from scribe.weather import fetch_forecast

DEFAULTS = {
    "printer_url": "http://scribe.local",
    "listen": "0.0.0.0",
    "port": 8787,
    "token": "",
    "timezone": "America/Chicago",
    "location_name": "",
    "latitude": None,
    "longitude": None,
    "units": "fahrenheit",
    "drape": True,
    "copies_max": 2,
    "max_prints_per_hour": 40,
    "min_interval_seconds": 2,
    "styles_file": "styles.json",
}

JOB_KEYS = {
    "template",
    "style",
    "title",
    "body",
    "subtitle",
    "kicker",
    "from",
    "items",
    "note",
    "blocks",
    "id",
    "copies",
    "latitude",
    "longitude",
    "timezone",
    "units",
    "location_name",
}


def load_config(path=None):
    path = path or os.environ.get("SCRIBE_CONFIG") or "config.json"
    config = dict(DEFAULTS)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                override = json.load(handle)
        except (OSError, ValueError) as exc:
            raise ScribeError("could not read {}: {}".format(path, exc))
        if not isinstance(override, dict):
            raise ScribeError("config must be a JSON object")
        unknown = set(override) - set(DEFAULTS)
        if unknown:
            raise ScribeError("unknown config field {}".format(sorted(unknown)[0]))
        config.update(override)
    config["_path"] = path
    return config


def local_now(config, now=None):
    if now is not None:
        return now
    try:
        tz = ZoneInfo(config.get("timezone") or "UTC")
    except Exception:
        raise ScribeError("unknown timezone {!r}".format(config.get("timezone")))
    return datetime.now(tz)


class App:
    def __init__(self, config, styles=None, poster=None, clock=None):
        self.config = config
        self.styles = styles if styles is not None else load_styles(config.get("styles_file"))
        self.poster = poster or post_printer
        self.clock = clock or time.time
        self.lock = threading.Lock()
        self.seen = OrderedDict()
        self.prints = deque()

    def render(self, job, now=None):
        now = local_now(self.config, now)
        forecast = None
        template = (job or {}).get("template") or "message"
        if template in ("weather", "briefing"):
            forecast = fetch_forecast(self.config, job)
        rendered = render_job(
            job,
            self.styles,
            now,
            drape=bool(self.config.get("drape", True)),
            forecast=forecast,
        )
        parse_wire(rendered.wire())
        return rendered

    def allow_print(self):
        now = self.clock()
        window = int(self.config.get("max_prints_per_hour") or 40)
        interval = float(self.config.get("min_interval_seconds") or 0)
        while self.prints and now - self.prints[0] > 3600:
            self.prints.popleft()
        if interval and self.prints and now - self.prints[-1] < interval:
            raise ScribeError("printer cooling down, try again in a few seconds", 429)
        if len(self.prints) >= window:
            raise ScribeError("hourly print cap reached", 429)
        self.prints.append(now)

    def remember(self, job_id, payload):
        if not job_id:
            return
        self.seen[job_id] = payload
        while len(self.seen) > 100:
            self.seen.popitem(last=False)

    def print_job(self, job, now=None):
        job = dict(job or {})
        job_id = job.get("id") or ""
        if job_id:
            if not isinstance(job_id, str) or len(job_id) > 80:
                raise ScribeError("id must be a string of at most 80 characters")
            allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
            if any(ch not in allowed for ch in job_id):
                raise ScribeError("id may contain only letters, numbers, and . _ : -")
        copies = job.get("copies", 1)
        if isinstance(copies, bool) or not isinstance(copies, int):
            raise ScribeError("copies must be an integer")
        copies_max = int(self.config.get("copies_max") or 1)
        if not 1 <= copies <= copies_max:
            raise ScribeError("copies must be 1 to {}".format(copies_max))
        with self.lock:
            if job_id and job_id in self.seen:
                payload = dict(self.seen[job_id])
                payload["duplicate"] = True
                return payload
            rendered = self.render(job, now=now)
            self.allow_print()
            wire = rendered.wire()
            printer_url = self.config.get("printer_url") or ""
            token = self.config.get("token") or ""
            for copy in range(copies):
                try:
                    self.poster(printer_url, wire, token)
                except ScribeError as exc:
                    if copy:
                        raise ScribeError(
                            "copy {} failed after an earlier copy was sent: {}".format(copy + 1, exc),
                            exc.status,
                        )
                    raise
            payload = {
                "ok": True,
                "id": job_id or None,
                "template": rendered.template,
                "style": rendered.style,
                "preview": rendered.preview(),
                "printer": printer_url,
                "copies": copies,
            }
            self.remember(job_id, payload)
            return payload


def post_printer(url, wire, token, timeout=25):
    endpoint = url.rstrip("/") + "/print"
    data = wire.encode("utf-8")
    request = Request(endpoint, data=data, method="POST")
    request.add_header("Content-Type", "text/plain; charset=utf-8")
    if token:
        request.add_header("X-Scribe-Token", token)
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read(512)
            return response.status
    except HTTPError as exc:
        body = exc.read(512).decode("utf-8", "replace").strip()
        raise ScribeError("printer returned {}: {}".format(exc.code, body or "error"), 502)
    except URLError as exc:
        raise ScribeError(
            "printer unreachable at {}. Flash the firmware and confirm it joined Wi-Fi. "
            "Preview still works. ({})".format(endpoint, exc.reason),
            502,
        )


def printer_status(url, timeout=2):
    endpoint = url.rstrip("/") + "/health"
    try:
        with urlopen(endpoint, timeout=timeout) as response:
            body = response.read(1024).decode("utf-8", "replace")
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {"raw": body.strip()}
            return {"reachable": True, "url": url, "health": payload}
    except Exception as exc:
        return {"reachable": False, "url": url, "error": str(exc)}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s  %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self):
        try:
            path = self.path.split("?", 1)[0]
            if path == "/health" or path == "/v1/health":
                self._send_json(200, self._health())
                return
            if path == "/":
                self._send_html(200, self._page({}))
                return
            self._require_token()
            if path == "/v1/styles":
                styles = self.server.app.styles
                self._send_json(200, {"styles": [public_style(name, styles[name]) for name in styles]})
                return
            if path == "/v1/templates":
                self._send_json(200, {"templates": [{"name": name, "description": text} for name, text in TEMPLATES.items()]})
                return
            if path == "/v1/config":
                self._send_json(200, self._public_config())
                return
            self._send_json(404, {"ok": False, "error": "not found"})
        except ScribeError as exc:
            self._send_json(exc.status, {"ok": False, "error": str(exc)})
        except Exception:
            traceback.print_exc()
            self._send_json(500, {"ok": False, "error": "internal error"})

    def do_POST(self):
        try:
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._handle_form()
                return
            self._require_token()
            if path == "/v1/preview":
                job = self._json_job()
                rendered = self.server.app.render(job)
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "dry_run": True,
                        "template": rendered.template,
                        "style": rendered.style,
                        "preview": rendered.preview(),
                        "wire": rendered.wire(),
                    },
                )
                return
            if path == "/v1/print":
                payload = self.server.app.print_job(self._json_job())
                sys.stdout.write(
                    "printed template={} style={} chars={}\n".format(
                        payload.get("template"), payload.get("style"), len(payload.get("preview") or "")
                    )
                )
                sys.stdout.flush()
                self._send_json(200, payload)
                return
            if path == "/v1/test":
                payload = self.server.app.print_job(
                    {
                        "template": "note",
                        "style": "receipt",
                        "title": "SCRIBE",
                        "body": "Printer test. If you can read this, wiring and Wi-Fi are good.",
                        "id": "test",
                    }
                )
                self._send_json(200, payload)
                return
            self._send_json(404, {"ok": False, "error": "not found"})
        except ScribeError as exc:
            self._send_json(exc.status, {"ok": False, "error": str(exc)})
        except Exception:
            traceback.print_exc()
            self._send_json(500, {"ok": False, "error": "internal error"})

    def _handle_form(self):
        form = self._form()
        self._require_token(form.get("token", ""))
        action = form.get("action") or "preview"
        job = {
            "template": form.get("template") or "message",
            "style": form.get("style") or "receipt",
            "title": form.get("title") or "",
            "body": form.get("body") or "",
            "from": form.get("from") or "",
            "note": form.get("note") or "",
            "items": [line.strip() for line in (form.get("items") or "").splitlines() if line.strip()],
        }
        job = {key: value for key, value in job.items() if value not in ("", [])}
        notice = ""
        preview = ""
        error = ""
        try:
            if action == "print":
                payload = self.server.app.print_job(job)
                notice = "Sent to the printer."
                preview = payload.get("preview") or ""
            else:
                rendered = self.server.app.render(job)
                notice = "Preview only. Nothing was printed."
                preview = rendered.preview()
        except ScribeError as exc:
            error = str(exc)
        self._send_html(200, self._page(form, notice=notice, preview=preview, error=error))

    def _health(self):
        printer = printer_status(self.server.app.config.get("printer_url") or "")
        config = self.server.app.config
        return {
            "ok": True,
            "service": "scribe",
            "printer": printer,
            "location_set": config.get("latitude") is not None and config.get("longitude") is not None,
            "timezone": config.get("timezone"),
            "token_required": bool(config.get("token")),
        }

    def _public_config(self):
        config = self.server.app.config
        return {
            "printer_url": config.get("printer_url"),
            "timezone": config.get("timezone"),
            "location_name": config.get("location_name") or "",
            "latitude": config.get("latitude"),
            "longitude": config.get("longitude"),
            "units": config.get("units"),
            "drape": config.get("drape"),
            "token_required": bool(config.get("token")),
            "styles": sorted(self.server.app.styles),
            "templates": list(TEMPLATES),
        }

    def _require_token(self, provided=None):
        expected = self.server.app.config.get("token") or ""
        if not expected:
            return
        if provided is None:
            provided = self.headers.get("X-Scribe-Token", "")
        if not hmac.compare_digest(str(provided), str(expected)):
            raise ScribeError("missing or wrong token", 401)

    def _json_job(self):
        raw = self._body()
        try:
            job = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise ScribeError("body must be JSON")
        if not isinstance(job, dict):
            raise ScribeError("body must be a JSON object")
        unknown = set(job) - JOB_KEYS
        if unknown:
            raise ScribeError("unknown field {}".format(sorted(unknown)[0]))
        return job

    def _body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length < 0 or length > 16384:
            raise ScribeError("body too large", 413)
        return self.rfile.read(length)

    def _form(self):
        raw = self._body().decode("utf-8", "replace")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _send_json(self, status, payload):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status, html):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _page(self, values, notice="", preview="", error=""):
        styles = self.server.app.styles
        template = values.get("template") or "message"
        style = values.get("style") or "receipt"
        token_required = bool(self.server.app.config.get("token"))
        template_options = []
        for name, description in TEMPLATES.items():
            if name == "custom":
                continue
            template_options.append(_option(name, name, template))
        style_options = [_option(name, name, style) for name in styles]
        token_field = ""
        if token_required:
            token_field = '<label>Token<input type="password" name="token" autocomplete="current-password"></label>'
        banner = ""
        if error:
            banner = '<p class="error">{}</p>'.format(_escape(error))
        elif notice:
            banner = "<p>{}</p>".format(_escape(notice))
        preview_block = ""
        if preview:
            preview_block = "<pre>{}</pre>".format(_escape(preview))
        return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scribe</title>
<style>
body {{ font: 18px/1.45 system-ui, sans-serif; max-width: 38rem; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; background: #f6f3ee; }}
h1 {{ font-size: 1.6rem; margin-bottom: 0.25rem; }}
p.lede {{ margin-top: 0; color: #333; }}
label {{ display: block; margin-top: 1rem; font-weight: 650; }}
input, select, textarea, button {{ width: 100%; box-sizing: border-box; font: inherit; padding: 0.65rem; margin-top: 0.35rem; }}
textarea {{ min-height: 7rem; }}
.actions {{ display: flex; gap: 0.75rem; }}
.actions button {{ margin-top: 1rem; }}
pre {{ background: #161616; color: #f4f1ea; padding: 1rem; overflow-x: auto; white-space: pre; }}
.error {{ color: #8a1f1f; font-weight: 650; }}
</style>
</head>
<body>
<h1>Scribe</h1>
<p class="lede">Send a slip to the printer on the counter.</p>
{banner}
{preview}
<form method="post" action="/">
<label>Type<select name="template">{templates}</select></label>
<label>Style<select name="style">{styles}</select></label>
<label>Title<input name="title" value="{title}" maxlength="120"></label>
<label>Message<textarea name="body">{body}</textarea></label>
<label>From<input name="from" value="{sender}" maxlength="40"></label>
<label>List items, one per line<textarea name="items">{items}</textarea></label>
<label>Note added to a briefing<textarea name="note">{note}</textarea></label>
{token}
<div class="actions">
<button name="action" value="preview">Preview</button>
<button name="action" value="print">Print</button>
</div>
</form>
</body>
</html>
""".format(
            banner=banner,
            preview=preview_block,
            templates="".join(template_options),
            styles="".join(style_options),
            title=_escape(values.get("title") or ""),
            body=_escape(values.get("body") or ""),
            sender=_escape(values.get("from") or ""),
            items=_escape(values.get("items") or ""),
            note=_escape(values.get("note") or ""),
            token=token_field,
        )


def _option(value, label, selected):
    mark = " selected" if value == selected else ""
    return '<option value="{}"{}>{}</option>'.format(_escape(value), mark, _escape(label))


def _escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def make_server(config, styles=None, poster=None, clock=None, host=None, port=None):
    app = App(config, styles=styles, poster=poster, clock=clock)
    bind_host = host or config.get("listen") or "127.0.0.1"
    bind_port = int(port if port is not None else config.get("port") or 8787)
    httpd = ThreadingHTTPServer((bind_host, bind_port), Handler)
    httpd.app = app
    return httpd
