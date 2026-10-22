"""CLI for the Scribe print API."""

from __future__ import annotations

import argparse
import json
import os
import sys

from scribe.render import ScribeError, TEMPLATES, builtin_styles
from scribe.server import App, load_config, local_now, make_server, printer_status


def main(argv=None):
    parser = argparse.ArgumentParser(prog="scribe")
    parser.add_argument("--config", default=os.environ.get("SCRIBE_CONFIG") or "config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the agent API")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)

    for name in ("preview", "print"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--json")
        cmd.add_argument("--template", choices=sorted(TEMPLATES))
        cmd.add_argument("--style")
        cmd.add_argument("--title")
        cmd.add_argument("--body")
        cmd.add_argument("--from", dest="sender")
        cmd.add_argument("--item", action="append")
        cmd.add_argument("--note")
        cmd.add_argument("--id")

    sub.add_parser("styles")
    sub.add_parser("health")
    locate = sub.add_parser("locate", help="fill latitude and longitude from this network")
    locate.add_argument("--write", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            _serve(args)
        elif args.command == "styles":
            for name, spec in builtin_styles().items():
                print("{}  {}".format(name, spec.get("description", "")))
        elif args.command == "health":
            config = load_config(args.config)
            print(json.dumps(printer_status(config["printer_url"]), indent=2))
        elif args.command == "locate":
            _locate(args)
        else:
            _job_command(args)
    except ScribeError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return exc.status if exc.status < 100 else 1
    return 0


def _serve(args):
    config = load_config(args.config)
    host = args.host or os.environ.get("HOST") or config.get("listen") or "0.0.0.0"
    if args.port:
        port = args.port
    elif os.environ.get("PASEO_PORT"):
        port = int(os.environ["PASEO_PORT"])
    else:
        port = int(config.get("port") or 8787)
    httpd = make_server(config, host=host, port=port)
    print("scribe listening on http://{}:{}".format(host, port), flush=True)
    print("printer {}".format(config.get("printer_url")), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        httpd.server_close()


def _job_from_args(args):
    if args.json:
        with open(args.json, "r", encoding="utf-8") as handle:
            job = json.load(handle)
        if not isinstance(job, dict):
            raise ScribeError("--json file must contain an object")
        return job
    job = {}
    if args.template:
        job["template"] = args.template
    if args.style:
        job["style"] = args.style
    if args.title:
        job["title"] = args.title
    if args.body:
        job["body"] = args.body
    if args.sender:
        job["from"] = args.sender
    if args.item:
        job["items"] = args.item
        job.setdefault("template", "list")
    if args.note:
        job["note"] = args.note
    if args.id:
        job["id"] = args.id
    if not job:
        raise ScribeError("pass --json or --title/--body")
    return job


def _job_command(args):
    config = load_config(args.config)
    job = _job_from_args(args)
    app = App(config)
    if args.command == "preview":
        rendered = app.render(job, now=local_now(config))
        print(rendered.preview())
        return
    payload = app.print_job(job, now=local_now(config))
    print(payload["preview"])
    if payload.get("duplicate"):
        print("(already printed)")


def _locate(args):
    from urllib.request import urlopen

    with urlopen("https://ipwho.is/", timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("success", True):
        raise ScribeError(payload.get("message") or "locate failed")
    timezone = payload.get("timezone")
    if isinstance(timezone, dict):
        timezone = timezone.get("id") or ""
    found = {
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
        "timezone": timezone or "",
        "location_name": payload.get("city") or "",
    }
    if found["latitude"] is None or found["longitude"] is None:
        raise ScribeError("locate did not return coordinates")
    print(json.dumps(found, indent=2))
    if not args.write:
        print("re-run with --write to save config.json", file=sys.stderr)
        return
    config = {}
    if os.path.exists(args.config):
        with open(args.config, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    config.update(found)
    with open(args.config, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    print("wrote {}".format(args.config), file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
