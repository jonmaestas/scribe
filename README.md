# Scribe

A thermal printer on the counter that agents can print to. Weather, a chore list, "dinner at 6", a reminder. Paper, not another phone notification.

The printer is a small renderer. Styles and schedules live in the API on the computer, so a new slip does not need a reflash.

Hardware is the [Project Scribe](https://github.com/urbancircles/scribe) build: a D1 Mini and a 58 mm thermal head. This tree replaces the notes page with the print API. The head on the bench is an **MC206H** (TTL, 9600 baud, 5–9 V), not the CSN-A4L named in the original BOM. Same class of printer.

## Agents

Start the API from this repo:

```bash
cp config.example.json config.json
python3 -m scribe locate --write   # fills lat/lon for weather; edit the city if it is wrong
python3 -m scribe serve
```

`GET http://127.0.0.1:8787/health`

`POST http://127.0.0.1:8787/v1/print`

```json
{"template":"message","style":"poster","title":"Dinner","body":"Tacos at 6.","from":"Dad"}
```

Preview does not use paper: `POST /v1/print` is the one that does. `POST /v1/preview` returns the slip and the wire format. Send an `id` on anything a schedule might retry (`weather-2026-10-22`). A repeated id is not printed again.

A phone on the same network can open `http://<this-machine>:8787/` and send a note. The form is not the product. The API is.

`paseo.json` registers `scribe` as a service. `paseo script start scribe` runs the same server.

### Templates

| template | needs | use |
| --- | --- | --- |
| `message` | title or body | a note to the house |
| `list` | `items` | chores, reminders |
| `note` | title or body | short status |
| `weather` | lat/lon in `config.json` | today's forecast |
| `briefing` | lat/lon, optional `note` | weather plus a line |
| `custom` | `blocks` | one-off layout |

### Styles

`receipt`, `poster`, `ticket`, `note`, `letter`. `GET /v1/styles` lists them, including any added in `styles.json` (see `styles.example.json`).

Change one slip without a reflash:

```json
{"template":"message","style":{"base":"poster","title":{"size":3}},"title":"HI","body":"Home soon"}
```

`size` is 1, 2, or 3 (32, 16, or 10 columns). `align` is `left`, `center`, or `right`.

Morning weather, once the printer answers `scribe.local` and `config.json` has coordinates:

```bash
paseo schedule create \
  --name "scribe-morning-weather" \
  --cron "0 7 * * *" \
  --timezone America/Chicago \
  --cwd "/Volumes/External Drive/code/gh/jonmaestas/scribe" \
  --provider codex/gpt-5.4 \
  "$(cat prompts/morning-weather.md)"
```

The prompt tells the agent to stop if the printer is down. It is not armed yet. The device is not on the network.

## Firmware

`firmware/scribe/scribe.ino`. Board: LOLIN(WEMOS) D1 R2 & mini. No extra libraries.

```bash
cp "firmware/scribe/secrets.example.h" "firmware/scribe/secrets.h"
```

Put the Wi-Fi name and password in `secrets.h`, then flash. On boot the printer should drop a slip with its IP, and `http://scribe.local/health` should answer.

The host sends a line protocol (`scribe/render.py`). The board prints those lines. It does not fetch weather.

`drape` defaults on, so a slip hung down the front reads top to bottom. If yours comes out upside down, set `"drape": false` in `config.json`. No reflash.

## Wiring

Do not power the printer from the D1 Mini. The head draws about 2 A while printing. The 2-pin red/black plug is heater power: red to a separate 5 V supply, black to that supply's ground and to D1 `G`. Power the D1 from USB or from the 5 V pin, never both.

The 4-wire plug is serial. Read the legend on the insulation. Usual colors: yellow = printer TX, white = printer RX, black = signal ground. Cross them. The red wire in that plug does not go to a GPIO. Cap it unless you already know what it is.

These heads are often 5 V TTL. D1 pins are 3.3 V. Put 1 kΩ in series on printer TX into the D1 receive pin until the level is known.

Pins the firmware talks on, matching the sketch that used to be in this repo:

| Printer | D1 Mini |
| --- | --- |
| TX (usually yellow) | D2 |
| RX (usually white) | D1 |
| GND | G |

D3 and D4 are boot straps. The old README named them, and the old sketch's comment named them, but the code used D2 and D1. The close-up shows duponts on D4, D3, D2, D1, and RX. Confirm with a continuity beep before flashing. If the data pair is not D2/D1, change `PRINTER_RX_PIN`, `PRINTER_TX_PIN`, and the two `*_NAME` defines at the top of `scribe.ino`, then flash again.

Nothing belongs on D8. It has to be free at boot.

## Layout

```
scribe/           API: styles, templates, weather, HTTP
firmware/scribe/  D1 Mini sketch
examples/         jobs an agent can POST
prompts/          morning-weather schedule prompt
skill/SKILL.md    instructions for agents
```

Python 3.9+, standard library only.

```bash
python3 -m unittest discover -s tests
python3 -m scribe preview --json examples/dinner.json
```
