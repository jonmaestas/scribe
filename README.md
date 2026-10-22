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

The prompt tells the agent to stop if the printer is down. It is not armed yet.

## Firmware

`firmware/scribe/scribe.ino`. Board: LOLIN(WEMOS) D1 R2 & mini. No extra libraries.

```bash
cp "firmware/scribe/secrets.example.h" "firmware/scribe/secrets.h"
```

Put the Wi-Fi name and password in `secrets.h`. The first flash is over USB. Switch the breadboard supply off while the D1 is on the Mac. After that, update over Wi-Fi:

```bash
arduino-cli compile --fqbn esp8266:esp8266:d1_mini --output-dir /tmp/scribe-build firmware/scribe
python3 ~/Library/Arduino15/packages/esp8266/hardware/esp8266/*/tools/espota.py \
  -i <printer-ip> -p 8266 -f /tmp/scribe-build/scribe.ino.bin
```

On boot the printer drops a slip with its IP. `GET http://<printer-ip>/health` answers with pins and heat settings.

The host sends a line protocol (`scribe/render.py`). The board prints those lines. It does not fetch weather.

`drape` defaults on, so a slip hung down the front reads top to bottom. If yours comes out upside down, set `"drape": false` in `config.json`. No reflash.

### Darkness and speed

`POST http://<printer-ip>/heat?dots=3&time=255&interval=120&pace=60` changes print settings until the next reboot. The defaults in `scribe.ino` are those bench values.

| arg | meaning | effect |
| --- | --- | --- |
| `dots` | heat `(n+1)*8` dots per burst | lower = less current, darker on a weak supply, slower |
| `time` | heat time per burst, 10 µs units, max 255 | higher = darker |
| `interval` | rest between bursts, 10 µs units | higher = less current, slower |
| `pace` | pause after each line, ms | higher = supply recovers, slower |

Faint print means low heat or a sagging supply. Lower `dots` before raising anything else.

## Wiring

The printer must not draw its power through the D1. On the bench, a 5 V 3 A supply in the D1's micro-USB, feeding the printer through the D1's 5V pin, moved paper and browned out on any real print. The fix was a breadboard power module (5 V setting) fed by a 12 V 2.5 A adapter, powering the rail. The printer's 2-pin `VCC`/`GND` and the D1's `5V`/`G` both sit on that rail. The D1's USB stays empty unless the module is switched off.

The printer's own USB port (`5V`, `D-`, `D+`, `GND`) does not power this MC206H.

Serial plug, from the printer's molded labels: red `DTR`, yellow `TX`, white `RX`, black `GND`. DTR is the printer's busy output. Leave it unconnected. Never tie it to ground or VCC.

| Printer | Wire | D1 Mini |
| --- | --- | --- |
| VCC | red | + rail |
| GND | green | − rail |
| TX | yellow | D2 (receive) |
| RX | white | D1 (transmit) |
| GND | green | − rail |
| DTR | red | not connected |

D1 Mini: `5V` red to + rail, `G` green to − rail. Nothing on D3, D4 (boot straps) or D8.

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
