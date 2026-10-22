---
name: scribe
description: "Print a physical slip on the family Scribe thermal printer. Use when an agent should print weather, a briefing, a family message, a chore list, a reminder, or any on-demand note. Covers the local print API, styles, templates, preview, and scheduled morning prints."
---

# Scribe

Print on the thermal printer in the house. Call the local API. Do not invent a second printer protocol.

## Before printing

1. `GET http://127.0.0.1:8787/health`
2. If that fails, the API is not running. From the scribe repo: `python3 -m scribe serve`
3. If `printer.reachable` is false, say so and stop. Do not retry in a loop.
4. Preview first when the wording is uncertain: `POST /v1/preview`. Preview does not use paper.

## Print

`POST http://127.0.0.1:8787/v1/print`

Header `X-Scribe-Token` only if `health.token_required` is true. The token is `token` in `config.json`.

```json
{"template":"message","style":"poster","title":"Dinner","body":"Tacos at 6.","from":"Dad"}
```

Send an `id` on scheduled or retryable jobs (`weather-2026-10-22`). A repeated id does not print again.

`copies` is 1 or 2. Do not spam the printer. Cap is 40 slips an hour.

## Templates

| template | required | use |
| --- | --- | --- |
| message | title or body | a note to the house |
| list | items[] | chores, reminders |
| note | title or body | short status |
| weather | config lat/lon | today's forecast |
| briefing | config lat/lon | weather plus `note` |
| custom | blocks[] | one-off layout |

Weather needs `latitude` and `longitude` in `config.json`. If the error says they are missing, stop. Do not guess a city.

## Styles

`receipt`, `poster`, `ticket`, `note`, `letter`. `GET /v1/styles` returns the live list, including styles added in `styles.json`.

- receipt: daily weather and briefings
- poster: short announcements
- ticket: lists
- note: quiet status
- letter: a family note with a close

Pass a style object to change one slip without a reflash:

```json
{"template":"message","style":{"base":"poster","title":{"size":3}},"title":"HI","body":"Home soon"}
```

`size` is 1, 2, or 3. `align` is left, center, or right.

Custom blocks:

```json
{"template":"custom","style":"ticket","blocks":[
  {"op":"text","text":"Game night","role":"title"},
  {"op":"kv","label":"When","value":"7pm"},
  {"op":"rule","kind":"equals"}
]}
```

## Paper

58mm thermal, 32 columns at normal size, 16 at size 2, 10 at size 3. The printer folds punctuation to ASCII. Keep slips short.
