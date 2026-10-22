"""Today's forecast, shaped into a slip. Open-Meteo needs no API key."""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import urlopen

from scribe.render import Content, Row, ScribeError

WMO = {
    0: "Clear",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Showers",
    81: "Showers",
    82: "Heavy showers",
    85: "Snow showers",
    86: "Snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm",
    99: "Thunderstorm",
}

DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def phrase(code):
    if code is None:
        return "Forecast"
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "Forecast"
    return WMO.get(code, "Weather {}".format(code))


def fmt_temp(value, units):
    if value is None:
        return "--"
    suffix = "F" if units == "fahrenheit" else "C"
    return "{}{}".format(int(round(float(value))), suffix)


def clock_from_iso(value):
    if not value or "T" not in value:
        return None
    part = value.split("T", 1)[1]
    bits = part.split(":")
    if len(bits) < 2:
        return None
    hour = int(bits[0])
    suffix = "a" if hour < 12 else "p"
    hour12 = hour % 12 or 12
    return "{}:{}{}".format(hour12, bits[1][:2], suffix)


def kicker_from_day(iso_date):
    when = datetime.strptime(iso_date, "%Y-%m-%d")
    return "{} {:02d} {}".format(DAYS[when.weekday()], when.day, MONTHS[when.month - 1])


def _first(values):
    if isinstance(values, list) and values:
        return values[0]
    return None


def content_from_forecast(payload, units, location_name=""):
    if not isinstance(payload, dict):
        raise ScribeError("forecast payload was not an object")
    current = payload.get("current") or {}
    daily = payload.get("daily") or {}
    day = _first(daily.get("time"))
    if not day:
        raise ScribeError("forecast did not include a day")
    code = current.get("weather_code")
    if code is None:
        code = _first(daily.get("weather_code"))
    high = _first(daily.get("temperature_2m_max"))
    low = _first(daily.get("temperature_2m_min"))
    rain = _first(daily.get("precipitation_probability_max"))
    rows = [Row("text", text="High {} / Low {}".format(fmt_temp(high, units), fmt_temp(low, units)))]
    if rain is not None:
        rows.append(Row("text", text="Rain {}%".format(int(round(float(rain))))))
    sunrise = clock_from_iso(_first(daily.get("sunrise")))
    sunset = clock_from_iso(_first(daily.get("sunset")))
    if sunrise and sunset:
        rows.append(Row("text", text="Sun {}-{}".format(sunrise, sunset)))
    return Content(
        kicker=kicker_from_day(day),
        title="{}  {}".format(fmt_temp(current.get("temperature_2m"), units), phrase(code)),
        subtitle=location_name or "",
        rows=rows,
        template="weather",
    )


def _coord(value, label, low, high):
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ScribeError("{} must be a number".format(label))
    if not low <= number <= high:
        raise ScribeError("{} is out of range".format(label))
    return number


def resolve_point(config, job):
    job = job or {}
    lat = _coord(job.get("latitude", config.get("latitude")), "latitude", -90, 90)
    lon = _coord(job.get("longitude", config.get("longitude")), "longitude", -180, 180)
    if lat is None or lon is None:
        raise ScribeError(
            "weather needs latitude and longitude in config.json (python3 -m scribe locate --write)"
        )
    units = job.get("units") or config.get("units") or "fahrenheit"
    if units not in ("fahrenheit", "celsius"):
        raise ScribeError("units must be fahrenheit or celsius")
    timezone = job.get("timezone") or config.get("timezone") or "UTC"
    name = job.get("location_name")
    if name is None:
        name = config.get("location_name") or ""
    if not isinstance(name, str):
        raise ScribeError("location_name must be a string")
    return lat, lon, units, timezone, name


def fetch_forecast(config, job=None, timeout=8):
    lat, lon, units, timezone, name = resolve_point(config, job)
    query = urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
            "timezone": timezone,
            "forecast_days": 1,
            "temperature_unit": units,
        }
    )
    url = "https://api.open-meteo.com/v1/forecast?" + query
    try:
        with urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise ScribeError("weather lookup failed: {}".format(exc), 502)
    if payload.get("error"):
        raise ScribeError("weather lookup failed: {}".format(payload.get("reason") or "rejected"), 502)
    return content_from_forecast(payload, units, name)
