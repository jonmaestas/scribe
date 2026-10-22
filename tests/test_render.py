from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from scribe.render import (
    COLS,
    ScribeError,
    builtin_styles,
    load_styles,
    parse_wire,
    render_job,
)
from scribe.weather import content_from_forecast

NOW = datetime(2026, 10, 22, 7, 4, tzinfo=ZoneInfo("America/Chicago"))
STYLES = builtin_styles()
FORECAST = {
    "current": {"temperature_2m": 74.2, "weather_code": 2},
    "daily": {
        "time": ["2026-10-22"],
        "weather_code": [2],
        "temperature_2m_max": [78.4],
        "temperature_2m_min": [61.1],
        "precipitation_probability_max": [20],
        "sunrise": ["2026-10-22T07:12"],
        "sunset": ["2026-10-22T18:31"],
    },
}


def forecast(name="Austin"):
    return content_from_forecast(FORECAST, "fahrenheit", name)


def texts(wire):
    return [op for op in parse_wire(wire)["ops"] if op["op"] == "text"]


class RenderTests(unittest.TestCase):
    def test_poster_is_large_centered_and_has_no_footer(self):
        rendered = render_job(
            {
                "template": "message",
                "style": "poster",
                "title": "Dinner",
                "body": "Tacos at 6.",
                "from": "Dad",
            },
            STYLES,
            NOW,
        )
        parsed = parse_wire(rendered.wire())
        self.assertEqual(parsed["drape"], 1)
        self.assertEqual(parsed["tailfeed"], 5)
        self.assertEqual(texts(rendered.wire())[0]["text"], "Dinner".center(COLS[2]))
        self.assertEqual(texts(rendered.wire())[0]["size"], 2)
        self.assertTrue(texts(rendered.wire())[0]["bold"])
        self.assertIn("feed|1", rendered.wire())
        self.assertIn("Tacos at 6.", rendered.wire())
        self.assertIn("- Dad", rendered.wire())
        self.assertNotIn("scribe", rendered.wire())
        self.assertIn("Dinner", rendered.preview())

    def test_receipt_weather_uses_inverse_kicker_and_footer(self):
        rendered = render_job({"template": "weather", "style": "receipt"}, STYLES, NOW, forecast=forecast())
        parsed = parse_wire(rendered.wire())
        kicker = texts(rendered.wire())[0]
        self.assertTrue(kicker["inverse"])
        self.assertTrue(kicker["bold"])
        self.assertIn("THU 22 OCT", kicker["text"])
        self.assertEqual(len(kicker["text"]), COLS[1])
        self.assertIn("74F Partly cloudy", rendered.wire())
        self.assertIn("High 78F / Low 61F", rendered.wire())
        self.assertIn("Rain 20%", rendered.wire())
        self.assertIn("Sun 7:12a-6:31p", rendered.wire())
        self.assertIn("Austin", rendered.wire())
        self.assertIn("scribe", rendered.wire())
        self.assertIn("7:04a", rendered.wire())
        self.assertEqual(parsed["tailfeed"], 4)
        self.assertTrue(any(op["text"].startswith("---") or set(op["text"]) == {"-"} for op in texts(rendered.wire())))

    def test_ticket_list_has_rules_and_bullets(self):
        rendered = render_job(
            {"template": "list", "style": "ticket", "title": "Saturday", "items": ["Groceries", "Library books"]},
            STYLES,
            NOW,
        )
        self.assertIn("=" * COLS[1], rendered.wire())
        self.assertIn("- Groceries", rendered.wire())
        self.assertIn("- Library books", rendered.wire())

    def test_letter_closes_with_dots(self):
        rendered = render_job(
            {"template": "message", "style": "letter", "title": "Hello", "body": "Miss you.", "from": "Mom"},
            STYLES,
            NOW,
        )
        self.assertIn("Hello".center(COLS[2]), rendered.wire())
        self.assertIn("." * COLS[1], rendered.wire())
        self.assertIn("- Mom", rendered.wire())
        self.assertEqual(texts(rendered.wire())[0]["size"], 2)

    def test_note_footer_and_unicode_fold(self):
        rendered = render_job({"template": "note", "style": "note", "body": "72° — done"}, STYLES, NOW)
        self.assertIn("72 - done", rendered.wire())
        self.assertNotIn("°", rendered.wire())
        self.assertIn("7:04a", rendered.preview())

    def test_unknown_style_and_empty_message_fail(self):
        with self.assertRaises(ScribeError) as unknown:
            render_job({"template": "note", "style": "neon", "body": "x"}, STYLES, NOW)
        self.assertIn("neon", str(unknown.exception))
        with self.assertRaises(ScribeError):
            render_job({"template": "message", "style": "receipt"}, STYLES, NOW)

    def test_custom_blocks_and_style_override(self):
        custom = render_job(
            {
                "template": "custom",
                "style": "note",
                "blocks": [
                    {"op": "text", "text": "HI", "size": 3, "align": "center", "bold": True},
                    {"op": "kv", "label": "High", "value": "74F"},
                    {"op": "rule", "kind": "equals"},
                ],
            },
            STYLES,
            NOW,
        )
        self.assertIn("text|3|1|0|{}".format("HI".center(COLS[3])), custom.wire())
        self.assertIn("High", custom.wire())
        self.assertIn("74F", custom.wire())
        self.assertIn("=" * COLS[1], custom.wire())

        overridden = render_job(
            {"template": "message", "style": {"base": "poster", "title": {"size": 3}}, "title": "HI", "body": "Yo"},
            STYLES,
            NOW,
        )
        self.assertEqual(texts(overridden.wire())[0]["size"], 3)
        self.assertEqual(texts(overridden.wire())[0]["text"], "HI".center(COLS[3]))

    def test_wrap_and_hard_split_stay_inside_width(self):
        wrapped = render_job({"template": "note", "style": "note", "body": "word " * 20}, STYLES, NOW)
        body = [op for op in texts(wrapped.wire()) if "scribe" not in op["text"]]
        self.assertGreaterEqual(len(body), 2)
        self.assertTrue(all(len(op["text"]) <= COLS[op["size"]] for op in texts(wrapped.wire())))

        split = render_job({"template": "note", "style": "note", "body": "A" * 40}, STYLES, NOW)
        pieces = [op["text"] for op in texts(split.wire()) if op["text"].startswith("A")]
        self.assertIn("A" * COLS[1], pieces)
        self.assertIn("A" * (40 - COLS[1]), pieces)

    def test_briefing_does_not_mutate_forecast(self):
        source = forecast()
        before = len(source.rows)
        rendered = render_job(
            {"template": "briefing", "style": "receipt", "note": "Library books due"},
            STYLES,
            NOW,
            forecast=source,
        )
        self.assertEqual(len(source.rows), before)
        self.assertIn("Library books due", rendered.wire())

    def test_drape_flag_and_example_style(self):
        rendered = render_job(
            {"template": "note", "style": "note", "body": "Hi"},
            STYLES,
            NOW,
            drape=False,
        )
        self.assertTrue(rendered.wire().startswith("drape 0\n"))
        styles = load_styles("styles.example.json")
        self.assertIn("headline", styles)
        rendered = render_job(
            {"template": "message", "style": "headline", "title": "HI", "body": "Yo"},
            styles,
            NOW,
        )
        self.assertEqual(texts(rendered.wire())[0]["size"], 3)

    def test_wire_parser_rejects_garbage(self):
        with self.assertRaises(ScribeError):
            parse_wire("nope\n")


if __name__ == "__main__":
    unittest.main()
