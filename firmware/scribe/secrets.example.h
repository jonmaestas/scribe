#pragma once

// Copy this file to secrets.h in the same folder, then flash.
// secrets.h is gitignored.

#define WIFI_SSID "your-ssid"
#define WIFI_PASSWORD "your-password"

// Leave empty to let any device on the LAN print.
// If set, the host config.json token must match and be sent as X-Scribe-Token.
#define SCRIBE_TOKEN ""
