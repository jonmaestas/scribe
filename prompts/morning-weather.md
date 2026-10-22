Print today's weather on the family Scribe printer.

Read the scribe skill, then POST to the local Scribe API:

- URL: http://127.0.0.1:8787/v1/print
- JSON: {"template":"weather","style":"receipt","id":"weather-YYYY-MM-DD"}
- Use today's date in the id so a retry does not print a second slip.

If GET /health says the printer is unreachable, or the error says latitude and longitude are missing, report that and stop. Do not retry. Do not print anything else.
