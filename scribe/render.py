"""Turn a print job into a slip the firmware can render.

Wire format (UTF-8, one record per line):

    drape 0|1
    tailfeed <0-8>
    text|<size>|<bold>|<inverse>|<payload>
    feed|<1-4>

`payload` is the exact glyphs for that line, including padding. `size` is
1, 2, or 3 (12-dot font, so 32 / 16 / 10 columns on a 384-dot head). The
firmware prints `text` and `feed` top to bottom, or bottom to top when
`drape` is 1, then feeds `tailfeed` lines. It does not reflow. Keep this
contract in sync with firmware/scribe/scribe.ino.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field

DOTS = 384
FONT_DOTS = 12
COLS = {1: DOTS // FONT_DOTS, 2: DOTS // (FONT_DOTS * 2), 3: DOTS // (FONT_DOTS * 3)}
MAX_INPUT_CHARS = 2000
MAX_BLOCKS = 40
MAX_LINES = 60



FOLD = {
    "\u00b0": "",
    "\u2014": "-",
    "\u2013": "-",
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2026": "...",
    "\u2022": "-",
    "\u00b7": "-",
    "\u00a0": " ",
}

ROLES = ("kicker", "title", "subtitle", "body", "item", "kv", "signoff")
ROLE_FIELDS = ("size", "bold", "inverse", "align")
SCALARS = (
    "rule",
    "footer",
    "feed",
    "gap_after_title",
    "rule_before_title",
    "rule_after_title",
    "rule_before_signoff",
    "rule_before_footer",
)
RULES = {"none": "", "dash": "-", "equals": "=", "dots": "."}
ALIGNS = {"left", "center", "right"}


class ScribeError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _role(size, bold, inverse, align):
    return {"size": size, "bold": bold, "inverse": inverse, "align": align}


def _style(**kwargs):
    base = {
        "kicker": _role(1, True, False, "center"),
        "title": _role(1, True, False, "left"),
        "subtitle": _role(1, False, False, "left"),
        "body": _role(1, False, False, "left"),
        "item": _role(1, False, False, "left"),
        "kv": _role(1, False, False, "left"),
        "signoff": _role(1, False, False, "left"),
        "rule": "none",
        "footer": True,
        "feed": 4,
        "gap_after_title": 0,
        "rule_before_title": False,
        "rule_after_title": False,
        "rule_before_signoff": False,
        "rule_before_footer": False,
    }
    base.update(kwargs)
    return base


STYLES = {
    "receipt": _style(
        description="Inverse kicker, compact body, tear footer. Weather and briefings.",
        kicker=_role(1, True, True, "center"),
        title=_role(1, True, False, "center"),
        subtitle=_role(1, False, False, "center"),
        rule="dash",
        rule_before_footer=True,
        feed=4,
    ),
    "poster": _style(
        description="Large centered title. Short family announcements.",
        title=_role(2, True, False, "center"),
        subtitle=_role(1, False, False, "center"),
        body=_role(1, False, False, "center"),
        item=_role(1, False, False, "center"),
        signoff=_role(1, False, False, "center"),
        footer=False,
        feed=5,
        gap_after_title=1,
    ),
    "ticket": _style(
        description="Ruled ticket. Chores, lists, and label/value rows.",
        title=_role(1, True, False, "center"),
        subtitle=_role(1, False, False, "center"),
        kv=_role(1, True, False, "left"),
        rule="equals",
        rule_before_title=True,
        rule_after_title=True,
        rule_before_footer=True,
        feed=4,
    ),
    "note": _style(
        description="Quiet left text and a time footer. Status and reminders.",
        title=_role(1, True, False, "left"),
        feed=3,
    ),
    "letter": _style(
        description="Centered greeting, left body, dotted close. Family notes.",
        title=_role(2, False, False, "center"),
        signoff=_role(1, False, False, "center"),
        rule="dots",
        feed=4,
        gap_after_title=1,
        rule_before_signoff=True,
    ),
}

TEMPLATES = {
    "message": "Title, body, and optional from. A note to the house.",
    "list": "Title plus items. Chores and reminders.",
    "note": "Short text with a time footer.",
    "weather": "Today's forecast. Needs latitude and longitude in config.json.",
    "briefing": "Weather plus an optional note.",
    "custom": "blocks of text, kv, rule, and feed. Full layout control.",
}


@dataclass
class TextLine:
    text: str
    size: int
    bold: bool
    inverse: bool


@dataclass
class FeedLine:
    n: int


@dataclass
class Row:
    kind: str
    text: str = ""
    label: str = ""
    value: str = ""
    role: str = "body"


@dataclass
class Content:
    kicker: str = ""
    title: str = ""
    subtitle: str = ""
    rows: list = field(default_factory=list)
    signoff: str = ""
    template: str = ""


@dataclass
class Rendered:
    lines: list
    drape: bool
    tailfeed: int
    template: str
    style: str

    def wire(self):
        out = ["drape {}".format(1 if self.drape else 0), "tailfeed {}".format(self.tailfeed)]
        for line in self.lines:
            if isinstance(line, FeedLine):
                out.append("feed|{}".format(line.n))
            else:
                out.append(
                    "text|{}|{}|{}|{}".format(
                        line.size, 1 if line.bold else 0, 1 if line.inverse else 0, line.text
                    )
                )
        return "\n".join(out) + "\n"

    def preview(self):
        rows = []
        for line in self.lines:
            if isinstance(line, FeedLine):
                rows.extend([""] * line.n)
                continue
            if line.size > 1:
                shown = line.text.strip().center(COLS[1])
            elif line.inverse:
                shown = line.text.center(COLS[1])
            else:
                shown = line.text.ljust(COLS[1])
            rows.append(shown.rstrip() if not line.inverse else shown)
        text = "\n".join(rows).rstrip()
        return (text + "\n") if text else "\n"


def fold(text):
    if text is None:
        return ""
    if not isinstance(text, str):
        raise ScribeError("text must be a string")
    for src, dst in FOLD.items():
        text = text.replace(src, dst)
    cleaned = []
    for ch in text:
        if ch in "\n\t":
            cleaned.append(" " if ch == "\t" else ch)
        elif 32 <= ord(ch) <= 126:
            cleaned.append(ch)
        else:
            cleaned.append("?")
    return "".join(cleaned)


def wrap(text, width):
    if width < 1:
        raise ScribeError("printer width must be positive")
    lines = []
    for para in fold(text).split("\n"):
        para = " ".join(para.split())
        if not para:
            continue
        current = ""
        for word in para.split(" "):
            while len(word) > width:
                if current:
                    lines.append(current)
                    current = ""
                lines.append(word[:width])
                word = word[width:]
            if not word:
                continue
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= width:
                current = current + " " + word
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
    return lines


def align_text(text, width, align, inverse):
    if align not in ALIGNS:
        raise ScribeError("align must be left, center, or right")
    if len(text) > width:
        text = text[:width]
    if align == "center":
        out = text.center(width)
    elif align == "right":
        out = text.rjust(width)
    else:
        out = text.ljust(width) if inverse else text
    if inverse and len(out) < width:
        out = out.ljust(width)
    return out


def footer_line(when, width=None):
    width = width or COLS[1]
    right = _clock(when)
    left = "scribe"
    gap = width - len(left) - len(right)
    if gap < 1:
        return right[:width]
    return left + (" " * gap) + right


def _clock(when):
    hour = when.hour
    suffix = "a" if hour < 12 else "p"
    hour12 = hour % 12 or 12
    return "{}:{:02d}{}".format(hour12, when.minute, suffix)




def _check_role(name, role):
    if not isinstance(role, dict):
        raise ScribeError("style.{} must be an object".format(name))
    unknown = set(role) - set(ROLE_FIELDS)
    if unknown:
        raise ScribeError("unknown style field {}.{}".format(name, sorted(unknown)[0]))
    if "size" in role and role["size"] not in (1, 2, 3):
        raise ScribeError("{}.size must be 1, 2, or 3".format(name))
    if "align" in role and role["align"] not in ALIGNS:
        raise ScribeError("{}.align must be left, center, or right".format(name))
    for flag in ("bold", "inverse"):
        if flag in role and not isinstance(role[flag], bool):
            raise ScribeError("{}.{} must be true or false".format(name, flag))


def _merge_style(base, override, label):
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in ("base", "description", "name"):
            continue
        if key in ROLES:
            _check_role(key, value)
            merged[key].update(value)
        elif key in SCALARS:
            merged[key] = value
        else:
            raise ScribeError("unknown style field {} in {}".format(key, label))
    if merged["rule"] not in RULES:
        raise ScribeError("rule must be none, dash, equals, or dots")
    if not isinstance(merged["footer"], bool):
        raise ScribeError("footer must be true or false")
    if not isinstance(merged["feed"], int) or not 0 <= merged["feed"] <= 8:
        raise ScribeError("feed must be an integer from 0 to 8")
    if not isinstance(merged["gap_after_title"], int) or not 0 <= merged["gap_after_title"] <= 3:
        raise ScribeError("gap_after_title must be an integer from 0 to 3")
    for flag in ("rule_before_title", "rule_after_title", "rule_before_signoff", "rule_before_footer"):
        if not isinstance(merged[flag], bool):
            raise ScribeError("{} must be true or false".format(flag))
    return merged


def builtin_styles():
    return copy.deepcopy(STYLES)


def load_styles(path):
    styles = builtin_styles()
    if not path or not os.path.exists(path):
        return styles
    try:
        with open(path, "r", encoding="utf-8") as handle:
            extra = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ScribeError("could not read styles file {}: {}".format(path, exc))
    if not isinstance(extra, dict):
        raise ScribeError("styles file must be an object of style names")
    for name, spec in extra.items():
        if not isinstance(name, str) or not name.strip():
            raise ScribeError("style names must be strings")
        if not isinstance(spec, dict):
            raise ScribeError("style {} must be an object".format(name))
        base_name = spec.get("base", "receipt")
        if base_name not in styles:
            raise ScribeError("style {} bases unknown style {}".format(name, base_name))
        styles[name] = _merge_style(styles[base_name], spec, name)
        if "description" in spec:
            styles[name]["description"] = spec["description"]
    return styles


def resolve_style(style, styles):
    if style in (None, ""):
        style = "receipt"
    if isinstance(style, str):
        if style not in styles:
            known = ", ".join(sorted(styles))
            raise ScribeError("unknown style {!r}. known: {}".format(style, known))
        return style, copy.deepcopy(styles[style])
    if isinstance(style, dict):
        base_name = style.get("base", "receipt")
        if base_name not in styles:
            raise ScribeError("unknown base style {!r}".format(base_name))
        return "custom", _merge_style(styles[base_name], style, "request")
    raise ScribeError("style must be a name or an object")


def _require_text(value, label):
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ScribeError("{} must be a string".format(label))
    return value


def _input_size(job):
    chunks = []
    for key in ("title", "body", "subtitle", "kicker", "from", "note"):
        value = job.get(key)
        if isinstance(value, str):
            chunks.append(value)
    items = job.get("items") or []
    if isinstance(items, list):
        chunks.extend(item for item in items if isinstance(item, str))
    blocks = job.get("blocks") or []
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict):
                for key in ("text", "label", "value"):
                    if isinstance(block.get(key), str):
                        chunks.append(block[key])
    return sum(len(chunk) for chunk in chunks)


def build_content(job, forecast=None, now=None):
    template = job.get("template") or "message"
    if template not in TEMPLATES:
        known = ", ".join(TEMPLATES)
        raise ScribeError("unknown template {!r}. known: {}".format(template, known))
    if _input_size(job) > MAX_INPUT_CHARS:
        raise ScribeError("slip is over {} characters".format(MAX_INPUT_CHARS))

    content = Content(template=template)
    title = _require_text(job.get("title"), "title")
    body = _require_text(job.get("body"), "body")
    subtitle = _require_text(job.get("subtitle"), "subtitle")
    kicker = _require_text(job.get("kicker"), "kicker")
    sender = _require_text(job.get("from"), "from").strip()
    note = _require_text(job.get("note"), "note")

    if template == "custom":
        content.rows = _rows_from_blocks(job.get("blocks"))
        content.kicker = kicker
        content.title = title
        content.subtitle = subtitle
        content.signoff = _signoff(sender)
        return content

    if template == "list":
        items = job.get("items")
        if not isinstance(items, list) or not items:
            raise ScribeError("list requires items")
        if not all(isinstance(item, str) and item.strip() for item in items):
            raise ScribeError("items must be non-empty strings")
        content.title = title or "LIST"
        content.subtitle = subtitle
        content.kicker = kicker
        content.rows = [Row("item", text=item) for item in items]
        content.signoff = _signoff(sender)
        return content

    if template in ("weather", "briefing"):
        if forecast is None:
            raise ScribeError("weather forecast is missing")
        content = Content(
            kicker=kicker or forecast.kicker,
            title=forecast.title,
            subtitle=forecast.subtitle,
            rows=list(forecast.rows),
            signoff=forecast.signoff,
            template=template,
        )
        if note:
            content.rows.append(Row("rule"))
            content.rows.append(Row("text", text=note))
        if sender and not content.signoff:
            content.signoff = _signoff(sender)
        return content

    if template == "note":
        if not title and not body:
            raise ScribeError("note requires title or body")
        content.title = title
        content.kicker = kicker
        content.subtitle = subtitle
        if body:
            content.rows.append(Row("text", text=body))
        content.signoff = _signoff(sender)
        return content

    if not title and not body:
        raise ScribeError("message requires title or body")
    content.title = title
    content.subtitle = subtitle
    content.kicker = kicker
    if body:
        content.rows.append(Row("text", text=body))
    content.signoff = _signoff(sender)
    return content


def _signoff(sender):
    if not sender:
        return ""
    if sender.startswith("-"):
        return sender
    return "- " + sender


def _rows_from_blocks(blocks):
    if not isinstance(blocks, list) or not blocks:
        raise ScribeError("custom requires blocks")
    if len(blocks) > MAX_BLOCKS:
        raise ScribeError("custom blocks exceed {}".format(MAX_BLOCKS))
    rows = []
    for block in blocks:
        if not isinstance(block, dict):
            raise ScribeError("each block must be an object")
        op = block.get("op") or block.get("type") or "text"
        if op == "text":
            text = _require_text(block.get("text"), "block.text")
            if not text:
                raise ScribeError("text block requires text")
            role = block.get("role") or "body"
            if role not in ROLES:
                raise ScribeError("unknown block role {!r}".format(role))
            row = Row("text", text=text, role=role)
            row.override = _block_override(block)
            rows.append(row)
        elif op == "kv":
            label = _require_text(block.get("label"), "block.label")
            value = _require_text(block.get("value"), "block.value")
            if not label and not value:
                raise ScribeError("kv block requires label or value")
            row = Row("kv", label=label, value=value)
            row.override = _block_override(block)
            rows.append(row)
        elif op == "rule":
            kind = block.get("kind") or ""
            if kind and kind not in RULES:
                raise ScribeError("rule kind must be none, dash, equals, or dots")
            rows.append(Row("rule", text=kind))
        elif op == "feed":
            n = block.get("lines", 1)
            if not isinstance(n, int) or not 1 <= n <= 3:
                raise ScribeError("feed lines must be 1 to 3")
            rows.append(Row("feed", text=str(n)))
        else:
            raise ScribeError("unknown block op {!r}".format(op))
    return rows


def _block_override(block):
    override = {}
    for key in ROLE_FIELDS:
        if key in block:
            override[key] = block[key]
    if override:
        _check_role("block", override)
    return override


def _emit_text(lines, text, spec):
    spec = dict(spec)
    width = COLS[spec["size"]]
    for piece in wrap(text, width):
        lines.append(
            TextLine(
                align_text(piece, width, spec["align"], spec["inverse"]),
                spec["size"],
                spec["bold"],
                spec["inverse"],
            )
        )


def _emit_rule(lines, spec, kind=None):
    name = spec["rule"] if kind is None else kind
    if not name or name == "none":
        return
    char = RULES.get(name)
    if not char:
        return
    lines.append(TextLine(char * COLS[1], 1, False, False))


def _emit_kv(lines, label, value, spec):
    width = COLS[spec["size"]]
    label = " ".join(fold(label).split())
    value = " ".join(fold(value).split())
    if label and value and len(label) + 1 + len(value) <= width:
        gap = width - len(label) - len(value)
        _emit_fitted(lines, label + (" " * gap) + value, spec)
        return
    if label:
        _emit_text(lines, label, spec)
    if value:
        _emit_text(lines, value, spec)


def _emit_fitted(lines, text, spec):
    width = COLS[spec["size"]]
    if len(text) > width:
        text = text[:width]
    lines.append(TextLine(text, spec["size"], spec["bold"], spec["inverse"]))


def _emit_item(lines, text, spec):
    width = COLS[spec["size"]]
    pieces = wrap(text, max(1, width - 2))
    if not pieces:
        return
    if spec["align"] == "left":
        first, rest = pieces[0], pieces[1:]
        _emit_fitted(lines, "- " + first, spec)
        for piece in rest:
            _emit_fitted(lines, "  " + piece, spec)
        return
    for piece in pieces:
        _emit_text(lines, "- " + piece, spec)


def paint(content, style_name, spec, now, drape):
    lines = []
    if content.kicker:
        if spec["rule_before_title"]:
            _emit_rule(lines, spec)
        _emit_text(lines, content.kicker, spec["kicker"])
    if content.title:
        if spec["rule_before_title"] and not content.kicker:
            _emit_rule(lines, spec)
        _emit_text(lines, content.title, spec["title"])
        if spec["gap_after_title"]:
            lines.append(FeedLine(spec["gap_after_title"]))
    if content.subtitle:
        _emit_text(lines, content.subtitle, spec["subtitle"])
    if spec["rule_after_title"] and (content.title or content.kicker or content.subtitle):
        _emit_rule(lines, spec)
    for row in content.rows:
        if row.kind == "rule":
            _emit_rule(lines, spec, row.text or None)
        elif row.kind == "feed":
            lines.append(FeedLine(int(row.text)))
        elif row.kind == "kv":
            kv_spec = dict(spec["kv"])
            kv_spec.update(getattr(row, "override", {}) or {})
            _emit_kv(lines, row.label, row.value, kv_spec)
        elif row.kind == "item":
            _emit_item(lines, row.text, spec["item"])
        else:
            role_name = row.role if row.role in spec else "body"
            role = dict(spec[role_name])
            role.update(getattr(row, "override", {}) or {})
            _emit_text(lines, row.text, role)
    if content.signoff:
        if spec["rule_before_signoff"]:
            _emit_rule(lines, spec)
        _emit_text(lines, content.signoff, spec["signoff"])
    if spec["footer"]:
        if spec["rule_before_footer"]:
            _emit_rule(lines, spec)
        _emit_fitted(lines, footer_line(now), {"size": 1, "bold": False, "inverse": False, "align": "left"})
    if len(lines) > MAX_LINES:
        raise ScribeError("slip is longer than {} lines".format(MAX_LINES))
    for line in lines:
        if isinstance(line, TextLine) and len(line.text) > COLS[line.size]:
            raise ScribeError("internal: line exceeds printer width")
    return Rendered(lines, bool(drape), int(spec["feed"]), content.template, style_name)


def render_job(job, styles, now, drape=True, forecast=None):
    if not isinstance(job, dict):
        raise ScribeError("job must be an object")
    style_name, spec = resolve_style(job.get("style"), styles)
    content = build_content(job, forecast=forecast, now=now)
    return paint(content, style_name, spec, now, drape)


def parse_wire(wire):
    """Reference parser for the firmware contract. Returns headers and ops."""
    drape = 1
    tailfeed = 3
    ops = []
    saw_op = False
    for raw in wire.split("\n"):
        if raw.endswith("\r"):
            raw = raw[:-1]
        if raw == "" or raw.startswith("#"):
            continue
        if raw.startswith("drape "):
            if saw_op:
                raise ScribeError("drape must be a header")
            value = raw[6:]
            if value not in ("0", "1"):
                raise ScribeError("drape must be 0 or 1")
            drape = int(value)
            continue
        if raw.startswith("tailfeed "):
            if saw_op:
                raise ScribeError("tailfeed must be a header")
            value = raw[9:]
            if not value.isdigit() or not 0 <= int(value) <= 8:
                raise ScribeError("tailfeed must be 0 to 8")
            tailfeed = int(value)
            continue
        if raw.startswith("feed|"):
            value = raw[5:]
            if not value.isdigit() or not 1 <= int(value) <= 4:
                raise ScribeError("feed must be 1 to 4")
            ops.append({"op": "feed", "n": int(value)})
            saw_op = True
            continue
        if raw.startswith("text|"):
            parts = raw.split("|", 4)
            if len(parts) != 5:
                raise ScribeError("text op needs size, bold, inverse, and payload")
            _, size, bold, inverse, payload = parts
            if size not in ("1", "2", "3") or bold not in ("0", "1") or inverse not in ("0", "1"):
                raise ScribeError("bad text op flags")
            if len(payload) > 48:
                raise ScribeError("text payload too long")
            ops.append(
                {
                    "op": "text",
                    "size": int(size),
                    "bold": bold == "1",
                    "inverse": inverse == "1",
                    "text": payload,
                }
            )
            saw_op = True
            continue
        raise ScribeError("unknown wire opcode {!r}".format(raw[:40]))
    return {"drape": drape, "tailfeed": tailfeed, "ops": ops}


def public_style(name, spec):
    shown = {"name": name, "description": spec.get("description", "")}
    for role in ROLES:
        shown[role] = spec[role]
    for key in SCALARS:
        shown[key] = spec[key]
    return shown
